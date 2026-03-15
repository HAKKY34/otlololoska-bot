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
from urllib.parse import urlparse, urljoin, quote
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import quote_html  # Правильный импорт для 2.x
from aiohttp import web
import feedparser
import json
import time

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN", "8776445236:AAHiSvhgKMjvLDlTvNVrWr9ozC18XwQY8J4")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003419109291"))
DATABASE_PATH = "news_bot.db"

# Интервал между постами (в минутах) - 72 мин = 20 постов/день
POST_INTERVAL_MINUTES = 72
CHECK_INTERVAL_MINUTES = 15  # Проверка источников
PARSER_TIMEOUT = 20

# Флаг для предотвращения двойной публикации
_publishing_lock = False

# --- ИНИЦИАЛИЗАЦИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# === БАЗА ДАННЫХ ===
def init_database():
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
    
    c.execute('''CREATE TABLE IF NOT EXISTS news_queue
                 (id TEXT PRIMARY KEY,
                  title TEXT,
                  url TEXT UNIQUE,
                  content TEXT,
                  image_url TEXT,
                  source TEXT,
                  found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def is_posted(url: str) -> bool:
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM posted_news WHERE url = ?", (url,))
    result = c.fetchone() is not None
    conn.close()
    return result

def is_in_queue(url: str) -> bool:
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM news_queue WHERE url = ?", (url,))
    result = c.fetchone() is not None
    conn.close()
    return result

def add_to_queue(news_item):
    news_id = hashlib.md5(news_item['url'].encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO news_queue (id, title, url, content, image_url, source) VALUES (?, ?, ?, ?, ?, ?)",
              (news_id, news_item['title'], news_item['url'], news_item.get('content', ''), 
               news_item.get('image_url', ''), news_item['source']))
    conn.commit()
    conn.close()
    logger.info(f"➕ Добавлено в очередь: {news_item['title'][:50]}...")

def get_from_queue():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, url, content, image_url, source FROM news_queue ORDER BY found_at ASC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'id': row[0],
            'title': row[1],
            'url': row[2],
            'content': row[3],
            'image_url': row[4],
            'source': row[5]
        }
    return None

def remove_from_queue(news_id: str):
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM news_queue WHERE id = ?", (news_id,))
    conn.commit()
    conn.close()

def get_queue_size() -> int:
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM news_queue")
    result = c.fetchone()[0]
    conn.close()
    return result

def get_last_post_time() -> datetime:
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_state WHERE key = 'last_post_time'")
    row = c.fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row[0])
    return datetime.min

def update_last_post_time():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
              ('last_post_time', datetime.now().isoformat()))
    conn.commit()
    conn.close()

def mark_as_posted(url: str, title: str, source: str):
    news_id = hashlib.md5(url.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted_news (id, title, url, source) VALUES (?, ?, ?, ?)",
              (news_id, title, url, source))
    conn.commit()
    conn.close()

# === ФУНКЦИИ ДЛЯ ОБРАБОТКИ ТЕКСТА ===

def clean_text(text: str) -> str:
    """Очищает текст от HTML и мусора"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    # Убираем "Читать дальше" и подобное
    text = re.sub(r'Читать (дальше|полностью).*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'Подробнее.*$', '', text, flags=re.IGNORECASE)
    # Заменяем длинные тире
    text = text.replace('—', '-').replace('–', '-')
    return text.strip()

async def rewrite_text(title: str, content: str) -> str:
    """
    Делает рерайт текста и форматирует для Telegram
    """
    try:
        # Очищаем
        cleaned_title = clean_text(title)
        cleaned_content = clean_text(content)[:500]
        
        # Добавляем эмодзи для красоты
        emojis = ["🔥", "⚡️", "🎮", "👀", "🤔", "💥", "📢"]
        if random.random() > 0.5:
            cleaned_title = f"{random.choice(emojis)} {cleaned_title}"
        
        # Форматируем с экранированием
        formatted_title = quote_html(cleaned_title)
        formatted_content = quote_html(cleaned_content)
        
        result = f"<b>{formatted_title}</b>\n\n"
        result += f"{formatted_content}"
        
        return result
        
    except Exception as e:
        logger.error(f"Ошибка рерайта: {e}")
        # Запасной вариант
        return f"<b>{quote_html(title)}</b>\n\n{quote_html(content[:500])}"

# === ПАРСЕРЫ RSS (только с картинками) ===

def extract_image_from_entry(entry):
    """Достаёт картинку из RSS entry"""
    # Пробуем media:content
    if hasattr(entry, 'media_content') and entry.media_content:
        for media in entry.media_content:
            if isinstance(media, dict) and media.get('medium') == 'image':
                return media.get('url')
            if isinstance(media, dict) and media.get('type', '').startswith('image/'):
                return media.get('url')
    
    # Пробуем enclosure
    if hasattr(entry, 'enclosures') and entry.enclosures:
        for enc in entry.enclosures:
            if hasattr(enc, 'type') and enc.type.startswith('image/'):
                return enc.href
    
    # Пробуем media:thumbnail
    if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
        return entry.media_thumbnail[0].get('url')
    
    return None

async def parse_stopgame():
    """stopgame.ru - RSS с картинками"""
    news = []
    try:
        feed = feedparser.parse("https://stopgame.ru/rss/news.xml")
        for entry in feed.entries[:10]:
            if is_posted(entry.link) or is_in_queue(entry.link):
                continue
            
            # Берём картинку
            image_url = extract_image_from_entry(entry)
            if not image_url:
                continue  # Пропускаем если нет картинки
            
            # Очищаем контент
            content = clean_text(entry.get('summary', ''))
            
            news.append({
                'title': entry.title,
                'url': entry.link,
                'content': content,
                'image_url': image_url,
                'source': 'stopgame.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка stopgame.ru: {e}")
    return news

async def parse_igromania():
    """igromania.ru - RSS с картинками"""
    news = []
    try:
        feed = feedparser.parse("https://www.igromania.ru/feed/news/")
        for entry in feed.entries[:10]:
            if is_posted(entry.link) or is_in_queue(entry.link):
                continue
            
            image_url = extract_image_from_entry(entry)
            if not image_url:
                continue
            
            content = clean_text(entry.get('description', ''))
            
            news.append({
                'title': entry.title,
                'url': entry.link,
                'content': content,
                'image_url': image_url,
                'source': 'igromania.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка igromania.ru: {e}")
    return news

async def parse_gamemag():
    """gamemag.ru - парсим HTML, ищем картинки"""
    news = []
    try:
        url = "https://gamemag.ru"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = soup.find_all('div', class_=re.compile(r'news-item|post|article'))
        
        for item in items[:10]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link) or is_in_queue(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title or len(title) < 10:
                continue
            
            # Ищем картинку
            img_tag = item.find('img')
            image_url = None
            if img_tag and img_tag.get('src'):
                image_url = urljoin(url, img_tag['src'])
            
            if not image_url:
                continue  # Пропускаем если нет картинки
            
            # Ищем текст
            content = ""
            p_tag = item.find('p')
            if p_tag:
                content = p_tag.get_text(strip=True)
            
            news.append({
                'title': title,
                'url': link,
                'content': content,
                'image_url': image_url,
                'source': 'gamemag.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка gamemag.ru: {e}")
    return news

async def parse_vgtimes():
    """vgtimes.ru/free - парсим бесплатные игры"""
    news = []
    try:
        url = "https://vgtimes.ru/free/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = soup.find_all('div', class_=re.compile(r'post|article'))
        
        for item in items[:10]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link) or is_in_queue(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title:
                continue
            
            # Ищем картинку
            img_tag = item.find('img')
            image_url = None
            if img_tag and img_tag.get('src'):
                image_url = urljoin(url, img_tag['src'])
            
            if not image_url:
                continue
            
            # Ищем текст
            content = ""
            p_tag = item.find('p')
            if p_tag:
                content = p_tag.get_text(strip=True)
            
            news.append({
                'title': title,
                'url': link,
                'content': content,
                'image_url': image_url,
                'source': 'vgtimes.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка vgtimes.ru: {e}")
    return news

# === СБОР НОВОСТЕЙ ===
async def run_parser_with_timeout(parser_func):
    try:
        return await asyncio.wait_for(parser_func(), timeout=PARSER_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ Парсер {parser_func.__name__} превысил таймаут")
        return []
    except Exception as e:
        logger.error(f"⚠️ Ошибка в парсере {parser_func.__name__}: {e}")
        return []

async def collect_news_to_queue():
    """Собирает новости со всех сайтов"""
    logger.info("🔍 Сканирую источники...")
    
    parsers = [
        run_parser_with_timeout(parse_stopgame),
        run_parser_with_timeout(parse_igromania),
        run_parser_with_timeout(parse_gamemag),
        run_parser_with_timeout(parse_vgtimes),
    ]
    
    results = await asyncio.gather(*parsers)
    
    total_new = 0
    for result in results:
        for news_item in result:
            add_to_queue(news_item)
            total_new += 1
    
    queue_size = get_queue_size()
    logger.info(f"📊 Добавлено в очередь: {total_new} новостей. Всего в очереди: {queue_size}")

# === ПУБЛИКАЦИЯ ===
async def publish_from_queue():
    """Публикует одну новость из очереди"""
    global _publishing_lock
    
    if _publishing_lock:
        logger.warning("⚠️ Публикация уже выполняется, пропускаю")
        return False
    
    _publishing_lock = True
    
    try:
        # Проверяем интервал
        last_time = get_last_post_time()
        minutes_passed = (datetime.now() - last_time).total_seconds() / 60
        
        if minutes_passed < POST_INTERVAL_MINUTES:
            next_post = last_time + timedelta(minutes=POST_INTERVAL_MINUTES)
            logger.info(f"⏳ Интервал: прошло {minutes_passed:.0f} мин, нужно до {next_post.strftime('%H:%M')}")
            return False
        
        # Берём новость из очереди
        news_item = get_from_queue()
        if not news_item:
            logger.info("📭 Очередь пуста")
            return False
        
        logger.info(f"📝 Беру из очереди: {news_item['title'][:50]}...")
        
        # Переписываем
        post_text = await rewrite_text(news_item['title'], news_item['content'])
        
        # Добавляем подпись
        post_text += "\n\n❤️ / 👎\n\n"
        post_text += '👉 <a href="https://t.me/gamesdevil">Game Devil</a>'
        
        # Публикуем с картинкой
        try:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=news_item['image_url'],
                caption=post_text,
                parse_mode="HTML"
            )
            
            # Отмечаем как опубликованное
            mark_as_posted(news_item['url'], news_item['title'], news_item['source'])
            remove_from_queue(news_item['id'])
            update_last_post_time()
            
            logger.info(f"✅ Опубликовано с фото: {news_item['title'][:50]}...")
            logger.info(f"📊 Осталось в очереди: {get_queue_size()}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False
            
    finally:
        _publishing_lock = False

# === ПЛАНИРОВЩИКИ ===
async def collector_scheduler():
    logger.info(f"🔄 Коллектор запущен (интервал {CHECK_INTERVAL_MINUTES} мин)")
    while True:
        await collect_news_to_queue()
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

async def publisher_scheduler():
    logger.info(f"📢 Паблишер запущен (интервал {POST_INTERVAL_MINUTES} мин)")
    while True:
        await publish_from_queue()
        await asyncio.sleep(120)  # Проверяем каждые 2 минуты

# === КОМАНДЫ ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    queue_size = get_queue_size()
    
    await message.answer(
        f"<b>📰 Game Devil News Bot</b>\n\n"
        f"✅ Мониторю 4+ игровых сайта\n"
        f"🖼 Только новости с картинками\n"
        f"⏱ Интервал: {POST_INTERVAL_MINUTES} мин (~{1440//POST_INTERVAL_MINUTES} постов/день)\n"
        f"📚 В очереди сейчас: {queue_size} новостей\n\n"
        f"Команды:\n"
        f"/stats — статистика\n"
        f"/queue — очередь\n"
        f"/post — ручная публикация",
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
    c.execute("SELECT COUNT(*) FROM news_queue")
    queue = c.fetchone()[0]
    conn.close()
    
    await message.answer(
        f"<b>📊 Статистика</b>\n\n"
        f"📝 Всего опубликовано: {total}\n"
        f"⏳ В очереди: {queue}\n"
        f"🕐 Последний пост: {last or 'никогда'}",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['queue'])
async def cmd_queue(message: types.Message):
    queue_size = get_queue_size()
    await message.answer(f"📚 В очереди сейчас: {queue_size} новостей")

@dp.message_handler(commands=['post'])
async def cmd_post(message: types.Message):
    await message.answer("⏳ Принудительная публикация...")
    success = await publish_from_queue()
    if success:
        await message.answer("✅ Пост опубликован!")
    else:
        await message.answer("❌ Не удалось опубликовать (нет новостей или рано)")

# === HEALTH CHECK ===
async def handle_health(request):
    return web.Response(text=f"OK\nQueue: {get_queue_size()}\nLock: {_publishing_lock}")

async def run_health_server():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000))).start()
    logger.info("🌐 Health check запущен")

# === MAIN ===
async def main():
    init_database()
    await run_health_server()
    asyncio.create_task(collector_scheduler())
    asyncio.create_task(publisher_scheduler())
    logger.info("🤖 Новостной бот запущен")
    logger.info(f"⏱ Интервал: {POST_INTERVAL_MINUTES} мин (~{1440//POST_INTERVAL_MINUTES} постов/день)")
    await dp.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
