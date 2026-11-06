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

## VPS (Docker)

Same setup as the other YLBots bots. Code lives in `/root/Razor-Bot-Code-Redeem`.

First time:

```bash
cd /root
git clone https://github.com/HammadDev-Z/Razor-Bot-Code-Redeem.git
cd Razor-Bot-Code-Redeem
cp .env.example .env
nano .env
sudo docker compose up -d --build
sudo docker compose logs -f bot
```

In `nano .env` set `TELEGRAM_BOT_TOKEN`, `BOT_OWNER_ID`, `BOT_SECRET_KEY` and
`TZ`. The browser always runs headless in Docker. When the logs show
`Application started`, send `/newaccount` and `/order` in Telegram.

Update after code changes:

```bash
cd /root/Razor-Bot-Code-Redeem
git pull
sudo docker compose up -d --build
```

- Run the bot in **one place only**. Stop it on the PC before starting it on the
  VPS, or Telegram rejects one of them.
- The encrypted login, codes and order live in the `bot-data` volume. Never run
  `docker compose down -v`; it deletes that volume.
- Snapshots (including backup-code refreshes) are in the `bot-debug` volume.
  Copy them out with `sudo docker compose cp bot:/app/debug ./debug`.
- If Razer asks for CAPTCHA or extra verification from the new server, the bot
  messages you; that needs a visible browser once (ask before working around it).

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

When 2 backup codes are left (`BACKUP_CODES_REFRESH_AT`), the bot logs in to
Razer ID, generates a new set of backup codes and saves them. The old codes stop
working. If it ever can't read the new codes, it pauses and asks you to send them
with `/addcodes`.

If the bot types a backup code but cannot confirm the purchase result, it pauses
and asks you to check your Razer transaction history and `/resolve`, so it never
buys twice by mistake.

If Razer requires a visible CAPTCHA or verification step, set
`RAZER_HEADLESS=false` and restart.

## Debug snapshots

With `RAZER_DEBUG_SNAPSHOTS=true`, every purchase step saves a screenshot and
HTML file into `debug/`. These contain account details; keep them local.
