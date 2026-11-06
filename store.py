"""Encrypted, crash-safe persistence for the account, order, and progress."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from razer_session import PRODUCTS_BY_CODE


@dataclass(slots=True)
class OrderItem:
    code: str
    quantity: int
    done: int = 0

    @property
    def product(self) -> str:
        return PRODUCTS_BY_CODE[self.code]

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.done, 0)


@dataclass(slots=True)
class PendingPurchase:
    """A purchase whose backup code was typed but whose result is not yet known."""

    code: str
    started_at: str


@dataclass(slots=True)
class BotState:
    # Changes whenever the account is replaced or deleted, so a check that is
    # still running can tell its results no longer belong to the current state.
    account_id: str = ""
    email: str = ""
    password: str = ""
    backup_codes: list[str] = field(default_factory=list)
    storage_state: dict[str, Any] | None = None
    login_blocked: str = ""
    # Set when something needs the user's eyes (e.g. a receipt for the wrong
    # product). Cleared by /checknow or a new /order.
    paused: str = ""
    order: list[OrderItem] = field(default_factory=list)
    pending: PendingPurchase | None = None
    last_balance: float | None = None
    last_check_at: str = ""
    last_notice: str = ""

    @property
    def has_account(self) -> bool:
        return bool(self.email and self.password)

    @property
    def order_complete(self) -> bool:
        return bool(self.order) and all(item.remaining == 0 for item in self.order)

    def next_item(self) -> OrderItem | None:
        """Items are bought strictly in order: finish one before the next."""
        for item in self.order:
            if item.remaining:
                return item
        return None

    def find_item(self, code: str) -> OrderItem | None:
        for item in self.order:
            if item.code == code:
                return item
        return None

    def find_item_by_product(self, product: str) -> OrderItem | None:
        for item in self.order:
            if item.product.casefold() == product.casefold():
                return item
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BotState:
        pending = data.get("pending")
        return cls(
            account_id=data.get("account_id", ""),
            email=data.get("email", ""),
            password=data.get("password", ""),
            backup_codes=list(data.get("backup_codes", [])),
            storage_state=data.get("storage_state"),
            login_blocked=data.get("login_blocked", ""),
            paused=data.get("paused", ""),
            order=[
                OrderItem(**item)
                for item in data.get("order", [])
                if item.get("code") in PRODUCTS_BY_CODE
            ],
            pending=PendingPurchase(**pending) if pending else None,
            last_balance=data.get("last_balance"),
            last_check_at=data.get("last_check_at", ""),
            last_notice=data.get("last_notice", ""),
        )


class StateStore:
    """Holds the single bot state in memory and writes it encrypted to disk."""

    def __init__(self, path: Path, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise RuntimeError(
                "BOT_SECRET_KEY is not a valid key. " + _KEY_HELP
            ) from exc
        self._path = path
        self.state = self._load()

    @classmethod
    def from_env(cls) -> StateStore:
        key = os.getenv("BOT_SECRET_KEY", "").strip()
        if not key:
            raise RuntimeError("BOT_SECRET_KEY is missing from .env. " + _KEY_HELP)
        default_dir = Path(__file__).with_name("data")
        data_dir = Path(os.getenv("BOT_DATA_DIR", "").strip() or default_dir)
        return cls(data_dir / "state.enc", key)

    def _load(self) -> BotState:
        if not self._path.exists():
            return BotState()
        try:
            raw = self._fernet.decrypt(self._path.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError(
                f"Could not decrypt {self._path}. BOT_SECRET_KEY has changed; "
                "restore the old key or delete that file to start over."
            ) from exc
        return BotState.from_dict(json.loads(raw))

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self.state)).encode()
        temporary = self._path.with_suffix(".tmp")
        temporary.write_bytes(self._fernet.encrypt(payload))
        # Atomic replace: a crash mid-write never leaves a half-written state.
        os.replace(temporary, self._path)

    def set_account(self, email: str, password: str, codes: list[str]) -> None:
        """Replace the account. The order and any unresolved purchase are kept."""
        old = self.state
        self.state = BotState(
            account_id=uuid.uuid4().hex,
            email=email,
            password=password,
            backup_codes=list(dict.fromkeys(codes)),
            order=old.order,
            pending=old.pending,
        )
        self.save()

    def add_codes(self, codes: list[str]) -> int:
        existing = set(self.state.backup_codes)
        added = [code for code in dict.fromkeys(codes) if code not in existing]
        self.state.backup_codes.extend(added)
        self.save()
        return len(added)

    def set_order(self, items: list[OrderItem]) -> None:
        self.state.order = items
        self.state.paused = ""
        self.state.last_notice = ""
        self.save()

    def clear(self) -> None:
        """Delete everything: account, saved login, codes, order, progress."""
        self.state = BotState()
        self._path.unlink(missing_ok=True)
        self._path.with_suffix(".tmp").unlink(missing_ok=True)


_KEY_HELP = (
    "Generate one with:\n"
    '  python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"\n'
    "and add it to .env as BOT_SECRET_KEY=..."
)

_ORDER_ITEM = re.compile(
    r"(?<![0-9a-z])(?P<code>"
    + "|".join(
        re.escape(code) for code in sorted(PRODUCTS_BY_CODE, key=len, reverse=True)
    )
    + r")\s*(?:[x×*]\s*(?P<after>\d+)|(?P<before>\d+)\s*[x×*])(?![0-9a-z])"
)


def parse_order(text: str) -> list[OrderItem]:
    """Parse '830x30 2320x20' or '830 30x 2320 20x' into ordered items."""
    normalized = text.casefold()
    leftover = _ORDER_ITEM.sub(" ", normalized).strip(" ,;")
    if leftover:
        raise ValueError(f"I could not understand: {leftover}")
    items: list[OrderItem] = []
    seen: set[str] = set()
    for match in _ORDER_ITEM.finditer(normalized):
        code = match.group("code")
        quantity = int(match.group("after") or match.group("before"))
        if code in seen:
            raise ValueError(f"{code} is listed twice.")
        if not 1 <= quantity <= 1000:
            raise ValueError(f"Quantity for {code} must be between 1 and 1000.")
        seen.add(code)
        items.append(OrderItem(code=code, quantity=quantity))
    if not items:
        raise ValueError("The order is empty.")
    return items


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
