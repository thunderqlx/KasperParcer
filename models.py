from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GiftType:
    gift_id: int
    title: str
    resale_available: int | None


@dataclass(frozen=True)
class MarketListing:
    gift_id: int
    title: str
    number: int | None
    username: str | None
    owner_gifts_count: int | None
    owner_premium: bool | None
    owner_stars_level: int | None
    price_amount: int | None
    price_nanos: int | None
    price_currency: str
    slug: str | None
    gift_address: str | None

    @property
    def nft_url(self) -> str | None:
        return f"https://t.me/nft/{self.slug}" if self.slug else None
