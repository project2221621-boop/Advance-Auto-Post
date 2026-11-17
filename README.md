# 🤖 Multi-Platform Link Filter Bot

One codebase, many platforms (Tera, Disk, Flezen, Tenbox, ...). Each platform runs as its own Telegram bot
inside a single process. It keeps only links containing that platform's keyword, removes all other URLs,
and auto-posts to that platform's channels (round-robin).

## Environment Variables

| Variable | Description |
|---|---|
| `ADMIN_IDS` | Comma-separated user IDs. **First ID = owner.** Shared by all platforms |
| `DATABASE_URL` | PostgreSQL URL (one DB for all platforms) |
| `PLATFORMS` | Comma-separated platform names, e.g. `tera,disk,flezen` |
| `<NAME>_BOT_TOKEN` | BotFather token for that platform, e.g. `TERA_BOT_TOKEN` |
| `<NAME>_DESTINATION_IDS` | Comma-separated destination IDs (channels) for that platform |
| `<NAME>_KEYWORD` | *(optional)* link keyword(s), comma-separated. Default = platform name |

## Add a new platform (e.g. Tenbox)

1. Create a bot in @BotFather, add it as admin in the destination channels.
2. Add to env:
   ```
   PLATFORMS=tera,disk,flezen,tenbox
   TENBOX_BOT_TOKEN=...
   TENBOX_DESTINATION_IDS=-100...,-100...
   ```
3. Restart: `sudo systemctl restart telegrambot`

No code change, no DB migration.

## Deploy

### VPS
```
git clone <repo> && cd <repo>
pip install -r requirements.txt
cp .env.example .bot.env && nano .bot.env
python main.py
```

### systemd
```
# /etc/systemd/system/telegrambot.service
[Unit]
Description=Multi-Platform Link Filter Bot
After=network.target

[Service]
User=your_user
WorkingDirectory=/home/your_user/REPO
EnvironmentFile=/home/your_user/REPO/.bot.env
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl daemon-reload && sudo systemctl enable --now telegrambot
```

### Railway
Push to GitHub → New Project → add the variables above. `Procfile` runs `python main.py`.

## Commands (per bot)

| Command | Who | Description |
|---|---|---|
| `/start` | Admin | Status of this platform |
| `/footer [text]` | Admin | View/set footer (HTML supported) — **shared by all platforms** |
| `/on` / `/off` | Admin | Auto-send on/off — per platform |
| `/cancel` | Admin | Clear this platform's batch |
| `/status` | Owner | All platforms: keywords, channels, pending, auto-send |
| `/admins`, `/addadmin <id>`, `/removeadmin <id>` | Owner | Manage admins (shared across platforms) |

## Notes
- Tables are prefixed `lf_` (`lf_admins`, `lf_footer`, `lf_state`, `lf_pending`), keyed by `platform`. Old single-bot tables are untouched; set `/footer` again after migrating (one footer applies to every platform).
- Keyword match is a substring of the URL, so avoid very short keywords.
- Telegram flood limits are handled with automatic retry.
