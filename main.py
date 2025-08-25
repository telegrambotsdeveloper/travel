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
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ========================
# НАСТРОЙКИ
# ========================
# Токен бота из переменной окружения (задаётся на Render в Environment Variables).
BOT_TOKEN = os.getenv("BOT_TOKEN")
# ID канала для публикации (публичный: @mychannel, приватный: -100xxxxxxxxxxxx).
CHANNEL_ID = os.getenv("CHANNEL_ID")
# Полный публичный URL сервиса на Render, например, https://your-bot.onrender.com.
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
# Порт для webhook-сервера (Render задаёт через переменную окружения PORT).
PORT = int(os.getenv("PORT", 8443))  # Fallback на 8443, если PORT не задан.

# Интервал между проверками источников (в секундах, по умолчанию 1 час).
CHECK_INTERVAL_SECONDS = 60 * 60
# Путь к SQLite-базе для дедупликации (хранится локально на Render).
DB_PATH = "posted.db"
# Таймаут для HTTP-запросов.
REQUEST_TIMEOUT = 15
# User-Agent для запросов, чтобы сайты не блокировали.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Список источников новостей (RSS + HTML-фолбэк).
SOURCES = [
    {"name": "TourDom", "rss": "https://www.tourdom.ru/news/rss/", "html": "https://www.tourdom.ru/news/"},
    {"name": "Tourister", "rss": "https://www.tourister.ru/news/rss", "html": "https://www.tourister.ru/news"},
    {"name": "Lenta: Путешествия", "rss": "https://lenta.ru/rss/rubrics/travel/", "html": "https://lenta.ru/rubrics/travel/"},
]

# Настройка логирования (логи видны в dashboard Render).
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("newsbot")

# HTTP-сессия с повторным использованием соединений.
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# ========================
# ХРАНИЛИЩЕ ДЕДУПЛИКАЦИИ
# ========================
def init_db():
    """
    Создаёт SQLite-базу для хранения уже опубликованных URL (дедупликация).
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
    Отмечает URL как опубликованный, сохраняя источник и время в UTC.
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
    Преобразует относительную ссылку в абсолютную на основе базового URL.
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
    Выполняет GET-запрос и возвращает распарсенный HTML (или None при ошибке).
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
    Извлекает URL картинки из meta og:image или og:image:secure_url.
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
# ПАРСИНГ RSS
# ========================
def fetch_via_rss(rss_url: str) -> List[Dict]:
    """
    Парсит RSS-ленту, возвращает список новостей {title, link, summary}.
    """
    items: List[Dict] = []
    try:
        feed = feedparser.parse(rss_url)
        for e in feed.entries[:30]:
            link = getattr(e, "link", None)
            title = getattr(e, "title", None)
            if not link or not title:
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
    Парсер HTML для TourDom: ищет ссылки в article/.news-list/.news.
    """
    soup = get_html(list_url)
    if not soup:
        return []
    candidates: List[Dict] = []

    for a in soup.select("article a[href], .news-list a[href], .news a[href]"):
        href = a.get("href")
        title = (a.get_text() or "").strip()
        if not href or not title:
            continue
        link = absolute(list_url, href)
        if "/news/" in link and same_host(link, "tourdom.ru") and 15 <= len(title) <= 160:
            candidates.append({"title": title, "link": link, "summary": ""})

    if not candidates:
        for a in soup.select("a[href*='/news/']"):
            href = a.get("href")
            title = (a.get_text() or "").strip()
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
    Парсер HTML для Tourister: ищет ссылки с '/news/'.
    """
    soup = get_html(list_url)
    if not soup:
        return []
    candidates: List[Dict] = []

    for a in soup.select("a[href*='/news/']"):
        href = a.get("href")
        title = (a.get_text() or "").strip()
        link = absolute(list_url, href)
        if not link or not title:
            continue
        if same_host(link, "tourister.ru") and "/news/" in link and 15 <= len(title) <= 160:
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

def fetch_html_lenta(list_url: str) -> List[Dict]:
    """
    Парсер HTML для Lenta/Travel: ищет ссылки с '/news/'.
    """
    soup = get_html(list_url)
    if not soup:
        return []
    candidates: List[Dict] = []

    for a in soup.select("a[href^='/news/'], a[href*='/news/']"):
        href = a.get("href")
        title = (a.get_text() or "").strip()
        if not href or not title:
            continue
        link = absolute(list_url, href)
        if same_host(link, "lenta.ru") and "/news/" in link and 15 <= len(title) <= 180:
            candidates.append({"title": title, "link": link, "summary": ""})

    if not candidates:
        for h in soup.select("h2, h3"):
            a = h.find("a", href=True)
            if not a:
                continue
            href = a.get("href")
            title = (a.get_text() or "").strip()
            link = absolute(list_url, href)
            if same_host(link, "lenta.ru") and "/news/" in link and 15 <= len(title) <= 180:
                candidates.append({"title": title, "link": link, "summary": ""})

    seen, items = set(), []
    for it in candidates:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        t = it["title"].lower()
        if t not in {"подробнее", "ещё", "читать далее"}:
            items.append(it)
    return items[:30]

def fetch_via_html(source: Dict) -> List[Dict]:
    """
    Выбирает подходящий HTML-парсер по домену или использует универсальный.
    """
    url = source["html"]
    host = urlparse(url).netloc
    if "tourdom.ru" in host:
        return fetch_html_tourdom(url)
    if "tourister.ru" in host:
        return fetch_html_tourister(url)
    if "lenta.ru" in host:
        return fetch_html_lenta(url)

    soup = get_html(url)
    if not soup:
        return []
    items: List[Dict] = []
    for a in soup.select("article a[href]"):
        title = (a.get_text() or "").strip()
        href = a.get("href")
        if title and href:
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
    Отправляет новость в канал с картинкой (если есть) и HTML-подписью.
    """
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

# ========================
# ДЖОБ: ПЕРИОДИЧЕСКАЯ ПРОВЕРКА
# ========================
async def check_sources_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодически проверяет источники, публикует новые новости (макс. 5 на источник).
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
    Точка входа:
    - Проверяет наличие BOT_TOKEN и CHANNEL_ID.
    - Инициализирует базу SQLite.
    - Настраивает webhook для работы на Render.
    - Регистрирует команды и периодическую задачу.
    """
    if not BOT_TOKEN:
        raise SystemExit("❌ Укажите BOT_TOKEN в переменной окружения.")
    if not CHANNEL_ID:
        logger.warning("⚠️ CHANNEL_ID не задан. Укажите @username канала или ID вида -100xxxxxxxxxxxx")
    if not WEBHOOK_URL:
        raise SystemExit("❌ Укажите WEBHOOK_URL в переменной окружения (например, https://your-bot.onrender.com).")

    init_db()

    # Создаем приложение PTB
    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("checknow", checknow))

    # Настраиваем периодическую задачу
    app.job_queue.run_repeating(check_sources_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    # Регистрируем команды в Telegram UI
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Поприветствовать"),
            BotCommand("ping", "Проверить, жив ли бот"),
            BotCommand("checknow", "Проверить источники сейчас"),
        ]
    )

    # Настраиваем webhook
    webhook_path = "/webhook"  # Произвольный путь для webhook
    full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"
    await app.bot.set_webhook(url=full_webhook_url)
    logger.info(f"Webhook установлен на {full_webhook_url}")

    # Запускаем приложение с webhook
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
    )

if __name__ == "__main__":
    asyncio.run(main())
