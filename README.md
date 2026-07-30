# Razer Bot Code Redeem

Telegram bot foundation with a `/login` conversation that opens and retains an
authenticated Razer browser session for the next workflow step.

## Setup

Requires Python 3.12 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` in `.env`. Strongly recommended: set `BOT_OWNER_ID` to
your numeric Telegram user ID so nobody else can submit credentials.

Run:

```powershell
python bot.py
```

In a private Telegram chat, send `/login`, then send the requested email and
password. The bot deletes the password message where Telegram permits and keeps
the authenticated browser context in memory. `/logout` closes it.

If Razer requires a visible CAPTCHA or verification step, set
`RAZER_HEADLESS=false`, restart the bot, and try `/login` again.

.\.venv\Scripts\python.exe .\bot.py