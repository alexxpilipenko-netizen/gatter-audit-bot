import logging
import os
import json
import io
import requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── КОНФИГУРАЦИЯ ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN")
YANDEX_FOLDER = os.environ.get("YANDEX_FOLDER", "Gatter Audit")

# Список разрешённых пользователей берётся из переменной окружения ALLOWED_USER_IDS.
# Формат: Telegram ID через запятую, например:
#   ALLOWED_USER_IDS = 244836501,1099924202,8351444988
# Чтобы добавить/убрать человека — поменяй эту переменную в Railway → Variables
# (бот перезапустится автоматически). Имя аудитора берётся из его Telegram-профиля.
def parse_allowed_ids(raw: str) -> set:
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning(f"ALLOWED_USER_IDS: пропущен нечисловой элемент {part!r}")
    return ids


ALLOWED_USER_IDS = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))

BRANDS = {
    "Ferrero": ["Nutella", "Ferrero Rocher", "Raffaello", "Kinder", "Tic Tac"],
    "Food Mix": ["Mondelez", "Yunus", "La Milk", "Bizon", "Kent Boringer", "MAY", "Orion"],
    "Non-Food": ["Lody", "Aerostar", "Energizer", "Wellnax", "Splat"],
}

CITIES = [
    "Ташкент", "Самарканд", "Андижан", "Бухара", "Фергана",
    "Ургенч", "Коканд", "Наманган", "Джиззак", "Карши", "Навои"
]

# Форматы торговых точек (тиры) с краткими описаниями
TT_FORMATS = {
    "AA+": "AA+ — Супермаркет L / Гипермаркет (Korzinka, Makro, Smart и аналоги). "
           "Самообслуживание, от 1000 м². Полная выкладка по всем категориям, "
           "оборудование на кассе, POS-материалы. Приоритет — 100% наличие на полках.",
    "AA": "AA — Супермаркет Medium. Самообслуживание, 600–1000 м². "
          "Полная выкладка по всем категориям, оборудование на кассе, POS-материалы. "
          "Приоритет — 100% наличие на полках.",
    "A": "A — Супермаркет Small / Минимаркет. Самообслуживание, 100–600 м². "
         "Фокус на ключевых SKU. Контроль ценников и сроков годности.",
    "B": "B — Food store / Павильон Big. Небольшой прилавочный магазин или "
         "базарный павильон, от 30 м². Поддержание MML по ключевым брендам, "
         "своевременный дозаказ.",
    "C": "C — Павильон Small / Ларёк. Киоск или малый прилавок, до 30 м². "
         "Ограниченный ассортимент, фокус на быстрооборачиваемых позициях. "
         "Строгий минимум MML.",
    "D": "D — Точка минимального присутствия. Малый киоск, уличная или удалённая "
         "точка. Очень ограниченная полочная ёмкость. Только базовый MML, "
         "регулярность посещения важнее расширения ассортимента.",
}

NO_BRANDS_LABEL = "❌ Нет наших брендов"

# ─── ШАГИ ДИАЛОГА ───────────────────────────────────────────────────────────
(PORTFOLIO, CITY, TT_NAME, LOCATION, TT_FORMAT, PHOTO,
 BRANDS_SELECT, SKU_INPUT, NOTES) = range(9)


# ─── GOOGLE HELPERS ─────────────────────────────────────────────────────────
def get_google_creds():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    return Credentials.from_service_account_info(creds_info, scopes=scopes)


SHEET_HEADER = [
    "ID визита", "Дата", "Аудитор", "Портфель", "Город",
    "Название ТТ", "Широта", "Долгота", "Формат ТТ",
    "Бренд", "SKU", "Заметки", "Ссылка на фото"
]


def save_to_sheet(data: dict):
    """Записывает аудит в 'длинном' формате: по одной строке на каждый бренд.
    Если брендов нет — одна строка с пометкой 'Нет наших брендов' и SKU=0."""
    creds = get_google_creds()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet("Аудит")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Аудит", rows=2000, cols=20)
        ws.append_row(SHEET_HEADER)

    common = [
        data.get("visit_id", ""),
        data.get("date", ""),
        data.get("auditor", ""),
        data.get("portfolio", ""),
        data.get("city", ""),
        data.get("tt_name", ""),
        data.get("lat", ""),
        data.get("lon", ""),
        data.get("tt_format", ""),
    ]

    brand_sku = data.get("brand_sku", [])
    notes = data.get("notes", "")
    photo_url = data.get("photo_url", "")

    rows = []
    if not brand_sku:
        rows.append(common + ["Нет наших брендов", "0", notes, photo_url])
    else:
        for brand, sku in brand_sku:
            rows.append(common + [brand, sku, notes, photo_url])

    ws.append_rows(rows, value_input_option="USER_ENTERED")


# ─── ЯНДЕКС.ДИСК ────────────────────────────────────────────────────────────
def upload_photo_to_yandex(file_bytes: bytes, filename: str) -> str:
    """Загружает фото на Яндекс.Диск, публикует его и возвращает публичную ссылку.
    При любой ошибке возвращает ''."""
    if not YANDEX_TOKEN:
        logger.error("YANDEX_TOKEN не задан")
        return ""

    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
    disk_path = f"{YANDEX_FOLDER}/{filename}"
    base = "https://cloud-api.yandex.net/v1/disk/resources"

    try:
        r = requests.get(
            f"{base}/upload",
            headers=headers,
            params={"path": disk_path, "overwrite": "true"},
            timeout=30,
        )
        r.raise_for_status()
        href = r.json()["href"]

        up = requests.put(href, data=file_bytes, timeout=60)
        up.raise_for_status()

        requests.put(
            f"{base}/publish",
            headers=headers,
            params={"path": disk_path},
            timeout=30,
        )

        meta = requests.get(
            base,
            headers=headers,
            params={"path": disk_path, "fields": "public_url"},
            timeout=30,
        )
        meta.raise_for_status()
        public_url = meta.json().get("public_url", "")
        return public_url

    except Exception as e:
        logger.error(f"Ошибка загрузки на Яндекс.Диск: {e}")
        return ""


# ─── АВТОРИЗАЦИЯ ────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    logger.info(f"START от user_id={user_id}; "
                f"разрешённые={sorted(ALLOWED_USER_IDS)}; "
                f"пускаем={user_id in ALLOWED_USER_IDS}")

    if not is_authorized(user_id):
        await update.message.reply_text(
            f"⛔ У вас нет доступа к этому боту.\n"
            f"Ваш Telegram ID: `{user_id}`\n"
            f"Передайте его администратору для получения доступа.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Имя аудитора берём из Telegram-профиля
    name = user.full_name or "Аудитор"
    context.user_data.clear()
    context.user_data["auditor"] = name
    context.user_data["photos"] = []

    keyboard = [[p] for p in BRANDS.keys()]
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\nВыбери портфель для аудита:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return PORTFOLIO


async def portfolio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in BRANDS:
        await update.message.reply_text("Выбери портфель из списка.")
        return PORTFOLIO
    context.user_data["portfolio"] = text

    keyboard = [[c] for c in CITIES]
    await update.message.reply_text(
        "🏙 Выбери город:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CITY


async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in CITIES:
        await update.message.reply_text("Выбери город из списка.")
        return CITY
    context.user_data["city"] = text

    await update.message.reply_text(
        "🏪 Введи название торговой точки:",
        reply_markup=ReplyKeyboardRemove()
    )
    return TT_NAME


async def tt_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tt_name"] = update.message.text

    keyboard = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
    await update.message.reply_text(
        "📍 Отправь геолокацию торговой точки:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return LOCATION


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    context.user_data["lat"] = loc.latitude
    context.user_data["lon"] = loc.longitude

    descriptions = "\n\n".join(TT_FORMATS.values())
    keyboard = [["AA+", "AA", "A"], ["B", "C", "D"]]
    await update.message.reply_text(
        f"🏷 *Выбери формат (тир) торговой точки:*\n\n{descriptions}",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return TT_FORMAT


async def tt_format_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in TT_FORMATS:
        await update.message.reply_text("Выбери формат из кнопок: AA+, AA, A, B, C или D.")
        return TT_FORMAT
    context.user_data["tt_format"] = text

    await update.message.reply_text(
        "📸 Отправь фото торговой точки (можно несколько).\n"
        "Когда закончишь — напиши *готово*.",
        reply_markup=ReplyKeyboardMarkup([["готово"]], resize_keyboard=True),
        parse_mode="Markdown"
    )
    return PHOTO


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() == "готово":
        return await photo_done(update, context)

    if update.message.photo:
        context.user_data["photos"].append(update.message.photo[-1].file_id)
        count = len(context.user_data["photos"])
        await update.message.reply_text(f"✅ Фото {count} принято. Отправь ещё или напиши *готово*.",
                                        parse_mode="Markdown")
    return PHOTO


async def photo_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = context.user_data["portfolio"]
    brand_list = BRANDS[portfolio]
    context.user_data["brand_sku"] = []
    context.user_data["selected_brands"] = []

    keyboard = [[b] for b in brand_list] + [[NO_BRANDS_LABEL], ["✅ Выбор завершён"]]
    await update.message.reply_text(
        f"🏷 Какие бренды портфеля *{portfolio}* присутствуют в точке?\n\n"
        "Нажми на бренд → впиши количество SKU этого бренда → выбери следующий.\n"
        f"Если наших брендов в точке нет — нажми *«{NO_BRANDS_LABEL}»*.\n"
        "Когда закончишь — нажми *«Выбор завершён»*.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return BRANDS_SELECT


async def brands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    portfolio = context.user_data["portfolio"]
    brand_list = BRANDS[portfolio]

    if text == NO_BRANDS_LABEL:
        context.user_data["brand_sku"] = []
        return await ask_notes(update, context)

    if text == "✅ Выбор завершён":
        if not context.user_data["brand_sku"]:
            await update.message.reply_text(
                f"Выбери хотя бы один бренд и впиши SKU, "
                f"либо нажми *«{NO_BRANDS_LABEL}»*.",
                parse_mode="Markdown"
            )
            return BRANDS_SELECT
        return await ask_notes(update, context)

    if text in brand_list:
        if text in context.user_data["selected_brands"]:
            await update.message.reply_text(f"⚠️ {text} уже добавлен. Выбери другой бренд.")
            return BRANDS_SELECT
        context.user_data["current_brand"] = text
        await update.message.reply_text(
            f"🔢 Сколько SKU бренда *{text}* в точке? Впиши число:",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return SKU_INPUT

    await update.message.reply_text("Выбери бренд из кнопок или нажми «Выбор завершён».")
    return BRANDS_SELECT


async def sku_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sku = update.message.text.strip()
    brand = context.user_data.get("current_brand")

    context.user_data["brand_sku"].append((brand, sku))
    context.user_data["selected_brands"].append(brand)

    portfolio = context.user_data["portfolio"]
    brand_list = BRANDS[portfolio]
    remaining = [b for b in brand_list if b not in context.user_data["selected_brands"]]
    keyboard = [[b] for b in remaining] + [["✅ Выбор завершён"]]

    await update.message.reply_text(
        f"✅ {brand}: {sku} SKU записано.\n"
        "Выбери следующий бренд или нажми *«Выбор завершён»*.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return BRANDS_SELECT


async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Добавь заметки по точке (любые наблюдения, проблемы, возможности).\n"
        "Если заметок нет — напиши *нет*.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return NOTES


async def notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = update.message.text
    context.user_data["notes"] = "" if notes.lower() == "нет" else notes

    await update.message.reply_text("⏳ Сохраняю данные и загружаю фото...")

    visit_id = f"{update.effective_user.id}_{int(datetime.now().timestamp())}"
    context.user_data["visit_id"] = visit_id
    context.user_data["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")

    photo_urls = []
    for i, file_id in enumerate(context.user_data.get("photos", []), 1):
        try:
            tg_file = await context.bot.get_file(file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            filename = f"{visit_id}_{i}.jpg"
            link = upload_photo_to_yandex(file_bytes, filename)
            if link:
                photo_urls.append(link)
        except Exception as e:
            logger.error(f"Ошибка обработки фото: {e}")

    context.user_data["photo_url"] = ", ".join(photo_urls)

    try:
        save_to_sheet(context.user_data)
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")
        await update.message.reply_text("❌ Ошибка сохранения. Обратитесь к администратору.")
        return ConversationHandler.END

    brand_sku = context.user_data.get("brand_sku", [])
    if brand_sku:
        brands_summary = "\n".join(f"   • {b}: {s} SKU" for b, s in brand_sku)
    else:
        brands_summary = "   • Нет наших брендов"

    d = context.user_data
    summary = (
        f"✅ *Аудит сохранён!*\n\n"
        f"🗂 Портфель: {d.get('portfolio')}\n"
        f"🏙 Город: {d.get('city')}\n"
        f"🏪 ТТ: {d.get('tt_name')}\n"
        f"🏷 Формат: {d.get('tt_format')}\n"
        f"🛍 Бренды и SKU:\n{brands_summary}\n"
        f"📝 Заметки: {d.get('notes') or '—'}\n"
        f"📸 Фото: {len(photo_urls)} шт.\n\n"
        f"Для нового аудита — /start"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Аудит отменён. Для начала нового — /start",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш Telegram ID: `{user.id}`\n"
        f"Username: @{user.username}\n"
        f"Имя: {user.full_name}",
        parse_mode="Markdown"
    )


# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PORTFOLIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, portfolio_handler)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_handler)],
            TT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tt_name_handler)],
            LOCATION: [MessageHandler(filters.LOCATION, location_handler)],
            TT_FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, tt_format_handler)],
            PHOTO: [
                MessageHandler(filters.PHOTO, photo_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_handler),
            ],
            BRANDS_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, brands_handler)],
            SKU_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sku_input_handler)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, notes_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.run_polling()


if __name__ == "__main__":
    main()
