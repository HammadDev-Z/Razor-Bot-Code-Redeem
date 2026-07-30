from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


class RazerLoginError(RuntimeError):
    """A safe, user-facing Razer login failure."""


class InvalidCredentialsError(RazerLoginError):
    """Razer rejected the email or password."""


class VerificationRequiredError(RazerLoginError):
    """Razer requires CAPTCHA, OTP, or another interactive check."""


@dataclass(slots=True)
class RazerSession:
    email: str
    context: BrowserContext
    page: Page
    backup_code: str
    checkout_started: bool = False


class RazerSessionManager:
    """Own one authenticated, isolated Razer browser context per Telegram user."""

    def __init__(self) -> None:
        self.headless = _env_bool("RAZER_HEADLESS", True)
        self.account_url = os.getenv(
            "RAZER_ACCOUNT_URL", "https://gold.razer.com/account-summary/"
        ).strip()
        self.login_timeout_ms = int(os.getenv("RAZER_LOGIN_TIMEOUT_MS", "120000"))
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[int, RazerSession] = {}
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            with contextlib.suppress(PlaywrightError):
                await session.context.close()
        self._sessions.clear()
        if self._browser is not None:
            with contextlib.suppress(PlaywrightError):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def login(
        self,
        user_id: int,
        email: str,
        password: str,
        backup_code: str,
    ) -> RazerSession:
        if not re.fullmatch(r"[A-Za-z0-9]{8}", backup_code):
            raise RazerLoginError("The first backup code must contain 8 characters.")
        await self.start()
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            assert self._browser is not None
            context = await self._browser.new_context(
                locale="en-US",
                viewport={"width": 1440, "height": 1000},
            )
            context.set_default_timeout(30_000)
            try:
                page = await context.new_page()
                page = await self._perform_login(context, page, email, password)
                page = await self._open_yalla_ludo(context, page)
            except Exception:
                with contextlib.suppress(PlaywrightError):
                    await context.close()
                raise

            old_session = self._sessions.pop(user_id, None)
            if old_session is not None:
                with contextlib.suppress(PlaywrightError):
                    await old_session.context.close()
            session = RazerSession(
                email=email,
                context=context,
                page=page,
                backup_code=backup_code.upper(),
            )
            self._sessions[user_id] = session
            return session

    async def logout(self, user_id: int) -> bool:
        session = self._sessions.pop(user_id, None)
        if session is None:
            return False
        with contextlib.suppress(PlaywrightError):
            await session.context.close()
        return True

    def get(self, user_id: int) -> RazerSession | None:
        return self._sessions.get(user_id)

    async def select_product(self, user_id: int, product_name: str) -> None:
        """Select one exact Yalla Ludo Diamonds product and do nothing else."""
        allowed_products = {
            "Yalla Ludo - USD 2 Diamonds",
            "Yalla Ludo - USD 5 Diamonds",
            "Yalla Ludo - USD 10 Diamonds",
            "Yalla Ludo - USD 25 Diamonds",
            "Yalla Ludo - USD 50 Diamonds",
            "Yalla Ludo - USD 100 Diamonds",
        }
        if product_name not in allowed_products:
            raise RazerLoginError("Unknown or unsafe product selection.")

        session = self._sessions.get(user_id)
        if session is None or session.page.is_closed():
            raise RazerLoginError("There is no active Razer session. Use /login.")
        if session.checkout_started:
            raise RazerLoginError(
                "Checkout was already started for this session. Use /login "
                "before starting another order."
            )
        if not session.backup_code:
            raise RazerLoginError("No backup code is available. Use /login.")
        page = session.page
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

        await self._select_razer_gold_and_checkout(session)

    async def _select_razer_gold_and_checkout(
        self, session: RazerSession
    ) -> None:
        """Choose Razer Gold, open checkout, and enter one backup code."""
        page = session.page
        razer_gold = await self._find_razer_gold_payment(
            page, timeout_ms=15_000
        )
        if razer_gold is None:
            raise RazerLoginError("Razer Gold payment method was not found.")
        await self._click_text_control(razer_gold)

        await self._scroll_to_page_bottom(page)
        checkout = await self._wait_for_exact_text(
            page, "CHECKOUT", timeout_ms=20_000, scroll_page=True
        )
        if checkout is None:
            raise RazerLoginError("CHECKOUT button was not found.")
        session.checkout_started = True
        try:
            await self._click_text_control(checkout)
        except RazerLoginError:
            session.checkout_started = False
            raise

        backup_codes = await self._open_backup_codes_menu(page)
        if backup_codes is None:
            raise RazerLoginError("Backup Codes method was not found.")
        await self._click_text_control(backup_codes)

        await self._fill_backup_code(page, session.backup_code)
        # The code is single-use sensitive data. Drop our reference immediately
        # after entry and deliberately do not click any later confirmation.
        session.backup_code = ""

    async def _open_backup_codes_menu(self, page: Page) -> Locator | None:
        """Open the alternate 2FA methods and return the Backup Codes control."""
        try:
            await page.wait_for_function(
                """() => /choose a different method/i.test(
                    document.body?.innerText || ''
                )""",
                timeout=60_000,
            )
        except PlaywrightTimeoutError as exc:
            raise RazerLoginError(
                "Two-step authentication options did not load after checkout."
            ) from exc

        # Let the initial modal reflow settle, but do not depend on the legal
        # text disappearing: Razer often only moves it below the viewport.
        first_seen = asyncio.get_running_loop().time()
        previous_box: dict[str, float] | None = None
        stable_since = first_seen
        moved = False
        while asyncio.get_running_loop().time() - first_seen < 12:
            trigger = await self._find_text_containing(
                page, "Choose a different method", timeout_ms=500
            )
            if trigger is None:
                await asyncio.sleep(0.1)
                continue
            try:
                box = await trigger.bounding_box()
            except PlaywrightError:
                box = None
            if box and previous_box:
                changed = (
                    abs(box["x"] - previous_box["x"]) > 2
                    or abs(box["y"] - previous_box["y"]) > 2
                    or abs(box["width"] - previous_box["width"]) > 2
                    or abs(box["height"] - previous_box["height"]) > 2
                )
                if changed:
                    moved = True
                    stable_since = asyncio.get_running_loop().time()
            elif box:
                stable_since = asyncio.get_running_loop().time()
            if box:
                previous_box = box
            now = asyncio.get_running_loop().time()
            if now - stable_since >= 0.6 and (moved or now - first_seen >= 3):
                break
            await asyncio.sleep(0.1)

        # Re-resolve and force the live control repeatedly. Each attempt is
        # verified by the appearance of Backup Codes, so a click on a temporary
        # element is harmless and never mistaken for success.
        click_deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < click_deadline:
            backup_codes = await self._wait_for_exact_text(
                page, "Backup Codes", timeout_ms=250, scroll_page=False
            )
            if backup_codes is not None:
                return backup_codes
            trigger = await self._find_text_containing(
                page, "Choose a different method", timeout_ms=750
            )
            if trigger is None:
                await asyncio.sleep(0.1)
                continue
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await trigger.scroll_into_view_if_needed()
                await trigger.click(force=True, timeout=1_000)
            backup_codes = await self._wait_for_exact_text(
                page, "Backup Codes", timeout_ms=500, scroll_page=False
            )
            if backup_codes is not None:
                return backup_codes

            # Fire through the smallest pointer-enabled ancestor.
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                await trigger.evaluate(
                    """element => {
                        let target = element;
                        for (let depth = 0; target && depth < 8; depth++) {
                            const style = getComputedStyle(target);
                            if (
                                target.matches(
                                    'button, a, [role="button"], [tabindex], [onclick]'
                                ) || style.cursor === 'pointer'
                            ) break;
                            target = target.parentElement;
                        }
                        (target || element).click();
                    }"""
                )
            backup_codes = await self._wait_for_exact_text(
                page, "Backup Codes", timeout_ms=500, scroll_page=False
            )
            if backup_codes is not None:
                return backup_codes

            # Final attempt in this round: a real mouse click at the current
            # on-screen center, followed by keyboard activation.
            with contextlib.suppress(PlaywrightTimeoutError, PlaywrightError):
                fresh_box = await trigger.bounding_box()
                if fresh_box:
                    await page.mouse.click(
                        fresh_box["x"] + fresh_box["width"] / 2,
                        fresh_box["y"] + fresh_box["height"] / 2,
                    )
                await trigger.focus()
                await page.keyboard.press("Enter")
            backup_codes = await self._wait_for_exact_text(
                page, "Backup Codes", timeout_ms=500, scroll_page=False
            )
            if backup_codes is not None:
                return backup_codes
            await asyncio.sleep(0.15)
        return None

    @staticmethod
    async def _find_text_containing(
        page: Page, phrase: str, *, timeout_ms: int
    ) -> Locator | None:
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            matches = page.get_by_text(pattern)
            best: tuple[int, Locator] | None = None
            try:
                for index in range(await matches.count()):
                    candidate = matches.nth(index)
                    if not await candidate.is_visible():
                        continue
                    text = await candidate.inner_text()
                    score = len(text.strip())
                    if best is None or score < best[0]:
                        best = (score, candidate)
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
            if best is not None:
                return best[1]
            await asyncio.sleep(0.1)
        return None

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
    async def _fill_backup_code(page: Page, code: str) -> None:
        try:
            await page.wait_for_function(
                """() => /enter the backup code/i.test(
                    document.body?.innerText || ''
                )""",
                timeout=15_000,
            )
        except PlaywrightTimeoutError as exc:
            raise RazerLoginError("Backup-code entry screen did not load.") from exc

        digit_selectors = (
            "input[maxlength='1']:visible, "
            "input[autocomplete='one-time-code']:visible, "
            "input[inputmode='numeric']:visible"
        )
        inputs = page.locator(digit_selectors)
        visible_inputs: list[Locator] = []
        for index in range(await inputs.count()):
            candidate = inputs.nth(index)
            try:
                if await candidate.is_visible():
                    visible_inputs.append(candidate)
            except PlaywrightError:
                continue
        if len(visible_inputs) >= len(code):
            for index, character in enumerate(code):
                await visible_inputs[index].fill(character)
            return
        if visible_inputs:
            await visible_inputs[0].fill(code)
            return
        raise RazerLoginError("Backup-code input boxes were not found.")

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
    ) -> Page:
        try:
            await page.goto(
                self.account_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except PlaywrightTimeoutError as exc:
            raise RazerLoginError("Razer Gold did not open.") from exc

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
                raise VerificationRequiredError(
                    "Razer requires CAPTCHA or verification. Set "
                    "RAZER_HEADLESS=false and try /login again."
                )

            for candidate in reversed(context.pages):
                if await self._is_authenticated(candidate):
                    with contextlib.suppress(PlaywrightError):
                        await candidate.bring_to_front()
                    return candidate
            await asyncio.sleep(0.2)

        if challenge_seen:
            raise VerificationRequiredError("Razer verification timed out.")
        raise RazerLoginError("Razer login timed out.")

    async def _open_yalla_ludo(
        self, context: BrowserContext, dashboard: Page
    ) -> Page:
        """Open Razer Gold, search for Yalla, and stop on Yalla Ludo."""
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
        return page

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
