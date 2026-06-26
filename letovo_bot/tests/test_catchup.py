"""Тесты режима «догона» пропущенных дней (send_catchup + счётчик catchup).

Догон должен выдавать по одному НОВОМУ набору за вызов, продвигая ученика
вперёд даже без завершения предыдущего набора, и останавливаться, когда
счётчик catchup доходит до нуля.
"""
from __future__ import annotations

import asyncio

import pytest

from letovo_bot.bot import handlers
from letovo_bot.core import userstore


class FakeBot:
    """Минимальный двойник aiogram.Bot: копит отправленные сообщения."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


@pytest.fixture()
def clean_kv():
    """Чистое in-memory KV для каждого теста (без Upstash в окружении)."""
    userstore._mem.clear()
    handlers.store.clear(0)  # на всякий случай
    yield
    userstore._mem.clear()


def test_find_chat_id_by_username(clean_kv):
    userstore.remember_user(555, name="Тест", username="TheoYhshs")
    # без учёта @ и регистра
    assert userstore.find_chat_id_by_username("@theoyhshs") == 555
    assert userstore.find_chat_id_by_username("theoyhshs") == 555
    assert userstore.find_chat_id_by_username("someone_else") is None


def test_catchup_counter(clean_kv):
    cid = 777
    assert userstore.get_catchup(cid) == 0
    userstore.set_catchup(cid, 7)
    assert userstore.get_catchup(cid) == 7
    assert userstore.decrement_catchup(cid) == 6
    userstore.set_catchup(cid, 0)
    assert userstore.decrement_catchup(cid) == 0  # не уходит ниже нуля


def test_catchup_advances_each_call_without_completion(clean_kv):
    cid = 999
    userstore.ensure_user(cid)
    userstore.set_catchup(cid, 3)
    bot = FakeBot()

    # Три вызова подряд, НИ ОДИН набор не завершаем.
    for _ in range(3):
        asyncio.run(handlers.send_catchup(cid, bot))

    # Каждый вызов должен был выдать новый день курса (1 → 2 → 3).
    intros = [t for _, t in bot.messages if "Догоняем пропущенное" in t]
    assert len(intros) == 3
    assert "день 1 из" in intros[0]
    assert "день 2 из" in intros[1]
    assert "день 3 из" in intros[2]

    # Прогресс продвинулся на 2 (после 1-го набора стоим на дне 0,
    # каждый следующий вызов перешагивает незавершённый набор).
    assert userstore.get_course_day(cid) == 2
    # Счётчик догона исчерпан.
    assert userstore.get_catchup(cid) == 0


def test_catchup_after_completion_does_not_skip(clean_kv):
    """Если ученик завершил набор сам, следующий догон не перешагивает день."""
    cid = 1001
    userstore.ensure_user(cid)
    userstore.set_catchup(cid, 2)
    bot = FakeBot()

    asyncio.run(handlers.send_catchup(cid, bot))   # выдан день 1 (course_day=0)
    assert userstore.get_course_day(cid) == 0

    # Имитируем завершение набора учеником: прогресс ушёл вперёд, сессия закрыта
    # (именно это делает _finish_day при штатном прохождении).
    userstore.advance_course_day(cid)
    handlers.store.clear(cid)
    assert userstore.get_course_day(cid) == 1

    asyncio.run(handlers.send_catchup(cid, bot))   # должен выдать день 2, не день 3
    intros = [t for _, t in bot.messages if "Догоняем пропущенное" in t]
    assert "день 2 из" in intros[1]
    # Незавершённой сессии не было → лишнего перешагивания нет.
    assert userstore.get_course_day(cid) == 1
