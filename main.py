import os
import re
import signal
import asyncio
import logging
import asyncpg
from telegram import Update, BotCommand
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("linkbot")

# ── ENV ──────────────────────────────────────────────────────────────
def need(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {key}")
    return v

def ids(s: str) -> list:
    return [int(x) for x in s.split(",") if x.strip()]

DATABASE_URL = need("DATABASE_URL")
ENV_ADMINS   = ids(need("ADMIN_IDS"))      # first ID = owner
OWNER_ID     = ENV_ADMINS[0]
ADMINS: set  = set()
pool: asyncpg.Pool = None

# ── PLATFORM ─────────────────────────────────────────────────────────
class Platform:
    def __init__(self, name, token, channels, keywords):
        self.name, self.token = name, token
        self.channels, self.kws = channels, keywords
        self.footer, self.auto_send, self.idx = "", False, 0

    async def load(self):
        await pool.execute(
            "INSERT INTO lf_state (platform) VALUES ($1) ON CONFLICT DO NOTHING", self.name)
        r = await pool.fetchrow(
            "SELECT auto_send, channel_idx FROM lf_state WHERE platform=$1", self.name)
        self.auto_send = r["auto_send"]
        self.idx = r["channel_idx"] % len(self.channels)
        f = await pool.fetchrow("SELECT content FROM lf_footer WHERE platform='*'")
        self.footer = f["content"] if f else ""

    async def set_auto_send(self, on: bool):
        await pool.execute("UPDATE lf_state SET auto_send=$1 WHERE platform=$2", on, self.name)
        self.auto_send = on

    async def set_footer(self, text: str):
        """Footer is shared: saving it here updates every platform."""
        await pool.execute(
            "INSERT INTO lf_footer (platform, content) VALUES ('*',$1) "
            "ON CONFLICT (platform) DO UPDATE SET content=EXCLUDED.content", text)
        for q in PLATFORMS:
            q.footer = text

    async def next_channel(self) -> int:
        ch = self.channels[self.idx % len(self.channels)]
        self.idx = (self.idx + 1) % len(self.channels)
        await pool.execute("UPDATE lf_state SET channel_idx=$1 WHERE platform=$2", self.idx, self.name)
        return ch

    async def pending(self):
        return await pool.fetch("SELECT * FROM lf_pending WHERE platform=$1 ORDER BY id", self.name)

    async def count(self) -> int:
        return await pool.fetchval("SELECT count(*) FROM lf_pending WHERE platform=$1", self.name)

    async def add(self, msg_type, caption=None, file_id=None, raw_text=None):
        return await pool.fetchrow(
            "INSERT INTO lf_pending (platform, msg_type, caption, file_id, raw_text) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING *",
            self.name, msg_type, caption, file_id, raw_text)

    async def delete(self, post_id: int):
        await pool.execute("DELETE FROM lf_pending WHERE id=$1", post_id)

    async def clear(self):
        await pool.execute("DELETE FROM lf_pending WHERE platform=$1", self.name)


def load_platforms() -> list:
    out, seen = [], set()
    for name in need("PLATFORMS").lower().split(","):
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        key = re.sub(r"\W", "_", name).upper()
        kws = [k.strip().lower() for k in os.environ.get(f"{key}_KEYWORD", name).split(",") if k.strip()]
        out.append(Platform(name, need(f"{key}_BOT_TOKEN"), ids(need(f"{key}_DESTINATION_IDS")), kws))
    return out

PLATFORMS = load_platforms()

# ── DB ───────────────────────────────────────────────────────────────
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS lf_admins (user_id BIGINT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS lf_footer (platform TEXT PRIMARY KEY, content TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS lf_state (
            platform    TEXT PRIMARY KEY,
            auto_send   BOOLEAN NOT NULL DEFAULT FALSE,
            channel_idx INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS lf_pending (
            id       SERIAL PRIMARY KEY,
            platform TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            caption  TEXT,
            file_id  TEXT,
            raw_text TEXT
        );
        CREATE INDEX IF NOT EXISTS lf_pending_platform ON lf_pending (platform, id);
    """)
    for aid in ENV_ADMINS:
        await pool.execute("INSERT INTO lf_admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", aid)
    ADMINS.update(r["user_id"] for r in await pool.fetch("SELECT user_id FROM lf_admins"))
    for p in PLATFORMS:
        await p.load()
    logger.info("DB ready. Platforms: %s", ", ".join(p.name for p in PLATFORMS))

# ── LINK FILTER ──────────────────────────────────────────────────────
URL_PATTERN = re.compile(r'(https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+)', re.IGNORECASE)

def filter_links(text: str, kws: list) -> str:
    """Keep only links containing a platform keyword; drop everything after the last such link."""
    if not text:
        return ""
    ok = lambda u: any(k in u.lower() for k in kws)
    last = None
    for m in URL_PATTERN.finditer(text):
        if ok(m.group(0)):
            last = m
    head = text[:last.end()] if last else text
    out = URL_PATTERN.sub(lambda m: m.group(0) if ok(m.group(0)) else "", head)
    return re.sub(r' {2,}', ' ', out).strip()

def build_preview(text: str, footer: str) -> str:
    return f"{text}\n\n{footer}".strip() if footer else text.strip()

async def safe(fn, **kw):
    """Try HTML parse mode, fall back to plain text if the markup is invalid."""
    try:
        return await fn(parse_mode="HTML", **kw)
    except BadRequest:
        return await fn(**kw)

# ── SENDING ──────────────────────────────────────────────────────────
async def send_one(bot, p: Platform, post, channel_id: int, retries: int = 3):
    kind = post["msg_type"]
    for _ in range(retries):
        try:
            if kind == "text":
                text = build_preview(post["raw_text"] or "", p.footer)
                await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
            else:
                cap = build_preview(post["caption"] or "", p.footer)
                await getattr(bot, f"send_{kind}")(
                    chat_id=channel_id, caption=cap, parse_mode="HTML",
                    **{kind: post["file_id"]})
            return True, None
        except RetryAfter as e:
            wait = int(e.retry_after) + 2
            logger.warning("[%s] FloodWait %ss on %s", p.name, wait, channel_id)
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error("[%s] post %s → %s FAILED: %s", p.name, post["id"], channel_id, e)
            return False, str(e)
    return False, "Max retries exceeded"

# ── DECORATORS ───────────────────────────────────────────────────────
def P(ctx) -> Platform:
    return ctx.application.bot_data["p"]

def admin_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id in ADMINS:
            return await fn(update, ctx)
    return w

def owner_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != OWNER_ID:
            await update.message.reply_text("⛔ Sirf owner ye command use kar sakta hai.")
            return
        return await fn(update, ctx)
    return w

# ── COMMANDS ─────────────────────────────────────────────────────────
@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = P(ctx)
    await update.message.reply_text(
        f"👋 *Link Filter Bot — {p.name.upper()}*\n\n"
        f"📦 Pending posts: `{await p.count()}`\n"
        f"📝 Footer: `{'Set ✅' if p.footer else 'Not set ❌'}`\n"
        f"📡 Auto-send: `{'ON ✅' if p.auto_send else 'OFF ⏸'}`\n"
        f"📺 Destinations: `{len(p.channels)}`\n"
        f"👤 Role: `{'Owner 👑' if update.effective_user.id == OWNER_ID else 'Admin'}`\n\n"
        f"*How to use:*\n"
        f"1. Posts bhejo (text/photo/video/doc/audio)\n"
        f"2. Auto-send OFF ho to posts batch mein save hote hain\n"
        f"3. /on — batch turant bhejta hai, naye posts bhi auto-send\n"
        f"4. /off — auto-send ruk jayega\n\n"
        f"_Sirf '{', '.join(p.kws)}' wale links rakhta hai, baaki remove._",
        parse_mode="Markdown"
    )

@admin_only
async def cmd_footer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p, msg = P(ctx), update.message
    parts = (msg.text or "").split(None, 1)
    content = parts[1].strip() if len(parts) > 1 else ""
    if not content:
        cur = f"<b>Current footer:</b>\n{p.footer}" if p.footer else "Footer set nahi hai abhi."
        await safe(msg.reply_text, text=(
            f"{cur}\n\n<b>Usage:</b> <code>/footer your text here</code>\n"
            f"Newlines aur HTML (bold, italic, links) support hai."))
        return
    await p.set_footer(content)
    await safe(msg.reply_text, text=f"✅ <b>Footer saved (sabhi platforms ke liye)!</b>\n\n{content}")

@admin_only
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = P(ctx)
    n = await p.count()
    await p.clear()
    await update.message.reply_text(f"🗑️ Batch cancelled. {n} posts clear ho gaye.")

@admin_only
async def cmd_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = P(ctx)
    if p.auto_send:
        await update.message.reply_text("ℹ️ Auto-send pehle se ON hai.")
        return
    await p.set_auto_send(True)
    posts = await p.pending()
    if not posts:
        await update.message.reply_text("✅ Auto-send ON. Ab naye posts turant channels mein jayenge.")
        return

    status = await update.message.reply_text(f"✅ Auto-send ON.\n📤 Batch ke {len(posts)} posts bhej raha hoon...")
    sent, fails = 0, []
    for i, post in enumerate(posts, 1):
        ch = await p.next_channel()
        ok, err = await send_one(ctx.bot, p, post, ch)
        if ok:
            sent += 1
            await p.delete(post["id"])
        else:
            fails.append(f"#{i} → {ch}: {err}")
        await asyncio.sleep(0.8)

    summary = f"✅ Batch done! {sent}/{len(posts)} sent." if not fails else (
        f"⚠️ {sent}/{len(posts)} sent. {len(fails)} failed (batch mein reh gaye).\n\n" + "\n".join(fails[:10]))
    await status.edit_text(summary + "\n📡 Auto-send ab ON hai.")

@admin_only
async def cmd_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = P(ctx)
    if not p.auto_send:
        await update.message.reply_text("ℹ️ Auto-send pehle se OFF hai.")
        return
    await p.set_auto_send(False)
    await update.message.reply_text("⏸ Auto-send OFF. Naye posts batch mein save honge. /on se dobara shuru karo.")

@owner_only
async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [f"• <code>{u}</code>{' 👑' if u == OWNER_ID else ''}" for u in sorted(ADMINS)]
    await update.message.reply_text("<b>👥 Admins:</b>\n" + "\n".join(lines), parse_mode="HTML")

@owner_only
async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    uid = int(ctx.args[0])
    await pool.execute("INSERT INTO lf_admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
    ADMINS.add(uid)
    await update.message.reply_text(f"✅ {uid} ab admin hai (sabhi platforms pe).")

@owner_only
async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    uid = int(ctx.args[0])
    if uid == OWNER_ID:
        await update.message.reply_text("⛔ Owner ko remove nahi kar sakte.")
        return
    await pool.execute("DELETE FROM lf_admins WHERE user_id=$1", uid)
    ADMINS.discard(uid)
    await update.message.reply_text(f"✅ {uid} admin se remove ho gaya.")

@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [
        f"• <b>{p.name}</b> | keys: <code>{','.join(p.kws)}</code> | destinations: {len(p.channels)} | "
        f"pending: {await p.count()} | auto-send: {'ON' if p.auto_send else 'OFF'}"
        for p in PLATFORMS
    ]
    await update.message.reply_text("<b>📊 All platforms:</b>\n" + "\n".join(lines), parse_mode="HTML")

# ── MESSAGE HANDLER ──────────────────────────────────────────────────
@admin_only
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p, msg = P(ctx), update.message
    if not msg:
        return

    if msg.text and not msg.text.startswith("/"):
        kind, fid, body = "text", None, msg.text
    elif msg.photo:
        kind, fid, body = "photo", msg.photo[-1].file_id, msg.caption
    elif msg.video:
        kind, fid, body = "video", msg.video.file_id, msg.caption
    elif msg.document:
        kind, fid, body = "document", msg.document.file_id, msg.caption
    elif msg.audio:
        kind, fid, body = "audio", msg.audio.file_id, msg.caption
    else:
        await msg.reply_text("⚠️ Unsupported media type.")
        return

    filtered = filter_links(body or "", p.kws)
    post = await p.add(
        kind,
        caption=None if kind == "text" else filtered,
        file_id=fid,
        raw_text=filtered if kind == "text" else None,
    )

    if p.auto_send:
        ch = await p.next_channel()
        ok, err = await send_one(ctx.bot, p, post, ch)
        if ok:
            await p.delete(post["id"])
            await msg.reply_text(f"📤 Sent to {ch} ✅")
        else:
            await msg.reply_text(f"⚠️ Send failed, batch mein save reh gaya: {err}")
        return

    preview = f"📋 Preview (batch mein saved):\n\n{build_preview(filtered, p.footer)}"
    if kind == "text":
        await safe(msg.reply_text, text=preview)
    else:
        await safe(getattr(msg, f"reply_{kind}"), caption=preview, **{kind: fid})

# ── RUN ──────────────────────────────────────────────────────────────
async def start_bot(p: Platform) -> Application:
    app = Application.builder().token(p.token).build()
    app.bot_data["p"] = p
    for name, fn in [
        ("start", cmd_start), ("footer", cmd_footer), ("on", cmd_on), ("off", cmd_off),
        ("cancel", cmd_cancel), ("admins", cmd_admins), ("addadmin", cmd_addadmin),
        ("removeadmin", cmd_removeadmin), ("status", cmd_status),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))

    await app.initialize()
    await app.bot.set_my_commands([
        BotCommand("start", "Bot status dekhein"),
        BotCommand("footer", "Footer set ya dekhein"),
        BotCommand("on", "Auto-send ON karein"),
        BotCommand("off", "Auto-send OFF karein"),
        BotCommand("cancel", "Batch cancel karein"),
        BotCommand("status", "Sabhi platforms ka status (owner)"),
        BotCommand("admins", "Admins list (owner)"),
        BotCommand("addadmin", "Admin add (owner)"),
        BotCommand("removeadmin", "Admin remove (owner)"),
    ])
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("[%s] polling started (%d channels)", p.name, len(p.channels))
    return app

async def main():
    await init_db()
    apps = []
    for p in PLATFORMS:
        try:
            apps.append(await start_bot(p))
        except Exception as e:
            logger.error("[%s] failed to start: %s", p.name, e)
    if not apps:
        raise SystemExit("No bot started. Check tokens/env.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    for app in apps:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
