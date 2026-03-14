import logging
import asyncio
import os
import requests
import time
import json
import sqlite3
import hashlib
import html
import re
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import feedparser
from urllib.parse import urlparse
import random
from googletrans import Translator  # Добавляем переводчик

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN", "8776445236:AAHiSvhgKMjvLDlTvNVrWr9ozC18XwQY8J4")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003419109291"))
DATABASE_PATH = "news_bot.db"

# --- РАСПИСАНИЕ ПУБЛИКАЦИЙ (6 раз в день) ---
PUBLISH_TIMES = ["09:00", "12:00", "15:00", "18:00", "21:00", "23:59"]

# --- ИСТОЧНИКИ НОВОСТЕЙ (RSS ленты) ---
RSS_FEEDS = [
    # Игровые порталы
    "https://3dnews.ru/news/rss/",
    "https://stopgame.ru/rss/news.xml",
    "https://www.igromania.ru/rss/news.xml",
    "https://kanobu.ru/rss/",
    "https://app2top.ru/feed/",
    "https://dtf.ru/rss",
    "https://www.gamespot.com/feeds/news/",
    "https://www.pcgamer.com/rss/",
    "https://www.rockpapershotgun.com/feed",
    "https://www.gamedeveloper.com/rss.xml",
    
    # Reddit сообщества
    "https://www.reddit.com/r/gaming/.rss",
    "https://www.reddit.com/r/pcgaming/.rss",
    "https://www.reddit.com/r/Games/.rss",
    "https://www.reddit.com/r/Steam/.rss",
    "https://www.reddit.com/r/GameDeals/.rss",
    
    # Новости индустрии
    "https://www.eurogamer.net/?format=rss",
    "https://www.vg247.com/feed",
    "https://www.polygon.com/rss/index.xml",
    
    # Steam и платформы
    "https://steamcommunity.com/games/593110/announcements/",  # Steam Blog
]

# --- КЛЮЧЕВЫЕ СЛОВА ДЛЯ ФИЛЬТРАЦИИ (можно расширять) ---
KEYWORDS = [
    "steam", "стим", "игра", "game", "gaming", "игровой", 
    "вышла", "релиз", "обновление", "скидка", "распродажа",
    "бесплатно", "free", "giveaway", "раздача", "ключи",
    "инди", "indie", "новинка", "анонс", "трейлер",
    "создатель", "разработчик", "developer", "студия",
    "прохождение", "рекорд", "скорость", "100%",
    "патч", "фикс", "исправление", "мод", "модификация"
]

# --- ИНИЦИАЛИЗАЦИЯ ПЕРЕВОДЧИКА ---
translator = Translator()

# === ФУНКЦИИ ОБРАБОТКИ ТЕКСТА ===

def clean_text(text: str) -> str:
    """
    Очищает текст:
    - Заменяет длинные тире (—, –) на обычные дефисы (-)
    - Удаляет лишние пробелы
    """
    if not text:
        return ""
    
    # Заменяем длинные тире на обычные дефисы
    text = text.replace('—', '-').replace('–', '-')
    
    # Удаляем множественные пробелы
    text = re.sub(r'\s+', ' ', text)
    
    # Убираем пробелы в начале и конце
    text = text.strip()
    
    return text

async def translate_to_russian(text: str) -> str:
    """
    Переводит текст с английского на русский
    Если текст уже на русском или перевод не удался, возвращает оригинал
    """
    if not text or len(text) < 10:  # Не переводим слишком короткие тексты
        return text
    
    try:
        # Определяем язык
        detected = await translator.detect(text)
        
        # Если текст уже на русском, не переводим
        if detected.lang == 'ru':
            return text
        
        # Переводим на русский
        translated = await translator.translate(text, dest='ru', src='en')
        result = translated.text
        
        # Применяем очистку к переведенному тексту
        result = clean_text(result)
        
        logger.info(f"🟢 Переведено: {text[:30]}... -> {result[:30]}...")
        return result
        
    except Exception as e:
        logger.error(f"🔴 Ошибка перевода: {e}")
        return text  # Возвращаем оригинал при ошибке

async def translate_news_item(news_item: dict) -> dict:
    """
    Переводит заголовок и описание новости
    """
    # Переводим заголовок
    if news_item['title']:
        news_item['title'] = await translate_to_russian(news_item['title'])
    
    # Переводим описание
    if news_item['summary']:
        news_item['summary'] = await translate_to_russian(news_item['summary'])
    
    return news_item

# === ИНИЦИАЛИЗАЦИЯ ===
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
    """Создаёт таблицы в SQLite, если их нет"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    # Таблица опубликованных новостей
    c.execute('''CREATE TABLE IF NOT EXISTS posted_news
                 (id TEXT PRIMARY KEY,
                  title TEXT,
                  link TEXT,
                  source TEXT,
                  published_at TIMESTAMP,
                  posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Таблица источников (для статистики)
    c.execute('''CREATE TABLE IF NOT EXISTS sources
                 (name TEXT PRIMARY KEY,
                  url TEXT,
                  last_fetched TIMESTAMP,
                  total_posts INTEGER DEFAULT 0)''')
    
    # Таблица для хранения состояния
    c.execute('''CREATE TABLE IF NOT EXISTS bot_state
                 (key TEXT PRIMARY KEY,
                  value TEXT)''')
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def is_posted(news_id: str) -> bool:
    """Проверяет, публиковалась ли новость"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM posted_news WHERE id = ?", (news_id,))
    result = c.fetchone() is not None
    conn.close()
    return result

def mark_as_posted(news_id: str, title: str, link: str, source: str, pub_date: str):
    """Отмечает новость как опубликованную"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted_news (id, title, link, source, published_at) VALUES (?, ?, ?, ?, ?)",
              (news_id, title, link, source, pub_date))
    conn.commit()
    conn.close()

def get_last_post_time() -> datetime:
    """Получает время последнего успешного поста"""
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

# === ПАРСИНГ RSS ===
def parse_rss_feed(feed_url: str, max_items: int = 10):
    """Парсит RSS ленту и возвращает список новостей"""
    news_items = []
    try:
        logger.info(f"🟡 Парсинг RSS: {feed_url}")
        feed = feedparser.parse(feed_url)
        
        if feed.bozo:  # Ошибка парсинга
            logger.warning(f"⚠️ Ошибка парсинга {feed_url}: {feed.bozo_exception}")
            return []
        
        source_name = urlparse(feed_url).netloc.replace('www.', '')
        
        for entry in feed.entries[:max_items]:
            # Генерируем уникальный ID
            news_id = hashlib.md5(f"{entry.link}{entry.title}".encode()).hexdigest()
            
            # Пропускаем если уже постили
            if is_posted(news_id):
                continue
            
            # Извлекаем дату публикации
            pub_date = entry.get('published', entry.get('updated', ''))
            
            # Очищаем описание от HTML
            summary = entry.get('summary', entry.get('description', ''))
            summary = re.sub(r'<[^>]+>', '', summary)
            summary = html.unescape(summary)
            summary = re.sub(r'\s+', ' ', summary).strip()
            
            # Обрезаем до разумной длины
            if len(summary) > 300:
                summary = summary[:300].rsplit(' ', 1)[0] + '...'
            
            # Извлекаем картинку
            image_url = None
            if 'media_content' in entry:
                for media in entry.media_content:
                    if media.get('type', '').startswith('image'):
                        image_url = media.get('url')
                        break
            elif 'links' in entry:
                for link in entry.links:
                    if link.get('type', '').startswith('image'):
                        image_url = link.get('href')
                        break
            
            # Очищаем заголовок от длинных тире
            title = clean_text(entry.get('title', 'Без заголовка'))
            
            news_items.append({
                'id': news_id,
                'title': title,
                'link': entry.get('link', ''),
                'summary': summary,
                'image_url': image_url,
                'source': source_name,
                'published': pub_date,
                'raw_data': entry
            })
        
        logger.info(f"🟢 {feed_url}: найдено {len(news_items)} новых новостей")
        
    except Exception as e:
        logger.error(f"🔴 Ошибка при парсинге {feed_url}: {e}")
    
    return news_items

# === СБОР ВСЕХ НОВОСТЕЙ ===
async def fetch_all_news():
    """Собирает новости из всех источников и переводит их"""
    all_news = []
    
    for feed_url in RSS_FEEDS:
        news = parse_rss_feed(feed_url, max_items=5)
        all_news.extend(news)
        await asyncio.sleep(1)  # Не ддосим сервера
    
    # Переводим новости
    translated_news = []
    for news_item in all_news:
        translated_item = await translate_news_item(news_item)
        translated_news.append(translated_item)
        await asyncio.sleep(0.5)  # Не перегружаем API переводчика
    
    # Перемешиваем, чтобы не было доминации одного источника
    random.shuffle(translated_news)
    
    logger.info(f"📊 Всего собрано: {len(translated_news)} новых новостей (после перевода)")
    return translated_news

# === ПОИСК КАРТИНКИ ===
def search_image(query: str) -> str | None:
    """Ищет картинку по запросу (заглушка)"""
    # TODO: Добавить Google Custom Search API
    # Пока возвращаем None
    return None

# === ФОРМАТИРОВАНИЕ ПОСТА ===
def format_post(news_item) -> str:
    """Форматирует новость в пост для Telegram"""
    
    # Заголовок жирным
    post = f"<b>{news_item['title']}</b>\n\n"
    
    # Описание (уже переведено и очищено от длинных тире)
    if news_item['summary']:
        post += f"{news_item['summary']}\n\n"
    
    # Реакции (символами, не кнопками)
    post += "❤️ / 👎\n\n"
    
    # Подпись с ссылкой на канал
    post += '👉 <a href="https://t.me/gamesdevil">Game Devil</a>'
    
    return post

# === ПУБЛИКАЦИЯ ПОСТА ===
async def publish_news(news_item):
    """Публикует новость в канал"""
    try:
        post_text = format_post(news_item)
        
        # Пробуем отправить с картинкой
        if news_item.get('image_url'):
            try:
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=news_item['image_url'],
                    caption=post_text,
                    parse_mode="HTML"
                )
                logger.info(f"🟢 Опубликовано с фото: {news_item['title'][:50]}...")
                return True
            except Exception as e:
                logger.warning(f"⚠️ Не удалось отправить с фото: {e}")
        
        # Если нет картинки или ошибка, отправляем без фото
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        logger.info(f"🟢 Опубликовано без фото: {news_item['title'][:50]}...")
        return True
        
    except Exception as e:
        logger.error(f"🔴 Ошибка публикации: {e}")
        return False

# === ОСНОВНАЯ ЗАДАЧА ===
async def news_job():
    """Главная задача: сбор и публикация новостей"""
    logger.info("🚀 Запуск задачи сбора новостей")
    
    # Собираем новости
    all_news = await fetch_all_news()
    
    if not all_news:
        logger.info("📭 Новых новостей нет")
        return
    
    # Публикуем ТОЛЬКО 1 новость за раз (остальные подождут следующего раза)
    published = 0
    for news in all_news[:1]:  # Берем только первую новость
        success = await publish_news(news)
        if success:
            mark_as_posted(
                news['id'],
                news['title'],
                news['link'],
                news['source'],
                news.get('published', '')
            )
            published += 1
            break  # Останавливаемся после первой публикации
    
    update_last_post_time()
    logger.info(f"✅ Опубликовано {published} новостей")

# === ПЛАНИРОВЩИК ===
async def scheduler():
    """Проверяет время и запускает публикации по расписанию"""
    logger.info("⏰ Планировщик запущен")
    
    last_run_date = None
    last_run_time = None
    
    while True:
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_date = now.date()
        
        # Проверяем, нужно ли запускать публикацию
        if current_time in PUBLISH_TIMES:
            # Проверяем, не запускали ли мы уже в это время сегодня
            if last_run_date != current_date or last_run_time != current_time:
                logger.info(f"⏰ Настало время {current_time}, запускаю публикацию")
                await news_job()
                last_run_date = current_date
                last_run_time = current_time
                # Ждём минуту, чтобы не запустить повторно
                await asyncio.sleep(60)
        
        # Проверяем каждые 30 секунд
        await asyncio.sleep(30)

# === КОМАНДЫ ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Приветствие"""
    await message.answer(
        "<b>📰 Новостной бот для @gamesdevil</b>\n\n"
        "Я автоматически собираю новости из игровой индустрии "
        "и публикую их в канал по расписанию.\n\n"
        "✅ Новости переводятся на русский\n"
        "✅ Удаляются длинные тире\n"
        "✅ 1 пост за раз (6 раз в день)\n\n"
        "Доступные команды:\n"
        "/stats — статистика работы\n"
        "/post — ручная публикация (для админа)\n"
        "/sources — список источников",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['stats'])
async def cmd_stats(message: types.Message):
    """Статистика работы"""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM posted_news")
    total_posts = c.fetchone()[0]
    
    c.execute("SELECT COUNT(DISTINCT source) FROM posted_news")
    total_sources = c.fetchone()[0]
    
    c.execute("SELECT MAX(posted_at) FROM posted_news")
    last_post = c.fetchone()[0]
    
    conn.close()
    
    stats_text = (
        f"<b>📊 Статистика бота</b>\n\n"
        f"📝 Всего постов: {total_posts}\n"
        f"📡 Источников: {total_sources}\n"
        f"🕐 Последний пост: {last_post or 'никогда'}\n"
        f"⏰ Расписание: {', '.join(PUBLISH_TIMES)}"
    )
    
    await message.answer(stats_text, parse_mode="HTML")

@dp.message_handler(commands=['post'])
async def cmd_post(message: types.Message):
    """Ручной запуск публикации"""
    await message.answer("⏳ Запускаю сбор новостей...")
    await news_job()
    await message.answer("✅ Готово!")

@dp.message_handler(commands=['sources'])
async def cmd_sources(message: types.Message):
    """Список источников"""
    sources_text = "<b>📡 Источники новостей:</b>\n\n"
    for i, feed in enumerate(RSS_FEEDS[:10], 1):
        name = urlparse(feed).netloc.replace('www.', '')
        sources_text += f"{i}. {name}\n"
    
    if len(RSS_FEEDS) > 10:
        sources_text += f"\n...и ещё {len(RSS_FEEDS) - 10} источников"
    
    await message.answer(sources_text, parse_mode="HTML")

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
    # Инициализация
    init_database()
    
    # Запуск health check
    await run_health_server()
    
    # Запуск планировщика
    asyncio.create_task(scheduler())
    
    # Запуск бота
    logger.info("🤖 Бот запущен")
    await dp.start_polling()

if __name__ == "__main__":
    asyncio.run(main())
