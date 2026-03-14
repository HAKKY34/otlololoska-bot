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
    
    # Опубликованные новости
    c.execute('''CREATE TABLE IF NOT EXISTS posted_news
                 (id TEXT PRIMARY KEY,
                  title TEXT,
                  url TEXT UNIQUE,
                  source TEXT,
                  published_at TIMESTAMP,
                  posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Время последнего поста
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

def mark_as_posted(news_id: str, title: str, url: str, source: str):
    """Отмечает новость как опубликованную"""
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
    """Очищает текст от HTML и лишних пробелов"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = text.replace('—', '-').replace('–', '-')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def extract_game_names(text: str) -> list:
    """Извлекает названия игр (слова с заглавных, не переводим)"""
    # Простая эвристика: слова длиной >3, начинающиеся с заглавной
    words = text.split()
    games = []
    for i, word in enumerate(words):
        if len(word) > 3 and word[0].isupper() and word[1:2].islower():
            # Проверяем, что это не начало предложения
            if i == 0 or words[i-1].endswith(('.', '!', '?')):
                continue
            games.append(word)
    return games

async def translate_text(text: str, preserve_game_names: bool = True) -> str:
    """Переводит текст, сохраняя названия игр"""
    if not text or len(text) < 20:
        return text
    
    try:
        # Определяем язык
        detected = await translator.detect(text)
        if detected.lang == 'ru':
            return text
        
        # Сохраняем названия игр
        game_names = extract_game_names(text) if preserve_game_names else []
        
        # Переводим
        translated = await translator.translate(text, dest='ru')
        result = translated.text
        
        # Возвращаем названия игр (упрощённо)
        for game in game_names:
            if game.lower() in text.lower() and game not in result:
                result += f" ({game})"
        
        return clean_text(result)
    except Exception as e:
        logger.error(f"Ошибка перевода: {e}")
        return text

async def rephrase_text(text: str) -> str:
    """
    Делает текст уникальным через перефразирование
    Используем шаблонный метод (без нейросетей для экономии)
    """
    if not text:
        return text
    
    # Набор шаблонов для перефразирования
    templates = [
        "{}",
        "Новость: {}",
        "🔥 {}",
        "⚡️ {}",
        "Интересное: {}",
        "Кстати, {}",
        "У нас новость: {}",
        "🤔 {}",
        "🎮 {}",
        "👀 {}"
    ]
    
    # Случайный шаблон
    template = random.choice(templates)
    
    # Удаляем восклицательные знаки в конце (для разнообразия)
    text = re.sub(r'!+$', '', text)
    
    return template.format(text)

# === ПАРСЕРЫ САЙТОВ ===

async def parse_stopgame():
    """Парсит stopgame.ru через RSS"""
    news = []
    try:
        feed = feedparser.parse("https://stopgame.ru/rss/news.xml")
        for entry in feed.entries[:10]:
            if is_posted(entry.link):
                continue
            
            # Очищаем описание
            summary = clean_text(entry.get('summary', ''))
            
            # Ищем картинку
            image_url = None
            if 'media_content' in entry:
                for media in entry.media_content:
                    if media.get('type', '').startswith('image'):
                        image_url = media.get('url')
                        break
            
            news.append({
                'title': entry.title,
                'url': entry.link,
                'summary': summary,
                'image_url': image_url,
                'source': 'stopgame.ru',
                'raw_date': entry.get('published', '')
            })
        logger.info(f"stopgame.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка stopgame.ru: {e}")
    return news

async def parse_gamemag():
    """Парсит gamemag.ru (HTML)"""
    news = []
    try:
        url = "https://gamemag.ru"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Ищем блоки новостей
        items = soup.find_all('div', class_=re.compile(r'news|item|post'))
        
        for item in items[:15]:
            # Ищем ссылку
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link):
                continue
            
            # Заголовок
            title = link_tag.get_text(strip=True)
            if not title or len(title) < 10:
                continue
            
            # Описание
            summary = ""
            summary_tag = item.find('p') or item.find('div', class_=re.compile(r'desc|text'))
            if summary_tag:
                summary = summary_tag.get_text(strip=True)[:300]
            
            # Картинка
            image_url = None
            img_tag = item.find('img')
            if img_tag and img_tag.get('src'):
                image_url = urljoin(url, img_tag['src'])
            
            news.append({
                'title': title,
                'url': link,
                'summary': summary,
                'image_url': image_url,
                'source': 'gamemag.ru',
                'raw_date': ''
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
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # DTF использует data-атрибуты
        items = soup.find_all('article', class_=re.compile(r'content'))
        
        for item in items[:15]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title:
                continue
            
            # Описание
            summary = ""
            summary_tag = item.find('div', class_=re.compile(r'text|desc'))
            if summary_tag:
                summary = summary_tag.get_text(strip=True)[:300]
            
            # Картинка
            image_url = None
            img_tag = item.find('img')
            if img_tag and img_tag.get('src'):
                image_url = urljoin(url, img_tag['src'])
            
            news.append({
                'title': title,
                'url': link,
                'summary': summary,
                'image_url': image_url,
                'source': 'dtf.ru',
                'raw_date': ''
            })
        
        logger.info(f"dtf.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка dtf.ru: {e}")
    return news

async def parse_shazoo():
    """Парсит shazoo.ru/tags/169/steam"""
    news = []
    try:
        url = "https://shazoo.ru/tags/169/steam"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = soup.find_all('div', class_=re.compile(r'post|item'))
        
        for item in items[:15]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title:
                continue
            
            # Описание
            summary = ""
            summary_tag = item.find('p') or item.find('div', class_=re.compile(r'desc|text'))
            if summary_tag:
                summary = summary_tag.get_text(strip=True)[:300]
            
            # Картинка
            image_url = None
            img_tag = item.find('img')
            if img_tag and img_tag.get('src'):
                image_url = urljoin(url, img_tag['src'])
            
            news.append({
                'title': title,
                'url': link,
                'summary': summary,
                'image_url': image_url,
                'source': 'shazoo.ru',
                'raw_date': ''
            })
        
        logger.info(f"shazoo.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка shazoo.ru: {e}")
    return news

async def parse_playground():
    """Парсит playground.ru/news"""
    news = []
    try:
        url = "https://www.playground.ru/news"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = soup.find_all('div', class_=re.compile(r'news|post|item'))
        
        for item in items[:15]:
            link_tag = item.find('a', href=True)
            if not link_tag:
                continue
            
            link = urljoin(url, link_tag['href'])
            if is_posted(link):
                continue
            
            title = link_tag.get_text(strip=True)
            if not title:
                continue
            
            # Описание
            summary = ""
            summary_tag = item.find('p') or item.find('div', class_=re.compile(r'desc|text'))
            if summary_tag:
                summary = summary_tag.get_text(strip=True)[:300]
            
            # Картинка
            image_url = None
            img_tag = item.find('img')
            if img_tag and img_tag.get('src'):
                image_url = urljoin(url, img_tag['src'])
            
            news.append({
                'title': title,
                'url': link,
                'summary': summary,
                'image_url': image_url,
                'source': 'playground.ru',
                'raw_date': ''
            })
        
        logger.info(f"playground.ru: {len(news)} новых")
    except Exception as e:
        logger.error(f"Ошибка playground.ru: {e}")
    return news

# === СБОР ВСЕХ НОВОСТЕЙ ===
async def fetch_all_news():
    """Собирает новости из всех источников"""
    all_news = []
    
    # Парсим все источники параллельно
    parsers = [
        parse_stopgame(),
        parse_gamemag(),
        parse_dtf(),
        parse_shazoo(),
        parse_playground()
    ]
    
    results = await asyncio.gather(*parsers, return_exceptions=True)
    
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Ошибка парсера: {result}")
        elif isinstance(result, list):
            all_news.extend(result)
    
    logger.info(f"📊 Всего новых новостей: {len(all_news)}")
    
    # Перемешиваем, чтобы не было доминирования одного источника
    random.shuffle(all_news)
    return all_news

# === ПОДГОТОВКА ПОСТА ===
async def prepare_post(news_item):
    """Переводит, перефразирует и готовит пост"""
    
    # Заголовок
    title = await translate_text(news_item['title'])
    title = await rephrase_text(title)
    
    # Описание
    summary = news_item['summary']
    if summary and len(summary) > 50:
        summary = await translate_text(summary)
        summary = await rephrase_text(summary)
    
    # Формируем пост
    post = f"<b>{title}</b>\n\n"
    if summary:
        post += f"{summary}\n\n"
    
    # Подпись
    post += "❤️ / 👎\n\n"
    post += '👉 <a href="https://t.me/gamesdevil">Game Devil</a>'
    
    return {
        'text': post,
        'image': news_item.get('image_url'),
        'url': news_item['url'],
        'title': title,
        'source': news_item['source']
    }

# === ПУБЛИКАЦИЯ ===
async def publish_post(prepared_post):
    """Публикует пост в канал"""
    try:
        if prepared_post['image']:
            try:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=prepared_post['image'],
                    caption=prepared_post['text'],
                    parse_mode="HTML"
                )
                logger.info(f"🟢 Опубликовано с фото: {prepared_post['title'][:50]}...")
                return True
            except Exception as e:
                logger.warning(f"⚠️ Не удалось отправить с фото: {e}")
        
        # Без фото
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=prepared_post['text'],
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        logger.info(f"🟢 Опубликовано без фото: {prepared_post['title'][:50]}...")
        return True
        
    except Exception as e:
        logger.error(f"🔴 Ошибка публикации: {e}")
        return False

# === ОСНОВНАЯ ЗАДАЧА ===
async def news_job():
    """Главная задача: сбор и публикация"""
    logger.info("🚀 Запуск сбора новостей")
    
    # Проверяем интервал
    if not can_post_now():
        next_post = get_last_post_time() + timedelta(hours=MIN_INTERVAL_HOURS)
        logger.info(f"⏳ Слишком рано. Следующий пост после {next_post.strftime('%H:%M')}")
        return
    
    # Собираем новости
    all_news = await fetch_all_news()
    
    if not all_news:
        logger.info("📭 Новостей нет")
        return
    
    # Берём первую новость
    news_item = all_news[0]
    
    # Готовим пост
    prepared = await prepare_post(news_item)
    
    # Публикуем
    success = await publish_post(prepared)
    
    if success:
        # Генерируем ID
        news_id = hashlib.md5(news_item['url'].encode()).hexdigest()
        mark_as_posted(news_id, news_item['title'], news_item['url'], news_item['source'])
        update_last_post_time()
        logger.info(f"✅ Опубликовано: {news_item['url']}")

# === ПЛАНИРОВЩИК ===
async def scheduler():
    """Запускает сбор по расписанию"""
    logger.info(f"⏰ Планировщик запущен (интервал {CHECK_INTERVAL_MINUTES} мин)")
    
    while True:
        now = datetime.now()
        logger.info(f"🔍 Проверка источников в {now.strftime('%H:%M')}")
        
        await news_job()
        
        # Ждём до следующей проверки
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

# === КОМАНДЫ ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer(
        "<b>📰 Новостной бот для @gamesdevil</b>\n\n"
        "Я автоматически собираю новости из игровых источников "
        "и публикую их в канал.\n\n"
        f"⏱ Интервал: {MIN_INTERVAL_HOURS} час между постами\n"
        f"🔍 Проверка: каждые {CHECK_INTERVAL_MINUTES} мин\n"
        f"📚 Источников: 5\n\n"
        "Команды:\n"
        "/stats — статистика\n"
        "/post — ручная публикация",
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
        f"🕐 Последний: {last or 'никогда'}\n"
        f"⏱ Интервал: {MIN_INTERVAL_HOURS} ч",
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
    logger.info("🌐 Health check сервер запущен")

# === MAIN ===
async def main():
    init_database()
    await run_health_server()
    asyncio.create_task(scheduler())
    logger.info("🤖 Бот запущен")
    await dp.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
