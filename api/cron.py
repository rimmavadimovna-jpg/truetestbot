"""Vercel Cron: ежедневная рассылка наборов в 7:00 МСК (04:00 UTC).

Расписание задаётся в vercel.json (crons → "0 4 * * *"). Vercel дёргает этот
эндпойнт раз в сутки; функция шлёт дневной набор каждому зарегистрированному
ученику. Время рассылки единое для всех (7:00 МСК), поэтому персональные
часовые пояса здесь не учитываются.

Эндпойнт защищён CRON_SECRET: Vercel автоматически добавляет заголовок
Authorization: Bearer <CRON_SECRET> к cron-запросам.
"""
from __future__ import annotations

import asyncio
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot                                   # noqa: E402
from aiogram.client.default import DefaultBotProperties   # noqa: E402

from letovo_bot import config                             # noqa: E402
from letovo_bot.core import userstore                     # noqa: E402
from letovo_bot.bot.handlers import send_daily            # noqa: E402


async def _run() -> int:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode="HTML"))
    sent = 0
    try:
        for chat_id in userstore.all_chat_ids():
            try:
                await send_daily(chat_id, bot)
                sent += 1
            except Exception as e:
                print(f"[cron] ошибка рассылки для {chat_id}: {e}")
    finally:
        await bot.session.close()
    return sent


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if config.CRON_SECRET and auth != f"Bearer {config.CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        try:
            sent = asyncio.run(_run())
            msg = f"daily sent to {sent} users"
        except Exception as e:
            print(f"[cron] fatal: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"error")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(msg.encode("utf-8"))
