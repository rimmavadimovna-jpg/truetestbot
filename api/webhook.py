"""Vercel-функция: приём апдейтов Telegram по вебхуку.

Telegram шлёт POST с JSON-апдейтом на этот эндпойнт. Один апдейт = один вызов
функции, поэтому никакого long polling: aiogram обрабатывает апдейт и завершает
работу. Состояние сессии и данные ученика берутся из KV (userstore).

Настройка вебхука (однократно после деплоя):
    https://api.telegram.org/bot<TOKEN>/setWebhook
      ?url=https://<project>.vercel.app/api/webhook
      &secret_token=<TELEGRAM_WEBHOOK_SECRET>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Чтобы пакет letovo_bot импортировался из корня репозитория.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot, Dispatcher                       # noqa: E402
from aiogram.client.default import DefaultBotProperties   # noqa: E402
from aiogram.types import Update                          # noqa: E402

from letovo_bot import config                             # noqa: E402
from letovo_bot.bot.handlers import router                # noqa: E402

# Диспетчер без состояния — собираем один раз на холодный старт.
_dp = Dispatcher()
_dp.include_router(router)


async def _process(update_data: dict) -> None:
    # Bot создаём на каждый вызов: его aiohttp-сессия привязана к event loop,
    # а asyncio.run() создаёт новый loop на каждый запрос.
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await _dp.feed_update(bot, Update.model_validate(update_data))
    finally:
        await bot.session.close()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        # 1) Проверяем секрет вебхука (Telegram присылает его в заголовке).
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if config.TELEGRAM_WEBHOOK_SECRET and secret != config.TELEGRAM_WEBHOOK_SECRET:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        # 2) Читаем и обрабатываем апдейт.
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            asyncio.run(_process(json.loads(raw.decode("utf-8"))))
        except Exception as e:  # не зацикливаем ретраи Telegram на «ядовитом» апдейте
            print(f"[webhook] error: {e}")
        # Всегда 200 — иначе Telegram будет повторять доставку.
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):  # noqa: N802 — health-check
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"letovo-bot webhook is up")
