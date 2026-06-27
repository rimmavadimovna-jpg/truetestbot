"""Тесты режима «догона» пропущенных дней (send_catchup + счётчик catchup).

Догон должен выдавать по одному НОВОМУ набору за вызов, продвигая ученика
вперёд даже без завершения предыдущего набора, и останавливаться, когда
счётчик catchup доходит до нуля.
"""
from __future__ import annotations

import asyncio

import pytest

from letovo_bot import config
from letovo_bot.bot import handlers
from letovo_bot.bot.session import DailySession
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
    """Каждый день догона даёт 8 автопроверяемых QUIZ-заданий (MCQ + открытые)."""
    for d in range(1, assembler.CATCHUP_DAYS + 1):
        tasks = assembler.catchup_extra_set(d)
        assert len(tasks) == 8, f"день догона {d}: ожидалось 8, получено {len(tasks)}"
        for t in tasks:
            assert int(t.task_type) == int(TaskType.QUIZ)
            assert t.payload.get("stem")
            correct = t.answer.get("correct")
            if correct:                                   # MCQ
                assert 1 <= int(correct) <= len(t.payload["options"])
            else:                                         # открытый (инфинитив)
                assert t.answer.get("answer_text")
                assert not t.payload.get("options")
    # ровно 7 открытых вопросов на инфинитив (по одному в день)
    open_total = sum(1 for d in range(1, assembler.CATCHUP_DAYS + 1)
                     for t in assembler.catchup_extra_set(d) if not t.answer.get("correct"))
    assert open_total == 7
    # вне диапазона — пусто
    assert assembler.catchup_extra_set(0) == []
    assert assembler.catchup_extra_set(assembler.CATCHUP_DAYS + 1) == []
    # синтетические id уникальны и не пересекаются с банком (id < 1000)
    all_ids = [t.id for d in range(1, assembler.CATCHUP_DAYS + 1)
               for t in assembler.catchup_extra_set(d)]
    assert len(all_ids) == len(set(all_ids))
    assert min(all_ids) > 1000


def test_catchup_extra_autocheck(clean_kv):
    """Доп. задания проверяются автоматически: и MCQ, и открытые (инфинитив)."""
    for d in range(1, assembler.CATCHUP_DAYS + 1):
        for t in assembler.catchup_extra_set(d):
            if t.answer.get("correct"):                   # MCQ
                correct = int(t.answer["correct"])
                assert checker.check(t, str(correct)).score == 1.0
                wrong = 1 if correct != 1 else 2
                assert checker.check(t, str(wrong)).score == 0.0
            else:                                         # открытый: вписать инфинитив
                ans = t.answer["answer_text"]
                assert checker.check(t, ans).score == 1.0
                assert checker.check(t, ans.upper() + "  ").score == 1.0   # регистр/пробелы не важны
                assert checker.check(t, "заведомонеправильно").score == 0.0


def test_quiz_no_internal_signature(clean_kv):
    """У доп. заданий нет внутренней «подписи» (Правило/путь к файлу) для ученика."""
    for d in range(1, assembler.CATCHUP_DAYS + 1):
        for t in assembler.catchup_extra_set(d):
            assert t.source == ""
            v = checker.check(t, "1")
            assert v.rule_source is None                  # строки «📖 Правило: …» не будет


def test_full_report_to_admin(clean_kv):
    """Преподавателю уходит полный отчёт: вопросы, ответы ученика и правильные ответы."""
    cid = 999
    assert cid != config.ADMIN_CHAT_ID
    userstore.remember_user(cid, name="Фёдор", username="theoyhshs")
    s = DailySession(chat_id=cid, tasks=[], index=2, day_scores=[1.0, 0.0])
    s.records = [
        {"topic": "Спряжение", "stem": "Какая буква пропущена: ты кле_шь конверт?",
         "answer": "и", "correct": True, "ref": "2) и"},
        {"topic": "Грамматические нормы", "stem": "В каком предложении ошибка?",
         "answer": "Не ложи вещи на кровать.", "correct": False,
         "ref": "Б) Не ложи вещи на кровать. — верно «класть»"},
    ]
    bot = FakeBot()
    asyncio.run(handlers._notify_admin(bot, cid, s, 0.5))

    sent_to = {c for c, _ in bot.messages}
    assert sent_to == {config.ADMIN_CHAT_ID}              # отчёт уходит преподавателю
    full = "\n".join(t for _, t in bot.messages)
    assert "полный отчёт" in full and "@theoyhshs" in full
    assert "Какая буква пропущена: ты кле_шь конверт?" in full   # сам вопрос
    assert "Не ложи вещи на кровать." in full                    # ответ ученика
    assert "Правильно:" in full                                  # правильный ответ для ошибки
    assert "1 из 2 верно" in full

    # сам админ не получает отчёт о себе
    bot2 = FakeBot()
    asyncio.run(handlers._notify_admin(bot2, config.ADMIN_CHAT_ID, s, 0.5))
    assert bot2.messages == []


def test_answer_record_mcq_and_open(clean_kv):
    """_answer_record показывает выбранный вариант текстом, а открытый — как ввели."""
    day = assembler.catchup_extra_set(1)
    mcq = next(t for t in day if t.answer.get("correct"))
    openq = next(t for t in day if not t.answer.get("correct"))
    # MCQ: ответ номером → в записи текст выбранного варианта
    rec = handlers._answer_record(mcq, str(mcq.answer["correct"]), checker.check(mcq, str(mcq.answer["correct"])))
    assert rec["answer"] == mcq.payload["options"][mcq.answer["correct"] - 1]
    assert rec["correct"] is True
    # открытый: как ввёл ученик
    rec2 = handlers._answer_record(openq, "странныйОтвет", checker.check(openq, "странныйОтвет"))
    assert rec2["answer"] == "странныйОтвет"
    assert rec2["correct"] is False
    assert rec2["ref"]                                    # есть правильный ответ-эталон


def test_catchup_session_is_17(clean_kv):
    """send_catchup кладёт в сессию полный набор дня: 9 курса + 8 доп. = 17."""
    cid = 1234
    userstore.ensure_user(cid)
    userstore.set_catchup(cid, assembler.CATCHUP_DAYS)   # remaining=7 → день догона 1
    bot = FakeBot()
    asyncio.run(handlers.send_catchup(cid, bot))

    s = handlers.store.get(cid)
    course_n = 9                                          # полный курсовой набор дня
    extra_n = len(assembler.catchup_extra_set(1))         # 8
    assert extra_n == 8
    assert len(s.tasks) == course_n + extra_n == 17
    # последние 8 заданий — наши доп. (синтетические id)
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
