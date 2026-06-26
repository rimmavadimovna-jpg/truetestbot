"""Хранилище данных пользователей, попыток и активных сессий.

Банк заданий (только чтение) живёт в SQLite. Но данные, которые пишутся на
лету — профили пользователей, попытки (для статистики) и состояние активной
дневной сессии — на serverless (Vercel) в файловой системе хранить нельзя:
она эфемерная и стирается после каждого вызова функции. Поэтому здесь — тонкий
слой поверх Upstash Redis (KV).

Если переменные Upstash не заданы (локальный запуск, тесты), используется
in-memory fallback — тот же интерфейс, но данные живут только в памяти процесса.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from .. import config

# --------------------------------------------------------------------------- #
# Бэкенд: Upstash Redis (REST) либо in-memory fallback
# --------------------------------------------------------------------------- #
_redis = None
_redis_init = False
_mem: dict[str, Any] = {}


def _client():
    """Ленивая инициализация клиента Upstash. None → используем in-memory."""
    global _redis, _redis_init
    if _redis_init:
        return _redis
    _redis_init = True
    if config.KV_REST_API_URL and config.KV_REST_API_TOKEN:
        from upstash_redis import Redis
        _redis = Redis(url=config.KV_REST_API_URL, token=config.KV_REST_API_TOKEN)
    return _redis


# --- низкоуровневые операции (единый интерфейс для обоих бэкендов) --- #
def _get(key: str) -> Optional[str]:
    c = _client()
    if c:
        return c.get(key)
    v = _mem.get(key)
    return v if isinstance(v, str) or v is None else None


def _set(key: str, value: str, ex: Optional[int] = None) -> None:
    c = _client()
    if c:
        c.set(key, value, ex=ex)
    else:
        _mem[key] = value


def _del(key: str) -> None:
    c = _client()
    if c:
        c.delete(key)
    else:
        _mem.pop(key, None)


def _exists(key: str) -> bool:
    c = _client()
    if c:
        return bool(c.exists(key))
    return key in _mem


def _sadd(key: str, member: str) -> None:
    c = _client()
    if c:
        c.sadd(key, member)
    else:
        _mem.setdefault(key, set()).add(member)


def _smembers(key: str) -> list[str]:
    c = _client()
    if c:
        return list(c.smembers(key) or [])
    return list(_mem.get(key, set()))


def _rpush(key: str, value: str) -> None:
    c = _client()
    if c:
        c.rpush(key, value)
    else:
        _mem.setdefault(key, []).append(value)


def _lrange(key: str) -> list[str]:
    c = _client()
    if c:
        return list(c.lrange(key, 0, -1) or [])
    return list(_mem.get(key, []))


# --- JSON-обёртки (используются и слоем сессий) --- #
def kv_get_json(key: str) -> Optional[Any]:
    raw = _get(key)
    return json.loads(raw) if raw else None


def kv_set_json(key: str, obj: Any, ex: Optional[int] = None) -> None:
    _set(key, json.dumps(obj, ensure_ascii=False), ex=ex)


def kv_del(key: str) -> None:
    _del(key)


# --------------------------------------------------------------------------- #
# Ключи
# --------------------------------------------------------------------------- #
_USERS_SET = "users"                       # SET всех chat_id (для рассылки)
def _user_key(chat_id: int) -> str: return f"user:{chat_id}"
def _attempts_key(chat_id: int) -> str: return f"attempts:{chat_id}"


# --------------------------------------------------------------------------- #
# Пользователи
# --------------------------------------------------------------------------- #
def ensure_user(chat_id: int, name: str = "") -> None:
    """Создать профиль пользователя, если его ещё нет (аналог INSERT OR IGNORE)."""
    if _exists(_user_key(chat_id)):
        return
    profile = {
        "chat_id": chat_id,
        "name": name,
        "timezone": config.DEFAULT_TIMEZONE,
        "daily_time": config.DEFAULT_DAILY_TIME,
        "course_day": 0,
    }
    kv_set_json(_user_key(chat_id), profile)
    _sadd(_USERS_SET, str(chat_id))


def get_user(chat_id: int) -> Optional[dict]:
    return kv_get_json(_user_key(chat_id))


def remember_user(chat_id: int, name: str = "", username: str = "") -> None:
    """Создать пользователя при необходимости и обновить имя/ник из Telegram.

    Ник (username) нужен для уведомлений администратору о прохождениях.
    Пустые значения не затирают уже сохранённые.
    """
    ensure_user(chat_id, name)
    profile = get_user(chat_id) or {}
    changed = False
    if name and profile.get("name") != name:
        profile["name"] = name
        changed = True
    if username and profile.get("username") != username:
        profile["username"] = username
        changed = True
    if changed:
        kv_set_json(_user_key(chat_id), profile)


def set_user_field(chat_id: int, field: str, value: Any) -> None:
    profile = get_user(chat_id) or {}
    profile[field] = value
    kv_set_json(_user_key(chat_id), profile)


def all_chat_ids() -> list[int]:
    """Все зарегистрированные chat_id (для ежедневной рассылки крона)."""
    out: list[int] = []
    for m in _smembers(_USERS_SET):
        try:
            out.append(int(m))
        except (TypeError, ValueError):
            continue
    return out


# --- курс --- #
def get_course_day(chat_id: int) -> int:
    profile = get_user(chat_id)
    if not profile:
        return 0
    val = profile.get("course_day")
    return int(val) if val is not None else 0


def advance_course_day(chat_id: int) -> None:
    set_user_field(chat_id, "course_day", get_course_day(chat_id) + 1)


def reset_course_day(chat_id: int) -> None:
    set_user_field(chat_id, "course_day", 0)


# --- режим «догона» пропущенных дней --- #
# Счётчик: сколько ещё дней крон должен выдавать ученику по одному набору в день,
# продвигая его вперёд даже без завершения предыдущего набора. 0 → обычная выдача.
def _catchup_key(chat_id: int) -> str: return f"catchup:{chat_id}"


def get_catchup(chat_id: int) -> int:
    v = _get(_catchup_key(chat_id))
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def set_catchup(chat_id: int, n: int) -> None:
    _set(_catchup_key(chat_id), str(max(0, int(n))))


def decrement_catchup(chat_id: int) -> int:
    n = max(0, get_catchup(chat_id) - 1)
    set_catchup(chat_id, n)
    return n


def find_chat_id_by_username(username: str) -> Optional[int]:
    """chat_id ученика по Telegram-нику (без учёта @ и регистра) или None.

    Ник попадает в профиль через remember_user при любом взаимодействии ученика
    с ботом. Если ученик ни разу не писал боту после появления этого поля —
    ник может отсутствовать, тогда вернётся None.
    """
    u = username.lstrip("@").lower()
    for chat_id in all_chat_ids():
        profile = get_user(chat_id) or {}
        if (profile.get("username") or "").lower() == u:
            return chat_id
    return None


# --------------------------------------------------------------------------- #
# Попытки (статистика)
# --------------------------------------------------------------------------- #
def save_attempt(chat_id: int, task_id: int, task_type: int, topic: str,
                 score: float, needs_review: bool) -> None:
    rec = {
        "task_id": int(task_id),
        "task_type": int(task_type),
        "topic": topic,
        "score": float(score),
        "needs_review": bool(needs_review),
        "ts": time.time(),
    }
    _rpush(_attempts_key(chat_id), json.dumps(rec, ensure_ascii=False))


def _attempts(chat_id: int) -> list[dict]:
    out: list[dict] = []
    for raw in _lrange(_attempts_key(chat_id)):
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return out


def topic_stats(chat_id: int) -> list[tuple[str, int, float]]:
    """[(topic, n, avg_score)], отсортировано от слабых тем к сильным.

    Аналог: SELECT topic, COUNT(*), AVG(score) ... GROUP BY topic ORDER BY avg.
    """
    agg: dict[str, list[float]] = {}
    for a in _attempts(chat_id):
        agg.setdefault(a.get("topic", ""), []).append(float(a.get("score", 0.0)))
    rows = [(t, len(s), (sum(s) / len(s)) if s else 0.0) for t, s in agg.items()]
    rows.sort(key=lambda r: r[2])
    return rows


def weak_topics(chat_id: int) -> dict[str, float]:
    """Средний балл по темам (меньше — слабее, выше приоритет в подборе)."""
    return {t: avg for t, _n, avg in topic_stats(chat_id)}


def recent_task_ids(chat_id: int, days: int) -> set[int]:
    """ID заданий, выдававшихся за последние `days` дней (чтобы не повторять)."""
    threshold = time.time() - int(days) * 86400
    return {int(a["task_id"]) for a in _attempts(chat_id)
            if a.get("ts", 0) >= threshold and "task_id" in a}
