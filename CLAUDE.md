# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot (python-telegram-bot 22, async) that stores one Razer account and, on a timer (default every 30 min), uses Playwright Chromium to buy Yalla Ludo Diamonds with Razer Gold until an order such as `830x30 2320x20` is finished. It resumes after the Gold is topped up.

## Commands

Windows / PowerShell, Python 3.12+:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env   # set TELEGRAM_BOT_TOKEN, BOT_OWNER_ID, BOT_SECRET_KEY
.\.venv\Scripts\python.exe .\bot.py
```

Production runs on the VPS with Docker, the same way as the sibling YLBots projects:
- The code is in `/root/Razor-Bot-Code-Redeem`. Deploy with `git pull && sudo docker compose up -d --build`, and check with `sudo docker compose logs -f bot`.
- `docker-compose.yml` sets `RAZER_HEADLESS=false`, and `docker-entrypoint.sh` starts Xvfb (`DISPLAY=:99`), so Chromium runs headed just as it did on the PC. The first headless run on the VPS stalled at login.
- A failed login always saves screenshots of every tab (`_snapshot_context`, `debug/*-login-*`).
- State is kept in the `bot-data` volume (`/app/data`) and snapshots in `bot-debug` (`/app/debug`). Never suggest `down -v`.
- Only one instance may poll the bot token at a time, so the PC and the VPS can't both run it.

There is no test suite or linter. `store.parse_order`, `bot._parse_backup_codes` and `StateStore` can be exercised offline. Everything in `razer_session.py` can only be verified against the live Razer site. `RAZER_HEADLESS=false` shows the browser, and `RAZER_DEBUG_SNAPSHOTS=true` saves a screenshot and HTML of each purchase step to `debug/`.

## Architecture

- `bot.py`: Telegram commands (`/newaccount`, `/order`, `/status`, `/checknow`, `/addcodes`, `/resolve`, `/cancel`) and the `JobQueue` timer that calls `Purchaser.run_cycle()`. Only `BOT_OWNER_ID` can use the bot (required). Inside the `/newaccount` and `/addcodes` conversation, `/cancel` cancels that step. Outside it, `/cancel` wipes all state after a confirmation button.
- `store.py`: a single `BotState` (account, backup codes, Playwright `storage_state` cookies, order with progress, pending purchase), written Fernet-encrypted and atomically to `data/state.enc`. `account_id` changes whenever the account is replaced or wiped.
- `purchaser.py`: one check = open the account → loop `purchase_one` on `state.next_item()` until done, Gold runs out, codes run out, or the result is uncertain. It holds a lock so only one check runs at a time. Before mutating state it re-checks `account_id` (`_current`), so a check that is still running after `/cancel` or `/newaccount` writes nothing. `_report` skips a message identical to the last one, so a 30-minute timer doesn't repeat "not enough Gold".
- `razer_session.py`: `RazerSessionManager` owns one Chromium. Each check gets a fresh `BrowserContext` seeded with the saved cookies (it logs in again only if they have expired) and is closed at the end. `purchase_one`: product → Razer Gold → check for insufficient balance → CHECKOUT → "Choose a different method" → Backup Codes → type the code → wait for success or rejection. The site has no stable selectors, so this relies on visible-text matching, lists of fallback selectors, scrolling, and scanning every page in the context (Razer opens new tabs). The `_*_PATTERN` regexes at the top are unverified guesses; tune them from debug snapshots.

## Invariants to preserve

- **No double purchases.** Clicking CHECKOUT is the point of no return, because Razer sometimes skips 2-step and completes the purchase straight away. So `mark_pending` writes `state.pending` right before that click. `take_code` removes a code only once the backup-code inputs are visible.
- `purchase_one` raises `RazerLoginError` only when nothing was bought: before CHECKOUT, or while the 2-step screen is still holding the payment. In that case the purchaser clears `pending`. Anything ambiguous returns `UNCERTAIN`, which leaves `pending` set, and purchases stay blocked until the user sends `/resolve`.
- After CHECKOUT, `_wait_for_checkout_stage` must recognise the success page (`/gold/purchase/transaction/<id>`, "Congratulations!") as well as the 2-step screen.
- The 2-step box is an iframe from `razerid.razer.com/otp` (`_find_otp_frame`). Anything that reads or clicks inside it must work on that frame. Page-level `inner_text()` and `content()` don't include it; use `_full_text`.
- Regexes passed to Playwright's `get_by_text` must use `re.IGNORECASE`, not inline `(?i)`, because Playwright converts them to JavaScript.
- **Never trust a product click.** Shortly after the page loads, Razer re-selects the previously bought product, which once undid the click and bought USD 5 instead of USD 2.
  - `_select_product` re-clicks until exactly the target tile has stayed `.selection-tile__content.selected` for 1.5s.
  - `_verify_selected` checks again just before `mark_pending`/CHECKOUT.
- **Automatic backup-code refresh.**
  - Before each purchase, if `len(backup_codes) <= BACKUP_CODES_REFRESH_AT` (default 2), `Purchaser._refresh_codes` runs once per check. It calls `refresh_backup_codes`, which opens Razer ID in a new tab and goes to profile → Backup Codes → (2-step with one stored code) → generate → (confirm in a dialog) → read the new codes. Razer ID has its own login, separate from Gold's.
  - New codes count only once generate has been clicked, and every code already on screen at that moment is excluded.
  - A plain `RazerLoginError` means nothing changed: the old codes are kept, and the last one is held in reserve so the next refresh can verify.
  - `BackupCodesUnreadableError` means generate was clicked but the codes couldn't be read. The saved codes are cleared and the bot pauses.
  - Refresh snapshots are always saved, as `debug/*-codes-*`.
- 2-step helpers take an `OtpScope`: the OTP iframe (only while visible), or a visible dialog showing 2-step text. They search for options and inputs only inside that scope, never across the whole page.
- **Count the receipt, not the intent.** `_receipt_product` reads "Thank you for your order of X!" with `textContent`. The purchaser credits X, and if X isn't the intended product it sets `state.paused`. `/checknow` or a new `/order` clears the pause.
- Order items are bought strictly in sequence. A cheaper later item is never bought ahead of an unfinished earlier one.
- `PRODUCTS_BY_CODE` in `razer_session.py` is the only allowlist of products that can be bought.
- `httpx`/`httpcore` loggers stay at WARNING because their request lines contain the bot token.
- `data/`, `debug/` and `.env` hold secrets and are gitignored.
