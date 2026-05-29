from __future__ import annotations

import asyncio
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from models import GiftType, MarketListing
from storage import init_storage, load_catalog_cache, save_catalog_cache, save_listings
from telegram_market import TelegramMarket


STATE: dict[int, dict[str, object]] = {}
SCAN_CANCEL: dict[int, asyncio.Event] = {}
CATALOG: list[GiftType] = []
CATALOG_PAGE_SIZE = 8


def require_settings() -> None:
    missing = []
    if not settings.telegram_api_id:
        missing.append("TELEGRAM_API_ID")
    if not settings.telegram_api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if missing:
        raise RuntimeError(f"Missing settings: {', '.join(missing)}")


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Парсинг профилей", callback_data="menu:profiles")],
        ]
    )


def profiles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Найти на маркете", callback_data="flow:market")],
            [InlineKeyboardButton("Обновить каталог NFT", callback_data="catalog:refresh")],
        ]
    )


def catalog_keyboard(page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(CATALOG) + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CATALOG_PAGE_SIZE
    gifts = CATALOG[start:start + CATALOG_PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(gift.title[:55], callback_data=f"gift:{start + index}")]
        for index, gift in enumerate(gifts)
    ]
    rows.append(
        [
            InlineKeyboardButton("Назад", callback_data=f"catalog:page:{max(0, page - 1)}"),
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton("Вперед", callback_data=f"catalog:page:{min(total_pages - 1, page + 1)}"),
        ]
    )
    rows.append([InlineKeyboardButton("Ввести название вручную", callback_data="gift:manual")])
    rows.append([InlineKeyboardButton("В меню", callback_data="menu:profiles")])
    return InlineKeyboardMarkup(rows)


def limit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("5", callback_data="limit:5"),
                InlineKeyboardButton("10", callback_data="limit:10"),
                InlineKeyboardButton("25", callback_data="limit:25"),
            ],
            [
                InlineKeyboardButton("50", callback_data="limit:50"),
                InlineKeyboardButton("100", callback_data="limit:100"),
                InlineKeyboardButton("150", callback_data="limit:150"),
            ],
        ]
    )


def owner_range_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любое", callback_data="owners:any"),
                InlineKeyboardButton("1", callback_data="owners:1-1"),
                InlineKeyboardButton("1-2", callback_data="owners:1-2"),
            ],
            [
                InlineKeyboardButton("1-5", callback_data="owners:1-5"),
                InlineKeyboardButton("3-10", callback_data="owners:3-10"),
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="owners:manual")],
        ]
    )


def extra_filters_keyboard(state: dict[str, object]) -> InlineKeyboardMarkup:
    premium = str(state.get("premium_filter", "any"))
    level = state.get("level_filter")
    premium_label = {"any": "Premium: любой", "yes": "Premium: есть", "no": "Premium: нет"}[premium]
    level_label = "Уровень: любой" if level is None else f"Уровень: {level}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(premium_label, callback_data="filter:premium"),
                InlineKeyboardButton(level_label, callback_data="filter:level"),
            ],
            [InlineKeyboardButton("Запустить парсинг", callback_data="run:market")],
        ]
    )


def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любой", callback_data="premium:any"),
                InlineKeyboardButton("Есть", callback_data="premium:yes"),
                InlineKeyboardButton("Нет", callback_data="premium:no"),
            ],
        ]
    )


def level_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любой", callback_data="level:any"),
                InlineKeyboardButton("1", callback_data="level:1"),
                InlineKeyboardButton("2", callback_data="level:2"),
            ],
        ]
    )


def stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Стоп", callback_data="scan:stop")]])


async def load_catalog(refresh: bool = False, progress=None) -> list[GiftType]:
    global CATALOG
    if CATALOG and not refresh:
        return CATALOG
    cached = load_catalog_cache()
    if cached and not refresh:
        CATALOG = cached
        return CATALOG
    async with user_client() as user_app:
        market = TelegramMarket(user_app, settings)
        CATALOG = await market.named_catalog(progress=progress)
    save_catalog_cache(CATALOG)
    return CATALOG


def parse_owner_range(text: str) -> tuple[int, int]:
    clean = text.strip().casefold().replace(" ", "")
    if clean in {"", "любой", "любое", "any", "all", "0"}:
        return 0, 0
    if "-" in clean:
        left, right = clean.split("-", 1)
        return int(left), int(right)
    value = int(clean)
    return value, value


def selected_gift(chat_id: int) -> GiftType:
    state = STATE[chat_id]
    gift = state.get("gift")
    if isinstance(gift, GiftType):
        return gift
    title = str(state.get("manual_gift", "")).strip()
    if not title:
        raise RuntimeError("NFT не выбран")
    return GiftType(gift_id=int(state["manual_gift_id"]), title=title, resale_available=None)


async def resolve_manual_gift(title: str) -> GiftType | None:
    catalog = await load_catalog(refresh=False)
    wanted = title.casefold()
    for gift in catalog:
        if wanted in gift.title.casefold():
            return gift
    return None


async def run_market_scan(bot: Client, message: Message, chat_id: int) -> None:
    state = STATE[chat_id]
    gift = selected_gift(chat_id)
    limit = int(state["limit"])
    owner_range = state["owner_range"]
    premium_filter = str(state.get("premium_filter", "any"))
    level_filter = state.get("level_filter")

    status = await message.reply(
        f"Запускаю свежий поиск по маркету.\n"
        f"NFT: {gift.title}\n"
        f"Нужно людей: {limit}\n"
        f"NFT в профиле: {range_label(owner_range)}\n"
        f"Premium: {premium_label(premium_filter)}\n"
        f"Уровень: {level_label(level_filter)}",
        reply_markup=stop_keyboard(),
    )
    progress_lines: list[str] = []
    done = asyncio.Event()
    cancel_event = asyncio.Event()
    SCAN_CANCEL[chat_id] = cancel_event

    async def updater() -> None:
        while not done.is_set():
            latest = "\n".join(progress_lines[-6:])
            try:
                await status.edit_text(
                    f"Идет парсинг маркета...\n\n{latest[:2500]}",
                    disable_web_page_preview=True,
                    reply_markup=stop_keyboard(),
                )
            except Exception:
                pass
            await asyncio.sleep(3)

    def progress(line: str) -> None:
        progress_lines.append(line)

    task = asyncio.create_task(updater())
    try:
        if cancel_event.is_set():
            listings = []
        else:
            progress("Подключаюсь к Telegram...")
            async with user_client() as user_app:
                market = TelegramMarket(user_app, settings)
                listings = await market.find_market_sellers(
                    gift,
                    limit,
                    owner_range,
                    premium_filter=premium_filter,
                    level_filter=level_filter if isinstance(level_filter, int) else None,
                    progress=progress,
                    cancel_event=cancel_event,
                )
    except Exception as exc:
        done.set()
        task.cancel()
        await status.edit_text(f"Ошибка парсинга: {exc}")
        return
    finally:
        done.set()
        SCAN_CANCEL.pop(chat_id, None)
        task.cancel()

    save_listings(listings)
    text = results_text(listings, gift.title)
    await status.edit_text(text, disable_web_page_preview=True)


def results_text(listings: list[MarketListing], gift_title: str) -> str:
    if not listings:
        return (
            f"По {gift_title} никого не нашёл под эти фильтры.\n\n"
            "Это значит: либо мало листингов, либо владельцы без публичного username, либо фильтр по NFT в профиле слишком жёсткий."
        )
    lines = [f"Найдено {len(listings)} по {gift_title}:", ""]
    for index, item in enumerate(listings[:30], start=1):
        username = item.username or ""
        clean = username.removeprefix("@")
        count = "?" if item.owner_gifts_count is None else str(item.owner_gifts_count)
        number = f" #{item.number}" if item.number is not None else ""
        price = f" | {item.price_amount} {item.price_currency}" if item.price_amount else ""
        premium = "Premium: ?" if item.owner_premium is None else f"Premium: {'да' if item.owner_premium else 'нет'}"
        level = "Уровень: ?" if item.owner_stars_level is None else f"Уровень: {item.owner_stars_level}"
        lines.append(f"{index}. {username} | [Написать](https://t.me/{clean})")
        lines.append(f"   {item.title}{number} | NFT: {count} | {premium} | {level}{price}")
    if len(listings) > 30:
        lines.append("")
        lines.append(f"Ещё {len(listings) - 30} сохранено в базе.")
    return "\n".join(lines)


def range_label(owner_range: tuple[int, int]) -> str:
    if owner_range == (0, 0):
        return "любое"
    if owner_range[0] == owner_range[1]:
        return str(owner_range[0])
    return f"{owner_range[0]}-{owner_range[1]}"


def premium_label(value: str) -> str:
    return {"any": "любой", "yes": "есть", "no": "нет"}.get(value, value)


def level_label(value: object) -> str:
    return "любой" if value is None else str(value)


@asynccontextmanager
async def user_client():
    session_name = prepare_runtime_session()
    client = Client(
        session_name,
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
    )
    try:
        async with client:
            yield client
    finally:
        cleanup_runtime_session(session_name)


def prepare_runtime_session() -> str:
    source = Path(f"{settings.telegram_user_session}.session")
    if not source.exists():
        raise RuntimeError(f"Session file not found: {source}")
    runtime_dir = settings.output_dir / "runtime_sessions"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    session_name = runtime_dir / f"{settings.telegram_user_session}_{uuid.uuid4().hex}"
    shutil.copy2(source, session_name.with_suffix(".session"))
    return str(session_name)


def cleanup_runtime_session(session_name: str) -> None:
    base = Path(session_name)
    for path in (base.with_suffix(".session"), base.with_suffix(".session-journal")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def main() -> None:
    require_settings()
    init_storage()
    app = Client(
        "kasper_clean_bot",
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        bot_token=settings.telegram_bot_token,
        in_memory=True,
    )

    @app.on_message(filters.command("start"))
    async def start(_: Client, message: Message) -> None:
        STATE.pop(message.chat.id, None)
        await message.reply(
            "<b>Каспер Парсер</b>\n\nВыбери действие.",
            reply_markup=main_keyboard(),
        )

    @app.on_callback_query()
    async def callbacks(bot: Client, callback: CallbackQuery) -> None:
        data = callback.data or ""
        chat_id = callback.message.chat.id
        if data == "noop":
            await callback.answer()
            return
        if data == "scan:stop":
            cancel_event = SCAN_CANCEL.get(chat_id)
            if cancel_event:
                cancel_event.set()
                await callback.answer("Останавливаю на найденном")
            else:
                await callback.answer("Активного парсинга нет")
            return
        if data == "menu:profiles":
            await callback.message.edit_text("Парсинг профилей:", reply_markup=profiles_keyboard())
            await callback.answer()
            return
        if data == "flow:market":
            STATE[chat_id] = {"mode": "market"}
            await callback.answer("Загружаю NFT...")
            await show_catalog(callback, refresh=False)
            return
        if data == "catalog:refresh":
            await callback.answer("Обновляю каталог, это может занять время...")
            await show_catalog(callback, refresh=True)
            return
        if data.startswith("catalog:page:"):
            page = int(data.rsplit(":", 1)[1])
            await callback.message.edit_text("Выбери NFT:", reply_markup=catalog_keyboard(page))
            await callback.answer()
            return
        if data == "gift:manual":
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["step"] = "manual_gift"
            await callback.message.reply("Напиши название NFT. Например: Snoop Dogg")
            await callback.answer()
            return
        if data.startswith("gift:"):
            index = int(data.split(":", 1)[1])
            if index >= len(CATALOG):
                await callback.answer("NFT не найден", show_alert=True)
                return
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["gift"] = CATALOG[index]
            await callback.message.edit_text(
                f"NFT: {CATALOG[index].title}\n\nСколько людей найти?",
                reply_markup=limit_keyboard(),
            )
            await callback.answer()
            return
        if data.startswith("limit:"):
            limit = max(5, min(int(data.split(":", 1)[1]), 150))
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["limit"] = limit
            await callback.message.edit_text(
                f"Людей найти: {limit}\n\nСколько NFT/gifts должно быть в профиле?",
                reply_markup=owner_range_keyboard(),
            )
            await callback.answer()
            return
        if data == "owners:manual":
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["step"] = "owner_range"
            await callback.message.reply("Напиши диапазон. Например: 1-2, 1-5, 3-10 или любое")
            await callback.answer()
            return
        if data.startswith("owners:"):
            raw = data.split(":", 1)[1]
            owner_range = (0, 0) if raw == "any" else parse_owner_range(raw)
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["owner_range"] = owner_range
            STATE[chat_id].setdefault("premium_filter", "any")
            STATE[chat_id].setdefault("level_filter", None)
            await callback.message.edit_text(
                scan_summary(chat_id),
                reply_markup=extra_filters_keyboard(STATE[chat_id]),
            )
            await callback.answer()
            return
        if data == "filter:premium":
            await callback.message.edit_text("Выбери Premium-фильтр:", reply_markup=premium_keyboard())
            await callback.answer()
            return
        if data == "filter:level":
            await callback.message.edit_text("Выбери уровень Telegram:", reply_markup=level_keyboard())
            await callback.answer()
            return
        if data.startswith("premium:"):
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["premium_filter"] = data.split(":", 1)[1]
            await callback.message.edit_text(
                scan_summary(chat_id),
                reply_markup=extra_filters_keyboard(STATE[chat_id]),
            )
            await callback.answer()
            return
        if data.startswith("level:"):
            raw = data.split(":", 1)[1]
            STATE.setdefault(chat_id, {"mode": "market"})
            STATE[chat_id]["level_filter"] = None if raw == "any" else int(raw)
            await callback.message.edit_text(
                scan_summary(chat_id),
                reply_markup=extra_filters_keyboard(STATE[chat_id]),
            )
            await callback.answer()
            return
        if data == "run:market":
            await callback.answer("Запускаю")
            await run_market_scan(bot, callback.message, chat_id)
            return

    @app.on_message(filters.text & ~filters.command("start"))
    async def text_input(bot: Client, message: Message) -> None:
        state = STATE.get(message.chat.id)
        if not state:
            return
        step = state.get("step")
        if step == "manual_gift":
            gift = await resolve_manual_gift(message.text or "")
            if not gift:
                await message.reply("Не нашёл такое NFT в каталоге. Нажми «Обновить каталог NFT» или напиши точнее.")
                return
            state["gift"] = gift
            state.pop("step", None)
            await message.reply(f"NFT: {gift.title}\n\nСколько людей найти?", reply_markup=limit_keyboard())
            return
        if step == "owner_range":
            try:
                owner_range = parse_owner_range(message.text or "")
            except ValueError:
                await message.reply("Не понял диапазон. Пример: 1-2, 1-5, 3-10 или любое")
                return
            state["owner_range"] = owner_range
            state.setdefault("premium_filter", "any")
            state.setdefault("level_filter", None)
            state.pop("step", None)
            await message.reply(
                scan_summary(message.chat.id),
                reply_markup=extra_filters_keyboard(state),
            )

    async def show_catalog(callback: CallbackQuery, refresh: bool) -> None:
        progress_message = await callback.message.reply("Загружаю каталог NFT...")

        def progress(line: str) -> None:
            pass

        try:
            await load_catalog(refresh=refresh, progress=progress)
        except Exception as exc:
            await progress_message.edit_text(f"Не смог загрузить каталог: {exc}")
            return
        await progress_message.edit_text("Выбери NFT:", reply_markup=catalog_keyboard(0))

    def scan_summary(chat_id: int) -> str:
        state = STATE[chat_id]
        gift = selected_gift(chat_id)
        return (
            "Параметры поиска:\n\n"
            f"NFT: {gift.title}\n"
            f"Людей найти: {state.get('limit')}\n"
            f"NFT/gifts в профиле: {range_label(state.get('owner_range', (0, 0)))}\n"
            f"Premium: {premium_label(str(state.get('premium_filter', 'any')))}\n"
            f"Уровень: {level_label(state.get('level_filter'))}\n\n"
            "Режим: свежий поиск по Telegram Market"
        )

    print("Clean Kasper bot is running. Press Ctrl+C to stop.")
    await app.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

