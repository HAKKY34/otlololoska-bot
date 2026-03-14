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
from googletrans import Translator
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import feedparser
import time

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN", "8776445236:AAHiSvhgKMjvLDlTvNVrWr9ozC18XwQY8J4")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003419109291"))
DATABASE_PATH = "news_bot.db"

# Google Custom Search API (бесплатно 100 запросов/день)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")  # Получи здесь: https://developers.google.com/custom-search/v1/introduction
GOOGLE_CX = os.getenv("GOOGLE_CX", "")  # ID поисковой системы: https://programmablesearchengine.google.com/

# Интервал между постами
POST_INTERVAL_MINUTES = 72  # ~20 постов в день
CHECK_INTERVAL_MINUTES = 15

# Таймауты
REQUEST_TIMEOUT = 10
PARSER_TIMEOUT = 25

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
translator = Translator()

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
                  summary TEXT,
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
    c.execute("INSERT OR IGNORE INTO news_queue (id, title, url, summary, source) VALUES (?, ?, ?, ?, ?)",
              (news_id, news_item['title'], news_item['url'], news_item.get('summary', ''), news_item['source']))
    conn.commit()
    conn.close()
    logger.info(f"➕ Добавлено в очередь: {news_item['title'][:50]}...")

def get_from_queue():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, url, summary, source FROM news_queue ORDER BY found_at ASC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'id': row[0],
            'title': row[1],
            'url': row[2],
            'summary': row[3],
            'source': row[4]
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

# === ФУНКЦИИ ОБРАБОТКИ ТЕКСТА ===

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = text.replace('—', '-').replace('–', '-')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

async def translate_text(text: str) -> str:
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

async def rephrase_text(text: str) -> str:
    if not text:
        return text
    emojis = ["🔥", "⚡️", "🎮", "👀", "🤔", "💥", "📢", "🕹️", "🎯", "💬"]
    if random.random() > 0.5:
        emoji = random.choice(emojis)
        return f"{emoji} {text}"
    return text

# === ПОИСК КАРТИНКИ ===
def search_image(query: str) -> str | None:
    """
    Ищет картинку через Google Custom Search API
    Бесплатно: 100 запросов/день [citation:1][citation:4]
    """
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logger.warning("⚠️ Google API не настроен, поиск картинок отключён")
        return None
    
    try:
        # Очищаем запрос от лишних символов
        search_query = re.sub(r'[^\w\s]', ' ', query)
        search_query = quote(search_query[:100])  # Ограничиваем длину
        
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            'key': GOOGLE_API_KEY,
            'cx': GOOGLE_CX,
            'q': search_query,
            'searchType': 'image',
            'num': 3,  # Просим 3 картинки
            'imgSize': 'medium',  # Не слишком большие
            'fileType': 'jpg,png',  # Только эти форматы
            'safe': 'active'  # Безопасный поиск
        }
        
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if 'items' in data:
            for item in data['items']:
                image_url = item['link']
                
                # Базовая проверка на водяные знаки (по домену)
                bad_domains = ['shutterstock', 'istock', 'gettyimages', 'depositphotos', '123rf']
                if not any(domain in image_url.lower() for domain in bad_domains):
                    # Проверяем, что картинка доступна
                    img_check = requests.head(image_url, timeout=5)
                    if img_check.status_code == 200:
                        logger.info(f"🖼 Найдена картинка: {image_url[:100]}...")
                        return image_url
        
        logger.info("❌ Картинка не найдена")
        return None
        
    except Exception as e:
        logger.error(f"Ошибка поиска картинки: {e}")
        return None

# === ПАРСЕРЫ ===
async def parse_stopgame():
    news = []
    try:
        feed = feedparser.parse("https://stopgame.ru/rss/news.xml")
        for entry in feed.entries[:10]:
            if is_posted(entry.link) or is_in_queue(entry.link):
                continue
            summary = clean_text(entry.get('summary', ''))[:300]
            news.append({
                'title': entry.title,
                'url': entry.link,
                'summary': summary,
                'source': 'stopgame.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка stopgame.ru: {e}")
    return news

async def parse_gamemag():
    news = []
    try:
        url = "https://gamemag.ru"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
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
            summary = ""
            p_tag = item.find('p')
            if p_tag:
                summary = p_tag.get_text(strip=True)[:300]
            news.append({
                'title': title,
                'url': link,
                'summary': summary,
                'source': 'gamemag.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка gamemag.ru: {e}")
    return news

async def parse_dtf():
    news = []
    try:
        url = "https://dtf.ru/tag/steam"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('article')
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
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'dtf.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка dtf.ru: {e}")
    return news

async def parse_shazoo():
    news = []
    try:
        url = "https://shazoo.ru/tags/169/steam"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('div', class_=re.compile(r'post|item'))
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
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'shazoo.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка shazoo.ru: {e}")
    return news

async def parse_playground():
    news = []
    try:
        url = "https://www.playground.ru/news"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('div', class_=re.compile(r'news|post|item'))
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
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'playground.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка playground.ru: {e}")
    return news

async def parse_playground_freebies():
    news = []
    try:
        url = "https://www.playground.ru/news/freebies"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('div', class_=re.compile(r'news|post|item'))
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
            news.append({
                'title': f"🎁 {title}",
                'url': link,
                'summary': 'Раздача или скидка',
                'source': 'playground-freebies'
            })
    except Exception as e:
        logger.error(f"Ошибка playground.ru/freebies: {e}")
    return news

async def parse_championat():
    news = []
    try:
        url = "https://www.championat.com/tags/29577-steam/news/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('div', class_=re.compile(r'news-item'))
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
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'championat.com'
            })
    except Exception as e:
        logger.error(f"Ошибка championat.com: {e}")
    return news

async def parse_iz():
    news = []
    try:
        url = "https://iz.ru/tag/steam"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.find_all('div', class_=re.compile(r'news-item|article'))
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
            news.append({
                'title': title,
                'url': link,
                'summary': '',
                'source': 'iz.ru'
            })
    except Exception as e:
        logger.error(f"Ошибка iz.ru: {e}")
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
    logger.info("🔍 Сканирую 8 источников...")
    
    parsers = [
        run_parser_with_timeout(parse_stopgame),
        run_parser_with_timeout(parse_gamemag),
        run_parser_with_timeout(parse_dtf),
        run_parser_with_timeout(parse_shazoo),
        run_parser_with_timeout(parse_playground),
        run_parser_with_timeout(parse_playground_freebies),
        run_parser_with_timeout(parse_championat),
        run_parser_with_timeout(parse_iz)
    ]
    
    results = await asyncio.gather(*parsers)
    
    total_new = 0
    for result in results:
        for news_item in result:
            add_to_queue(news_item)
            total_new += 1
    
    queue_size = get_queue_size()
    logger.info(f"📊 Добавлено в очередь: {total_new} новостей. Всего в очереди: {queue_size}")

# === ПУБЛИКАЦИЯ С КАРТИНКОЙ ===
async def prepare_post(news_item):
    """Готовит пост с картинкой"""
    title = await translate_text(news_item['title'])
    title = await rephrase_text(title)
    
    summary = news_item.get('summary', '')
    if summary:
        summary = await translate_text(summary)
    
    # Ищем картинку по заголовку
    image_url = None
    if GOOGLE_API_KEY and GOOGLE_CX:
        logger.info(f"🔍 Ищу картинку для: {title[:50]}...")
        image_url = search_image(title)
        if image_url:
            # Небольшая задержка, чтобы не спамить API
            await asyncio.sleep(1)
    
    post = f"<b>{title}</b>\n\n"
    if summary:
        post += f"{summary}\n\n"
    
    post += "❤️ / 👎\n\n"
    post += '👉 <a href="https://t.me/gamesdevil">Game Devil</a>'
    
    return post, image_url

async def publish_from_queue():
    """Публикует одну новость из очереди с блокировкой от двойных постов"""
    global _publishing_lock
    
    # Блокировка от двойной публикации [citation:7]
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
        
        # Готовим пост и ищем картинку
        post_text, image_url = await prepare_post(news_item)
        
        # Публикуем с картинкой или без
        try:
            if image_url:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=image_url,
                    caption=post_text,
                    parse_mode="HTML"
                )
                logger.info(f"🖼 Опубликовано с фото")
            else:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_text,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
                logger.info(f"📝 Опубликовано без фото")
            
            # Отмечаем как опубликованное
            mark_as_posted(news_item['url'], news_item['title'], news_item['source'])
            remove_from_queue(news_item['id'])
            update_last_post_time()
            
            logger.info(f"✅ Успешно: {news_item['title'][:50]}...")
            logger.info(f"📊 Осталось в очереди: {get_queue_size()}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка публикации: {e}")
            return False
            
    finally:
        # Снимаем блокировку [citation:7]
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
        # Проверяем каждые 2 минуты, но публикация только когда проходит интервал
        await asyncio.sleep(120)

# === КОМАНДЫ ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    queue_size = get_queue_size()
    google_status = "✅" if GOOGLE_API_KEY and GOOGLE_CX else "❌"
    
    await message.answer(
        f"<b>📰 Game Devil News Bot</b>\n\n"
        f"✅ Мониторю 8 игровых сайтов 24/7\n"
        f"🖼 Поиск картинок: {google_status}\n"
        f"⏱ Интервал: {POST_INTERVAL_MINUTES} мин (~{1440//POST_INTERVAL_MINUTES} постов/день)\n"
        f"🔍 Проверка источников: каждые {CHECK_INTERVAL_MINUTES} мин\n"
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
        await message.answer("❌ Не удалось опубликовать (нет новостей, рано или идёт другая публикация)")

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
    logger.info("🤖 Бот запущен с 8 источниками и поиском картинок")
    logger.info(f"⏱ Интервал: {POST_INTERVAL_MINUTES} мин (~{1440//POST_INTERVAL_MINUTES} постов/день)")
    await dp.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
