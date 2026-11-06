"""One purchase check: open the account and buy until something stops it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from razer_session import (
    BackupCodesUnreadableError,
    InvalidCredentialsError,
    PurchaseOutcome,
    RazerAccountSession,
    RazerLoginError,
    RazerSessionManager,
    VerificationRequiredError,
)
from store import BotState, PendingPurchase, StateStore, now_text


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


class Purchaser:
    def __init__(
        self,
        store: StateStore,
        sessions: RazerSessionManager,
        notify: Notify,
        interval_minutes: int,
        refresh_at: int = 2,
        notify_every_check: bool = False,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._notify = notify
        self._interval_minutes = interval_minutes
        # Get new backup codes from Razer ID once this few (or fewer) are left.
        self._refresh_at = refresh_at
        # Message after every check, even when nothing changed since the last one.
        self._notify_every_check = notify_every_check
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

    def _current(self, account_id: str) -> BotState | None:
        """The live state, or None if the account was replaced or deleted."""
        state = self._store.state
        return state if state.account_id == account_id else None

    async def _run_cycle(self, manual: bool) -> None:
        state = self._store.state
        account_id = state.account_id
        reason = blocked_reason(state)
        if reason:
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
                        stop_message = state.paused
                        break
                    self._store.save()
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
        await self._report(
            stop_message,
            force=manual or bought > 0,
            bought=bought,
            balance=balance_now,
        )

    async def _report(
        self,
        message: str,
        *,
        force: bool,
        bought: int = 0,
        balance: float | None = None,
    ) -> None:
        """Send a status message, skipping exact repeats of the last one.

        The timer runs every 30 minutes; without this, "not enough Gold" would
        be sent every half hour until the account is topped up. Set
        NOTIFY_EVERY_CHECK=true to get a message after every check anyway.
        ``balance`` is the Gold left, read at the end of this check.
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
        if lines and balance is not None:
            lines.append(f"Razer Gold left: {balance:.2f}")
        if lines:
            await self._notify("\n\n".join(lines))
