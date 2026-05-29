from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from models import GiftType, MarketListing


DB_PATH = Path("output/kasper_clean.db")
CATALOG_PATH = Path("output/gift_catalog.json")


def init_storage() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                gift_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                number INTEGER,
                username TEXT,
                owner_gifts_count INTEGER,
                owner_premium INTEGER,
                owner_stars_level INTEGER,
                price_amount INTEGER,
                price_nanos INTEGER,
                price_currency TEXT,
                slug TEXT,
                gift_address TEXT,
                nft_url TEXT,
                seen_at INTEGER NOT NULL
            )
            """
        )
        ensure_column(connection, "listings", "owner_stars_level", "INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_cache (
                username TEXT PRIMARY KEY,
                gifts_count INTEGER,
                is_premium INTEGER,
                stars_level INTEGER,
                checked_at INTEGER NOT NULL
            )
            """
        )
        ensure_column(connection, "profile_cache", "is_premium", "INTEGER")
        ensure_column(connection, "profile_cache", "stars_level", "INTEGER")


def save_listings(listings: list[MarketListing]) -> None:
    if not listings:
        return
    init_storage()
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as connection:
        for item in listings:
            row_id = item.gift_address or item.nft_url or f"{item.gift_id}:{item.number}:{item.username}"
            connection.execute(
                """
                INSERT INTO listings (
                    id, gift_id, title, number, username, owner_gifts_count,
                    owner_premium, owner_stars_level, price_amount, price_nanos, price_currency,
                    slug, gift_address, nft_url, seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = excluded.username,
                    owner_gifts_count = excluded.owner_gifts_count,
                    owner_premium = excluded.owner_premium,
                    owner_stars_level = excluded.owner_stars_level,
                    price_amount = excluded.price_amount,
                    price_nanos = excluded.price_nanos,
                    price_currency = excluded.price_currency,
                    seen_at = excluded.seen_at
                """,
                (
                    row_id,
                    item.gift_id,
                    item.title,
                    item.number,
                    item.username,
                    item.owner_gifts_count,
                    none_bool(item.owner_premium),
                    item.owner_stars_level,
                    item.price_amount,
                    item.price_nanos,
                    item.price_currency,
                    item.slug,
                    item.gift_address,
                    item.nft_url,
                    now,
                ),
            )


def load_candidate_usernames(limit: int = 1000) -> list[str]:
    init_storage()
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT username
            FROM listings
            WHERE username IS NOT NULL AND username != ''
            GROUP BY username
            ORDER BY max(seen_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def cached_profile_info(username: str, ttl_seconds: int = 3600) -> dict[str, int | bool | None] | None:
    init_storage()
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT gifts_count, is_premium, stars_level, checked_at
            FROM profile_cache
            WHERE username = ?
            """,
            (username.casefold(),),
        ).fetchone()
    if not row:
        return None
    gifts_count, is_premium, stars_level, checked_at = row
    if now - int(checked_at) > ttl_seconds:
        return None
    return {
        "gifts_count": None if gifts_count is None else int(gifts_count),
        "is_premium": None if is_premium is None else bool(is_premium),
        "stars_level": None if stars_level is None else int(stars_level),
    }


def save_profile_info(
    username: str,
    gifts_count: int | None,
    is_premium: bool | None,
    stars_level: int | None,
) -> None:
    init_storage()
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO profile_cache (username, gifts_count, is_premium, stars_level, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                gifts_count = excluded.gifts_count,
                is_premium = excluded.is_premium,
                stars_level = excluded.stars_level,
                checked_at = excluded.checked_at
            """,
            (username.casefold(), gifts_count, none_bool(is_premium), stars_level, int(time.time())),
        )


def load_catalog_cache() -> list[GiftType]:
    if not CATALOG_PATH.exists():
        return []
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    result = []
    for row in rows:
        try:
            result.append(
                GiftType(
                    gift_id=int(row["gift_id"]),
                    title=str(row["title"]),
                    resale_available=row.get("resale_available"),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return result


def save_catalog_cache(catalog: list[GiftType]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "gift_id": item.gift_id,
            "title": item.title,
            "resale_available": item.resale_available,
        }
        for item in catalog
    ]
    CATALOG_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def none_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def ensure_column(connection: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
