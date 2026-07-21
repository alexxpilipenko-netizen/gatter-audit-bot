import logging
import os
import json
import io
import asyncio
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
    "Food Mix": ["Mondelez", "Yunus", "La Milk", "Bizon", "Kent Boringer", "MAY", "Orion", "Korona"],
    "Non-Food": ["Lody", "Aerostar", "Energizer", "Wellnax", "Splat"],
}

CITIES = [
    "Ташкент", "Самарканд", "Андижан", "Бухара", "Фергана",
    "Ургенч", "Коканд", "Наманган", "Джиззак", "Карши", "Навои",
    "Гулистан", "Ангрен", "Чирчик", "Янгиюль"
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
        ("Об. Kinder касса",
         "Наличие киндер блок на кассе — на оборудовании магазина или на нашем оборудовании?",
         "choice3"),
        ("Ценники", "Наличие ценников", "yesno"),
        ("POSm", "Наличие POSm (шелф-токеры, воблеры, ленты и т.д.)", "yesno"),
        ("Семифреди", "Наличие Семифреди в ТТ", "yesno"),
        ("Плиточный шоколад", "Наличие плиточного шоколада в категории", "yesno"),
    ],
    "Non-Food": [
        ("ТОП-3 Splat", "Наличие ТОП-3 Splat", "yesno"),
        ("Блок освежителей", "Наличие блока освежителей 300мл + 250мл", "yesno"),
    ],
    "Food Mix": [
        ("ЧП 48 касса", "Наличие ЧП 48 (штучные) на кассе", "yesno"),
        ("ЧП 48 отдел", "Наличие ЧП 48 (штучные) в отделе", "yesno"),
        ("SKU предкасса", "Введите количество SKU на предкассовом узле", "number"),
    ],
}

# Варианты ответа для вопроса типа "choice3" (киндер блок на кассе)
CHOICE3_OPTIONS = ["На оборудовании магазина", "На нашем оборудовании", "Нет"]

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
 TT_FORMAT, PHOTO, BRANDS_SELECT, SKU_INPUT, FOCUS, NOTES,
 CONFIRM, EDIT_MENU, EDIT_BRANDS, EDIT_SKU_PICK, EDIT_SKU_VALUE,
 EDIT_DEL_PICK, EDIT_NOTES, EDIT_ADD_PICK, EDIT_ADD_SKU,
 CANCEL_CONFIRM) = range(23)


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
YANDEX_API = "https://cloud-api.yandex.net/v1/disk/resources"


def _ya_headers():
    return {"Authorization": f"OAuth {YANDEX_TOKEN}"}


def create_visit_folder(visit_id: str) -> str:
    """Создаёт папку визита на Яндекс.Диске. Возвращает путь папки или '' при ошибке."""
    if not YANDEX_TOKEN:
        logger.error("YANDEX_TOKEN не задан")
        return ""
    folder_path = f"{YANDEX_FOLDER}/{visit_id}"
    try:
        r = requests.put(YANDEX_API, headers=_ya_headers(),
                         params={"path": folder_path}, timeout=30)
        # 201 = создана; 409 = уже существует (тоже ок)
        if r.status_code not in (201, 409):
            r.raise_for_status()
        return folder_path
    except Exception as e:
        logger.error(f"Ошибка создания папки визита: {e}")
        return ""


def upload_photo_to_folder(file_bytes: bytes, folder_path: str, filename: str) -> bool:
    """Загружает фото в папку визита БЕЗ индивидуальной публикации
    (доступ даёт публикация всей папки). Возвращает успех/неуспех."""
    disk_path = f"{folder_path}/{filename}"
    try:
        r = requests.get(f"{YANDEX_API}/upload", headers=_ya_headers(),
                         params={"path": disk_path, "overwrite": "true"}, timeout=30)
        r.raise_for_status()
        href = r.json()["href"]
        up = requests.put(href, data=file_bytes, timeout=60)
        up.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Ошибка загрузки фото {filename}: {e}")
        return False


def publish_folder(folder_path: str) -> str:
    """Публикует папку визита и возвращает её публичную ссылку (или '')."""
    try:
        requests.put(f"{YANDEX_API}/publish", headers=_ya_headers(),
                     params={"path": folder_path}, timeout=30)
        meta = requests.get(YANDEX_API, headers=_ya_headers(),
                            params={"path": folder_path, "fields": "public_url"},
                            timeout=30)
        meta.raise_for_status()
        return meta.json().get("public_url", "")
    except Exception as e:
        logger.error(f"Ошибка публикации папки: {e}")
        return ""


async def _download_and_upload_one(context, file_id, folder_path, filename):
    """Скачивает одно фото из Telegram и грузит в папку визита.
    Блокирующая часть — в отдельном потоке (event loop свободен)."""
    try:
        tg_file = await context.bot.get_file(file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        return await asyncio.to_thread(
            upload_photo_to_folder, file_bytes, folder_path, filename)
    except Exception as e:
        logger.error(f"Ошибка обработки фото {filename}: {e}")
        return False


async def upload_all_photos(update, context, visit_id):
    """Создаёт папку визита, грузит все фото ПАРАЛЛЕЛЬНО с живым счётчиком,
    публикует папку. Возвращает ОДНУ публичную ссылку на папку (или '')."""
    photos = context.user_data.get("photos", [])
    total = len(photos)
    if total == 0:
        return ""

    # 1. Папка визита (один короткий последовательный шаг)
    folder_path = await asyncio.to_thread(create_visit_folder, visit_id)
    if not folder_path:
        await update.message.reply_text(
            "⚠️ Не удалось создать папку для фото — фото не сохранятся, "
            "данные аудита будут записаны без них.")
        return ""

    progress_msg = await update.message.reply_text(f"⏳ Загружаю фото: 0 из {total}...")

    # 2. Параллельная заливка в папку (без индивидуальной публикации — быстрее)
    tasks = [
        asyncio.create_task(
            _download_and_upload_one(context, fid, folder_path, f"{visit_id}_{i}.jpg"))
        for i, fid in enumerate(photos, 1)
    ]

    ok_count = 0
    done = 0
    for coro in asyncio.as_completed(tasks):
        success = await coro
        done += 1
        if success:
            ok_count += 1
        try:
            await progress_msg.edit_text(f"⏳ Загружаю фото: {done} из {total}...")
        except Exception:
            pass

    # 3. Публикация папки — одна ссылка на все фото визита
    folder_url = await asyncio.to_thread(publish_folder, folder_path)

    try:
        await progress_msg.edit_text(f"✅ Фото загружено: {ok_count} из {total}.")
    except Exception:
        pass
    return folder_url


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
    elif qtype == "choice3":
        keyboard = [[opt] for opt in CHOICE3_OPTIONS]
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
    return await ask_brands(update, context)


async def ask_brands(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    elif qtype == "choice3":
        if answer not in CHOICE3_OPTIONS:
            await update.message.reply_text("Выбери один из вариантов на кнопках.")
            return FOCUS
    # number — принимаем как есть (по договорённости не валидируем)

    context.user_data["focus_answers"][col] = answer
    context.user_data["focus_idx"] = idx + 1
    return await ask_focus_question(update, context)


async def ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Добавь заметки по точке: наблюдения, проблемы, возможности, "
        "итоги общения с ЛПР точки.\n"
        "Если заметок нет — напиши *нет*.",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return NOTES


async def notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = update.message.text
    context.user_data["notes"] = "" if notes.lower() == "нет" else notes
    return await show_confirm(update, context)


def build_summary_text(context) -> str:
    """Текстовая сводка визита для экрана подтверждения и финального сообщения."""
    d = context.user_data
    brand_sku = d.get("brand_sku", [])
    if brand_sku:
        brands_summary = "\n".join(f"   • {b}: {s} SKU" for b, s in brand_sku)
    else:
        brands_summary = "   • Нет наших брендов"

    focus_answers = d.get("focus_answers", {})
    portfolio = d.get("portfolio")
    focus_lines = [f"   • {col}: {focus_answers.get(col, '—')}"
                   for col, _q, _t in FOCUS_QUESTIONS.get(portfolio, [])]
    focus_summary = "\n".join(focus_lines) if focus_lines else "   • —"

    audit_line = ("🔁 Тип: повторный аудит\n" if d.get("audit_type") == TYPE_REPEAT
                  else "🆕 Тип: первичный аудит\n")
    return (
        f"{audit_line}"
        f"🗂 Портфель: {d.get('portfolio')}\n"
        f"🏙 Город: {d.get('city')}\n"
        f"🏪 ТТ: {d.get('tt_name')}\n"
        f"🏷 Формат: {d.get('tt_format')}\n"
        f"🛍 Бренды и SKU:\n{brands_summary}\n"
        f"📋 Фокусные:\n{focus_summary}\n"
        f"📝 Заметки: {d.get('notes') or '—'}"
    )


# ─── ПОДТВЕРЖДЕНИЕ И ПРАВКА ──────────────────────────────────────────────────
async def show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["✅ Всё верно, сохранить"], ["✏️ Исправить"], ["❌ Аннулировать аудит"]]
    await update.message.reply_text(
        f"🔎 *Проверь данные перед сохранением:*\n\n{build_summary_text(context)}",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown")
    return CONFIRM


async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "✅ Всё верно, сохранить":
        # Дальше — фото, затем сохранение
        await update.message.reply_text(
            "📸 Теперь приложи фото торговой точки (можно несколько).\n"
            "Когда закончишь — напиши *готово*.",
            reply_markup=ReplyKeyboardMarkup([["готово"]], resize_keyboard=True),
            parse_mode="Markdown")
        return PHOTO
    if text == "✏️ Исправить":
        keyboard = [["Бренды / SKU"], ["Заметки"], ["⬅️ Назад к проверке"]]
        await update.message.reply_text(
            "Что исправить?",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return EDIT_MENU
    if text == "❌ Аннулировать аудит":
        keyboard = [["Да, аннулировать"], ["Нет, вернуться"]]
        await update.message.reply_text(
            "⚠️ Точно аннулировать аудит? Все введённые данные будут потеряны.",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True,
                                             resize_keyboard=True))
        return CANCEL_CONFIRM
    await update.message.reply_text("Выбери действие из кнопок.")
    return CONFIRM


async def cancel_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Да, аннулировать":
        context.user_data.clear()
        await update.message.reply_text(
            "❌ Аудит аннулирован, данные не сохранены.\n"
            "Для нового аудита — /start",
            reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    if text == "Нет, вернуться":
        return await show_confirm(update, context)
    await update.message.reply_text("Выбери: Да, аннулировать / Нет, вернуться.")
    return CANCEL_CONFIRM


async def edit_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Бренды / SKU":
        return await show_edit_brands(update, context)
    if text == "Заметки":
        await update.message.reply_text(
            "📝 Введи заметки заново (или напиши *нет*):",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        return EDIT_NOTES
    if text == "⬅️ Назад к проверке":
        return await show_confirm(update, context)
    await update.message.reply_text("Выбери из кнопок.")
    return EDIT_MENU


async def edit_notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = update.message.text
    context.user_data["notes"] = "" if notes.lower() == "нет" else notes
    await update.message.reply_text("✅ Заметки обновлены.")
    return await show_confirm(update, context)


async def show_edit_brands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    brand_sku = context.user_data.get("brand_sku", [])
    if brand_sku:
        current = "\n".join(f"   • {b}: {s} SKU" for b, s in brand_sku)
    else:
        current = "   • (брендов нет — было выбрано «Нет наших брендов»)"
    keyboard = [["➕ Добавить бренд"], ["🔢 Изменить SKU"], ["🗑 Удалить бренд"],
                ["⬅️ Назад к проверке"]]
    await update.message.reply_text(
        f"Текущие бренды:\n{current}\n\nЧто сделать?",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return EDIT_BRANDS


async def edit_brands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    brand_sku = context.user_data.get("brand_sku", [])

    if text == "➕ Добавить бренд":
        portfolio = context.user_data["portfolio"]
        already = [b for b, _s in brand_sku]
        available = [b for b in BRANDS[portfolio] if b not in already]
        if not available:
            await update.message.reply_text(
                "Все бренды портфеля уже добавлены.")
            return await show_edit_brands(update, context)
        keyboard = [[b] for b in available] + [["⬅️ Отмена"]]
        await update.message.reply_text(
            "Выбери бренд для добавления:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return EDIT_ADD_PICK
    if text == "🔢 Изменить SKU":
        if not brand_sku:
            await update.message.reply_text("Список брендов пуст — сначала добавь бренд.")
            return await show_edit_brands(update, context)
        keyboard = [[b] for b, _s in brand_sku] + [["⬅️ Отмена"]]
        await update.message.reply_text(
            "Выбери бренд, у которого изменить SKU:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return EDIT_SKU_PICK
    if text == "🗑 Удалить бренд":
        if not brand_sku:
            await update.message.reply_text("Список брендов пуст — удалять нечего.")
            return await show_edit_brands(update, context)
        keyboard = [[b] for b, _s in brand_sku] + [["⬅️ Отмена"]]
        await update.message.reply_text(
            "Выбери бренд для удаления:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return EDIT_DEL_PICK
    if text == "⬅️ Назад к проверке":
        return await show_confirm(update, context)
    await update.message.reply_text("Выбери из кнопок.")
    return EDIT_BRANDS


async def edit_add_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Отмена":
        return await show_edit_brands(update, context)
    portfolio = context.user_data["portfolio"]
    already = [b for b, _s in context.user_data.get("brand_sku", [])]
    available = [b for b in BRANDS[portfolio] if b not in already]
    if text not in available:
        await update.message.reply_text("Выбери бренд из кнопок.")
        return EDIT_ADD_PICK
    context.user_data["edit_brand"] = text
    await update.message.reply_text(
        f"🔢 Сколько SKU бренда *{text}* в точке? Впиши число:",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return EDIT_ADD_SKU


async def edit_add_sku_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sku = update.message.text.strip()
    brand = context.user_data.get("edit_brand")
    context.user_data.setdefault("brand_sku", []).append((brand, sku))
    if "selected_brands" in context.user_data:
        context.user_data["selected_brands"].append(brand)
    await update.message.reply_text(f"✅ {brand}: {sku} SKU добавлено.")
    return await show_edit_brands(update, context)


async def edit_sku_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Отмена":
        return await show_edit_brands(update, context)
    brands = [b for b, _s in context.user_data.get("brand_sku", [])]
    if text not in brands:
        await update.message.reply_text("Выбери бренд из кнопок.")
        return EDIT_SKU_PICK
    context.user_data["edit_brand"] = text
    await update.message.reply_text(
        f"🔢 Новое количество SKU для *{text}*:",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return EDIT_SKU_VALUE


async def edit_sku_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_sku = update.message.text.strip()
    brand = context.user_data.get("edit_brand")
    # обновляем значение в списке пар
    pairs = context.user_data.get("brand_sku", [])
    context.user_data["brand_sku"] = [
        (b, new_sku if b == brand else s) for b, s in pairs
    ]
    await update.message.reply_text(f"✅ {brand}: SKU изменён на {new_sku}.")
    return await show_edit_brands(update, context)


async def edit_del_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "⬅️ Отмена":
        return await show_edit_brands(update, context)
    pairs = context.user_data.get("brand_sku", [])
    brands = [b for b, _s in pairs]
    if text not in brands:
        await update.message.reply_text("Выбери бренд из кнопок.")
        return EDIT_DEL_PICK
    # удаляем бренд из пар и из списка выбранных
    context.user_data["brand_sku"] = [(b, s) for b, s in pairs if b != text]
    if "selected_brands" in context.user_data:
        context.user_data["selected_brands"] = [
            b for b in context.user_data["selected_brands"] if b != text
        ]
    await update.message.reply_text(f"🗑 Бренд {text} удалён.")
    return await show_edit_brands(update, context)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.lower() == "готово":
        return await finalize_and_save(update, context)
    if update.message.photo:
        context.user_data["photos"].append(update.message.photo[-1].file_id)
        count = len(context.user_data["photos"])
        await update.message.reply_text(
            f"✅ Фото {count} принято. Отправь ещё или напиши *готово*.",
            parse_mode="Markdown")
    return PHOTO


async def finalize_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Сохраняю данные...")

    visit_id = f"{update.effective_user.id}_{int(datetime.now().timestamp())}"
    context.user_data["visit_id"] = visit_id
    context.user_data["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Параллельная загрузка фото с живым счётчиком (не блокирует бота)
    folder_url = await upload_all_photos(update, context, visit_id)
    context.user_data["photo_url"] = folder_url

    try:
        save_to_sheet(context.user_data)
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")
        await update.message.reply_text("❌ Ошибка сохранения. Обратитесь к администратору.")
        return ConversationHandler.END

    brand_sku = context.user_data.get("brand_sku", [])
    summary = (
        f"✅ *Аудит сохранён!*\n\n"
        f"{build_summary_text(context)}\n"
        f"📸 Фото: {len(context.user_data.get('photos', []))} шт.\n\n"
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
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_handler)],
            EDIT_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_menu_handler)],
            EDIT_BRANDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_brands_handler)],
            EDIT_SKU_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_sku_pick_handler)],
            EDIT_SKU_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_sku_value_handler)],
            EDIT_DEL_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_del_pick_handler)],
            EDIT_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_notes_handler)],
            EDIT_ADD_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_add_pick_handler)],
            EDIT_ADD_SKU: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_add_sku_handler)],
            CANCEL_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_confirm_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.run_polling()


if __name__ == "__main__":
    main()
