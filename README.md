# Razer Bot Code Redeem

Telegram bot that keeps one Razer account on file and, every 30 minutes, buys
Yalla Ludo Diamonds with Razer Gold until a set order is finished. When the Gold
runs out it waits, and continues where it left off after the account is topped up.

## Setup

Requires Python 3.12 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
```

In `.env` set:

- `TELEGRAM_BOT_TOKEN`
- `BOT_OWNER_ID`: your numeric Telegram user ID (required; only you can use the bot)
- `BOT_SECRET_KEY`: generate with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  It encrypts the saved login in `data/`. Losing or changing it makes that data unreadable.

Run:

```powershell
.\.venv\Scripts\python.exe .\bot.py
```

## Commands

| Command | What it does |
|---|---|
| `/newaccount` | Save Razer email, password and backup codes (messages are deleted) |
| `/order 830x30 2320x20` | Set the order; items are bought strictly in the order given |
| `/status` | Progress, backup codes left, last Gold balance, next check |
| `/checknow` | Run a check immediately |
| `/addcodes` | Add more backup codes (each purchase uses one) |
| `/resolve bought` / `/resolve notbought` | Settle a purchase whose result could not be confirmed |
| `/cancel` | Delete the account, saved login, codes, order and progress |

Product codes: `830`, `2320`, `5k`, `13k`, `27k`, `56k` (USD 2, 5, 10, 25, 50, 100).

If the bot types a backup code but cannot confirm the purchase result, it pauses
and asks you to check your Razer transaction history and `/resolve`, so it never
buys twice by mistake.

If Razer requires a visible CAPTCHA or verification step, set
`RAZER_HEADLESS=false` and restart.

## Debug snapshots

With `RAZER_DEBUG_SNAPSHOTS=true`, every purchase step saves a screenshot and
HTML file into `debug/`. These contain account details; keep them local.
