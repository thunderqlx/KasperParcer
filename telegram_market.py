from __future__ import annotations

import asyncio
import random
from typing import Any

from pyrogram import Client
from pyrogram.raw.functions import payments
from pyrogram.raw.functions.users import GetFullUser

from config import Settings
from models import GiftType, MarketListing
from storage import cached_profile_info, save_profile_info


class TelegramMarket:
    def __init__(self, app: Client, settings: Settings):
        self.app = app
        self.settings = settings

    async def sleep(self) -> None:
        await asyncio.sleep(random.uniform(self.settings.request_delay_min, self.settings.request_delay_max))

    async def raw_catalog(self) -> list[GiftType]:
        await self.sleep()
        result = await asyncio.wait_for(self.app.invoke(payments.GetStarGifts(hash=0)), timeout=45)
        gifts = getattr(result, "gifts", []) or []
        catalog = []
        for gift in gifts:
            gift_id = int(getattr(gift, "id"))
            title = self._raw_title(gift) or f"Gift {gift_id}"
            catalog.append(
                GiftType(
                    gift_id=gift_id,
                    title=title,
                    resale_available=getattr(gift, "availability_resale", None),
                )
            )
        return catalog

    async def named_catalog(self, progress=None) -> list[GiftType]:
        raw = self._prefer_resale_gifts(await self.raw_catalog())
        named: list[GiftType] = []
        for index, gift in enumerate(raw, start=1):
            title = gift.title
            if self._looks_like_id(title):
                found = await self.first_resale_title(gift)
                if found:
                    title = found
            if progress:
                progress(f"Каталог NFT: {index}/{len(raw)}")
            if not self._looks_like_id(title):
                named.append(GiftType(gift_id=gift.gift_id, title=title, resale_available=gift.resale_available))
        return sorted(unique_gifts(named), key=lambda item: item.title.casefold())

    async def first_resale_title(self, gift: GiftType) -> str | None:
        try:
            await self.sleep()
            result = await asyncio.wait_for(
                self.app.invoke(
                    payments.GetResaleStarGifts(
                        gift_id=gift.gift_id,
                        offset="",
                        limit=1,
                        sort_by_price=False,
                    )
                ),
                timeout=20,
            )
            raw_gifts = getattr(result, "gifts", []) or []
            if not raw_gifts:
                return None
            unique = getattr(raw_gifts[0], "gift", raw_gifts[0])
            title = self._raw_title(unique)
            return None if self._looks_like_id(title or "") else title
        except Exception:
            return None

    async def find_market_sellers(
        self,
        gift: GiftType,
        result_limit: int,
        owner_range: tuple[int, int],
        premium_filter: str = "any",
        level_filter: int | None = None,
        progress=None,
        cancel_event: asyncio.Event | None = None,
    ) -> list[MarketListing]:
        found: list[MarketListing] = []
        checked = 0
        seen_usernames: set[str] = set()
        offset = ""
        max_checks = min(
            self.settings.max_checks_per_scan,
            max(self.settings.min_checks_per_scan, result_limit * self.settings.checks_per_requested_user),
        )
        while len(found) < result_limit and checked < max_checks:
            if cancel_event and cancel_event.is_set():
                if progress:
                    progress(f"Остановлено вручную: проверено {checked}, найдено {len(found)}")
                break
            await self.sleep()
            limit = min(100, max_checks - checked)
            result = await asyncio.wait_for(
                self.app.invoke(
                    payments.GetResaleStarGifts(
                        gift_id=gift.gift_id,
                        offset=offset,
                        limit=limit,
                        sort_by_price=False,
                    )
                ),
                timeout=45,
            )
            raw_gifts = getattr(result, "gifts", []) or []
            if not raw_gifts:
                break
            owners = self._owners(result)
            for raw_gift in raw_gifts:
                if cancel_event and cancel_event.is_set():
                    break
                checked += 1
                listing = self._listing_from_raw(gift, raw_gift, owners)
                if not listing.username:
                    continue
                if listing.username.casefold() in seen_usernames:
                    continue
                count, count_known, profile_premium, stars_level = await self.public_profile_info(listing.username)
                effective_count = count if count_known else max(count or 0, 1)
                owner_premium = listing.owner_premium if listing.owner_premium is not None else profile_premium
                listing = MarketListing(
                    gift_id=listing.gift_id,
                    title=listing.title,
                    number=listing.number,
                    username=listing.username,
                    owner_gifts_count=effective_count,
                    owner_premium=owner_premium,
                    owner_stars_level=stars_level,
                    price_amount=listing.price_amount,
                    price_nanos=listing.price_nanos,
                    price_currency=listing.price_currency,
                    slug=listing.slug,
                    gift_address=listing.gift_address,
                )
                if not count_matches(effective_count, owner_range):
                    if progress and checked % 5 == 0:
                        progress(f"Проверено {checked}, подошло {len(found)}")
                    continue
                if not premium_matches(owner_premium, premium_filter):
                    if progress and checked % 5 == 0:
                        progress(f"Проверено {checked}, подошло {len(found)}")
                    continue
                if not level_matches(stars_level, level_filter):
                    if progress and checked % 5 == 0:
                        progress(f"Проверено {checked}, подошло {len(found)}")
                    continue
                found.append(listing)
                seen_usernames.add(listing.username.casefold())
                if progress:
                    progress(f"Найдено {len(found)}/{result_limit}: {listing.username}")
                if len(found) >= result_limit:
                    break
            if cancel_event and cancel_event.is_set():
                break
            next_offset = getattr(result, "next_offset", None)
            if not next_offset:
                break
            offset = next_offset
        if progress:
            if checked >= max_checks and len(found) < result_limit:
                progress(f"Проверено {checked}. Больше профили не проверяю, выдаю найденное: {len(found)}")
            else:
                progress(f"Готово: проверено {checked}, найдено {len(found)}")
        return found

    async def public_profile_info(self, username: str) -> tuple[int | None, bool, bool | None, int | None]:
        cached = cached_profile_info(username)
        if cached is not None:
            return (
                cached["gifts_count"] if isinstance(cached["gifts_count"], int) else None,
                cached["gifts_count"] is not None,
                cached["is_premium"] if isinstance(cached["is_premium"], bool) else None,
                cached["stars_level"] if isinstance(cached["stars_level"], int) else None,
            )
        try:
            await self.sleep()
            peer = await self.app.resolve_peer(username)
            result = await asyncio.wait_for(
                self.app.invoke(
                    payments.GetSavedStarGifts(
                        peer=peer,
                        offset="",
                        limit=100,
                        exclude_unlimited=True,
                    )
                ),
                timeout=25,
            )
            full = await asyncio.wait_for(self.app.invoke(GetFullUser(id=peer)), timeout=25)
            full_user = getattr(full, "full_user", None)
            rating = getattr(full_user, "stars_rating", None)
            stars_level = getattr(rating, "level", None)
            raw_users = getattr(full, "users", []) or []
            raw_user = raw_users[0] if raw_users else None
            is_premium = bool(getattr(raw_user, "premium", False)) if raw_user is not None else None

            gifts = getattr(result, "gifts", []) or []
            local_count = sum(1 for item in gifts if is_collectible_saved_gift(item))
            total_count = getattr(result, "count", None)
            if total_count is None:
                save_profile_info(username, local_count, is_premium, stars_level)
                return local_count, True, is_premium, stars_level
            count = max(int(total_count), local_count)
            save_profile_info(username, count, is_premium, stars_level)
            return count, True, is_premium, stars_level
        except Exception:
            save_profile_info(username, None, None, None)
            return None, False, None, None

    @staticmethod
    def _prefer_resale_gifts(catalog: list[GiftType]) -> list[GiftType]:
        resale = [gift for gift in catalog if gift.resale_available]
        return resale or catalog

    @staticmethod
    def _raw_title(raw: Any) -> str | None:
        for attr in ("title", "name"):
            value = getattr(raw, attr, None)
            if value:
                return str(value)
        return None

    @staticmethod
    def _looks_like_id(title: str) -> bool:
        clean = title.strip().replace("Gift ", "")
        return clean.isdigit()

    @staticmethod
    def _listing_from_raw(
        gift: GiftType,
        raw_gift: Any,
        owners: dict[tuple[str, int], dict[str, object]],
    ) -> MarketListing:
        unique = getattr(raw_gift, "gift", raw_gift)
        owner = TelegramMarket._owner_info(getattr(unique, "owner_id", None), owners) or {}
        amount = getattr(unique, "resell_amount", None)
        title = TelegramMarket._raw_title(unique) or gift.title
        slug = getattr(unique, "slug", None)
        return MarketListing(
            gift_id=gift.gift_id,
            title=title,
            number=getattr(unique, "num", None),
            username=owner.get("username") if owner.get("username") else None,
            owner_gifts_count=None,
            owner_premium=owner.get("premium") if "premium" in owner else None,
            owner_stars_level=None,
            price_amount=getattr(amount, "amount", None),
            price_nanos=getattr(amount, "nanos", None),
            price_currency="TON" if getattr(unique, "resale_ton_only", False) else "Stars",
            slug=slug,
            gift_address=getattr(unique, "gift_address", None),
        )

    @staticmethod
    def _owners(result: Any) -> dict[tuple[str, int], dict[str, object]]:
        owners: dict[tuple[str, int], dict[str, object]] = {}
        for user in getattr(result, "users", []) or []:
            username = public_username(user)
            if username:
                owners[("user", int(getattr(user, "id")))] = {
                    "username": username,
                    "premium": bool(getattr(user, "premium", False)),
                }
        for chat in getattr(result, "chats", []) or []:
            username = public_username(chat)
            if username:
                info = {"username": username, "premium": None}
                owners[("chat", int(getattr(chat, "id")))] = info
                owners[("channel", int(getattr(chat, "id")))] = info
        return owners

    @staticmethod
    def _owner_info(owner_id: Any, owners: dict[tuple[str, int], dict[str, object]]) -> dict[str, object] | None:
        if owner_id is None:
            return None
        if hasattr(owner_id, "user_id"):
            return owners.get(("user", int(owner_id.user_id)))
        if hasattr(owner_id, "chat_id"):
            return owners.get(("chat", int(owner_id.chat_id)))
        if hasattr(owner_id, "channel_id"):
            return owners.get(("channel", int(owner_id.channel_id)))
        return None


def public_username(peer: Any) -> str | None:
    username = getattr(peer, "username", None)
    if username:
        return f"@{username}"
    usernames = getattr(peer, "usernames", None) or []
    for item in usernames:
        username = getattr(item, "username", None)
        if username and not getattr(item, "hidden", False):
            return f"@{username}"
    return None


def is_collectible_saved_gift(saved_gift: Any) -> bool:
    gift = getattr(saved_gift, "gift", None)
    if gift is None:
        return False
    if gift.__class__.__name__ == "StarGiftUnique":
        return True
    return bool(getattr(gift, "slug", None))


def count_matches(count: int | None, owner_range: tuple[int, int]) -> bool:
    if owner_range == (0, 0):
        return True
    if count is None:
        return False
    return owner_range[0] <= count <= owner_range[1]


def premium_matches(is_premium: bool | None, premium_filter: str) -> bool:
    if premium_filter == "any":
        return True
    if is_premium is None:
        return False
    if premium_filter == "yes":
        return is_premium
    if premium_filter == "no":
        return not is_premium
    return True


def level_matches(stars_level: int | None, level_filter: int | None) -> bool:
    if level_filter is None:
        return True
    if stars_level is None:
        return False
    return stars_level == level_filter


def unique_gifts(catalog: list[GiftType]) -> list[GiftType]:
    by_title: dict[str, GiftType] = {}
    for gift in catalog:
        by_title.setdefault(gift.title.casefold(), gift)
    return list(by_title.values())
