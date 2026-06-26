"""Тесты режима «догона» пропущенных дней (send_catchup + счётчик catchup).

Догон должен выдавать по одному НОВОМУ набору за вызов, продвигая ученика
вперёд даже без завершения предыдущего набора, и останавливаться, когда
счётчик catchup доходит до нуля.
"""
from __future__ import annotations

import asyncio

import pytest

from letovo_bot.bot import handlers
from letovo_bot.core import assembler, checker, userstore
from letovo_bot.core.models import TaskType


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


def test_catchup_extra_set_loads_all_days(clean_kv):
    """Каждый из 7 дней догона даёт непустой набор автопроверяемых QUIZ-заданий."""
    for d in range(1, assembler.CATCHUP_DAYS + 1):
        tasks = assembler.catchup_extra_set(d)
        assert tasks, f"день догона {d}: пусто"
        for t in tasks:
            assert int(t.task_type) == int(TaskType.QUIZ)
            assert t.payload.get("stem")
            # MCQ: верный номер в пределах вариантов
            correct = t.answer.get("correct")
            assert correct and 1 <= int(correct) <= len(t.payload["options"])
    # вне диапазона — пусто
    assert assembler.catchup_extra_set(0) == []
    assert assembler.catchup_extra_set(assembler.CATCHUP_DAYS + 1) == []
    # синтетические id уникальны и не пересекаются с банком (id < 1000)
    all_ids = [t.id for d in range(1, assembler.CATCHUP_DAYS + 1)
               for t in assembler.catchup_extra_set(d)]
    assert len(all_ids) == len(set(all_ids))
    assert min(all_ids) > 1000


def test_catchup_extra_autocheck(clean_kv):
    """Доп. задания реально проверяются автоматически: верный вариант → 1.0, иной → 0.0."""
    for d in range(1, assembler.CATCHUP_DAYS + 1):
        for t in assembler.catchup_extra_set(d):
            correct = int(t.answer["correct"])
            assert checker.check(t, str(correct)).score == 1.0
            wrong = 1 if correct != 1 else 2
            assert checker.check(t, str(wrong)).score == 0.0


def test_catchup_appends_extras_to_session(clean_kv):
    """send_catchup кладёт в сессию набор курса + доп. задания этого дня догона."""
    cid = 1234
    userstore.ensure_user(cid)
    userstore.set_catchup(cid, assembler.CATCHUP_DAYS)   # remaining=7 → день догона 1
    bot = FakeBot()
    asyncio.run(handlers.send_catchup(cid, bot))

    s = handlers.store.get(cid)
    course_n = 9          # курс: 9 QUIZ-вопросов в день
    extra_n = len(assembler.catchup_extra_set(1))
    assert len(s.tasks) == course_n + extra_n
    # последние extra_n заданий — наши доп. (синтетические id)
    assert all(t.id > 1000 for t in s.tasks[-extra_n:])
    intro = next(t for _, t in bot.messages if "Догоняем" in t)
    assert f"+ {extra_n} доп. заданий" in intro


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
