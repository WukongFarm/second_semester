import datetime
import logging
import os
import xml.etree.ElementTree as ET
import aiohttp
from io import BytesIO
import re
import asyncio
import random
import time
from collections import defaultdict
import pickle
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from transformers import pipeline
from googletrans import Translator  # Google Translator

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEN_API_KEY = os.getenv("GEN_API_KEY")
QRCODER_API_KEY = os.getenv("QRCODER_API_KEY")

GEN_API_URL = "https://api.gen-api.ru/api/v1/networks/glm-5"

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info(f"BOT_TOKEN загружен: {'да' if BOT_TOKEN else 'нет'}")
logger.info(f"GEN_API_KEY загружен: {'да' if GEN_API_KEY else 'нет'}")
logger.info(f"QRCODER_API_KEY загружен: {'да' if QRCODER_API_KEY else 'нет'}")

# ========== МОДЕЛЬ ТОНАЛЬНОСТИ ==========
try:
    sentiment_pipeline = pipeline(
        "sentiment-analysis",
        model="blanchefort/rubert-base-cased-sentiment",
        tokenizer="blanchefort/rubert-base-cased-sentiment"
    )
    logger.info("✅ Модель тональности загружена.")
except Exception as e:
    logger.error(f"❌ Не удалось загрузить модель тональности: {e}")
    sentiment_pipeline = None

# ========== ИНИЦИАЛИЗАЦИЯ ПЕРЕВОДЧИКА ==========
translator = Translator()


# ========== АНТИСПАМ СИСТЕМА ==========

class AntiSpam:
    def __init__(self):
        # Белый список (админы, доверенные пользователи)
        self.whitelist = set()

        # Черный список (заблокированные пользователи)
        self.blacklist = set()

        # Ограничение по времени: {user_id: [timestamps]}
        self.user_requests = defaultdict(list)

        # Настройки по умолчанию
        self.max_requests_per_minute = 5  # Максимум запросов в минуту
        self.max_requests_per_hour = 30  # Максимум запросов в час
        self.max_requests_per_day = 100  # Максимум запросов в день

        # ID администратора (ВАШ ID)
        self.admin_id = 1141119678  # ВСТАВЬТЕ ВАШ ID

        # Загружаем списки из файла
        self.load_lists()
        logger.info(f"✅ Антиспам инициализирован. Админ ID: {self.admin_id}")

    def load_lists(self):
        """Загружает белый и черный списки из файла"""
        try:
            if Path("whitelist.pkl").exists():
                with open("whitelist.pkl", "rb") as f:
                    self.whitelist = pickle.load(f)
            if Path("blacklist.pkl").exists():
                with open("blacklist.pkl", "rb") as f:
                    self.blacklist = pickle.load(f)
            logger.info(f"✅ Загружены списки: белый ({len(self.whitelist)}), черный ({len(self.blacklist)})")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки списков: {e}")

    def save_lists(self):
        """Сохраняет белый и черный списки в файл"""
        try:
            with open("whitelist.pkl", "wb") as f:
                pickle.dump(self.whitelist, f)
            with open("blacklist.pkl", "wb") as f:
                pickle.dump(self.blacklist, f)
            logger.info("💾 Списки сохранены")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения списков: {e}")

    def is_admin(self, user_id):
        """Проверяет, является ли пользователь администратором"""
        return user_id == self.admin_id

    def check_user(self, user_id):
        """Проверяет, может ли пользователь использовать бота"""
        # Админы пропускаются всегда
        if self.is_admin(user_id):
            return True, "admin"

        # Проверка в черном списке
        if user_id in self.blacklist:
            return False, "blacklisted"

        # Проверка в белом списке (если не пуст)
        if self.whitelist and user_id not in self.whitelist:
            return False, "not_whitelisted"

        return True, "allowed"

    def check_rate_limit(self, user_id):
        """Проверяет лимиты запросов"""
        now = time.time()
        minute_ago = now - 60
        hour_ago = now - 3600
        day_ago = now - 86400

        # Очищаем старые записи
        self.user_requests[user_id] = [
            ts for ts in self.user_requests[user_id]
            if ts > day_ago
        ]

        # Считаем запросы за разные периоды
        minute_requests = sum(1 for ts in self.user_requests[user_id] if ts > minute_ago)
        hour_requests = sum(1 for ts in self.user_requests[user_id] if ts > hour_ago)
        day_requests = len(self.user_requests[user_id])

        # Проверяем лимиты
        if minute_requests >= self.max_requests_per_minute:
            return False, f"minute_limit ({self.max_requests_per_minute}/мин)"
        if hour_requests >= self.max_requests_per_hour:
            return False, f"hour_limit ({self.max_requests_per_hour}/час)"
        if day_requests >= self.max_requests_per_day:
            return False, f"day_limit ({self.max_requests_per_day}/день)"

        # Добавляем текущий запрос
        self.user_requests[user_id].append(now)
        return True, "ok"

    def add_to_whitelist(self, user_id):
        """Добавляет пользователя в белый список"""
        self.whitelist.add(user_id)
        self.save_lists()

    def remove_from_whitelist(self, user_id):
        """Удаляет пользователя из белого списка"""
        self.whitelist.discard(user_id)
        self.save_lists()

    def add_to_blacklist(self, user_id):
        """Добавляет пользователя в черный список"""
        self.blacklist.add(user_id)
        self.whitelist.discard(user_id)
        self.save_lists()

    def remove_from_blacklist(self, user_id):
        """Удаляет пользователя из черного списка"""
        self.blacklist.discard(user_id)
        self.save_lists()


# Создаем глобальный экземпляр антиспама
antispam = AntiSpam()


def anti_spam_decorator(func):
    """Декоратор для защиты команд от спама"""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return

        user_id = user.id
        username = user.username or "без username"

        # Проверяем доступ пользователя
        allowed, reason = antispam.check_user(user_id)
        if not allowed:
            if reason == "blacklisted":
                await update.message.reply_text("⛔ Вы заблокированы в этом боте.")
            elif reason == "not_whitelisted":
                await update.message.reply_text("🔒 Доступ только по приглашению. Обратитесь к администратору.")
            logger.warning(f"🚫 Блокировка {user_id} (@{username}): {reason}")
            return

        # Проверяем лимиты
        rate_ok, rate_reason = antispam.check_rate_limit(user_id)
        if not rate_ok:
            await update.message.reply_text(f"⚠️ Слишком много запросов! Лимит: {rate_reason}")
            logger.warning(f"⚠️ Rate limit {user_id} (@{username}): {rate_reason}")
            return

        # Если все проверки пройдены, выполняем функцию
        logger.info(f"✅ Запрос от {user_id} (@{username}): {update.message.text}")
        return await func(update, context, *args, **kwargs)

    return wrapper


# ========== СПИСОК КОМАНД ==========

COMMANDS = {
    "start": {
        "description": "🚀 Запустить бота и показать меню",
        "example": "/start",
        "usage": "Просто отправьте /start для начала работы"
    },
    "help": {
        "description": "📚 Показать это сообщение с командами",
        "example": "/help",
        "usage": "/help - показывает все доступные команды"
    },
    "weather": {
        "description": "🌤 Узнать прогноз погоды",
        "example": "/weather Москва",
        "usage": "/weather <город> - например: /weather Санкт-Петербург"
    },
    "exchange": {
        "description": "💱 Конвертировать валюту",
        "example": "/exchange USD RUB 100",
        "usage": "/exchange <из> <в> <сумма> - например: /exchange EUR USD 50"
    },
    "qr": {
        "description": "📱 Создать QR-код",
        "example": "/qr https://example.com",
        "usage": "/qr <текст или ссылка> - создает QR-код"
    },
    "sentiment": {
        "description": "😊 Проанализировать тональность текста",
        "example": "/sentiment Отличный день!",
        "usage": "/sentiment <текст> - например: /sentiment Это ужасный фильм"
    },
    "translate": {
        "description": "🌐 Google переводчик",
        "example": "/translate en ru Hello world",
        "usage": "/translate <с_какого> <на_какой> <текст> - например: /translate en ru Hello world"
    },
    "ask": {
        "description": "🤖 Задать вопрос AI",
        "example": "/ask Когда был основан Рим?",
        "usage": "/ask <вопрос> - например: /ask Какая сегодня погода в Париже?"
    },
    "cancel": {
        "description": "❌ Завершить диалог",
        "example": "/cancel",
        "usage": "/cancel - попрощаться с ботом"
    },
    "whitelist": {
        "description": "🔓 Управление белым списком (только админ)",
        "example": "/whitelist add 123456789",
        "usage": "/whitelist add <id> - добавить, /whitelist remove <id> - удалить, /whitelist list - список"
    },
    "blacklist": {
        "description": "🔒 Управление черным списком (только админ)",
        "example": "/blacklist add 123456789",
        "usage": "/blacklist add <id> - заблокировать, /blacklist remove <id> - разблокировать, /blacklist list - список"
    },
    "stats": {
        "description": "📊 Статистика использования",
        "example": "/stats",
        "usage": "/stats - показать статистику"
    }
}

# Список всех доступных команд
AVAILABLE_COMMANDS = list(COMMANDS.keys())


# ========== ОСНОВНЫЕ ФУНКЦИИ БОТА ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветствие и показывает меню с кнопками."""
    keyboard = [
        [KeyboardButton("/help"), KeyboardButton("/cancel")],
        [KeyboardButton("/weather Москва"), KeyboardButton("/exchange USD EUR 100")],
        [KeyboardButton("/qr https://google.com"), KeyboardButton("/translate en ru Hello")],
        [KeyboardButton("/sentiment Отличный день!"), KeyboardButton("/ask Когда был основан Рим?")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_text(
        "🌟 Привет! Я обновленный многофункциональный бот.\n"
        "Теперь у меня есть новые команды: /qr и /translate!\n"
        "Нажми на кнопку, введи команду или просто напиши / чтобы увидеть все команды:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подробную справку со всеми командами."""
    help_text = "📋 **Доступные команды:**\n\n"

    for cmd, info in COMMANDS.items():
        help_text += f"• `/{cmd}` - {info['description']}\n"
        help_text += f"  _Пример:_ `{info['example']}`\n\n"

    help_text += "💡 **Совет:** Начните печатать / и увидите подсказки!"

    await update.message.reply_text(help_text, parse_mode='Markdown')


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает диалог."""
    await update.message.reply_text("👋 До свидания! /start - чтобы начать заново")


async def generate_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует QR-код через API QR Coder с таймаутом."""
    if not context.args:
        await update.message.reply_text(
            "Укажите текст или ссылку для QR-кода, например:\n"
            "/qr https://google.com"
        )
        return

    api_key = os.getenv("QRCODER_API_KEY")
    if not api_key:
        logger.error("QRCODER_API_KEY не найден в .env")
        await update.message.reply_text("❌ Ошибка конфигурации: ключ API не найден.")
        return

    text = " ".join(context.args)

    # Проверяем длину текста
    if len(text) > 500:
        await update.message.reply_text("⚠️ Текст слишком длинный. QR-код может быть сложным для сканирования.")

    await update.message.chat.send_action(action="upload_photo")

    base_url = "https://www.qrcoder.co.uk/api/v4/"
    params = {
        "key": api_key,
        "text": text,
        "type": "png",
        "size": 400,
    }

    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(base_url, params=params) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"QR API ошибка {response.status}: {error_text}")

                    if response.status == 429:
                        await update.message.reply_text("❌ Превышен лимит запросов к QR API. Попробуйте позже.")
                    else:
                        await update.message.reply_text(f"❌ Ошибка API: {response.status}")
                    return

                image_data = await response.read()
                if len(image_data) > 10 * 1024 * 1024:  # 10 MB
                    await update.message.reply_text("❌ Слишком большой QR-код.")
                    return

        await update.message.reply_photo(
            photo=BytesIO(image_data),
            filename="qrcode.png",
            caption=f"📱 QR-код для: {text[:50]}{'...' if len(text) > 50 else ''}"
        )

        logger.info(f"QR-код успешно создан для текста длиной {len(text)} символов")

    except asyncio.TimeoutError:
        logger.error("Таймаут при запросе к QR API")
        await update.message.reply_text("❌ Сервис QR-кодов не отвечает. Попробуйте позже.")
    except aiohttp.ClientError as e:
        logger.error(f"Сетевая ошибка при запросе к QR API: {e}")
        await update.message.reply_text("❌ Сетевая ошибка. Проверьте интернет-соединение.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка в generate_qr: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании QR-кода.")


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переводит текст через Google Translate."""
    if len(context.args) < 3:
        await update.message.reply_text(
            "Укажите языки и текст, например:\n"
            "/translate en ru Hello world\n"
            "/translate ru en Привет мир\n\n"
            "Коды языков:\n"
            "ru - русский\n"
            "en - английский\n"
            "de - немецкий\n"
            "fr - французский\n"
            "es - испанский\n"
            "it - итальянский\n"
            "zh-cn - китайский\n"
            "ja - японский\n"
            "ar - арабский"
        )
        return

    from_lang = context.args[0]
    to_lang = context.args[1]
    text = " ".join(context.args[2:])

    await update.message.chat.send_action(action="typing")

    try:
        # Выполняем перевод
        result = translator.translate(text, src=from_lang, dest=to_lang)

        await update.message.reply_text(
            f"🌐 **Google Перевод**\n\n"
            f"Исходный текст: {text}\n"
            f"Язык источника: {result.src}\n"
            f"Перевод: {result.text}",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Ошибка перевода: {e}")
        await update.message.reply_text("❌ Не удалось выполнить перевод. Проверьте коды языков.")


async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает прогноз погоды для указанного города через Open-Meteo."""
    if not context.args:
        await update.message.reply_text(
            "Укажите город после команды, например:\n"
            "/weather Москва"
        )
        return

    city_name = " ".join(context.args)
    await update.message.chat.send_action(action="typing")

    try:
        geocode_url = "https://nominatim.openstreetmap.org/search"
        geocode_params = {
            "q": city_name,
            "format": "json",
            "limit": 1
        }
        headers = {
            'User-Agent': 'TelegramBot/1.0 (https://t.me/NeuroSmiBot)'
        }
        geo_response = requests.get(geocode_url, params=geocode_params, headers=headers, timeout=10)
        geo_response.raise_for_status()
        geo_data = geo_response.json()

        if not geo_data:
            await update.message.reply_text(f"Город '{city_name}' не найден. Попробуйте уточнить название.")
            return

        lat = geo_data[0]['lat']
        lon = geo_data[0]['lon']
        display_name = geo_data[0]['display_name']

        weather_url = "https://api.open-meteo.com/v1/forecast"
        weather_params = {
            "latitude": lat,
            "longitude": lon,
            "daily": ["temperature_2m_max", "temperature_2m_min", "weathercode", "precipitation_sum"],
            "timezone": "auto",
            "forecast_days": 3
        }
        weather_response = requests.get(weather_url, params=weather_params, timeout=10)
        weather_response.raise_for_status()
        weather_data = weather_response.json()

        daily = weather_data['daily']
        times = daily['time']
        temp_max = daily['temperature_2m_max']
        temp_min = daily['temperature_2m_min']
        weathercode = daily['weathercode']
        precip = daily['precipitation_sum']

        weather_desc = {
            0: "Ясно", 1: "Преимущественно ясно", 2: "Переменная облачность", 3: "Пасмурно",
            45: "Туман", 48: "Изморозь",
            51: "Легкая морось", 53: "Морось", 55: "Сильная морось",
            56: "Легкая ледяная морось", 57: "Ледяная морось",
            61: "Небольшой дождь", 63: "Дождь", 65: "Сильный дождь",
            66: "Ледяной дождь", 67: "Сильный ледяной дождь",
            71: "Небольшой снег", 73: "Снег", 75: "Сильный снег",
            77: "Снежная крупа",
            80: "Небольшой ливень", 81: "Ливень", 82: "Сильный ливень",
            85: "Небольшой снегопад", 86: "Снегопад",
            95: "Гроза", 96: "Гроза с градом", 99: "Сильная гроза с градом"
        }

        reply_lines = [f"🌍 Прогноз погоды для: {display_name}\n"]
        for i in range(len(times)):
            date = datetime.datetime.fromisoformat(times[i]).strftime("%d.%m")
            desc = weather_desc.get(weathercode[i], f"Код {weathercode[i]}")
            reply_lines.append(
                f"📅 {date}: {desc}\n"
                f"   🌡 {temp_min[i]:.0f}°C ... {temp_max[i]:.0f}°C\n"
                f"   💧 Осадки: {precip[i]:.1f} мм\n"
            )

        await update.message.reply_text("\n".join(reply_lines))

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети при запросе погоды: {e}")
        await update.message.reply_text("Не удалось получить данные о погоде из-за сетевой ошибки.")
    except KeyError as e:
        logger.error(f"Ошибка в данных API погоды: {e}")
        await update.message.reply_text("Не удалось обработать данные о погоде.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка в weather: {e}")
        await update.message.reply_text("Произошла неизвестная ошибка при получении погоды.")


async def exchange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Конвертирует валюты по курсу ЦБ РФ."""
    if len(context.args) < 3:
        await update.message.reply_text(
            "Укажите валюты и сумму, например:\n"
            "/exchange USD EUR 100\n"
            "Список валют: USD, EUR, GBP, CNY, JPY и др."
        )
        return

    from_currency = context.args[0].upper()
    to_currency = context.args[1].upper()
    try:
        amount = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return

    await update.message.chat.send_action(action="typing")

    try:
        cbr_url = "https://www.cbr.ru/scripts/XML_daily.asp"
        response = requests.get(cbr_url, timeout=10)
        response.encoding = 'windows-1251'
        response.raise_for_status()

        root = ET.fromstring(response.text)

        def get_rate(currency_code):
            if currency_code == "RUB":
                return 1.0
            for valute in root.findall('Valute'):
                char_code = valute.find('CharCode').text
                if char_code == currency_code:
                    nominal = float(valute.find('Nominal').text.replace(',', '.'))
                    value = float(valute.find('Value').text.replace(',', '.'))
                    return value / nominal
            return None

        rate_from = get_rate(from_currency)
        rate_to = get_rate(to_currency)

        if rate_from is None:
            await update.message.reply_text(f"Валюта '{from_currency}' не найдена.")
            return
        if rate_to is None:
            await update.message.reply_text(f"Валюта '{to_currency}' не найдена.")
            return

        result = amount * rate_from / rate_to

        await update.message.reply_text(
            f"{amount:.2f} {from_currency} = {result:.2f} {to_currency}\n"
            f"(Курс ЦБ РФ на {datetime.datetime.now().strftime('%d.%m.%Y')})"
        )

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети при запросе курса валют: {e}")
        await update.message.reply_text("Не удалось получить курс валют из-за сетевой ошибки.")
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга XML: {e}")
        await update.message.reply_text("Не удалось обработать данные о курсах валют.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка в exchange: {e}")
        await update.message.reply_text("Произошла неизвестная ошибка при конвертации.")


async def sentiment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализирует тональность текста."""
    if not context.args:
        await update.message.reply_text("Напишите текст после команды, например: /sentiment Это отличный день!")
        return

    if sentiment_pipeline is None:
        await update.message.reply_text("Модель анализа тональности не загружена.")
        return

    text = " ".join(context.args)
    try:
        result = sentiment_pipeline(text)[0]

        label = result["label"]
        score = result["score"]

        if label == "positive":
            emotion = "😊 положительная"
        elif label == "negative":
            emotion = "😞 отрицательная"
        else:
            emotion = "😐 нейтральная"

        await update.message.reply_text(f"Тональность: {emotion} (уверенность: {score:.2f})")

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        await update.message.reply_text("Не удалось выполнить анализ.")


async def ask_genai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Задает вопрос AI через Gen API."""
    if not context.args:
        await update.message.reply_text("Напишите вопрос после команды, например: /ask Какой сегодня праздник?")
        return

    if not GEN_API_KEY:
        await update.message.reply_text("API ключ Gen API не настроен.")
        return

    question = " ".join(context.args)
    await update.message.chat.send_action(action="typing")

    payload = {
        "is_sync": True,
        "messages": [{"role": "user", "content": question}]
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {GEN_API_KEY}"
    }

    try:
        logger.info("Отправка запроса к Gen API")
        response = requests.post(GEN_API_URL, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        logger.info("Ответ от Gen API получен")

        answer = None
        if data.get("response") and len(data["response"]) > 0:
            first_response = data["response"][0]
            if first_response.get("choices") and len(first_response["choices"]) > 0:
                choice = first_response["choices"][0]
                if choice.get("message") and choice["message"].get("content"):
                    answer = choice["message"]["content"]
                elif choice.get("text"):
                    answer = choice["text"]

        if answer is None:
            answer = f"Не удалось извлечь ответ. Сырые данные: {str(data)[:500]}..."
            logger.warning(answer)

        MAX_LEN = 4000
        if len(answer) > MAX_LEN:
            answer = answer[:MAX_LEN] + "\n\n... (сообщение обрезано из-за ограничения Telegram)"

        await update.message.reply_text(answer)

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        error_body = e.response.text
        logger.error(f"HTTP ошибка {status_code}: {error_body}")
        if status_code == 422:
            await update.message.reply_text(
                f"❌ Ошибка 422: неверный формат запроса.\n"
                f"Детали: {error_body}\n\n"
                f"Отправленный payload: {payload}"
            )
        elif status_code == 402:
            await update.message.reply_text("❌ Недостаточно средств на счету Gen API.")
        elif status_code == 401:
            await update.message.reply_text("Ошибка аутентификации. Проверьте API ключ.")
        elif status_code == 429:
            await update.message.reply_text("Превышен лимит запросов. Попробуйте позже.")
        else:
            await update.message.reply_text(f"Ошибка API: {status_code}. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка: {e}")
        await update.message.reply_text("Не удалось получить ответ. Попробуйте позже.")


# ========== КОМАНДЫ АДМИНИСТРИРОВАНИЯ ==========

async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление белым списком (только админ)"""
    user_id = update.effective_user.id

    if not antispam.is_admin(user_id):
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/whitelist add <user_id> - добавить пользователя\n"
            "/whitelist remove <user_id> - удалить пользователя\n"
            "/whitelist list - показать список"
        )
        return

    action = context.args[0].lower()

    if action == "list":
        if antispam.whitelist:
            users = "\n".join([f"• `{uid}`" for uid in antispam.whitelist])
            await update.message.reply_text(f"📋 **Белый список:**\n{users}", parse_mode='Markdown')
        else:
            await update.message.reply_text("📋 Белый список пуст.")

    elif action in ["add", "remove"] and len(context.args) >= 2:
        try:
            target_id = int(context.args[1])

            if action == "add":
                antispam.add_to_whitelist(target_id)
                await update.message.reply_text(f"✅ Пользователь `{target_id}` добавлен в белый список.",
                                                parse_mode='Markdown')
            else:
                antispam.remove_from_whitelist(target_id)
                await update.message.reply_text(f"✅ Пользователь `{target_id}` удален из белого списка.",
                                                parse_mode='Markdown')
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
    else:
        await update.message.reply_text("❌ Неверная команда.")


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление черным списком (только админ)"""
    user_id = update.effective_user.id

    if not antispam.is_admin(user_id):
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/blacklist add <user_id> - заблокировать пользователя\n"
            "/blacklist remove <user_id> - разблокировать\n"
            "/blacklist list - показать список"
        )
        return

    action = context.args[0].lower()

    if action == "list":
        if antispam.blacklist:
            users = "\n".join([f"• `{uid}`" for uid in antispam.blacklist])
            await update.message.reply_text(f"📋 **Черный список:**\n{users}", parse_mode='Markdown')
        else:
            await update.message.reply_text("📋 Черный список пуст.")

    elif action in ["add", "remove"] and len(context.args) >= 2:
        try:
            target_id = int(context.args[1])

            if action == "add":
                antispam.add_to_blacklist(target_id)
                await update.message.reply_text(f"⛔ Пользователь `{target_id}` заблокирован.", parse_mode='Markdown')
            else:
                antispam.remove_from_blacklist(target_id)
                await update.message.reply_text(f"✅ Пользователь `{target_id}` разблокирован.", parse_mode='Markdown')
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
    else:
        await update.message.reply_text("❌ Неверная команда.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику использования"""
    user_id = update.effective_user.id

    if not antispam.is_admin(user_id):
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return

    total_users = len(antispam.user_requests)
    active_today = sum(1 for requests in antispam.user_requests.values()
                       if any(ts > time.time() - 86400 for ts in requests))
    active_hour = sum(1 for requests in antispam.user_requests.values()
                      if any(ts > time.time() - 3600 for ts in requests))

    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🕐 Активных за час: {active_hour}\n"
        f"📅 Активных за день: {active_today}\n"
        f"✅ В белом списке: {len(antispam.whitelist)}\n"
        f"⛔ В черном списке: {len(antispam.blacklist)}\n"
        f"⚙️ Лимиты: {antispam.max_requests_per_minute}/мин, {antispam.max_requests_per_hour}/час, {antispam.max_requests_per_day}/день"
    )

    await update.message.reply_text(stats_text, parse_mode='Markdown')


# ========== ОБРАБОТЧИК ОБЫЧНЫХ СООБЩЕНИЙ ==========

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отвечает на обычные сообщения."""
    await update.message.reply_text("Я понимаю только команды. Нажмите /help или воспользуйтесь меню.")


# ========== ГЛАВНАЯ ФУНКЦИЯ ==========

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден! Бот не может запуститься.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Основные команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))

    # Погода и валюта
    application.add_handler(CommandHandler("weather", anti_spam_decorator(weather)))
    application.add_handler(CommandHandler("exchange", anti_spam_decorator(exchange)))

    # Анализ тональности и AI
    application.add_handler(CommandHandler("sentiment", anti_spam_decorator(sentiment)))
    application.add_handler(CommandHandler("ask", anti_spam_decorator(ask_genai)))

    # Новые команды
    application.add_handler(CommandHandler("qr", anti_spam_decorator(generate_qr)))
    application.add_handler(CommandHandler("translate", anti_spam_decorator(translate_command)))

    # Команды администрирования (без декоратора, чтобы не блокировать админа)
    application.add_handler(CommandHandler("whitelist", whitelist_command))
    application.add_handler(CommandHandler("blacklist", blacklist_command))
    application.add_handler(CommandHandler("stats", stats_command))

    # Обработчик обычных сообщений (тоже с защитой)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_spam_decorator(echo)))

    logger.info("✅ Бот запущен и готов к работе!")
    logger.info(f"📋 Доступные команды: {', '.join(COMMANDS.keys())}")
    logger.info(f"🛡️ Антиспам включен: {antispam.max_requests_per_minute} запросов в минуту")
    logger.info(f"👑 ID администратора: {antispam.admin_id}")

    application.run_polling()


if __name__ == "__main__":
    main()