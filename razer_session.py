from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

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

    async def login(self, user_id: int, email: str, password: str) -> RazerSession:
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
            except Exception:
                with contextlib.suppress(PlaywrightError):
                    await context.close()
                raise

            old_session = self._sessions.pop(user_id, None)
            if old_session is not None:
                with contextlib.suppress(PlaywrightError):
                    await old_session.context.close()
            session = RazerSession(email=email, context=context, page=page)
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

