"""Send bought voucher PINs to the website's stock (POST /api/stock/import)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx


logger = logging.getLogger(__name__)


class UploadError(Exception):
    """The upload did not go through. The message never contains codes or the key."""


@dataclass(slots=True)
class UploadResult:
    imported: int
    skipped: int


class StockUploader:
    def __init__(self, url: str, key: str, timeout: float = 15.0) -> None:
        self._url = url
        self._key = key
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> StockUploader | None:
        """Configured by STOCK_API_URL and STOCK_API_KEY; None (off) if either is empty."""
        url = os.getenv("STOCK_API_URL", "").strip()
        key = os.getenv("STOCK_API_KEY", "").strip()
        if not url or not key:
            return None
        if not url.lower().startswith("https://") and "localhost" not in url and "127.0.0.1" not in url:
            raise RuntimeError(
                "STOCK_API_URL must use https:// (the key and the codes travel in it)."
            )
        return cls(url, key)

    async def upload(self, category: str, codes: list[str]) -> UploadResult:
        """Add codes under a category. The website resolves aliases (5k is 5150) and
        skips codes it already has, so repeating an upload is harmless."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._url,
                    json={"category": category, "codes": codes},
                    headers={"Authorization": f"Bearer {self._key}"},
                )
        except httpx.HTTPError as exc:
            raise UploadError(f"website not reachable ({type(exc).__name__})") from None
        if response.status_code == 401:
            raise UploadError("website refused the key (check STOCK_API_KEY)")
        if response.status_code == 404:
            raise UploadError("website has no stock endpoint (STOCK_API_KEY unset there?)")
        if response.status_code != 200:
            raise UploadError(f"website answered HTTP {response.status_code}")
        try:
            data = response.json()
            imported, skipped, failed = (
                int(data["imported"]),
                int(data["skipped"]),
                int(data["failed"]),
            )
        except (ValueError, KeyError, TypeError):
            raise UploadError("website answered with something unexpected") from None
        if failed or imported + skipped != len(codes):
            raise UploadError(f"website could not take {len(codes) - imported - skipped} codes")
        return UploadResult(imported=imported, skipped=skipped)
