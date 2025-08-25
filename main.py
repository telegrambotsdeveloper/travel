import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Инициализация Flask
flask_app = Flask(__name__)

# ========================
# НАСТРОЙКИ
# ========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # Проверьте: @YourChannelName или -100xxxxxxxxxxxx
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например, https://travel-9x10.onrender.com
PORT = int(os.getenv("PORT", 8443))  # Render задаёт PORT, fallback на 8443

CHECK_INTERVAL_SECONDS = 60 * 60  # Проверка каждые 60 минут
DB_PATH = "posted.db"
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Список политических ключевых слов для фильтрации
POLITICAL_KEYWORDS = [
    "политика", "санкции", "президент", "правительство", "выборы", "протест", "митинг", 
    "война", "конфликт", "дипломатия", "внешняя политика", "геополитика", "парламент", 
    "депутат", "кремль", "белый дом", "угрозы", "международные отношения"
]

SOURCES = [
    {"name": "TourDom", "rss": "https://www.tourdom.ru/news/rss/", "html": "https://www.tourdom.ru/news/"},
    {"name": "Tourister", "rss": "https://www.tourister.ru/publications/rss", "html": "https://www.tourister.ru/publications"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("newsbot")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# Маршрут для корневого URL, чтобы UptimeRobot и браузер не получали 404
@flask_app.route('/')
def home():
    return "Travel bot is alive!", 200

# ========================
# ХРАНИЛИЩЕ ДЕДУПЛИКАЦИИ
# ========================
def init_db():
    """
    Создаёт SQLite-базу для хранения уже опубликованных URL.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS posted (
            url TEXT PRIMARY KEY,
            source TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def already_posted(url: str) -> bool:
    """
    Проверяет, публиковался ли URL ранее.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE url = ?", (url,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def mark_posted(url: str, source: str):
    """
    Отмечает URL как опубликованный.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO posted (url, source, created_at) VALUES (?, ?, ?)",
        (url, source, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

# ========================
# УТИЛИТЫ
# ========================
def absolute(base: str, href: str) -> str:
    """
    Преобразует относительную ссылку в абсолютную.
    """
    return href if (href or "").startswith("http") else urljoin(base, href or "")

def same_host(url: str, host_tail: str) -> bool:
    """
    Проверяет, принадлежит ли URL указанному домену.
    """
    try:
        return urlparse(url).netloc.endswith(host_tail)
    except Exception:
        return False

def get_html(url: str) -> Optional[BeautifulSoup]:
    """
    Выполняет GET-запрос и возвращает распарсенный HTML.
    """
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        logger.warning(f"GET failed {url}: {e}")
        return None

def get_og_image(url: str) -> Optional[str]:
    """
    Извлекает URL картинки из meta og:image.
    """
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        og = soup.find("meta", property="og:image:secure_url") or soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"].strip()
    except Exception as e:
        logger.warning(f"[og:image] {url} -> {e}")
    return None

# ========================
# ФИЛЬТРАЦИЯ ПОЛИТИЧЕСКИХ НОВОСТЕЙ
# ========================
def is_political(title: str) -> bool:
    """
    Проверяет, содержит ли заголовок политические ключевые слова.
    """
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in POLITICAL_KEYWORDS)

# ========================
# ПАРСИНГ RSS
# ========================
def fetch_via_rss(rss_url: str) -> List[Dict]:
    """
    Парсит RSS-ленту, возвращает список новостей.
    """
    items: List[Dict] = []
    try:
        feed = feedparser.parse(rss_url)
        for e in feed.entries[:30]:
            link = getattr(e, "link", None)
            title = getattr(e, "title", None)
            if not link or not title or is_political(title):
                continue
            summary = getattr(e, "summary", "")
            items.append({"title": title.strip(), "link": link.strip(), "summary": (summary or "").strip()})
    except Exception as e:
        logger.warning(f"RSS error {rss_url}: {e}")
    return items

# ========================
# HTML-ПАРСЕРЫ
# ========================
def fetch_html_tourdom(list_url: str) -> List[Dict]:
    """
    Парсер HTML для TourDom.
    """
    soup = get_html(list_url)
    if not soup:
        return []
    candidates: List[Dict] = []

    for a in soup.select("article a[href], .news-list a[href], .news a[href]"):
        href = a.get("href")
        title = (a.get_text() or "").strip()
        if not href or not title or is_political(title):
            continue
        link = absolute(list_url, href)
        if "/news/" in link and same_host(link, "tourdom.ru") and 15 <= len(title) <= 160:
            candidates.append({"title": title, "link": link, "summary": ""})

    if not candidates:
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href")
            title = (a.get_text() or "").strip()
            if not title or is_political(title):
                continue
            link = absolute(list_url, href)
            if same_host(link, "tourdom.ru") and title and 15 <= len(title) <= 160:
                candidates.append({"title": title, "link": link, "summary": ""})

    seen, items = set(), []
    for it in candidates:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        t = it["title"].lower()
        if t not in {"новости", "читать далее", "ещё"}:
            items.append(it)
    return items[:25]

def fetch_html_tourister(list_url: str) -> List[Dict]:
    """
    Парсер HTML для Tourister.
    """
    soup = get_html(list_url)
    if not soup:
        return []
    candidates: List[Dict] = []

    for a in soup.select("a[href*='/publications/']"):
        href = a.get("href")
        title = (a.get_text() or "").strip()
        if not href or not title or is_political(title):
            continue
        link = absolute(list_url, href)
        if same_host(link, "tourister.ru") and "/publications/" in link and 15 <= len(title) <= 160:
            candidates.append({"title": title, "link": link, "summary": ""})

    seen, items = set(), []
    for it in candidates:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        t = it["title"].lower()
        if t not in {"новости", "читать далее", "далее", "подробнее"}:
            items.append(it)
    return items[:25]

def fetch_via_html(source: Dict) -> List[Dict]:
    """
    Выбирает подходящий HTML-парсер по домену.
    """
    url = source["html"]
    host = urlparse(url).netloc
    if "tourdom.ru" in host:
        return fetch_html_tourdom(url)
    if "tourister.ru" in host:
        return fetch_html_tourister(url)

    soup = get_html(url)
    if not soup:
        return []
    items: List[Dict] = []
    for a in soup.select("article a[href]"):
        title = (a.get_text() or "").strip()
        href = a.get("href")
        if not title or not href or is_political(title):
            continue
        items.append({"title": title, "link": absolute(url, href), "summary": ""})
    return items[:20]

def fetch_source_items(source: Dict) -> List[Dict]:
    """
    Пытается получить новости через RSS, при неудаче — через HTML.
    """
    if source.get("rss"):
        items = fetch_via_rss(source["rss"])
        if items:
            return items
    return fetch_via_html(source)

# ========================
# ОТПРАВКА В ТЕЛЕГРАМ
# ========================
async def post_news(context: ContextTypes.DEFAULT_TYPE, item: Dict, source_name: str):
    """
    Отправляет новость в канал с картинкой и HTML-подписью.
    """
    logger.info(f"Попытка отправки в канал: {CHANNEL_ID}")
    title = item["title"].strip()
    link = item["link"].strip()
    summary = (item.get("summary") or "").strip()

    image = get_og_image(link)

    caption_parts = [f"<b>{title}</b>"]
    if summary:
        clean = BeautifulSoup(summary, "lxml").get_text(" ", strip=True)
        if len(clean) > 300:
            clean = clean[:297] + "…"
        caption_parts.append(clean)
    caption_parts.append(f'\n<a href="{link}">Читать на {source_name}</a>')
    caption = "\n".join(caption_parts)

    try:
        if image:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        mark_posted(link, source_name)
        logger.info(f"Posted: {title} | {link}")
    except Exception as e:
        logger.error(f"Send failed for {link}: {e}")
        raise

# ========================
# ДЖОБ: ПЕРИОДИЧЕСКАЯ ПРОВЕРКА
# ========================
async def check_sources_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодически проверяет источники, публикует новые новости.
    """
    logger.info("Checking sources...")
    for src in SOURCES:
        await asyncio.sleep(random.uniform(0.5, 1.5))
        items = fetch_source_items(src)
        new_items = [it for it in items if not already_posted(it["link"])]
        for it in reversed(new_items[-5:]):
            await post_news(context, it, src["name"])

# ========================
# КОМАНДЫ
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — приветственное сообщение.
    """
    await update.message.reply_text("Привет! Я раз в час проверяю новые новости и публикую их в канале.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ping — проверка, что бот работает.
    """
    await update.message.reply_text("Работаю ✅")

async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /checknow — немедленная проверка источников.
    """
    await update.message.reply_text("Проверяю источники…")
    await check_sources_job(context)
    await update.message.reply_text("Готово ✅")

# ========================
# MAIN
# ========================
async def main():
    """
    Точка входа: настраивает webhook, команды и периодические задачи.
    """
    if not BOT_TOKEN:
        raise SystemExit("❌ Укажите BOT_TOKEN в переменной окружения.")
    if not CHANNEL_ID:
        raise SystemExit("❌ Укажите CHANNEL_ID в переменной окружения (например, @YourChannel или -100xxxxxxxxxxxx).")
    if not WEBHOOK_URL:
        raise SystemExit("❌ Укажите WEBHOOK_URL в переменной окружения (например, https://travel-9x10.onrender.com).")

    init_db()

    # Создаем приложение PTB
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("checknow", checknow))

    # Настраиваем периодическую задачу
    app.job_queue.run_repeating(check_sources_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    # Регистрируем команды в Telegram
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Поприветствовать"),
            BotCommand("ping", "Проверить, жив ли бот"),
            BotCommand("checknow", "Проверить источники сейчас"),
        ]
    )

    # Настраиваем webhook
    webhook_path = "/webhook"
    full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"
    await app.bot.set_webhook(url=full_webhook_url)
    logger.info(f"Webhook установлен на {full_webhook_url}")

    # Инициализируем приложение
    await app.initialize()
    await app.start()

    # Запускаем Flask в отдельном потоке
    from threading import Thread
    def run_flask():
        flask_app.run(host="0.0.0.0", port=PORT, debug=False)
    
    flask_thread = Thread(target=run_flask)
    flask_thread.start()

    # Запускаем webhook для PTB
    try:
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
        )
        logger.info("Бот запущен. Ожидание обновлений...")
    except Exception as e:
        logger.error(f"Ошибка при запуске webhook: {e}")
        raise

    # Держим приложение активным
    while True:
        await asyncio.sleep(3600)

async def stop(app: Application):
    """
    Корректное завершение работы приложения.
    """
    try:
        if app and app.running:
            await app.stop()
            await app.updater.stop()
            await app.shutdown()
            logger.info("Бот остановлен.")
        else:
            logger.warning("Приложение не запущено, пропускаем остановку.")
    except Exception as e:
        logger.error(f"Ошибка при остановке приложения: {e}")

if __name__ == "__main__":
    app = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Получен сигнал завершения.")
        loop.run_until_complete(stop(app))
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        loop.run_until_complete(stop(app))
    finally:
        loop.close()
