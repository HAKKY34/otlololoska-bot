import logging
import asyncio
import os
import sqlite3
import hashlib
import html
import re
import random
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from googletrans import Translator
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import feedparser

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN", "8776445236:AAHiSvhgKMjvLDlTvNVrWr9ozC18XwQY8J4")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003419109291"))
DATABASE_PATH = "news_bot.db"
MIN_INTERVAL_HOURS = 1  # Минимум 1 час между постами
CHECK_INTERVAL_MINUTES = 30  # Проверка источников каждые 30 минут

# Таймауты для Render (важно!)
REQUEST_TIMEOUT = 10  # секунд на запрос к сайту
PARSER_TIMEOUT = 15   # секунд на парсинг одного источника

# --- ИНИЦИАЛИЗАЦИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())
translator = Translator()

# === БАЗА ДАННЫХ ===
def init_database():
    """Создаёт таблицы в SQLite"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS posted_news
                 (id TEXT PRIMARY KEY,
                  title TEXT,
                  url TEXT UNIQUE,
                  source TEXT,
                  posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS bot_state
                 (key TEXT PRIMARY KEY,
                  value TEXT)''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def is_posted(url: str) -> bool:
    """Проверяет, публиковалась ли новость"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM posted_news WHERE url = ?", (url,))
    result = c.fetchone() is not None
    conn.close()
    return result

def mark_as_posted(url: str, title: str, source: str):
    """Отмечает новость как опубликованную"""
    news_id = hashlib.md5(url.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted_news (id, title, url, source) VALUES (?, ?, ?, ?)",
              (news_id, title, url, source))
    conn.commit()
    conn.close()

def get_last_post_time() -> datetime:
    """Время последнего поста"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_state WHERE key = 'last_post_time'")
    row = c.fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row[0])
    return datetime.min

def update_last_post_time():
    """Обновляет время последнего поста"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
              ('last_post_time', datetime.now().isoformat()))
    conn.commit()
    conn.close()

def can_post_now() -> bool:
    """Проверяет, прошёл ли минимальный интервал"""
    last = get_last_post_time()
    hours_passed = (datetime.now() - last).total_seconds() / 3600
    return hours_passed >= MIN_INTERVAL_HOURS

# === ФУНКЦИИ ОБРАБОТКИ ТЕКСТА ===

def clean_text(text: str) -> str:
    """Очищает текст от HTML"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = text.replace('—', '-').replace('–', '-')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

async def translate_text(text: str) -> str:
    """Переводит текст"""
    if not text or len(text) < 20:
        return text
    
    try:
        detected = await translator.detect(text)
        if detected.lang == 'ru':
            return text
        
        translated = await translator.translate(text, dest='ru')
        return clean_text(translated.text)
    except Exception as e:
        logger.error(f"Ошибка перевода: {e}")
        return text

# === ПАРСЕРЫ С ТАЙМАУТАМИ ===

async def parse_stopgame():
    """Парсит stopgame.ru через RSS"""
    news = []
    try:
        feed = feedparser.parse("https://stopgame.ru/rss/news.xml")
        for entry in feed.entries[:5]:
            if is_posted(entry.link):
                continue
            
            summary = clean_text(entry.get('summary', ''))[:300]
            
            news.append({
                'title': entry.title,
                'url': entry.link,
                'summary': summary,
                'source': 'stopgame.ru'
            })
        logger.info(f"stopgame.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка stopgame.ru: {e}")
    return news

async def parse_gamemag():
    """Парсит gamemag.ru"""
    news = []
    try:
        url = "https://gamemag.ru"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Простой поиск новостей
        items = soup.find_all('div', class_=re.compile(r'news-item|post|article'))
        
        for item in items[:5]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title or len(title) < 10:
                continue
            
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'gamemag.ru'
            })
        
        logger.info(f"gamemag.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка gamemag.ru: {e}")
    return news

async def parse_dtf():
    """Парсит dtf.ru/tag/steam"""
    news = []
    try:
        url = "https://dtf.ru/tag/steam"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = soup.find_all('article')
        
        for item in items[:5]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title:
                continue
            
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'dtf.ru'
            })
        
        logger.info(f"dtf.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка dtf.ru: {e}")
    return news

# === СБОР НОВОСТЕЙ С ТАЙМАУТАМИ ===
async def run_parser_with_timeout(parser_func):
    """Запускает парсер с таймаутом"""
    try:
        return await asyncio.wait_for(parser_func(), timeout=PARSER_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ Парсер {parser_func.__name__} превысил таймаут")
        return []
    except Exception as e:
        logger.error(f"⚠️ Ошибка в парсере {parser_func.__name__}: {e}")
        return []

async def fetch_all_news():
    """Собирает новости из всех источников"""
    all_news = []
    
    parsers = [
        run_parser_with_timeout(parse_stopgame),
        run_parser_with_timeout(parse_gamemag),
        run_parser_with_timeout(parse_dtf)
    ]
    
    results = await asyncio.gather(*parsers)
    
    for result in results:
        all_news.extend(result)
    
    logger.info(f"📊 Всего новых новостей: {len(all_news)}")
    random.shuffle(all_news)
    return all_news

# === ПОДГОТОВКА ПОСТА ===
async def prepare_post(news_item):
    """Готовит пост для публикации"""
    
    title = await translate_text(news_item['title'])
    summary = news_item['summary']
    if summary:
        summary = await translate_text(summary)
    
    post = f"<b>{title}</b>\n\n"
    if summary:
        post += f"{summary}\n\n"
    
    post += "❤️ / 👎\n\n"
    post += '👉 <a href="https://t.me/gamesdevil">Game Devil</a>'
    
    return post

# === ПУБЛИКАЦИЯ ===
async def publish_post(news_item, post_text):
    """Публикует пост в канал"""
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        logger.info(f"🟢 Опубликовано: {news_item['title'][:50]}...")
        return True
    except Exception as e:
        logger.error(f"🔴 Ошибка публикации: {e}")
        return False

# === ОСНОВНАЯ ЗАДАЧА ===
async def news_job():
    """Главная задача"""
    logger.info("🚀 Запуск сбора новостей")
    
    if not can_post_now():
        logger.info("⏳ Слишком рано, ждём")
        return
    
    all_news = await fetch_all_news()
    
    if not all_news:
        logger.info("📭 Новостей нет")
        return
    
    # Берём первую новость
    news_item = all_news[0]
    
    # Готовим пост
    post_text = await prepare_post(news_item)
    
    # Публикуем
    success = await publish_post(news_item, post_text)
    
    if success:
        mark_as_posted(news_item['url'], news_item['title'], news_item['source'])
        update_last_post_time()
        logger.info(f"✅ Успешно: {news_item['url']}")

# === ПЛАНИРОВЩИК ===
async def scheduler():
    """Запускает сбор по расписанию"""
    logger.info(f"⏰ Планировщик запущен")
    
    while True:
        now = datetime.now()
        logger.info(f"🔍 Проверка в {now.strftime('%H:%M')}")
        
        await news_job()
        
        # Ждём
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

# === КОМАНДЫ ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer(
        "<b>📰 Новостной бот</b>\n\n"
        "Я собираю новости и публикую их в канал.\n\n"
        f"⏱ Интервал: {MIN_INTERVAL_HOURS} ч\n"
        f"🔍 Проверка: каждые {CHECK_INTERVAL_MINUTES} мин",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['stats'])
async def cmd_stats(message: types.Message):
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM posted_news")
    total = c.fetchone()[0]
    c.execute("SELECT MAX(posted_at) FROM posted_news")
    last = c.fetchone()[0]
    conn.close()
    
    await message.answer(
        f"<b>📊 Статистика</b>\n\n"
        f"📝 Всего постов: {total}\n"
        f"🕐 Последний: {last or 'никогда'}",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['post'])
async def cmd_post(message: types.Message):
    await message.answer("⏳ Запускаю...")
    await news_job()
    await message.answer("✅ Готово")

# === HEALTH CHECK ===
async def handle_health(request):
    return web.Response(text="OK")

async def run_health_server():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000))).start()
    logger.info("🌐 Health check запущен")

async def main():
    init_database()
    await run_health_server()
    asyncio.create_task(scheduler())
    logger.info("🤖 Бот запущен")
    await dp.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
