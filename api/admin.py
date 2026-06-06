"""Служебный диагностический эндпойнт: дамп данных учеников из KV.

Защищён CRON_SECRET (заголовок Authorization: Bearer <CRON_SECRET>) — без него
401. Нужен, чтобы убедиться, что профили/попытки/сессии реально пишутся в
Upstash Redis, и посмотреть прогресс. Можно удалить после проверки.

GET /api/admin   (с заголовком Authorization: Bearer <CRON_SECRET>)
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from letovo_bot import config                 # noqa: E402
from letovo_bot.core import userstore         # noqa: E402


def _dump() -> dict:
    backend = "upstash" if (config.KV_REST_API_URL and config.KV_REST_API_TOKEN) else "in-memory"
    users = []
    for chat_id in userstore.all_chat_ids():
        profile = userstore.get_user(chat_id) or {}
        attempts = userstore._attempts(chat_id)
        scores = [a.get("score", 0.0) for a in attempts]
        avg = (sum(scores) / len(scores)) if scores else None
        session = userstore.kv_get_json(f"session:{chat_id}")
        users.append({
            "chat_id": chat_id,
            "name": profile.get("name", ""),
            "course_day_completed": profile.get("course_day", 0),
            "attempts_total": len(attempts),
            "avg_score": round(avg, 3) if avg is not None else None,
            "by_topic": [
                {"topic": t, "n": n, "avg": round(a, 3)}
                for t, n, a in userstore.topic_stats(chat_id)
            ],
            "active_session": (
                {"index": session.get("index"), "total": len(session.get("tasks", []))}
                if session else None
            ),
            "recent_attempts": [
                {"task_type": a.get("task_type"), "topic": a.get("topic"),
                 "score": a.get("score"), "ts": a.get("ts")}
                for a in attempts[-30:]
            ],
        })
    return {"backend": backend, "users_count": len(users), "users": users}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if not config.CRON_SECRET or auth != f"Bearer {config.CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        try:
            body = json.dumps(_dump(), ensure_ascii=False, indent=2).encode("utf-8")
        except Exception as e:
            print(f"[admin] error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"error")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
