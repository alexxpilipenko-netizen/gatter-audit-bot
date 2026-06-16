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

# Авторизованные пользователи: {telegram_user_id: "Имя"}
AUTHORIZED_USERS = {
    244836501: "Александр Пилипенко",
}

BRANDS = {
    "Ferrero": ["Nutella", "Ferrero Rocher", "Raffaello", "Kinder", "Tic Tac"],
    "Food Mix": ["Mondelez", "Yunus", "La Milk", "Bizon", "Kent Boringer", "MAY", "Orion"],
    "Non-Food": ["Lody", "Aerostar", "Energizer", "Wellnax", "Splat"],
}

CITIES = [
    "Ташкент", "Самарканд", "Андижан", "Бухара", "Фергана",
    "Ургенч", "Коканд", "Наманган", "Джиззак", "Карши", "Навои"
]

RATINGS = {
    "1": "1 — Очень плохо: товар почти отсутствует, полка пустая или занята конкурентами",
    "2": "2 — Плохо: товар есть, но мало, расположен неудобно, конкуренты представлены лучше",
    "3": "3 — Нормально: товар есть, но ничего особенного, средняя полка",
    "4": "4 — Хорошо: товар хорошо виден, полка аккуратная, смотрится лучше конкурентов",
    "5": "5 — Отлично: товар на лучшем месте, идеальная полка, сразу бросается в глаза",
}

# ─── ШАГИ ДИАЛОГА ───────────────────────────────────────────────────────────
(PORTFOLIO, CITY, TT_NAME, LOCATION, PHOTO,
 BRANDS_SELECT, GOLDEN_SHELF, OMP, DMP, RATING, NOTES) = range(11)


# ─── GOOGLE HELPERS ─────────────────────────────────────────────────────────
def get_google_creds():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    return Credentials.from_service_account_info(creds_info, scopes=scopes)


def save_to_sheet(data: dict):
    creds = get_google_creds()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet("Аудит")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Аудит", rows=1000, cols=20)
        ws.append_row([
            "ID визита", "Дата", "Аудитор", "Портфель", "Город",
            "Название ТТ", "Широта", "Долгота",
            "Бренды в точке", "Золотая полка", "ОМП", "ДМП",
            "Оценка", "Заметки", "Ссылка на фото"
        ])

    ws.append_row([
        data.get("visit_id", ""),
        data.get("date", ""),
        data.get("auditor", ""),
        data.get("portfolio", ""),
        data.get("city", ""),
        data.get("tt_name", ""),
        data.get("lat", ""),
        data.get("lon", ""),
        data.get("brands", ""),
        data.get("golden_shelf", ""),
        data.get("omp", ""),
        data.get("dmp", ""),
        data.get("rating", ""),
        data.get("notes", ""),
        data.get("photo_url", ""),
    ])


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
        # 1. Получаем ссылку для загрузки
        r = requests.get(
            f"{base}/upload",
            headers=headers,
            params={"path": disk_path, "overwrite": "true"},
            timeout=30,
        )
        r.raise_for_status()
        href = r.json()["href"]

        # 2. Загружаем байты файла
        up = requests.put(href, data=file_bytes, timeout=60)
        up.raise_for_status()

        # 3. Публикуем файл
        requests.put(
            f"{base}/publish",
            headers=headers,
            params={"path": disk_path},
            timeout=30,
        )

        # 4. Получаем публичную ссылку
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
    return user_id in AUTHORIZED_USERS


# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text(
            f"⛔ У вас нет доступа к этому боту.\n"
            f"Ваш Telegram ID: `{user_id}`\n"
            f"Передайте его администратору для получения доступа.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    name = AUTHORIZED_USERS[user_id]
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
    keyboard = [[b] for b in brand_list] + [["✅ Выбор завершён"]]
    context.user_data["selected_brands"] = []

    await update.message.reply_text(
        f"🏷 Какие бренды портфеля *{portfolio}* присутствуют в точке?\n"
        "Выбирай по одному, затем нажми *«Выбор завершён»*.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return BRANDS_SELECT


async def brands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    portfolio = context.user_data["portfolio"]
    brand_list = BRANDS[portfolio]

    if text == "✅ Выбор завершён":
        selected = context.user_data.get("selected_brands", [])
        if not selected:
            await update.message.reply_text("Выбери хотя бы один бренд.")
            return BRANDS_SELECT
        context.user_data["brands"] = ", ".join(selected)
        return await ask_golden_shelf(update, context)

    if text in brand_list:
        if text not in context.user_data["selected_brands"]:
            context.user_data["selected_brands"].append(text)
            await update.message.reply_text(f"✅ {text} добавлен. Выбери ещё или нажми *«Выбор завершён»*.",
                                            parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ {text} уже выбран.")
    return BRANDS_SELECT


async def ask_golden_shelf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Да", "Нет", "Частично"]]
    await update.message.reply_text(
        "⭐ *Золотая полка* — лучшее место в торговом зале на уровне глаз покупателя "
        "(обычно 2-я и 3-я полка снизу), где товар заметен сразу при входе.\n\n"
        "Наш товар находится на золотой полке?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return GOLDEN_SHELF


async def golden_shelf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ["Да", "Нет", "Частично"]:
        await update.message.reply_text("Выбери: Да, Нет или Частично.")
        return GOLDEN_SHELF
    context.user_data["golden_shelf"] = text

    keyboard = [["Да", "Нет", "Частично"]]
    await update.message.reply_text(
        "🛒 *ОМП* (обязательное место продаж) — стеллаж или полка, "
        "где наш товар должен присутствовать согласно стандарту.\n\n"
        "Наш товар представлен в ОМП?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return OMP


async def omp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ["Да", "Нет", "Частично"]:
        await update.message.reply_text("Выбери: Да, Нет или Частично.")
        return OMP
    context.user_data["omp"] = text

    keyboard = [["Да", "Нет", "Частично"]]
    await update.message.reply_text(
        "📦 *ДМП* (дополнительное место продаж) — паллет, стойка, дисплей или "
        "любое дополнительное размещение товара вне основной полки.\n\n"
        "В точке организовано ДМП?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return DMP


async def dmp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ["Да", "Нет", "Частично"]:
        await update.message.reply_text("Выбери: Да, Нет или Частично.")
        return DMP
    context.user_data["dmp"] = text

    rating_text = "\n".join(RATINGS.values())
    keyboard = [["1", "2", "3", "4", "5"]]
    await update.message.reply_text(
        f"📊 *Общая оценка представленности нашего товара:*\n\n{rating_text}",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return RATING


async def rating_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in RATINGS:
        await update.message.reply_text("Выбери оценку от 1 до 5.")
        return RATING
    context.user_data["rating"] = text

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

    # Скачиваем фото из Telegram и заливаем на Яндекс.Диск
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

    # Сохраняем в Google Sheets
    try:
        save_to_sheet(context.user_data)
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")
        await update.message.reply_text("❌ Ошибка сохранения. Обратитесь к администратору.")
        return ConversationHandler.END

    # Итоговый отчёт
    d = context.user_data
    summary = (
        f"✅ *Аудит сохранён!*\n\n"
        f"🗂 Портфель: {d.get('portfolio')}\n"
        f"🏙 Город: {d.get('city')}\n"
        f"🏪 ТТ: {d.get('tt_name')}\n"
        f"🏷 Бренды: {d.get('brands')}\n"
        f"⭐ Золотая полка: {d.get('golden_shelf')}\n"
        f"🛒 ОМП: {d.get('omp')}\n"
        f"📦 ДМП: {d.get('dmp')}\n"
        f"📊 Оценка: {d.get('rating')}/5\n"
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
            PHOTO: [
                MessageHandler(filters.PHOTO, photo_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_handler),
            ],
            BRANDS_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, brands_handler)],
            GOLDEN_SHELF: [MessageHandler(filters.TEXT & ~filters.COMMAND, golden_shelf_handler)],
            OMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, omp_handler)],
            DMP: [MessageHandler(filters.TEXT & ~filters.COMMAND, dmp_handler)],
            RATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, rating_handler)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, notes_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.run_polling()


if __name__ == "__main__":
    main()
