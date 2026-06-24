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


# Список разрешённых пользователей — из переменной окружения ALLOWED_USER_IDS.
#   ALLOWED_USER_IDS = 244836501,1099924202,8351444988
# Добавить/убрать человека — поменяй переменную в Railway → Variables.
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

TT_FORMATS = {
    "AA+": "AA+ — Супермаркет L / Гипермаркет (Korzinka, Makro, Smart и аналоги). "
           "Самообслуживание, от 1000 м².",
    "AA": "AA — Супермаркет Medium. Самообслуживание, 600–1000 м².",
    "A": "A — Супермаркет Small / Минимаркет. Самообслуживание, 100–600 м².",
    "B": "B — Food store / Павильон Big. Небольшой прилавочный магазин или "
         "базарный павильон, от 30 м².",
    "C": "C — Павильон Small / Ларёк. Киоск или малый прилавок, до 30 м².",
    "D": "D — Точка минимального присутствия. Малый киоск, уличная или удалённая "
         "точка. Очень ограниченная полочная ёмкость.",
}

# Фокусные вопросы по портфелям.
# Каждый вопрос: (заголовок_столбца, текст_вопроса, тип_ответа)
# тип: "yesno" — кнопки Да/Нет; "number" — ввод числа.
FOCUS_QUESTIONS = {
    "Ferrero": [
        ("Об. Tic Tac касса", "Наличие оборудования Tic Tac на кассе", "yesno"),
        ("Об. Kinder касса", "Наличие оборудования Kinder на кассе", "yesno"),
        ("Ценники", "Наличие ценников", "yesno"),
        ("POSm", "Наличие POSm (шелф-токеры, воблеры, ленты и т.д.)", "yesno"),
        ("Семифреди", "Наличие Семифреди в ТТ", "yesno"),
        ("Плиточный шоколад", "Наличие плиточного шоколада в категории", "yesno"),
    ],
    "Non-Food": [
        ("Splat Биокальций", "Наличие Splat Биокальций", "yesno"),
        ("Splat Лечебные травы", "Наличие Splat Лечебные травы", "yesno"),
        ("Splat Отбеливание+", "Наличие Splat Отбеливание плюс", "yesno"),
        ("Освежитель 300мл", "Наличие освежителя воздуха 300 мл", "yesno"),
    ],
    "Food Mix": [
        ("SKU предкасса", "Введите количество SKU на предкассовом узле", "number"),
    ],
}

# Все фокусные столбцы в фиксированном порядке (для шапки и записи)
FOCUS_COLUMNS = []
for _portf in ["Ferrero", "Non-Food", "Food Mix"]:
    for _col, _q, _t in FOCUS_QUESTIONS[_portf]:
        FOCUS_COLUMNS.append(_col)

NO_BRANDS_LABEL = "❌ Нет наших брендов"
TYPE_PRIMARY = "Первичный аудит"
TYPE_REPEAT = "Повторный аудит"

# ─── ШАГИ ДИАЛОГА ───────────────────────────────────────────────────────────
(AUDIT_TYPE, PORTFOLIO, CITY, REPEAT_CITY, REPEAT_PICK, TT_NAME, LOCATION,
 TT_FORMAT, PHOTO, BRANDS_SELECT, SKU_INPUT, FOCUS, NOTES) = range(13)


# ─── GOOGLE HELPERS ─────────────────────────────────────────────────────────
def get_google_creds():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(creds_info, scopes=scopes)


BASE_HEADER = [
    "ID визита", "Дата", "Аудитор", "Портфель", "Город",
    "Название ТТ", "Широта", "Долгота", "Формат ТТ",
    "Бренд", "SKU", "Заметки", "Ссылка на фото",
    "Тип аудита", "Связан с визитом"
]
SHEET_HEADER = BASE_HEADER + FOCUS_COLUMNS
COL = {name: i for i, name in enumerate(SHEET_HEADER)}


def _get_worksheet():
    creds = get_google_creds()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Аудит")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Аудит", rows=2000, cols=max(30, len(SHEET_HEADER) + 2))
        ws.append_row(SHEET_HEADER)
    return ws


def get_primary_without_repeat(city: str):
    """Первичные аудиты по городу, у которых ещё нет повторного.
    Возвращает список (visit_id, tt_name, date, auditor)."""
    ws = _get_worksheet()
    all_rows = ws.get_all_values()
    if not all_rows or len(all_rows) < 2:
        return []
    data = all_rows[1:]

    primary = {}
    repeated_links = set()
    for r in data:
        if len(r) < len(SHEET_HEADER):
            r = r + [""] * (len(SHEET_HEADER) - len(r))
        vid = r[COL["ID визита"]]
        rcity = r[COL["Город"]]
        atype = r[COL["Тип аудита"]]
        linked = r[COL["Связан с визитом"]]
        if atype == TYPE_REPEAT and linked:
            repeated_links.add(linked)
        if atype == TYPE_PRIMARY and rcity == city:
            if vid not in primary:
                primary[vid] = (r[COL["Название ТТ"]], r[COL["Дата"]], r[COL["Аудитор"]])

    result = []
    for vid, (tt, date, auditor) in primary.items():
        if vid not in repeated_links:
            result.append((vid, tt, date, auditor))
    return result


def save_to_sheet(data: dict):
    """Длинный формат: одна строка на бренд. Фокусные ответы повторяются во всех
    строках визита; заполнены только столбцы фокусных вопросов своего портфеля."""
    ws = _get_worksheet()

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
    tail = [
        data.get("audit_type", ""),
        data.get("linked_visit", ""),
    ]
    # фокусные ячейки в порядке FOCUS_COLUMNS
    focus_answers = data.get("focus_answers", {})
    focus_cells = [focus_answers.get(col, "") for col in FOCUS_COLUMNS]

    brand_sku = data.get("brand_sku", [])
    notes = data.get("notes", "")
    photo_url = data.get("photo_url", "")

    rows = []
    if not brand_sku:
        rows.append(common + ["Нет наших брендов", "0", notes, photo_url] + tail + focus_cells)
    else:
        for brand, sku in brand_sku:
            rows.append(common + [brand, sku, notes, photo_url] + tail + focus_cells)

    ws.append_rows(rows, value_input_option="USER_ENTERED")


# ─── ЯНДЕКС.ДИСК ────────────────────────────────────────────────────────────
def upload_photo_to_yandex(file_bytes: bytes, filename: str) -> str:
    if not YANDEX_TOKEN:
        logger.error("YANDEX_TOKEN не задан")
        return ""
    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
    disk_path = f"{YANDEX_FOLDER}/{filename}"
    base = "https://cloud-api.yandex.net/v1/disk/resources"
    try:
        r = requests.get(f"{base}/upload", headers=headers,
                         params={"path": disk_path, "overwrite": "true"}, timeout=30)
        r.raise_for_status()
        href = r.json()["href"]
        up = requests.put(href, data=file_bytes, timeout=60)
        up.raise_for_status()
        requests.put(f"{base}/publish", headers=headers,
                     params={"path": disk_path}, timeout=30)
        meta = requests.get(base, headers=headers,
                            params={"path": disk_path, "fields": "public_url"}, timeout=30)
        meta.raise_for_status()
        return meta.json().get("public_url", "")
    except Exception as e:
        logger.error(f"Ошибка загрузки на Яндекс.Диск: {e}")
        return ""


# ─── АВТОРИЗАЦИЯ ────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


# ─── ФОКУСНЫЕ ВОПРОСЫ: helper ────────────────────────────────────────────────
async def ask_focus_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Задаёт текущий фокусный вопрос портфеля или, если они кончились, идёт к заметкам."""
    portfolio = context.user_data["portfolio"]
    questions = FOCUS_QUESTIONS.get(portfolio, [])
    idx = context.user_data.get("focus_idx", 0)

    if idx >= len(questions):
        # все фокусные заданы — к заметкам
        return await ask_notes(update, context)

    col, qtext, qtype = questions[idx]
    if qtype == "yesno":
        keyboard = [["Да", "Нет"]]
        await update.message.reply_text(
            f"❓ {qtext}",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
    else:  # number
        await update.message.reply_text(
            f"🔢 {qtext}",
            reply_markup=ReplyKeyboardRemove()
        )
    return FOCUS


# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    logger.info(f"START от user_id={user_id}; разрешённые={sorted(ALLOWED_USER_IDS)}; "
                f"пускаем={user_id in ALLOWED_USER_IDS}")

    if not is_authorized(user_id):
        await update.message.reply_text(
            f"⛔ У вас нет доступа к этому боту.\n"
            f"Ваш Telegram ID: `{user_id}`\n"
            f"Передайте его администратору для получения доступа.",
            parse_mode="Markdown")
        return ConversationHandler.END

    name = user.full_name or "Аудитор"
    context.user_data.clear()
    context.user_data["auditor"] = name
    context.user_data["photos"] = []

    keyboard = [[TYPE_PRIMARY], [TYPE_REPEAT]]
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\nВыбери тип аудита:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    return AUDIT_TYPE


async def audit_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == TYPE_PRIMARY:
        context.user_data["audit_type"] = TYPE_PRIMARY
        context.user_data["linked_visit"] = ""
        keyboard = [[p] for p in BRANDS.keys()]
        await update.message.reply_text(
            "Выбери портфель для аудита:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
        return PORTFOLIO

    if text == TYPE_REPEAT:
        context.user_data["audit_type"] = TYPE_REPEAT
        keyboard = [[c] for c in CITIES]
        await update.message.reply_text(
            "🔁 Повторный аудит.\n🏙 В каком городе точка?",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
        return REPEAT_CITY

    await update.message.reply_text("Выбери тип аудита из кнопок.")
    return AUDIT_TYPE


# ----- ВЕТКА ПОВТОРНОГО -----
async def repeat_city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text
    if city not in CITIES:
        await update.message.reply_text("Выбери город из списка.")
        return REPEAT_CITY

    await update.message.reply_text("⏳ Ищу первичные аудиты по городу...",
                                    reply_markup=ReplyKeyboardRemove())
    try:
        available = get_primary_without_repeat(city)
    except Exception as e:
        logger.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("❌ Не удалось получить список. Попробуйте позже.")
        return ConversationHandler.END

    if not available:
        await update.message.reply_text(
            f"В городе {city} нет первичных аудитов, доступных для повторного "
            f"(либо их нет, либо по всем уже проведён повторный).\n\n"
            f"Для нового аудита — /start")
        return ConversationHandler.END

    context.user_data["repeat_options"] = {}
    keyboard = []
    for vid, tt, date, auditor in available:
        label = f"{tt} | {date} | {auditor}"[:60]
        context.user_data["repeat_options"][label] = (vid, tt)
        keyboard.append([label])

    context.user_data["city"] = city
    await update.message.reply_text(
        "Выбери точку, по которой проводишь повторный аудит:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    return REPEAT_PICK


async def repeat_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    options = context.user_data.get("repeat_options", {})
    if text not in options:
        await update.message.reply_text("Выбери точку из предложенных кнопок.")
        return REPEAT_PICK

    vid, tt = options[text]
    context.user_data["linked_visit"] = vid
    context.user_data["tt_name"] = tt
    context.user_data["repeat_flow"] = True

    keyboard = [[p] for p in BRANDS.keys()]
    await update.message.reply_text(
        f"Точка: {tt}\nПовторный аудит привязан к первичному визиту.\n\n"
        "Выбери портфель для аудита:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    return PORTFOLIO


# ----- ОБЩИЙ ПОТОК -----
async def portfolio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in BRANDS:
        await update.message.reply_text("Выбери портфель из списка.")
        return PORTFOLIO
    context.user_data["portfolio"] = text

    if context.user_data.get("repeat_flow"):
        keyboard = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
        await update.message.reply_text(
            "📍 Отправь геолокацию торговой точки:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
        return LOCATION

    keyboard = [[c] for c in CITIES]
    await update.message.reply_text(
        "🏙 Выбери город:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
    return CITY


async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in CITIES:
        await update.message.reply_text("Выбери город из списка.")
        return CITY
    context.user_data["city"] = text
    await update.message.reply_text(
        "🏪 Введи название торговой точки:",
        reply_markup=ReplyKeyboardRemove())
    return TT_NAME


async def tt_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tt_name"] = update.message.text
    keyboard = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
    await update.message.reply_text(
        "📍 Отправь геолокацию торговой точки:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True))
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
        parse_mode="Markdown")
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
        parse_mode="Markdown")
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
        parse_mode="Markdown")
    return BRANDS_SELECT


async def brands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    portfolio = context.user_data["portfolio"]
    brand_list = BRANDS[portfolio]

    if text == NO_BRANDS_LABEL:
        context.user_data["brand_sku"] = []
        # фокусные задаём даже без брендов
        context.user_data["focus_idx"] = 0
        context.user_data["focus_answers"] = {}
        return await ask_focus_question(update, context)

    if text == "✅ Выбор завершён":
        if not context.user_data["brand_sku"]:
            await update.message.reply_text(
                f"Выбери хотя бы один бренд и впиши SKU, либо нажми *«{NO_BRANDS_LABEL}»*.",
                parse_mode="Markdown")
            return BRANDS_SELECT
        context.user_data["focus_idx"] = 0
        context.user_data["focus_answers"] = {}
        return await ask_focus_question(update, context)

    if text in brand_list:
        if text in context.user_data["selected_brands"]:
            await update.message.reply_text(f"⚠️ {text} уже добавлен. Выбери другой бренд.")
            return BRANDS_SELECT
        context.user_data["current_brand"] = text
        await update.message.reply_text(
            f"🔢 Сколько SKU бренда *{text}* в точке? Впиши число:",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
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
        parse_mode="Markdown")
    return BRANDS_SELECT


async def focus_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает ответ на текущий фокусный вопрос, переходит к следующему."""
    portfolio = context.user_data["portfolio"]
    questions = FOCUS_QUESTIONS.get(portfolio, [])
    idx = context.user_data.get("focus_idx", 0)

    if idx >= len(questions):
        return await ask_notes(update, context)

    col, qtext, qtype = questions[idx]
    answer = update.message.text.strip()

    if qtype == "yesno":
        if answer not in ("Да", "Нет"):
            await update.message.reply_text("Выбери: Да или Нет.")
            return FOCUS
    # number — принимаем как есть (по договорённости не валидируем)

    context.user_data["focus_answers"][col] = answer
    context.user_data["focus_idx"] = idx + 1
    return await ask_focus_question(update, context)


async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Добавь заметки по точке (любые наблюдения, проблемы, возможности).\n"
        "Если заметок нет — напиши *нет*.",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
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

    # сводка по фокусным
    focus_answers = context.user_data.get("focus_answers", {})
    portfolio = context.user_data.get("portfolio")
    focus_lines = []
    for col, qtext, qtype in FOCUS_QUESTIONS.get(portfolio, []):
        focus_lines.append(f"   • {col}: {focus_answers.get(col, '—')}")
    focus_summary = "\n".join(focus_lines) if focus_lines else "   • —"

    d = context.user_data
    audit_line = ("🔁 Тип: повторный аудит\n" if d.get("audit_type") == TYPE_REPEAT
                  else "🆕 Тип: первичный аудит\n")
    summary = (
        f"✅ *Аудит сохранён!*\n\n"
        f"{audit_line}"
        f"🗂 Портфель: {d.get('portfolio')}\n"
        f"🏙 Город: {d.get('city')}\n"
        f"🏪 ТТ: {d.get('tt_name')}\n"
        f"🏷 Формат: {d.get('tt_format')}\n"
        f"🛍 Бренды и SKU:\n{brands_summary}\n"
        f"📋 Фокусные:\n{focus_summary}\n"
        f"📝 Заметки: {d.get('notes') or '—'}\n"
        f"📸 Фото: {len(photo_urls)} шт.\n\n"
        f"Для нового аудита — /start"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Аудит отменён. Для начала нового — /start",
        reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш Telegram ID: `{user.id}`\n"
        f"Username: @{user.username}\n"
        f"Имя: {user.full_name}",
        parse_mode="Markdown")


# ─── MAIN ───────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AUDIT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, audit_type_handler)],
            REPEAT_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, repeat_city_handler)],
            REPEAT_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, repeat_pick_handler)],
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
            FOCUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, focus_handler)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, notes_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.run_polling()


if __name__ == "__main__":
    main()
