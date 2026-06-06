"""Точка входа для ЛОКАЛЬНОГО запуска бота через long polling.

На Vercel бот работает иначе — через вебхук (api/webhook.py) и Cron
(api/cron.py). Этот модуль нужен только для локальной отладки.

Запуск:  python -m letovo_bot.bot.main
Требует TELEGRAM_BOT_TOKEN. Банк должен быть собран заранее:
    python -m letovo_bot.data.build_bank
Данные пользователей/попыток/сессий берутся из KV (Upstash); без переменных
Upstash используется in-memory fallback — удобно для локальной отладки.
Ежедневная рассылка локально не запускается (на проде её делает Vercel Cron);
получить набор вручную можно командой /today.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .. import config
from .handlers import router


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в окружении.")
    if not config.BANK_PATH.exists():
        raise SystemExit(f"Банк не найден: {config.BANK_PATH}. "
                         "Собери его: python -m letovo_bot.data.build_bank")

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Бот запущен (локальный polling). LLM-судья: %s",
                 "вкл" if config.ENABLE_LLM_JUDGE else "выкл")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
