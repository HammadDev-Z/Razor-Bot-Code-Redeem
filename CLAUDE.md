# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot (python-telegram-bot 22, async) that stores one Razer account and, on a timer (default every 20 min), uses Playwright Chromium to buy Yalla Ludo Diamonds with Razer Gold until an order such as `830x30 2320x20` is finished. It resumes after the Gold is topped up.

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
- **Cookie banner (EU / VPS only).** From the VPS, Razer shows a CookieYes banner. It covers the page, so the bot's clicks land on it; it once blocked the "Contact Permission" form at login until the login timed out.
  - `_dismiss_cookie_banner` presses Reject All (`[data-cky-tag='reject-button']`).
  - Call it after every navigation, and inside the login loop before `_decline_consent`.
- State is kept in the `bot-data` volume (`/app/data`) and snapshots in `bot-debug` (`/app/debug`). Never suggest `down -v`.
- Only one instance may poll the bot token at a time, so the PC and the VPS can't both run it.

There is no test suite or linter. `store.parse_order`, `bot._parse_backup_codes` and `StateStore` can be exercised offline. Everything in `razer_session.py` can only be verified against the live Razer site. `RAZER_HEADLESS=false` shows the browser, and `RAZER_DEBUG_SNAPSHOTS=true` saves a screenshot and HTML of each purchase step to `debug/`.

## Architecture

- `bot.py`: Telegram commands (`/newaccount`, `/order`, `/status`, `/checknow`, `/addcodes`, `/resolve`, `/cancel`) and the `JobQueue` timer that calls `Purchaser.run_cycle()`. Only the IDs in `BOT_OWNER_ID` (comma-separated, required; `_owner_ids`) can use the bot. They share one account and order, and `notify` sends every message to each of them. Inside the `/newaccount` and `/addcodes` conversation, `/cancel` cancels that step. Outside it, `/cancel` wipes all state after a confirmation button.
- `store.py`: a single `BotState` (account, backup codes, Playwright `storage_state` cookies, order with progress, pending purchase), written Fernet-encrypted and atomically to `data/state.enc`. `account_id` changes whenever the account is replaced or wiped.
- `purchaser.py`: one check = open the account → loop `purchase_one` on `state.next_item()` until done, Gold runs out, codes run out, or the result is uncertain. It holds a lock so only one check runs at a time. After every check that ran, manual or timer, `on_cycle_done` (`restart_check_timer` in `bot.py`) moves the repeating job's next run to now + the interval, so the next automatic check is always a full interval after the last check ended, e.g. `/checknow` restarts the 20-minute wait. A check skipped because the lock is held doesn't reset it. Before mutating state it re-checks `account_id` (`_current`), so a check that is still running after `/cancel` or `/newaccount` writes nothing. `_report` skips a message identical to the last one, so a 20-minute timer doesn't repeat "not enough Gold" (the timer still runs: the log has a "Purchase check started/finished" pair every check, and `/status` shows the last check time). `NOTIFY_EVERY_CHECK` (default true) disables the skip, and each check's message starts with "Check at <time>". Each message ends with "Razer Gold left", read from the header just before the browser closes (`current_balance`); it is omitted if unreadable, never stale.
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
- **Product PINs.** The receipt shows the voucher PIN in `<div class="pin-code">` (S/N below it is not part of it). `_receipt_pin` reads it into `session.last_pin`; the purchaser stores it in `state.pins[item.code]` in the same step that credits the purchase. A new `/order` (`set_order`) clears `pins`, and `/order` first sends the old order's PINs. When a check finishes the order (`bought` and `order_complete`) `codes_messages` sends them under per-product headings, chunked below 3500 characters. Purchases made before PINs were saved show as "Not listed: N". `/codes` resends them. An unreadable PIN forces a `success-no-pin` snapshot.
- Backup-code refresh path on Razer ID (confirmed from live screenshots): login lands on the Dashboard, so the refresh goes to `/account`, clicks the "Backup Codes" row (2-step box, then `/account/security/codes`; if still elsewhere after verifying it opens that URL directly), clicks GENERATE NEW CODES and the dialog button GET NEW CODES. Razer opens that dialog ("You have 9 backup codes remaining") on arrival at the codes page, covering the page, so GET NEW CODES is clicked first (up to 3 times), GENERATE NEW CODES at most twice and at least 8s after another click, and never before the old list has loaded (so old codes can't be mistaken for new ones). The generate pattern is anchored (`^generate`): the shortest-text match used to pick GET NEW CODES over the real button, and "Generated on: ..." must not match. Used codes are shown struck through and old codes are excluded by value. `_find_backup_codes_item` also follows Security links as a fallback and saves `codes-1b-after-N` snapshots. Still untested end to end on the real site (a real run replaces the codes).
- **Speed.** Every purchase logs `Purchase of X took N s (menu, select, payment, checkout-ready, razer-..., receipt)`: read that before tuning. Most of a purchase is Razer processing CHECKOUT, which can't be sped up. Safe cuts already made: shorter polling sleeps, a scroll that stops when CHECKOUT is visible, and faster typing of the backup code (40ms per character; it falls back to `fill`). Deliberately NOT cut: the 1.5s stable-selection window in `_select_product` and `_verify_selected` before CHECKOUT (they stop a wrong-product purchase). `RAZER_BLOCK_IMAGES=true` (off by default, untested live) aborts image/video requests only when the requesting frame is on gold.razer.com, so sign-in and the 2-step box are untouched.
- `PRODUCTS_BY_CODE` in `razer_session.py` is the only allowlist of products that can be bought.
- `httpx`/`httpcore` loggers stay at WARNING because their request lines contain the bot token.
- `data/`, `debug/` and `.env` hold secrets and are gitignored.
