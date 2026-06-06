"""Тесты слоя сборки: валидация банка и адаптивный подбор набора."""
from __future__ import annotations

from letovo_bot.core import assembler, db, userstore


def test_validate_bank_no_errors(bank):
    assert assembler.validate_bank(bank) == []


def test_build_daily_set_size_and_uniqueness(bank):
    tasks = assembler.build_daily_set(bank, chat_id=777, n_min=6, n_max=8)
    assert 6 <= len(tasks) <= 8
    # без повторов одного задания
    assert len({t.id for t in tasks}) == len(tasks)
    # все верифицированы и валидны
    for t in tasks:
        assert t.verified


def test_no_repeat_recent(bank):
    chat_id = 888
    tasks = assembler.build_daily_set(bank, chat_id, n_min=2, n_max=2)
    # «проходим» эти задания (попытки пишутся в KV-хранилище, не в банк)
    for t in tasks:
        userstore.save_attempt(chat_id, t.id, int(t.task_type), t.topic, 1.0, False)
    recent = assembler.recent_task_ids(chat_id, days=14)
    assert {t.id for t in tasks}.issubset(recent)
    next_set = assembler.build_daily_set(bank, chat_id, n_min=2, n_max=2)
    # недавно выданные не повторяются
    assert not ({t.id for t in next_set} & {t.id for t in tasks})


def test_weak_topics_priority(bank):
    chat_id = 999
    all_t = db.all_tasks(bank, verified_only=True)
    weak = all_t[0]
    userstore.save_attempt(chat_id, weak.id, int(weak.task_type), weak.topic, 0.1, False)
    avg = assembler.weak_topics(chat_id)
    assert avg[weak.topic] < 0.5
