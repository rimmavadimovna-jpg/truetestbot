"""Конфигурация бота.

Секреты берём ТОЛЬКО из переменных окружения. Строки моделей Anthropic
держим в конфиге (сверяй актуальные значения на docs.claude.com).
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Пути ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BANK_PATH = Path(os.getenv("LETOVO_BANK_PATH", str(DATA_DIR / "bank.sqlite")))

# --- Секреты (только из окружения) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Хостинг на Vercel (serverless) ---
# KV-хранилище Upstash Redis. Маркетплейс Vercel прокидывает переменные с
# префиксом KV_REST_API_* (Vercel KV) либо UPSTASH_REDIS_REST_* — принимаем оба.
KV_REST_API_URL = (os.getenv("KV_REST_API_URL", "")
                   or os.getenv("UPSTASH_REDIS_REST_URL", ""))
KV_REST_API_TOKEN = (os.getenv("KV_REST_API_TOKEN", "")
                     or os.getenv("UPSTASH_REDIS_REST_TOKEN", ""))
# Секрет для защиты cron-эндпойнта (Vercel шлёт его в заголовке Authorization).
CRON_SECRET = os.getenv("CRON_SECRET", "")
# Секрет вебхука Telegram (заголовок X-Telegram-Bot-Api-Secret-Token).
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# --- Модели Anthropic ---
# Сильная модель — для разовой офлайн-выверки банка.
ANTHROPIC_MODEL_VERIFY = os.getenv("ANTHROPIC_MODEL_VERIFY", "claude-opus-4-8")
# Быстрая/дешёвая модель — для рантайм-проверки открытых ответов (уровень C).
ANTHROPIC_MODEL_JUDGE = os.getenv("ANTHROPIC_MODEL_JUDGE", "claude-haiku-4-5-20251001")

# --- Поведение бота ---
DEFAULT_TIMEZONE = os.getenv("LETOVO_DEFAULT_TZ", "Europe/Moscow")
DEFAULT_DAILY_TIME = os.getenv("LETOVO_DEFAULT_TIME", "10:00")

# Сколько заданий в дневном наборе (15–20 минут).
DAILY_TASK_MIN = int(os.getenv("LETOVO_DAILY_MIN", "6"))
DAILY_TASK_MAX = int(os.getenv("LETOVO_DAILY_MAX", "8"))

# Не повторять задание, которое выдавалось за последние N дней.
NO_REPEAT_DAYS = int(os.getenv("LETOVO_NO_REPEAT_DAYS", "14"))

# Включать ли уровень C (LLM-судья). Без ключа автоматически выключается.
ENABLE_LLM_JUDGE = bool(ANTHROPIC_API_KEY) and os.getenv("LETOVO_ENABLE_LLM", "1") != "0"
