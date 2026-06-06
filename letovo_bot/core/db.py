"""Доступ к банку SQLite.

Синхронный слой (sqlite3) используется скриптами сборки/выверки и тестами;
асинхронный (aiosqlite) — ботом. Схема одна и та же.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .models import Task, TaskType

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY, word_gapped TEXT, hint TEXT,
  answer_letter TEXT, check_word TEXT, orthogram_type TEXT,
  topic TEXT, source TEXT, verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, task_type INTEGER,
  topic TEXT, difficulty INTEGER,
  payload_json TEXT, answer_json TEXT, rubric_json TEXT,
  source TEXT, verified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS texts (
  id INTEGER PRIMARY KEY, body TEXT, license TEXT,
  statements_json TEXT, phrasemes_json TEXT
);
CREATE TABLE IF NOT EXISTS users (
  chat_id INTEGER PRIMARY KEY, name TEXT, timezone TEXT, daily_time TEXT,
  course_day INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, chat_id INTEGER, task_id INTEGER, task_type INTEGER,
  topic TEXT, score REAL, user_answer TEXT, needs_review INTEGER DEFAULT 0,
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_attempts_chat ON attempts(chat_id);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type);
"""


def connect(path: str | Path, read_only: bool = False) -> sqlite3.Connection:
    """Подключение к банку.

    read_only=True — для рантайма бота на serverless: файловая система Vercel
    доступна только для чтения, поэтому банк открываем в режиме ro/immutable
    (без создания журналов и попыток записи). Скрипты сборки/выверки и тесты
    используют обычное (записываемое) подключение.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # миграция для старых БД: добавить course_day, если столбца ещё нет
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "course_day" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN course_day INTEGER DEFAULT 0")
    conn.commit()


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        task_type=TaskType(row["task_type"]),
        topic=row["topic"] or "",
        difficulty=row["difficulty"] or 1,
        payload=json.loads(row["payload_json"]),
        answer=json.loads(row["answer_json"]),
        rubric=json.loads(row["rubric_json"]) if row["rubric_json"] else None,
        source=row["source"] or "",
        verified=bool(row["verified"]),
    )


def insert_task(
    conn: sqlite3.Connection,
    task_type: int,
    payload: dict[str, Any],
    answer: dict[str, Any],
    topic: str = "",
    difficulty: int = 1,
    rubric: Optional[dict[str, Any]] = None,
    source: str = "",
    verified: bool = False,
) -> int:
    cur = conn.execute(
        """INSERT INTO tasks
           (task_type, topic, difficulty, payload_json, answer_json,
            rubric_json, source, verified)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            int(task_type), topic, difficulty,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(answer, ensure_ascii=False),
            json.dumps(rubric, ensure_ascii=False) if rubric else None,
            source, int(verified),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def all_tasks(conn: sqlite3.Connection, verified_only: bool = False) -> list[Task]:
    sql = "SELECT * FROM tasks"
    if verified_only:
        sql += " WHERE verified=1"
    return [_row_to_task(r) for r in conn.execute(sql).fetchall()]


def insert_text(
    conn: sqlite3.Connection,
    body: str,
    license: str,
    statements: Optional[dict[str, Any]] = None,
    phrasemes: Optional[dict[str, Any]] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO texts (body, license, statements_json, phrasemes_json) VALUES (?,?,?,?)",
        (
            body, license,
            json.dumps(statements, ensure_ascii=False) if statements else None,
            json.dumps(phrasemes, ensure_ascii=False) if phrasemes else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)
