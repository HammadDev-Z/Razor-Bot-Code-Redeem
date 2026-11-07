"""One purchase check: open the account and buy until something stops it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from razer_session import (
    PRODUCTS_BY_CODE,
    BackupCodesUnreadableError,
    InvalidCredentialsError,
    PurchaseOutcome,
    RazerAccountSession,
    RazerLoginError,
    RazerSessionManager,
    VerificationRequiredError,
)
from store import BotState, PendingPurchase, StateStore, now_text
from stock_uploader import StockUploader, UploadError


logger = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]


def blocked_reason(state: BotState) -> str | None:
    """Why no purchase can be attempted right now, or None if it can."""
    if not state.has_account:
        return "No account is set. Use /newaccount."
    if not state.order:
        return "No order is set. Use /order, e.g. /order 830x30 2320x20"
    if state.order_complete:
        return "The order is complete."
    if state.login_blocked:
        return f"{state.login_blocked} Use /newaccount to enter new details."
    if state.pending:
        return pending_message(state)
    if state.paused:
        return state.paused
    if not state.backup_codes:
        return "Out of backup codes. Send /addcodes with new Razer backup codes."
    return None


def pending_message(state: BotState) -> str:
    assert state.pending is not None
    item = state.find_item(state.pending.code)
    name = item.product if item else state.pending.code
    return (
        f"I could not confirm whether the {name} purchase started at "
        f"{state.pending.started_at} went through. Check your Razer transaction "
        "history, then send /resolve bought or /resolve notbought. "
        "Purchases are paused until then."
    )


def progress_text(state: BotState) -> str:
    return "\n".join(
        f"{item.code} ({item.product.removeprefix('Yalla Ludo - ')}): "
        f"{item.done}/{item.quantity}"
        for item in state.order
    )


_MESSAGE_LIMIT = 3500  # Telegram's cap is 4096 characters


def codes_messages(state: BotState, title: str) -> list[str]:
    """The saved PINs, one message per ~3500 characters, under a heading per product.

    Empty when no PIN is saved. Codes bought before PINs were saved (or whose
    PIN could not be read) are counted in a note, never invented.
    """
    blocks: list[tuple[str, list[str]]] = []
    missing = 0
    for item in state.order:
        pins = state.pins.get(item.code, [])
        missing += max(item.done - len(pins), 0)
        if pins:
            heading = (
                f"{item.product.removeprefix('Yalla Ludo - ')} "
                f"({item.code}): {len(pins)} {'code' if len(pins) == 1 else 'codes'}"
            )
            blocks.append((heading, pins))
    if not blocks:
        return []
    footer = ""
    if missing:
        footer = (
            f"Not listed: {missing} bought without a saved code (before codes were "
            "saved, or the receipt could not be read). They are in your Razer "
            "email and transaction history."
        )
        if state.missing_receipts:
            footer += (
                f" {len(state.missing_receipts)} of them are being read again from "
                "their receipts at the next checks."
            )
    return _chunk_blocks(title, blocks, footer)


def _chunk_blocks(
    title: str, blocks: list[tuple[str, list[str]]], footer: str = ""
) -> list[str]:
    """Headings with their codes, split into messages below Telegram's limit."""
    messages: list[str] = []
    current = [title]
    size = len(title)
    for heading, pins in blocks:
        current += ["", heading]
        size += len(heading) + 2
        for pin in pins:
            if size + len(pin) + 1 > _MESSAGE_LIMIT:
                messages.append("\n".join(current))
                current = [f"{heading} (continued)"]
                size = len(current[0])
            current.append(pin)
            size += len(pin) + 1
    if footer:
        current += ["", footer]
    messages.append("\n".join(current))
    return messages


def waiting_codes_messages(state: BotState, title: str) -> list[str]:
    """The codes still waiting to be uploaded, for copying by hand."""
    grouped: dict[str, list[str]] = {}
    for category, pin in state.upload_queue:
        grouped.setdefault(category, []).append(pin)
    blocks = []
    for category, pins in grouped.items():
        product = PRODUCTS_BY_CODE.get(category, category)
        name = product.removeprefix("Yalla Ludo - ")
        blocks.append((f"{name} ({category}): {len(pins)} codes", pins))
    return _chunk_blocks(title, blocks) if blocks else []


class Purchaser:
    def __init__(
        self,
        store: StateStore,
        sessions: RazerSessionManager,
        notify: Notify,
        interval_minutes: int,
        refresh_at: int = 2,
        notify_every_check: bool = True,
        on_cycle_done: Callable[[], None] | None = None,
        uploader: StockUploader | None = None,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._notify = notify
        self._interval_minutes = interval_minutes
        # Get new backup codes from Razer ID once this few (or fewer) are left.
        self._refresh_at = refresh_at
        # Message after every check, even when nothing changed since the last one.
        self._notify_every_check = notify_every_check
        # Called after every check that ran (manual or timer), e.g. to restart
        # the countdown to the next one.
        self._on_cycle_done = on_cycle_done
        # Sends each bought PIN to the website's stock; None when not configured.
        self._uploader = uploader
        self._uploaded_this_check = 0
        self._upload_error = ""
        self._upload_down = False
        # PINs handled by this process, so a receipt seen twice is queued once.
        self._seen_pins: set[str] = set()
        # The product being bought, for a receipt whose product name is unreadable.
        self._intended_code = ""
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run_cycle(self, *, manual: bool = False) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            try:
                logger.info("Purchase check started (%s)", "manual" if manual else "timer")
                await self._run_cycle(manual)
                logger.info("Purchase check finished")
            except Exception:
                logger.exception("Purchase check failed")
                await self._report(
                    "The check failed because of an unexpected error. "
                    "It will retry at the next check.",
                    force=True,
                )
            finally:
                if self._on_cycle_done is not None:
                    try:
                        self._on_cycle_done()
                    except Exception:
                        logger.exception("Could not reset the check timer")

    async def _refresh_codes(self, session: RazerAccountSession, account_id: str) -> bool:
        """Get a new set of backup codes from Razer ID. True on success."""
        state = self._current(account_id)
        if state is None:
            return False

        async def take_code() -> str:
            live = self._current(account_id)
            if live is None or not live.backup_codes:
                raise RazerLoginError("No backup code is left to verify with.")
            code = live.backup_codes.pop(0)
            self._store.save()
            return code

        try:
            codes = await self._sessions.refresh_backup_codes(
                session,
                state.email,
                state.password,
                list(state.backup_codes),
                take_code,
            )
        except BackupCodesUnreadableError as exc:
            if (live := self._current(account_id)) is not None:
                # The old codes were most likely replaced on Razer's side.
                live.backup_codes = []
                live.paused = (
                    f"New backup codes were requested on Razer ID but could not be "
                    f"read ({exc}), so the saved ones probably no longer work. Open "
                    "Razer ID > Backup Codes, send the codes shown there with "
                    "/addcodes, then /checknow."
                )
                self._store.save()
                await self._notify(live.paused)
            return False
        except RazerLoginError as exc:
            live = self._current(account_id)
            left = len(live.backup_codes) if live else 0
            await self._report(
                f"Could not get new backup codes: {exc} The saved codes still work "
                f"({left} left). Trying again at the next check.",
                force=False,
            )
            return False

        if (live := self._current(account_id)) is None:
            return False
        live.backup_codes = codes
        self._store.save()
        logger.info("Saved %s new backup codes", len(codes))
        await self._notify(
            f"Got {len(codes)} new backup codes from Razer ID and saved them. "
            "The old codes no longer work."
        )
        return True

    async def _flush_uploads(self, account_id: str, *, quick: bool = False) -> None:
        """Send queued PINs to the website's stock, per category.

        A PIN leaves the queue only once the website confirmed it, so a failed
        or interrupted upload is retried at the next check. ``quick`` (right
        after a purchase) gives up for the rest of this check after one
        failure, so a website that is down never slows the buying.
        """
        if self._uploader is None or (quick and self._upload_down):
            return
        state = self._current(account_id)
        if state is None or not state.upload_queue:
            return
        batches: dict[str, list[str]] = {}
        for category, pin in state.upload_queue:
            batches.setdefault(category, []).append(pin)
        for category, pins in batches.items():
            try:
                await self._uploader.upload(category, pins)
            except UploadError as exc:
                self._upload_error = str(exc)
                self._upload_down = True
                logger.warning("Stock upload failed: %s", exc)
                return
            live = self._current(account_id)
            if live is None:
                return
            sent = set(pins)
            live.upload_queue = [
                entry
                for entry in live.upload_queue
                if not (entry[0] == category and entry[1] in sent)
            ]
            self._store.save()
            self._uploaded_this_check += len(pins)
            logger.info("Uploaded %s codes to the website (%s)", len(pins), category)

    def _upload_note(self, account_id: str) -> str:
        """One line for the check's message about the website upload, or ''."""
        if self._uploader is None:
            return ""
        state = self._current(account_id)
        waiting = len(state.upload_queue) if state else 0
        parts = []
        if self._uploaded_this_check:
            parts.append(f"Uploaded {self._uploaded_this_check} codes to the website.")
        if waiting:
            reason = f": {self._upload_error}" if self._upload_error else ""
            parts.append(
                f"{waiting} codes are waiting to upload{reason}. "
                "Retrying at the next check."
            )
        return " ".join(parts)

    def _remember_pin(
        self, state: BotState, product: str | None, pin: str | None, url: str | None
    ) -> None:
        """Keep a bought code safe: save it, queue it for the website.

        Called the moment the receipt is read and again when the purchase is
        counted, so it must be harmless to repeat. A code that could not be
        read leaves the receipt address behind to read it again later.
        """
        code = {name: key for key, name in PRODUCTS_BY_CODE.items()}.get(product or "")
        code = code or self._intended_code
        if not code:
            return
        if pin:
            if pin in self._seen_pins:
                return
            self._seen_pins.add(pin)
            if state.find_item(code) is not None:
                saved = state.pins.setdefault(code, [])
                if pin not in saved:
                    saved.append(pin)
            if not any(entry[1] == pin for entry in state.upload_queue):
                state.upload_queue.append([code, pin])
        elif url and not any(entry[1] == url for entry in state.missing_receipts):
            state.missing_receipts.append([code, url, "0"])
        self._store.save()

    async def _recover_pins(self, session: RazerAccountSession, account_id: str) -> None:
        """Read again the receipts whose code could not be read when bought."""
        state = self._current(account_id)
        if state is None or not state.missing_receipts:
            return
        for entry in list(state.missing_receipts):
            code, url, tries = entry
            try:
                pin = await self._sessions.read_receipt_pin(session, url)
            except Exception:
                logger.exception("Could not re-read a receipt")
                pin = None
            live = self._current(account_id)
            if live is None:
                return
            if pin:
                live.missing_receipts = [e for e in live.missing_receipts if e[1] != url]
                if pin not in self._seen_pins:
                    self._seen_pins.add(pin)
                    if live.find_item(code) is not None:
                        saved = live.pins.setdefault(code, [])
                        if pin not in saved:
                            saved.append(pin)
                    if not any(e[1] == pin for e in live.upload_queue):
                        live.upload_queue.append([code, pin])
                logger.info("Read a code from an earlier receipt")
                await self._notify(
                    f"Read the missing {PRODUCTS_BY_CODE.get(code, code)} code from its "
                    f"receipt: {pin}"
                )
            else:
                entry[2] = str(int(tries) + 1)
                if int(entry[2]) == 3:
                    product = PRODUCTS_BY_CODE.get(code, code)
                    await self._notify(
                        f"Could not read the code of a {product} purchase after 3 "
                        "tries. Open this receipt while logged in to the Razer "
                        "account and copy the code by hand:\n" + url
                    )
            self._store.save()

    async def _recover_only(self, account_id: str) -> None:
        """Open the account just to re-read receipts (nothing is bought)."""
        state = self._current(account_id)
        if state is None:
            return
        try:
            session = await self._sessions.open_account(
                state.email, state.password, state.storage_state
            )
        except RazerLoginError as exc:
            logger.warning("Could not open the account to re-read receipts: %s", exc)
            return
        try:
            await self._recover_pins(session, account_id)
        finally:
            storage_state = await self._sessions.close(session)
            if (live := self._current(account_id)) is not None and storage_state:
                live.storage_state = storage_state
                self._store.save()

    async def _finish_uploads(self, account_id: str) -> None:
        """Last upload attempt of a check; hand the codes over if it keeps failing.

        After 3 checks in a row with codes still waiting, the codes themselves
        are sent to Telegram once, so none can be lost with a broken website.
        """
        await self._flush_uploads(account_id)
        state = self._current(account_id)
        if state is None or self._uploader is None:
            return
        if not state.upload_queue:
            if state.upload_failures or state.upload_alerted:
                state.upload_failures, state.upload_alerted = 0, False
                self._store.save()
            return
        if self._upload_error:
            state.upload_failures += 1
            self._store.save()
        if state.upload_failures >= 3 and not state.upload_alerted:
            for text in waiting_codes_messages(
                state,
                "These bought codes could not be uploaded to the website after 3 "
                "checks. Add them by hand (they stay queued and keep retrying):",
            ):
                await self._notify(text)
            state.upload_alerted = True
            self._store.save()

    def _current(self, account_id: str) -> BotState | None:
        """The live state, or None if the account was replaced or deleted."""
        state = self._store.state
        return state if state.account_id == account_id else None

    async def _run_cycle(self, manual: bool) -> None:
        state = self._store.state
        account_id = state.account_id
        self._uploaded_this_check = 0
        self._upload_error = ""
        self._upload_down = False
        reason = blocked_reason(state)
        if reason:
            # Even with nothing to buy, a code that could not be read at purchase
            # time is read again, and bought codes still reach the website.
            if state.missing_receipts and state.has_account and not state.login_blocked:
                await self._recover_only(account_id)
            await self._finish_uploads(account_id)
            if manual:
                await self._report(reason, force=True)
            return

        state.last_check_at = now_text()
        self._store.save()
        try:
            session = await self._sessions.open_account(
                state.email, state.password, state.storage_state
            )
        except InvalidCredentialsError:
            if (state := self._current(account_id)) is not None:
                state.login_blocked = "Razer rejected the email or password."
                state.storage_state = None
                self._store.save()
                await self._report(blocked_reason(state) or "", force=True)
            return
        except VerificationRequiredError as exc:
            await self._report(f"Could not log in: {exc}", force=manual)
            return
        except RazerLoginError as exc:
            await self._report(
                f"Could not open the account: {exc} Retrying at the next check.",
                force=manual,
            )
            return

        bought = 0
        stop_message = ""
        balance_now: float | None = None
        try:
            if (state := self._current(account_id)) is None:
                return
            state.storage_state = await self._sessions.export_storage_state(session)
            state.last_balance = session.balance
            self._store.save()

            async def record_receipt(
                product: str | None, pin: str | None, url: str | None
            ) -> None:
                # Runs the moment the receipt is read: the code is saved before
                # anything else can go wrong.
                live = self._current(account_id)
                if live is not None:
                    self._remember_pin(live, product, pin, url)

            session.on_receipt = record_receipt
            await self._recover_pins(session, account_id)

            refresh_tried = refresh_failed = False
            while True:
                if (state := self._current(account_id)) is None:
                    return
                item = state.next_item()
                reason = blocked_reason(state)
                if item is None or reason:
                    stop_message = reason or ""
                    break
                if not refresh_tried and len(state.backup_codes) <= self._refresh_at:
                    refresh_tried = True
                    refresh_failed = not await self._refresh_codes(session, account_id)
                    continue  # re-read the state: codes replaced, or paused
                if refresh_failed and len(state.backup_codes) <= 1:
                    stop_message = (
                        "Keeping the last backup code for the next backup-code "
                        "refresh, so buying waits until that works. Trying again "
                        "at the next check."
                    )
                    break

                async def mark_pending(code: str = item.code) -> None:
                    # Saved right before CHECKOUT: from then on the purchase
                    # may complete even without a 2-step code.
                    live = self._current(account_id)
                    if live is None:
                        raise RazerLoginError("The account was removed.")
                    live.pending = PendingPurchase(code=code, started_at=now_text())
                    self._store.save()

                async def take_code() -> str:
                    live = self._current(account_id)
                    if live is None or not live.backup_codes:
                        raise RazerLoginError("No backup code is available.")
                    backup_code = live.backup_codes.pop(0)
                    self._store.save()
                    return backup_code

                self._intended_code = item.code
                outcome = await self._sessions.purchase_one(
                    session, item.product, mark_pending, take_code
                )
                if (state := self._current(account_id)) is None:
                    return
                if outcome is PurchaseOutcome.SUCCESS:
                    # Count what Razer's receipt says was bought, not what we
                    # meant to buy; they differed once when Razer re-selected
                    # the previous product.
                    receipt = getattr(session, "last_purchased", None)
                    actual = receipt or item.product
                    if receipt is None:
                        logger.warning("Receipt product unreadable; assuming %s", actual)
                    credited = state.find_item_by_product(actual)
                    if credited is not None:
                        credited.done += 1
                    # Normally already saved by the receipt callback; this is
                    # the safety net (and handles a receipt for another product).
                    pin = getattr(session, "last_pin", None)
                    self._remember_pin(
                        state, actual, pin, getattr(session, "last_receipt_url", None)
                    )
                    if not pin:
                        logger.warning("No code could be read for %s", actual)
                    state.pending = None
                    bought += 1
                    logger.info("Bought %s (%s total this check)", actual, bought)
                    if actual.casefold() != item.product.casefold():
                        state.paused = (
                            f"Razer's receipt shows {actual}, but the bot meant to "
                            f"buy {item.product}. The receipt's product was counted. "
                            "Purchases are paused: check your Razer transaction "
                            "history and /status, then send /checknow to continue."
                        )
                        self._store.save()
                        await self._flush_uploads(account_id, quick=True)
                        stop_message = state.paused
                        break
                    self._store.save()
                    await self._flush_uploads(account_id, quick=True)
                    continue
                if outcome is PurchaseOutcome.INSUFFICIENT_GOLD:
                    state.pending = None
                    self._store.save()
                    stop_message = (
                        "Not enough Razer Gold for "
                        f"{item.product}. Checking again in "
                        f"{self._interval_minutes} minutes."
                    )
                    break
                if outcome is PurchaseOutcome.CODE_REJECTED:
                    state.pending = None
                    self._store.save()
                    stop_message = (
                        "Razer rejected a backup code, so nothing was bought. "
                        "The next check will use the next code. "
                        f"{len(state.backup_codes)} codes left."
                    )
                    break
                # UNCERTAIN: the pending record stays and blocks purchases.
                self._store.save()
                stop_message = pending_message(state)
                break
        except RazerLoginError as exc:
            # purchase_one raises this only when nothing was bought, so a
            # pending record from this attempt can be dropped.
            if (live := self._current(account_id)) is not None and live.pending:
                live.pending = None
                self._store.save()
            stop_message = f"Stopped: {exc} Retrying at the next check."
        finally:
            # What the purchases left, read before the browser is closed.
            try:
                balance_now = await self._sessions.current_balance(session)
            except Exception:
                balance_now = None
            if (live := self._current(account_id)) is not None and balance_now is not None:
                live.last_balance = balance_now
                self._store.save()
            storage_state = await self._sessions.close(session)
            if (live := self._current(account_id)) is not None and storage_state:
                live.storage_state = storage_state
                self._store.save()

        if self._current(account_id) is None:
            return
        await self._finish_uploads(account_id)
        await self._report(
            stop_message,
            force=manual or bought > 0,
            bought=bought,
            balance=balance_now,
            stamp=True,
            note=self._upload_note(account_id),
        )
        # The order was finished by this check: hand over every code.
        if bought and (live := self._current(account_id)) is not None and live.order_complete:
            for text in codes_messages(live, "Order complete. All codes:"):
                await self._notify(text)

    async def _report(
        self,
        message: str,
        *,
        force: bool,
        bought: int = 0,
        balance: float | None = None,
        stamp: bool = False,
        note: str = "",
    ) -> None:
        """Send a status message, skipping exact repeats of the last one.

        The timer runs every 20 minutes; without this, "not enough Gold" would
        be sent every half hour until the account is topped up. With
        NOTIFY_EVERY_CHECK=true (the default) every check sends a message
        anyway. ``balance`` is the Gold left, read at the end of this check;
        ``stamp`` adds the check time, so a repeat is clearly a fresh check.
        """
        state = self._store.state
        if not (force or self._notify_every_check) and message == state.last_notice:
            return
        state.last_notice = message
        if state.account_id:
            self._store.save()
        lines: list[str] = []
        if bought:
            lines.append(f"Bought {bought} this check.")
        if state.order:
            lines.append(progress_text(state))
        if state.order_complete:
            lines.append("Order complete. No more purchases will be made.")
        elif message:
            lines.append(message)
        if lines and note:
            lines.append(note)
        if lines and balance is not None:
            lines.append(f"Razer Gold left: {balance:.2f}")
        if lines and stamp:
            lines.insert(0, f"Check at {now_text()}")
        if lines:
            await self._notify("\n\n".join(lines))
