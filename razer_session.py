from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


logger = logging.getLogger(__name__)

# Short code -> exact Razer product name. This is the only list of products the
# bot is allowed to buy.
PRODUCTS_BY_CODE: dict[str, str] = {
    "830": "Yalla Ludo - USD 2 Diamonds",
    "2320": "Yalla Ludo - USD 5 Diamonds",
    "5k": "Yalla Ludo - USD 10 Diamonds",
    "13k": "Yalla Ludo - USD 25 Diamonds",
    "27k": "Yalla Ludo - USD 50 Diamonds",
    "56k": "Yalla Ludo - USD 100 Diamonds",
}

# Page-text patterns. The 2-step ones are confirmed from a live snapshot; the
# insufficient/success/rejected ones are still best guesses until checked
# against real snapshots (RAZER_DEBUG_SNAPSHOTS=true).
# Patterns passed to Playwright's get_by_text are converted to JavaScript
# regexes, which reject inline "(?i)", so they must use re.IGNORECASE.
_CHOOSE_METHOD_PATTERN = re.compile(
    r"choose\s+(?:a\s+different|another)(?:\s+verification)?\s+method",
    re.IGNORECASE,
)
_TWO_STEP_PATTERN = re.compile(
    r"(?i)(2-step\s+(?:authentication|verification)|"
    r"choose\s+(?:a\s+different|another)(?:\s+verification)?\s+method)"
)
_BACKUP_OPTION_PATTERN = re.compile(r"backup\s+codes?", re.IGNORECASE)
RAZER_ID_URL = "https://razerid.razer.com/"
_SECURITY_SECTION_PATTERN = re.compile(
    r"^\s*(security|account\s+security|security\s+settings|two[- ]step|2[- ]step)\b",
    re.IGNORECASE,
)
_GENERATE_CODES_PATTERN = re.compile(
    r"(re)?generate|get\s+new\s+codes|new\s+(backup\s+)?codes", re.IGNORECASE
)
_CONFIRM_BUTTON_PATTERN = re.compile(
    r"^\s*(yes|confirm|continue|ok|proceed|generate|regenerate)\b", re.IGNORECASE
)
# A backup code as shown on screen: 8 letters/digits, optionally "1234 5678"
# or "1234-5678". At least two digits, so ordinary 8-letter words never count.
_CODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9]{4})[ \-]?([A-Za-z0-9]{4})(?![A-Za-z0-9])"
)


def _code_tokens(text: str) -> list[str]:
    """Backup-code-looking tokens in page text, upper-cased, in order, unique."""
    tokens: list[str] = []
    for match in _CODE_TOKEN.finditer(text):
        token = (match.group(1) + match.group(2)).upper()
        if sum(ch.isdigit() for ch in token) >= 2 and token not in tokens:
            tokens.append(token)
    return tokens
# With too little Gold, Razer swaps CHECKOUT for this button (confirmed live).
_RELOAD_TO_CHECKOUT = (
    "[data-cs-override-id='purchase-webshop-reload-checkout-btn'], "
    "button[aria-label='RELOAD TO CHECKOUT' i]"
)
_INSUFFICIENT_PATTERN = re.compile(
    r"(?i)(reload\s+to\s+checkout|"
    r"insufficient\s+(?:razer\s+)?(?:gold|balance|funds?)|"
    r"not\s+enough\s+(?:razer\s+)?(?:gold|balance)|"
    r"balance\s+is\s+(?:insufficient|not\s+enough))"
)
_SUCCESS_PATTERN = re.compile(
    r"(?i)(transaction\s+(?:was\s+|is\s+)?successful|"
    r"payment\s+(?:was\s+|is\s+)?successful|"
    r"purchase\s+(?:was\s+|is\s+)?successful|"
    r"order\s+(?:was\s+|is\s+|has\s+been\s+)?(?:completed?|successful)|"
    r"thank\s+you\s+for\s+your\s+(?:purchase|order)|"
    r"successfully\s+purchased|"
    # Confirmed on the live success page (/gold/purchase/transaction/<id>).
    r"congratulations!)"
)
_CODE_REJECTED_PATTERN = re.compile(
    r"(?i)((?:invalid|incorrect|wrong|expired)[^.\n]{0,40}\bcode|"
    r"\bcode[^.\n]{0,40}(?:is\s+invalid|is\s+incorrect|has\s+expired|"
    r"already\s+been\s+used))"
)


# The 2-step box: Razer's OTP iframe, or a dialog element on the page.
OtpScope = Frame | Locator
_OTP_DIALOG_PATTERN = re.compile(
    r"(2-step|two[- ]step|verification\s+code|enter\s+the\s+code|backup\s+code|"
    r"choose\s+(?:a\s+different|another))",
    re.IGNORECASE,
)


def _find_otp_frame(page: Page) -> Frame | None:
    """Razer's 2-step box is an iframe served from razerid.razer.com/otp."""
    for frame in page.frames:
        if "razerid.razer.com/otp" in frame.url:
            return frame
    return None


class RazerLoginError(RuntimeError):
    """A safe, user-facing Razer failure."""


class InvalidCredentialsError(RazerLoginError):
    """Razer rejected the email or password."""


class VerificationRequiredError(RazerLoginError):
    """Razer requires CAPTCHA, OTP, or another interactive check."""


class BackupCodesUnreadableError(RazerLoginError):
    """New backup codes were requested but could not be read.

    The old codes have probably stopped working, unlike a plain
    RazerLoginError from refresh_backup_codes, which means nothing changed.
    """


class PurchaseOutcome(enum.Enum):
    SUCCESS = "success"
    INSUFFICIENT_GOLD = "insufficient_gold"
    CODE_REJECTED = "code_rejected"
    # The backup code was typed but neither success nor failure was seen.
    # The purchase may have gone through, so the caller must not retry blindly.
    UNCERTAIN = "uncertain"


@dataclass(slots=True)
class RazerAccountSession:
    context: BrowserContext
    page: Page
    yalla_url: str
    balance: float | None = None
    on_product_menu: bool = True
    # Product named on the last receipt, or None if it could not be read.
    last_purchased: str | None = None


class RazerSessionManager:
    """Own the shared Chromium and open one isolated context per account check."""

    def __init__(self) -> None:
        self.headless = _env_bool("RAZER_HEADLESS", True)
        self.account_url = os.getenv(
            "RAZER_ACCOUNT_URL", "https://gold.razer.com/account-summary/"
        ).strip()
        self.login_timeout_ms = int(os.getenv("RAZER_LOGIN_TIMEOUT_MS", "120000"))
        self.snapshots_enabled = _env_bool("RAZER_DEBUG_SNAPSHOTS", False)
        self.snapshot_dir = Path(__file__).with_name("debug")
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )

    async def stop(self) -> None:
        # Ctrl-C also kills Playwright's driver process, so closing can fail
        # with a plain Exception; shutdown should stay quiet either way.
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def open_account(
        self,
        email: str,
        password: str,
        storage_state: dict[str, Any] | None,
    ) -> RazerAccountSession:
        """Open the account (reusing saved cookies) and stop on Yalla Ludo."""
        await self.start()
        assert self._browser is not None
        context = await self._browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
            storage_state=storage_state,
        )
        context.set_default_timeout(30_000)
        try:
            page = await context.new_page()
            page = await self._open_authenticated(
                context,
                page,
                email,
                password,
                have_cookies=bool(storage_state and storage_state.get("cookies")),
            )
            balance = await self._read_gold_balance(page)
            await self._snapshot(page, "account")
            page = await self._open_yalla_ludo(context, page)
            if balance is None:
                balance = await self._read_gold_balance(page)
        except Exception:
            with contextlib.suppress(PlaywrightError):
                await context.close()
            raise
        return RazerAccountSession(
            context=context, page=page, yalla_url=page.url, balance=balance
        )

    @staticmethod
    async def export_storage_state(
        session: RazerAccountSession,
    ) -> dict[str, Any] | None:
        with contextlib.suppress(PlaywrightError):
            return await session.context.storage_state()
        return None

    async def close(self, session: RazerAccountSession) -> dict[str, Any] | None:
        """Close the context and return its cookies for the next check."""
        storage_state = await self.export_storage_state(session)
        with contextlib.suppress(PlaywrightError):
            await session.context.close()
        return storage_state

    async def refresh_backup_codes(
        self,
        session: RazerAccountSession,
        email: str,
        password: str,
        known_codes: list[str],
        take_code: Callable[[], Awaitable[str]],
    ) -> list[str]:
        """Generate a new set of backup codes on Razer ID and return them.

        Razer ID profile → Backup Codes → (2-step verification with one backup
        code, if asked) → generate → (confirm, if asked) → read the new codes.
        Runs in a new tab of the check's browser context.

        Raises RazerLoginError if it failed before generate was clicked (the old
        codes still work), or BackupCodesUnreadableError after it (they likely
        do not). Snapshots of every step go to debug/codes-*.
        """
        context = session.context
        page = await context.new_page()
        try:
            page = await self._open_razer_id(context, page, email, password)
            await self._snapshot(page, "codes-1-profile", force=True)
            await self._dismiss_cookie_banner(page)
            item = await self._find_backup_codes_item(page)
            if item is None:
                await self._snapshot(page, "codes-2-not-found", force=True)
                raise RazerLoginError(
                    "The Backup Codes option was not found on the Razer ID profile."
                )
            await self._snapshot(page, "codes-2-found", force=True)
            exclude = {code.upper() for code in known_codes}
            exclude |= set(_code_tokens(await self._full_text(page)))
            await self._click_text_control(item)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 120
            verified = generate_clicked = confirm_clicked = False
            while loop.time() < deadline:
                scope = await self._find_otp_scope(page)
                if scope is not None and _TWO_STEP_PATTERN.search(
                    await self._scope_text(scope)
                ):
                    if verified:
                        raise self._refresh_error(
                            generate_clicked, "Razer asked for 2-step verification twice."
                        )
                    await self._snapshot(page, "codes-3-two-step", force=True)
                    await self._verify_with_backup_code(page, take_code)
                    verified = True
                    await self._snapshot(page, "codes-4-verified", force=True)
                    continue

                # Codes count as new only after generate was clicked: before
                # that the page may list old (used or unused) codes.
                new_codes = [
                    code
                    for code in _code_tokens(await self._full_text(page))
                    if code not in exclude
                ]
                if generate_clicked and len(new_codes) >= 5:
                    await page.wait_for_timeout(1_000)  # let the full list render
                    new_codes = [
                        code
                        for code in _code_tokens(await self._full_text(page))
                        if code not in exclude
                    ]
                    await self._snapshot(page, "codes-6-new-codes", force=True)
                    return new_codes

                if not generate_clicked:
                    generate = await self._find_pattern(
                        page, _GENERATE_CODES_PATTERN, timeout_ms=500
                    )
                    if generate is not None:
                        await self._snapshot(page, "codes-5-generate", force=True)
                        exclude |= set(_code_tokens(await self._full_text(page)))
                        generate_clicked = True
                        await self._click_text_control(generate)
                elif not confirm_clicked:
                    confirm = await self._find_confirm_button(page)
                    if confirm is not None:
                        await self._snapshot(page, "codes-5-confirm", force=True)
                        confirm_clicked = True
                        await self._click_text_control(confirm)
                await asyncio.sleep(0.5)

            await self._snapshot(page, "codes-timeout", force=True)
            raise self._refresh_error(
                generate_clicked,
                "New codes did not appear after generating."
                if generate_clicked
                else "The generate-codes button did not appear.",
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            await self._snapshot(page, "codes-error", force=True)
            raise RazerLoginError(f"Razer ID stopped responding: {exc}") from exc
        finally:
            with contextlib.suppress(PlaywrightError):
                await page.close()

    @staticmethod
    def _refresh_error(generate_clicked: bool, message: str) -> RazerLoginError:
        if generate_clicked:
            return BackupCodesUnreadableError(message)
        return RazerLoginError(message)

    async def _verify_with_backup_code(
        self, page: Page, take_code: Callable[[], Awaitable[str]]
    ) -> None:
        """Pass a 2-step prompt with one backup code (used on Razer ID)."""
        opened = await self._open_backup_codes_menu(page)
        if opened is None:
            raise RazerLoginError("Backup Codes method was not found.")
        scope, option = opened
        await self._click_text_control(option)
        inputs = await self._find_backup_code_inputs(scope)
        code = await take_code()
        await self._type_backup_code(inputs, code)
        code = ""

        loop = asyncio.get_running_loop()
        started = loop.time()
        confirm_tried = False
        while loop.time() - started < 30:
            scope = await self._find_otp_scope(page)
            text = await self._scope_text(scope) if scope is not None else ""
            if _CODE_REJECTED_PATTERN.search(text):
                await self._snapshot(page, "codes-code-rejected", force=True)
                raise RazerLoginError("Razer rejected the backup code.")
            if scope is None or not re.search(r"backup\s+code", text, re.IGNORECASE):
                return  # The 2-step box closed: verified.
            if not confirm_tried and loop.time() - started > 5:
                confirm_tried = True
                await self._click_first(
                    scope,
                    [
                        "button:has-text('Verify')",
                        "button:has-text('Confirm')",
                        "button:has-text('Submit')",
                        "button:has-text('Continue')",
                    ],
                )
            await asyncio.sleep(0.5)
        raise RazerLoginError("2-step verification did not finish.")

    async def _find_confirm_button(self, page: Page) -> Locator | None:
        """A confirmation button inside a visible dialog (e.g. 'this replaces
        your old codes'). Only dialogs are searched so nothing else is clicked."""
        dialogs = page.locator("[role='dialog'], .modal.show, [aria-modal='true']")
        with contextlib.suppress(PlaywrightError):
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                button = await self._find_pattern(
                    dialog, _CONFIRM_BUTTON_PATTERN, timeout_ms=300
                )
                if button is not None:
                    return button
        return None

    async def _open_razer_id(
        self, context: BrowserContext, page: Page, email: str, password: str
    ) -> Page:
        """Open razerid.razer.com logged in. Its session is separate from Gold's."""
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await page.goto(RAZER_ID_URL, wait_until="domcontentloaded", timeout=60_000)
        await self._dismiss_cookie_banner(page)
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            if await self._is_authenticated(page):
                return page
            await asyncio.sleep(0.25)
        return await self._perform_login(
            context, page, email, password, start_url=RAZER_ID_URL
        )

    async def _find_backup_codes_item(self, page: Page) -> Locator | None:
        """Find the Backup Codes option on the profile, scrolling as needed."""
        for _ in range(2):
            for _ in range(8):
                item = await self._find_pattern(
                    page, _BACKUP_OPTION_PATTERN, timeout_ms=600
                )
                if item is not None:
                    return item
                with contextlib.suppress(PlaywrightError):
                    await page.mouse.wheel(0, 700)
            # Not on this page: try a Security / Account section link.
            section = await self._find_pattern(
                page, _SECURITY_SECTION_PATTERN, timeout_ms=1_000
            )
            if section is None:
                return None
            with contextlib.suppress(RazerLoginError):
                await self._click_text_control(section)
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await page.wait_for_timeout(2_000)
        return None

    async def purchase_one(
        self,
        session: RazerAccountSession,
        product_name: str,
        mark_pending: Callable[[], Awaitable[None]],
        take_code: Callable[[], Awaitable[str]],
    ) -> PurchaseOutcome:
        """Buy one product with Razer Gold.

        Clicking CHECKOUT is the point of no return: Razer sometimes skips
        2-step verification (e.g. shortly after a previous one) and completes
        the purchase immediately. So ``mark_pending`` is awaited right before
        that click, and ``take_code`` only once the backup-code inputs are on
        screen, immediately before typing.

        Contract with the caller: RazerLoginError is raised only when nothing
        was bought (before CHECKOUT, or while 2-step is still blocking the
        payment). Anything ambiguous returns UNCERTAIN instead.
        """
        if product_name not in PRODUCTS_BY_CODE.values():
            raise RazerLoginError("Unknown or unsafe product selection.")

        await self._show_product_menu(session)
        session.on_product_menu = False
        session.last_purchased = None
        page = session.page
        await self._dismiss_cookie_banner(page)
        await self._select_product(page, product_name)

        razer_gold = await self._find_razer_gold_payment(page, timeout_ms=15_000)
        if razer_gold is None:
            raise RazerLoginError("Razer Gold payment method was not found.")
        await self._click_text_control(razer_gold)
        await self._snapshot(page, "payment-selected")
        if _INSUFFICIENT_PATTERN.search(await self._page_text(page)):
            return PurchaseOutcome.INSUFFICIENT_GOLD

        await self._scroll_to_page_bottom(page)
        checkout: Locator | None = None
        deadline = asyncio.get_running_loop().time() + 20
        while checkout is None and asyncio.get_running_loop().time() < deadline:
            reload_button = page.locator(_RELOAD_TO_CHECKOUT).first
            with contextlib.suppress(PlaywrightError):
                if await reload_button.is_visible():
                    await self._snapshot(page, "insufficient")
                    return PurchaseOutcome.INSUFFICIENT_GOLD
            checkout = await self._wait_for_exact_text(
                page, "CHECKOUT", timeout_ms=500, scroll_page=True
            )
        if checkout is None:
            raise RazerLoginError("CHECKOUT button was not found.")
        # Last check before paying: Razer can still switch the product back.
        await self._verify_selected(page, product_name)

        await mark_pending()
        # If this click raises, nothing was submitted (RazerLoginError).
        await self._click_text_control(checkout)
        stage = await self._wait_for_checkout_stage(session, timeout_ms=60_000)
        page = session.page
        if stage == "success":
            await self._snapshot(page, "success-no-2fa")
            session.last_purchased = await self._receipt_product(page)
            return PurchaseOutcome.SUCCESS
        if stage == "insufficient":
            await self._snapshot(page, "insufficient")
            return PurchaseOutcome.INSUFFICIENT_GOLD
        if stage != "two_step":
            await self._snapshot(page, "checkout-unknown")
            return PurchaseOutcome.UNCERTAIN
        await self._snapshot(page, "two-step")

        # 2-step is on screen, so the payment is held until a code is
        # accepted: any failure from here until typing means nothing was bought.
        try:
            opened = await self._open_backup_codes_menu(page)
            if opened is None:
                await self._snapshot(page, "backup-option-missing")
                raise RazerLoginError("Backup Codes method was not found.")
            otp_frame, backup_option = opened
            await self._snapshot(page, "methods")
            await self._click_text_control(backup_option)
            inputs = await self._find_backup_code_inputs(otp_frame)
            await self._snapshot(page, "backup-entry")
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise RazerLoginError("The 2-step screen stopped responding.") from exc

        code = await take_code()
        try:
            await self._type_backup_code(inputs, code)
            await self._snapshot(page, "code-entered")
            return await self._wait_for_purchase_outcome(session)
        except Exception:
            logger.exception("Purchase result unknown after the backup code was used")
            await self._snapshot(page, "outcome-error")
            return PurchaseOutcome.UNCERTAIN
        finally:
            code = ""

    async def _open_authenticated(
        self,
        context: BrowserContext,
        page: Page,
        email: str,
        password: str,
        *,
        have_cookies: bool,
    ) -> Page:
        """Reuse saved cookies when they are still valid; log in otherwise."""
        if have_cookies:
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await page.goto(
                    self.account_url, wait_until="domcontentloaded", timeout=60_000
                )
            await self._dismiss_cookie_banner(page)
            deadline = asyncio.get_running_loop().time() + 8
            while asyncio.get_running_loop().time() < deadline:
                if await self._is_authenticated(page):
                    return page
                await asyncio.sleep(0.25)
        return await self._perform_login(context, page, email, password)

    async def _show_product_menu(self, session: RazerAccountSession) -> None:
        """Return to the Yalla Ludo product list before each further purchase."""
        if session.on_product_menu and not session.page.is_closed():
            return
        if session.page.is_closed():
            session.page = await session.context.new_page()
        # Close receipts or other tabs left by the previous purchase so page
        # text scans only ever look at the active order.
        for other in list(session.context.pages):
            if other is not session.page:
                with contextlib.suppress(PlaywrightError):
                    await other.close()
        try:
            await session.page.goto(
                session.yalla_url, wait_until="domcontentloaded", timeout=30_000
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise RazerLoginError("Yalla Ludo page could not be reopened.") from exc
        await self._dismiss_cookie_banner(session.page)
        await self._wait_for_product_menu(session.page)
        session.on_product_menu = True

    @staticmethod
    async def _selected_products(page: Page) -> list[str]:
        """Names of the product tiles Razer currently shows as selected.

        Each product is an <input name="paymentAmountItem" type="radio"> inside
        a .selection-tile whose .selection-tile__content gets class "selected".
        """
        try:
            return await page.evaluate(
                """() => {
                    const tiles = [...document.querySelectorAll(
                            'input[name="paymentAmountItem"]')]
                        .map(input => input.closest('.selection-tile'))
                        .filter(Boolean);
                    // Razer's visible "selected" class is authoritative; the
                    // radio's checked state is only a fallback.
                    let chosen = tiles.filter(tile =>
                        tile.querySelector('.selection-tile__content.selected'));
                    if (!chosen.length) chosen = tiles.filter(tile =>
                        tile.querySelector('input[name="paymentAmountItem"]').checked);
                    return chosen.map(tile => (tile.querySelector(
                        '.selection-tile__text')?.innerText || '')
                        .replace(/\\s+/g, ' ').trim());
                }"""
            )
        except PlaywrightError:
            return []

    async def _verify_selected(self, page: Page, product_name: str) -> None:
        selected = await self._selected_products(page)
        if selected != [product_name]:
            await self._snapshot(page, "wrong-product")
            raise RazerLoginError(
                f"{product_name} is not the selected product "
                f"(Razer shows: {', '.join(selected) or 'nothing'}). "
                "Nothing was bought."
            )

    async def _select_product(self, page: Page, product_name: str) -> None:
        """Click the product and wait until it is the only one selected.

        Razer re-applies the previously bought product shortly after the page
        loads, which silently undid our click and bought the wrong item. So the
        selection must hold for a moment before we move on.
        """
        loop = asyncio.get_running_loop()
        selected: list[str] = []
        for _ in range(4):
            await self._click_product(page, product_name)
            clicked_at = loop.time()
            stable_since: float | None = None
            while loop.time() - clicked_at < 5:
                selected = await self._selected_products(page)
                now = loop.time()
                if selected == [product_name]:
                    stable_since = stable_since or now
                    if now - stable_since >= 1.5:
                        return
                else:
                    stable_since = None
                    if now - clicked_at >= 1.0:
                        break  # Not taken, or Razer switched it back: click again.
                await asyncio.sleep(0.2)
        await self._snapshot(page, "wrong-product")
        raise RazerLoginError(
            f"{product_name} could not be selected "
            f"(Razer shows: {', '.join(selected) or 'nothing'}). Nothing was bought."
        )

    async def _click_product(self, page: Page, product_name: str) -> None:
        amount = re.search(r"USD\s+(\d+)", product_name)
        assert amount is not None
        exact_product = re.compile(
            rf"^\s*Yalla\s+Ludo\s*-\s*USD\s+{amount.group(1)}\s+Diamonds\s*$",
            re.IGNORECASE,
        )
        matches = page.get_by_text(exact_product)
        target: Locator | None = None
        try:
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                if await candidate.is_visible():
                    target = candidate
                    break
        except (PlaywrightTimeoutError, PlaywrightError):
            target = None
        if target is None:
            raise RazerLoginError(f"{product_name} was not found.")

        await target.scroll_into_view_if_needed()
        clickable = target.locator(
            "xpath=ancestor-or-self::*[self::button or self::label or "
            "@role='button' or @role='radio'][1]"
        )
        if await clickable.count():
            target = clickable
        try:
            await target.click(force=True, no_wait_after=True)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise RazerLoginError(f"{product_name} could not be selected.") from exc

    async def _wait_for_checkout_stage(
        self, session: RazerAccountSession, *, timeout_ms: int
    ) -> str:
        """After CHECKOUT: "two_step", "success", "insufficient" or "unknown"."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for candidate in reversed(session.context.pages):
                text = await self._full_text(candidate)
                if _SUCCESS_PATTERN.search(text):
                    session.page = candidate
                    return "success"
                if _TWO_STEP_PATTERN.search(text):
                    session.page = candidate
                    return "two_step"
                if _INSUFFICIENT_PATTERN.search(text):
                    session.page = candidate
                    return "insufficient"
            await asyncio.sleep(0.25)
        return "unknown"

    async def _wait_for_purchase_outcome(
        self, session: RazerAccountSession
    ) -> PurchaseOutcome:
        loop = asyncio.get_running_loop()
        started = loop.time()
        confirm_tried = False
        while loop.time() - started < 90:
            for candidate in reversed(session.context.pages):
                text = await self._full_text(candidate)
                if _SUCCESS_PATTERN.search(text):
                    session.page = candidate
                    await self._snapshot(candidate, "success")
                    session.last_purchased = await self._receipt_product(candidate)
                    return PurchaseOutcome.SUCCESS
                if _CODE_REJECTED_PATTERN.search(text):
                    await self._snapshot(candidate, "code-rejected")
                    return PurchaseOutcome.CODE_REJECTED
            # Razer may submit on the last character; if it has not reacted
            # after a few seconds, press the modal's confirm button once.
            if not confirm_tried and loop.time() - started > 6:
                confirm_tried = True
                buttons = [
                    "button:has-text('Verify')",
                    "button:has-text('Confirm')",
                    "button:has-text('Submit')",
                    "button:has-text('Pay Now')",
                    "button:has-text('Continue')",
                ]
                # Prefer the button inside the 2-step iframe when it is open.
                otp_frame = _find_otp_frame(session.page)
                clicked = otp_frame is not None and await self._click_first(
                    otp_frame, buttons
                )
                if not clicked:
                    clicked = await self._click_first(session.page, buttons)
                if clicked:
                    await self._snapshot(session.page, "confirm-clicked")
            await asyncio.sleep(0.5)
        await self._snapshot(session.page, "outcome-unknown")
        return PurchaseOutcome.UNCERTAIN

    @staticmethod
    async def _read_gold_balance(page: Page) -> float | None:
        """Read the Razer Gold balance (USD) from the gold.razer.com header.

        The header holds e.g. <div data-cs-override-id="nav-gold-balance">
        ... <span class="sr-only">Razer Gold Balance USD 61.12</span>.
        """
        if "gold.razer.com" not in page.url:
            return None
        text = ""
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            text = await page.locator(
                "[data-cs-override-id='nav-gold-balance']"
            ).first.text_content(timeout=5_000) or ""
        if not text:
            text = await RazerSessionManager._page_text(page)
        match = re.search(
            r"(?i)gold\s+balance\D{0,10}?([\d,]+(?:\.\d+)?)", text
        ) or re.search(r"([\d,]+(?:\.\d+)?)", text)
        if match:
            with contextlib.suppress(ValueError):
                return float(match.group(1).replace(",", ""))
        return None

    @staticmethod
    async def _receipt_product(page: Page) -> str | None:
        """The product named on the receipt: 'Thank you for your order of X!'.

        Uses textContent (raw text), not innerText, so it does not depend on
        how the receipt is rendered. Markup: "Thank you for your order of
        <strong>Yalla Ludo - USD 5 Diamonds</strong>!".
        """
        for _ in range(10):
            try:
                text = await page.evaluate("document.body?.textContent || ''")
            except PlaywrightError:
                text = ""
            match = re.search(
                r"thank\s+you\s+for\s+your\s+order\s+of\s+(.{3,80}?)\s*!",
                text,
                re.IGNORECASE,
            )
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip()
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _page_text(scope: Page | Frame) -> str:
        try:
            return await scope.locator("body").inner_text(timeout=1_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            return ""

    @staticmethod
    async def _full_text(page: Page) -> str:
        """Page text plus the text of Razer iframes such as the 2-step box."""
        parts = [await RazerSessionManager._page_text(page)]
        for frame in page.frames:
            if frame is not page.main_frame and "razer" in frame.url:
                parts.append(await RazerSessionManager._page_text(frame))
        return "\n".join(parts)

    async def _snapshot(self, page: Page, label: str, *, force: bool = False) -> None:
        """Save a screenshot and the HTML when RAZER_DEBUG_SNAPSHOTS is on."""
        if not (self.snapshots_enabled or force):
            return
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            base = self.snapshot_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
            await page.screenshot(path=f"{base}.png", full_page=True)
            Path(f"{base}.html").write_text(await page.content(), encoding="utf-8")
            # page.content() does not include iframes; save Razer's separately.
            for index, frame in enumerate(page.frames):
                if frame is page.main_frame or "razer" not in frame.url:
                    continue
                with contextlib.suppress(PlaywrightError):
                    Path(f"{base}-frame{index}.html").write_text(
                        await frame.content(), encoding="utf-8"
                    )
        except Exception:
            logger.warning("Could not save debug snapshot %s", label, exc_info=True)

    @staticmethod
    async def _scope_text(scope: OtpScope) -> str:
        if isinstance(scope, Frame):
            return await RazerSessionManager._page_text(scope)
        try:
            return await scope.inner_text(timeout=1_500)
        except (PlaywrightTimeoutError, PlaywrightError):
            return ""

    @staticmethod
    async def _find_otp_scope(page: Page) -> OtpScope | None:
        """Where the 2-step box is: Razer's OTP iframe (as on gold.razer.com),
        or a visible dialog on the page itself showing 2-step text.

        A dialog is used rather than the whole page so that settings text such
        as a "Backup Codes" menu entry behind it is never mistaken for the
        2-step box's own options.
        """
        frame = _find_otp_frame(page)
        if frame is not None:
            # Razer can leave the iframe in the page, hidden, after it is done.
            with contextlib.suppress(PlaywrightError):
                if await (await frame.frame_element()).is_visible():
                    return frame
        dialogs = page.locator("[role='dialog'], .modal.show, [aria-modal='true']")
        with contextlib.suppress(PlaywrightError):
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                if await dialog.is_visible() and _OTP_DIALOG_PATTERN.search(
                    await RazerSessionManager._scope_text(dialog)
                ):
                    return dialog
        return None

    async def _wait_for_otp_scope(self, page: Page, *, timeout_ms: int) -> OtpScope:
        """Return the 2-step box once its verification text is showing.

        On gold.razer.com it is an iframe from razerid.razer.com/otp, so neither
        page text scans nor page-level locators can see into it.
        """
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            scope = await self._find_otp_scope(page)
            if scope is not None and _TWO_STEP_PATTERN.search(
                await self._scope_text(scope)
            ):
                return scope
            await asyncio.sleep(0.2)
        raise RazerLoginError("Two-step authentication options did not load.")

    async def _open_backup_codes_menu(
        self, page: Page
    ) -> tuple[OtpScope, Locator] | None:
        """Open 'Choose another verification method' and return Backup Codes."""
        frame = await self._wait_for_otp_scope(page, timeout_ms=60_000)

        async def backup_option(timeout_ms: int) -> Locator | None:
            return await self._find_pattern(
                frame, _BACKUP_OPTION_PATTERN, timeout_ms=timeout_ms
            )

        # Let the modal's opening animation settle so clicks land on the
        # final position of the link.
        previous_box: dict[str, float] | None = None
        settle_deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < settle_deadline:
            trigger = await self._find_pattern(
                frame, _CHOOSE_METHOD_PATTERN, timeout_ms=500
            )
            box = None
            if trigger is not None:
                with contextlib.suppress(PlaywrightError):
                    box = await trigger.bounding_box()
            if box and previous_box and all(
                abs(box[key] - previous_box[key]) <= 1 for key in box
            ):
                break
            previous_box = box
            await asyncio.sleep(0.25)

        # Each strategy is verified by Backup Codes appearing, so a click that
        # lands on a temporary element is harmless and never taken as success.
        click_deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < click_deadline:
            option = await backup_option(250)
            if option is not None:
                return frame, option
            trigger = await self._find_pattern(
                frame, _CHOOSE_METHOD_PATTERN, timeout_ms=750
            )
            if trigger is None:
                await asyncio.sleep(0.2)
                continue

            # 1. Ordinary trusted click on the link or its clickable ancestor.
            with contextlib.suppress(RazerLoginError):
                await self._click_text_control(trigger)
            if (option := await backup_option(1_000)) is not None:
                return frame, option

            # 2. Keyboard activation.
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await trigger.focus()
                await page.keyboard.press("Enter")
            if (option := await backup_option(750)) is not None:
                return frame, option

            # 3. The words can be a passive label while the green chevron
            # beside them owns the click handler; find and click it.
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await trigger.evaluate(
                    """label => {
                        const labelBox = label.getBoundingClientRect();
                        const centerY = labelBox.top + labelBox.height / 2;
                        const candidates = [...document.querySelectorAll('*')]
                            .filter(element => {
                                if (element === label) return false;
                                const box = element.getBoundingClientRect();
                                if (!box.width || !box.height) return false;
                                const text = (element.textContent || '').trim();
                                const hint = [
                                    element.className || '',
                                    element.getAttribute('aria-label') || '',
                                ].join(' ');
                                const isArrow = /^[>›»→❯]$/.test(text) ||
                                    /chevron|arrow|next/i.test(hint);
                                return isArrow &&
                                    box.left >= labelBox.right - 12 &&
                                    box.left <= labelBox.right + 100 &&
                                    Math.abs(box.top + box.height / 2 - centerY) <= 45;
                            });
                        const arrow = candidates[0];
                        if (!arrow) return false;
                        (arrow.closest('button, a, [role="button"], [tabindex]') ||
                            arrow).click();
                        return true;
                    }"""
                )
            if (option := await backup_option(750)) is not None:
                return frame, option

            # 4. Real mouse clicks just right of the words, for arrows drawn
            # as pseudo-elements. bounding_box() is in page coordinates.
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                arrow_box = await trigger.bounding_box()
                if arrow_box:
                    arrow_y = arrow_box["y"] + arrow_box["height"] / 2
                    for offset in (3, 8, 14, 22):
                        await page.mouse.click(
                            arrow_box["x"] + arrow_box["width"] + offset, arrow_y
                        )
                        if (option := await backup_option(350)) is not None:
                            return frame, option

            # 5. DOM click on the nearest pointer-enabled ancestor.
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await trigger.evaluate(
                    """element => {
                        let target = element;
                        for (let depth = 0; target && depth < 8; depth++) {
                            if (target.matches(
                                'button, a, [role="button"], [tabindex], [onclick]'
                            ) || getComputedStyle(target).cursor === 'pointer') break;
                            target = target.parentElement;
                        }
                        (target || element).click();
                    }"""
                )
            if (option := await backup_option(500)) is not None:
                return frame, option
            await asyncio.sleep(0.15)
        return None

    @staticmethod
    async def _find_pattern(
        scope: Page | Frame | Locator, pattern: re.Pattern[str], *, timeout_ms: int
    ) -> Locator | None:
        """Return the visible element with the shortest text matching pattern."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            matches = scope.get_by_text(pattern)
            best: tuple[int, Locator] | None = None
            try:
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if not await candidate.is_visible():
                        continue
                    score = len((await candidate.inner_text()).strip())
                    if best is None or score < best[0]:
                        best = (score, candidate)
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
            if best is not None:
                return best[1]
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    @staticmethod
    async def _find_razer_gold_payment(
        page: Page, *, timeout_ms: int
    ) -> Locator | None:
        """Find Razer Gold inside the payment card, never a footer/nav link."""
        exact = re.compile(r"^\s*Razer\s+Gold\s*$", re.IGNORECASE)
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            matches = page.get_by_text(exact)
            try:
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if not await candidate.is_visible():
                        continue
                    is_payment = await candidate.evaluate(
                        """element => {
                            if (element.closest('footer, nav')) return false;
                            const link = element.closest('a');
                            if (link && /\\/gold\\/(?:about)?(?:[?#/]|$)/i.test(
                                link.getAttribute('href') || ''
                            )) return false;
                            let parent = element;
                            for (let depth = 0; parent && depth < 7; depth++) {
                                const text = (parent.innerText || '').toLowerCase();
                                if (
                                    text.includes('razer gold') &&
                                    (
                                        text.includes('bonus:') ||
                                        text.includes('spend gold') ||
                                        text.includes('paypal') ||
                                        text.includes('fawry') ||
                                        text.includes('alipay')
                                    )
                                ) return true;
                                parent = parent.parentElement;
                            }
                            return false;
                        }"""
                    )
                    if is_payment:
                        return candidate
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
            with contextlib.suppress(PlaywrightError):
                await page.mouse.wheel(0, 700)
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    async def _scroll_to_page_bottom(page: Page) -> None:
        """Reach lazy-rendered checkout controls at the real document bottom."""
        for _ in range(3):
            with contextlib.suppress(PlaywrightError):
                await page.keyboard.press("End")
            with contextlib.suppress(PlaywrightError):
                await page.evaluate(
                    "window.scrollTo(0, document.documentElement.scrollHeight)"
                )
            with contextlib.suppress(PlaywrightError):
                await page.mouse.wheel(0, 3_000)
            await asyncio.sleep(0.3)

    @staticmethod
    async def _find_backup_code_inputs(scope: OtpScope) -> list[Locator]:
        """Wait for the backup-code entry screen and return its visible inputs.

        Only inputs inside the 2-step box (iframe or dialog) are considered, so
        a code can never be typed into some other field on the page.
        """
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            text = await RazerSessionManager._scope_text(scope)
            if re.search(r"backup\s+code", text, re.IGNORECASE) and not re.search(
                r"generated\s+by\s+your\s+authenticator", text, re.IGNORECASE
            ):
                inputs = scope.locator(
                    "input:not([type='hidden']):not([type='checkbox'])"
                    ":not([type='radio']):visible"
                )
                visible_inputs: list[Locator] = []
                with contextlib.suppress(PlaywrightError):
                    for index in range(await inputs.count()):
                        candidate = inputs.nth(index)
                        if await candidate.is_visible() and await candidate.is_editable():
                            visible_inputs.append(candidate)
                if visible_inputs:
                    return visible_inputs
            await asyncio.sleep(0.25)
        raise RazerLoginError("Backup-code entry screen did not load.")

    @staticmethod
    async def _type_backup_code(inputs: list[Locator], code: str) -> None:
        """Type like a person so per-box OTP widgets advance focus themselves."""
        page = inputs[0].page
        await inputs[0].click()
        await page.keyboard.type(code, delay=90)

        async def entered() -> str | None:
            """What the boxes hold, or None if they are gone (code submitted)."""
            values: list[str] = []
            try:
                for box in inputs:
                    values.append(await box.input_value(timeout=1_000))
            except (PlaywrightTimeoutError, PlaywrightError):
                return None
            return "".join(values).replace(" ", "").replace("-", "").upper()

        typed = await entered()
        if typed is None or typed == code.upper():
            return
        # The widget did not accept typed keys; set the boxes directly.
        if len(inputs) >= len(code):
            for index, character in enumerate(code):
                await inputs[index].fill(character)
        else:
            await inputs[0].fill(code)

    @staticmethod
    async def _wait_for_exact_text(
        page: Page,
        text: str,
        *,
        timeout_ms: int,
        scroll_page: bool,
    ) -> Locator | None:
        # Razer appends a visible ">" glyph to some action labels (notably
        # "Choose a different method >"). Treat only that suffix as optional.
        pattern = re.compile(
            rf"^\s*{re.escape(text).replace(r'\ ', r'\s+')}\s*>?\s*$",
            re.IGNORECASE,
        )
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            matches = page.get_by_text(pattern)
            try:
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
            if scroll_page:
                with contextlib.suppress(PlaywrightError):
                    await page.mouse.wheel(0, 800)
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    async def _click_text_control(target: Locator) -> None:
        clickable = target.locator(
            "xpath=ancestor-or-self::*[self::button or self::a or self::label "
            "or @role='button' or @role='radio'][1]"
        )
        if await clickable.count():
            target = clickable
        try:
            await target.scroll_into_view_if_needed()
            await target.click(force=True, no_wait_after=True)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise RazerLoginError("The expected control could not be clicked.") from exc

    async def _perform_login(
        self,
        context: BrowserContext,
        page: Page,
        email: str,
        password: str,
        *,
        start_url: str | None = None,
    ) -> Page:
        try:
            await page.goto(
                start_url or self.account_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except PlaywrightTimeoutError as exc:
            raise RazerLoginError("Razer did not open.") from exc
        await self._dismiss_cookie_banner(page)

        email_input = await self._find_visible(
            context, self._email_selectors(), timeout_ms=2_000
        )
        if email_input is None:
            await self._click_login(page)
            email_input = await self._find_visible(
                context,
                self._email_selectors(),
                timeout_ms=self.login_timeout_ms,
            )
        if email_input is None:
            raise RazerLoginError("Razer ID email field was not found.")

        page = email_input.page
        await email_input.fill(email)
        await email_input.press("Tab")
        password_input = await self._find_visible(
            context, self._password_selectors(), timeout_ms=3_000
        )
        if password_input is None:
            await self._click_first(
                page,
                [
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                    "button[type='submit']",
                ],
            )
            password_input = await self._find_visible(
                context,
                self._password_selectors(),
                timeout_ms=30_000,
            )
        if password_input is None:
            raise RazerLoginError("Razer ID password field was not found.")

        page = password_input.page
        await password_input.fill(password)
        submitted = await self._click_first(
            page,
            [
                "button:has-text('Log In')",
                "button:has-text('Sign In')",
                "button[type='submit']",
                "input[type='submit']",
            ],
        )
        if not submitted:
            await password_input.press("Enter")

        deadline = asyncio.get_running_loop().time() + self.login_timeout_ms / 1000
        challenge_seen = False
        while asyncio.get_running_loop().time() < deadline:
            # The cookie banner covers the consent form, so clear it first.
            await self._dismiss_cookie_banners(context)
            for candidate in list(context.pages):
                if await self._looks_like_consent(candidate):
                    await self._decline_consent(candidate)

            combined_text = await self._combined_text(context)
            if re.search(
                r"(?i)(invalid|incorrect|wrong).{0,40}(password|credentials|email)",
                combined_text,
            ):
                raise InvalidCredentialsError("Incorrect Razer email or password.")

            challenge_seen = bool(
                re.search(
                    r"(?i)(captcha|verification code|one[- ]time|two[- ]factor|"
                    r"authenticator|verify (?:your|this))",
                    combined_text,
                )
            )
            if challenge_seen and self.headless:
                await self._snapshot_context(context, "login-verification")
                raise VerificationRequiredError(
                    "Razer asked for a CAPTCHA or verification at login. "
                    "Screenshots were saved in debug/."
                )

            for candidate in reversed(context.pages):
                if await self._is_authenticated(candidate):
                    with contextlib.suppress(PlaywrightError):
                        await candidate.bring_to_front()
                    return candidate
            await asyncio.sleep(0.2)

        # Always keep evidence of a failed login, whatever the snapshot setting.
        await self._snapshot_context(context, "login-timeout")
        if challenge_seen:
            raise VerificationRequiredError(
                "Razer verification timed out. Screenshots were saved in debug/."
            )
        raise RazerLoginError("Razer login timed out. Screenshots were saved in debug/.")

    async def _snapshot_context(self, context: BrowserContext, label: str) -> None:
        """Force-save every open tab (login can spread over several)."""
        for index, candidate in enumerate(list(context.pages)):
            if not candidate.is_closed():
                await self._snapshot(candidate, f"{label}-tab{index}", force=True)

    async def _open_yalla_ludo(
        self, context: BrowserContext, dashboard: Page
    ) -> Page:
        """Open Razer Gold, search for Yalla, and stop on Yalla Ludo."""
        await self._dismiss_cookie_banners(context)
        page = dashboard
        if "razerid.razer.com" in dashboard.url:
            recharge = await self._wait_for_visible_locator(
                dashboard,
                [
                    "button:has-text('RECHARGE GAMES')",
                    "a:has-text('RECHARGE GAMES')",
                    "text=/^\\s*RECHARGE GAMES\\s*$/i",
                ],
                timeout_ms=15_000,
                scroll_page=True,
            )
            if recharge is None:
                raise RazerLoginError(
                    "Login succeeded, but RECHARGE GAMES was not found."
                )

            pages_before = set(context.pages)
            await recharge.scroll_into_view_if_needed()
            await recharge.click(force=True, no_wait_after=True)
            page = await self._wait_for_gold_page(
                context, dashboard, pages_before, timeout_ms=20_000
            )
            if page is dashboard and "gold.razer.com" not in page.url:
                raise RazerLoginError(
                    "RECHARGE GAMES did not open Razer Gold."
                )

            # RECHARGE GAMES normally opens Gold in a new tab. Keep only that
            # authenticated tab so later commands cannot target the dashboard.
            for old_page in list(context.pages):
                if old_page is page or old_page.is_closed():
                    continue
                with contextlib.suppress(PlaywrightError):
                    await old_page.close()

        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        with contextlib.suppress(PlaywrightError):
            await page.bring_to_front()

        search_input = await self._find_search_input(page, timeout_ms=10_000)
        if search_input is None:
            raise RazerLoginError("Razer Gold search box was not found.")
        await search_input.fill("Yalla")
        await search_input.press("Enter")

        if not await self._wait_for_yalla_results(page, timeout_ms=2_500):
            # Some Razer variants display the value but ignore Enter. Try the
            # magnifier/search control next, then verify before falling back.
            await self._click_search_submit(page, search_input)
        if not await self._wait_for_yalla_results(page, timeout_ms=2_500):
            # Use the same authenticated regional route produced by the search
            # widget. This avoids leaving the account stuck on /gold/reload.
            search_url = self._search_results_url(page.url, "Yalla")
            try:
                await page.goto(
                    search_url,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                raise RazerLoginError("Yalla search could not be submitted.") from exc
        if not await self._wait_for_yalla_results(page, timeout_ms=15_000):
            raise RazerLoginError("Yalla search results did not load.")

        yalla_ludo = page.get_by_text(
            re.compile(r"^\s*Yalla Ludo\s*$", re.IGNORECASE)
        ).first
        try:
            await yalla_ludo.wait_for(state="visible", timeout=10_000)
            link = yalla_ludo.locator("xpath=ancestor-or-self::a[1]")
            target = link if await link.count() else yalla_ludo
            await target.scroll_into_view_if_needed()
            await target.click(force=True, no_wait_after=True)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise RazerLoginError("Yalla Ludo result could not be opened.") from exc

        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body?.innerText || '';
                    return /yalla ludo/i.test(text) &&
                        !/search results\\s+for/i.test(text);
                }""",
                timeout=20_000,
            )
        except PlaywrightTimeoutError as exc:
            raise RazerLoginError("Yalla Ludo page did not finish opening.") from exc

        await self._wait_for_product_menu(page)
        return page

    async def _wait_for_product_menu(self, page: Page) -> None:
        select_product = await self._wait_for_visible_locator(
            page,
            ["text=/^\\s*Select Product\\s*$/i"],
            timeout_ms=15_000,
            scroll_page=True,
        )
        if select_product is None:
            raise RazerLoginError("Yalla Ludo product menu did not load.")
        await select_product.scroll_into_view_if_needed()
        diamond_option = await self._wait_for_visible_locator(
            page,
            ["text=/^\\s*Yalla Ludo\\s*-\\s*USD\\s+2\\s+Diamonds\\s*$/i"],
            timeout_ms=10_000,
            scroll_page=True,
        )
        if diamond_option is None:
            raise RazerLoginError("Yalla Ludo Diamonds options did not load.")

    @staticmethod
    async def _wait_for_yalla_results(page: Page, *, timeout_ms: int) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body?.innerText || '';
                    return /search results/i.test(text) && /yalla ludo/i.test(text);
                }""",
                timeout=timeout_ms,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError):
            return False

    @staticmethod
    async def _click_search_submit(page: Page, search_input: Locator) -> bool:
        """Click the magnifier associated with the active Gold search input."""
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            clicked = await search_input.evaluate(
                """input => {
                    const inputBox = input.getBoundingClientRect();
                    let container = input.parentElement;
                    for (let depth = 0; container && depth < 4; depth++, container = container.parentElement) {
                        const candidates = [...container.querySelectorAll(
                            'button, [role="button"], [aria-label*="search" i], ' +
                            '[class*="search-icon" i], [class*="icon-search" i], svg'
                        )].filter(element => element !== input);
                        const trigger = candidates.find(element => {
                            const box = element.getBoundingClientRect();
                            return box.width > 0 && box.height > 0 &&
                                box.right <= inputBox.left + 70 &&
                                Math.abs((box.top + box.bottom) / 2 -
                                    (inputBox.top + inputBox.bottom) / 2) < 60;
                        });
                        if (trigger) {
                            const clickable = trigger.closest(
                                'button, [role="button"], a'
                            ) || trigger;
                            clickable.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if clicked:
                return True
        return await RazerSessionManager._click_first(
            page,
            [
                "button[aria-label*='search' i]",
                "[role='button'][aria-label*='search' i]",
                "[class*='search-icon' i]",
                "[class*='icon-search' i]",
            ],
        )

    @staticmethod
    def _search_results_url(current_url: str, query: str) -> str:
        parsed = urlparse(current_url)
        parts = [part for part in parsed.path.split("/") if part]
        locale_path = "/".join(parts[:2]) if len(parts) >= 2 else "global/en"
        return (
            f"{parsed.scheme or 'https'}://{parsed.netloc or 'gold.razer.com'}/"
            f"{locale_path}/search-result?{urlencode({'search': query})}"
        )

    async def _find_search_input(
        self, page: Page, *, timeout_ms: int
    ) -> Locator | None:
        selectors = [
            "input[placeholder*='Search gold.razer.com' i]",
            "input[placeholder*='Search' i]",
            "input[type='search']",
            "[role='searchbox']",
        ]
        search_input = await self._wait_for_visible_locator(
            page, selectors, timeout_ms=2_000
        )
        if search_input is not None:
            return search_input

        # Some Gold builds render a collapsed search control first and mount
        # the input only after it is clicked.
        await self._click_first(
            page,
            [
                "button[aria-label*='search' i]",
                "[role='button'][aria-label*='search' i]",
                "text=Search gold.razer.com",
            ],
        )
        return await self._wait_for_visible_locator(
            page, selectors, timeout_ms=timeout_ms
        )

    @staticmethod
    async def _wait_for_gold_page(
        context: BrowserContext,
        source: Page,
        pages_before: set[Page],
        *,
        timeout_ms: int,
    ) -> Page:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            new_gold_pages = [
                candidate
                for candidate in context.pages
                if candidate not in pages_before
                and not candidate.is_closed()
                and "gold.razer.com" in candidate.url
            ]
            if new_gold_pages:
                return new_gold_pages[-1]
            if not source.is_closed() and "gold.razer.com" in source.url:
                return source
            await asyncio.sleep(0.1)
        return source

    @staticmethod
    async def _wait_for_visible_locator(
        page: Page,
        selectors: list[str],
        *,
        timeout_ms: int,
        scroll_page: bool = False,
    ) -> Locator | None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                matches = page.locator(selector)
                try:
                    for index in range(await matches.count()):
                        candidate = matches.nth(index)
                        if await candidate.is_visible():
                            return candidate
                except (PlaywrightTimeoutError, PlaywrightError):
                    continue
            if scroll_page:
                with contextlib.suppress(PlaywrightError):
                    await page.mouse.wheel(0, 600)
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    async def _click_login(page: Page) -> None:
        links = page.locator(
            "a:has-text('Log In'), a:has-text('Sign In'), "
            "button:has-text('Log In'), button:has-text('Sign In')"
        )
        for index in range(await links.count()):
            link = links.nth(index)
            try:
                if not await link.is_visible():
                    continue
                href = await link.get_attribute("href")
                if href and not href.lower().startswith("javascript:"):
                    await page.goto(
                        urljoin(page.url, href),
                        wait_until="commit",
                        timeout=60_000,
                    )
                else:
                    await link.click(no_wait_after=True)
                return
            except (PlaywrightTimeoutError, PlaywrightError):
                if "razerid.razer.com" in page.url:
                    return
        raise RazerLoginError("Razer Gold login button was not found.")

    @staticmethod
    async def _is_authenticated(page: Page) -> bool:
        try:
            parsed = urlparse(page.url)
            password_visible = await page.locator(
                "input[type='password']:visible"
            ).count()
            if password_visible:
                return False
            if (
                parsed.netloc == "razerid.razer.com"
                and parsed.path.rstrip("/").endswith("/dashboard")
            ):
                return True
            if parsed.netloc != "gold.razer.com":
                return False
            login_controls = page.locator(
                "button:has-text('LOG IN'), a:has-text('LOG IN'), "
                "button:has-text('SIGN IN'), a:has-text('SIGN IN')"
            )
            for index in range(await login_controls.count()):
                if await login_controls.nth(index).is_visible():
                    return False
            body = (await page.locator("body").inner_text(timeout=2_000)).lower()
            return any(
                marker in body
                for marker in (
                    "razer gold wallet",
                    "transaction history",
                    "account summary",
                    "silver",
                    "rewards",
                )
            )
        except (PlaywrightTimeoutError, PlaywrightError):
            return False

    @staticmethod
    async def _dismiss_cookie_banner(page: Page) -> bool:
        """Press "Reject All" on Razer's cookie banner if it is showing.

        Shown to EU visitors (e.g. the VPS) and not on the PC; it covers the
        page, so clicks meant for the form underneath land on it. Rejecting is
        the most privacy-preserving choice, and the consent cookie it sets is
        kept in the saved storage_state, so it should not return.
        """
        buttons = page.locator(
            "[data-cky-tag='reject-button'], "
            "button.cky-btn-reject, button[aria-label='Reject All' i]"
        )
        with contextlib.suppress(PlaywrightError):
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if await button.is_visible():
                    await button.click(timeout=5_000)
                    await page.wait_for_timeout(500)
                    return True
        return False

    async def _dismiss_cookie_banners(self, context: BrowserContext) -> None:
        for candidate in list(context.pages):
            if not candidate.is_closed():
                await self._dismiss_cookie_banner(candidate)

    @staticmethod
    async def _looks_like_consent(page: Page) -> bool:
        if "newsletter-consent-check" in page.url:
            return True
        try:
            text = (await page.locator("body").inner_text(timeout=1_000)).lower()
            return "contact permission" in text or "marketing communications" in text
        except (PlaywrightTimeoutError, PlaywrightError):
            return False

    async def _decline_consent(self, page: Page) -> None:
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            await page.locator("input[type='radio'], [role='radio']").first.wait_for(
                state="attached", timeout=3_000
            )
        with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
            selected = await page.evaluate(
                """() => {
                    const clean = value => (value || '').trim().replace(/[.]+$/, '');
                    const label = [...document.querySelectorAll('label')].find(
                        item => clean(item.innerText).toLowerCase() === 'no'
                    );
                    let radio = label?.control;
                    if (!radio && label?.htmlFor) radio = document.getElementById(label.htmlFor);
                    const radios = [...document.querySelectorAll(
                        'input[type="radio"], [role="radio"]'
                    )];
                    radio ||= radios.find(item => /^(no|false|decline|reject|opt.?out)$/i.test(
                        item.value || item.getAttribute('aria-label') || ''
                    )) || radios.at(-1);
                    if (!radio) return false;
                    radio.click();
                    if (radio instanceof HTMLInputElement && !radio.checked) {
                        radio.checked = true;
                        radio.dispatchEvent(new Event('input', {bubbles: true}));
                        radio.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    return true;
                }"""
            )
            if not selected:
                return
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._click_first(
            page,
            [
                "button:has-text('Continue')",
                "button:has-text('Submit')",
                "button:has-text('Confirm')",
                "button:has-text('Save')",
                "button:has-text('Next')",
                "button[type='submit']",
                "input[type='submit']",
            ],
        )

    @staticmethod
    async def _combined_text(context: BrowserContext) -> str:
        samples: list[str] = []
        for page in list(context.pages):
            try:
                samples.append(
                    (await page.locator("body").inner_text(timeout=1_000))[:4_000]
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        return " ".join(samples)

    @staticmethod
    async def _find_visible(
        context: BrowserContext,
        selectors: list[str],
        *,
        timeout_ms: int,
    ) -> Locator | None:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for page in reversed(context.pages):
                for frame in page.frames:
                    for selector in selectors:
                        locator = frame.locator(selector).first
                        try:
                            if await locator.count() and await locator.is_visible():
                                return locator
                        except (PlaywrightTimeoutError, PlaywrightError):
                            continue
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    async def _click_first(page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            matches = page.locator(selector)
            try:
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        await candidate.click(no_wait_after=True)
                        return True
            except (PlaywrightTimeoutError, PlaywrightError):
                continue
        return False

    @staticmethod
    def _email_selectors() -> list[str]:
        return [
            "#input-login-email",
            "input[type='email']",
            "input[autocomplete='email']",
            "input[autocomplete='username']",
            "input[name*='email' i]",
            "input[placeholder*='email' i]",
        ]

    @staticmethod
    def _password_selectors() -> list[str]:
        return [
            "#input-login-password",
            "input[autocomplete='current-password']",
            "input[name='password']",
            "input[type='password']",
        ]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
