"""Слой сборки: детерминированная подготовка заданий + проверка инвариантов.

Никакой LLM-сборки. Перед выдачей задание проходит assert'ы инвариантов
(см. §5). Здесь же — адаптивный подбор дневного набора (приоритет слабым темам,
без недавних повторов).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .. import config
from . import db, userstore
from .models import Task, TaskType


class InvariantError(AssertionError):
    """Нарушение инварианта задания — задание не должно попадать в выдачу."""


def odd_one_out_index(props: list) -> Optional[int]:
    """Индекс единственного отличающегося элемента или None.

    Возвращает индекс i, если ровно один элемент уникален, а все остальные
    равны между собой (то есть «лишнее» определяется однозначно). Иначе None —
    это значит, что ряд неоднозначен (нет лишнего или их несколько).
    """
    if len(props) < 3:
        return None
    counts: dict = {}
    for p in props:
        counts[p] = counts.get(p, 0) + 1
    singles = [p for p, c in counts.items() if c == 1]
    # ровно одно уникальное значение и ровно одно «общее» значение у остальных
    if len(singles) == 1 and len(counts) == 2:
        return props.index(singles[0])
    return None


# --------------------------------------------------------------------------- #
# Инварианты (вызываются при сборке банка и в тестах)
# --------------------------------------------------------------------------- #
def validate_task(task: Task) -> None:
    """Общие инварианты + специфичные для типа. Бросает InvariantError."""
    if not task.verified:
        raise InvariantError(f"Задание {task.id}: не verified=1, в выдачу нельзя")
    if not task.answer:
        raise InvariantError(f"Задание {task.id}: пустой эталон")
    if not task.source:
        raise InvariantError(f"Задание {task.id}: нет ссылки на источник")

    tt = TaskType(task.task_type)
    if tt == TaskType.THIRD_EXTRA:
        _validate_third_extra(task)
    elif tt == TaskType.FOURTH_EXTRA:
        _validate_fourth_extra(task)
    elif tt == TaskType.PHONETICS:
        _validate_phonetics(task)
    elif tt == TaskType.WORD_FORMATION:
        _validate_word_formation(task)


def _validate_third_extra(task: Task) -> None:
    prows = task.payload["rows"]
    arows = task.answer["rows"]
    if len(prows) != len(arows):
        raise InvariantError(f"Задание {task.id}: рассинхрон payload/answer рядов")
    for i, (p, a) in enumerate(zip(prows, arows)):
        words = p["words"]
        if len(words) != 3:
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: должно быть 3 слова")
        extra = a["extra"]
        # «лишнее» обязано быть одним из трёх слов ряда (по нормализованному сравнению)
        from .detectors import norm_word
        norm_words = [norm_word(w) for w in words]
        if norm_word(extra) not in norm_words and norm_word(a.get("spelling", extra)) not in norm_words:
            raise InvariantError(
                f"Задание {task.id}, ряд {i + 1}: лишнее «{extra}» не входит в ряд {words}")
        if not p.get("principle"):
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: не задан общий принцип")
        if not a.get("spelling"):
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: нет верного написания лишнего")
        # Ровно одно слово нарушает принцип, и это «лишнее» (нет второго кандидата).
        props = a.get("props")
        if not props or len(props) != 3:
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: нет props для проверки однозначности")
        odd = odd_one_out_index(props)
        if odd is None:
            raise InvariantError(
                f"Задание {task.id}, ряд {i + 1}: лишнее неоднозначно (props={props})")
        if norm_word(words[odd]) != norm_word(extra):
            raise InvariantError(
                f"Задание {task.id}, ряд {i + 1}: «лишнее» ({extra}) не совпадает с отличающимся словом")


def _validate_fourth_extra(task: Task) -> None:
    prows = task.payload["rows"]
    arows = task.answer["rows"]
    if len(prows) != len(arows):
        raise InvariantError(f"Задание {task.id}: рассинхрон payload/answer рядов")
    from .detectors import norm_word
    for i, (p, a) in enumerate(zip(prows, arows)):
        words = p["words"]
        if len(words) != 4:
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: должно быть 4 слова")
        if norm_word(a["extra"]) not in [norm_word(w) for w in words]:
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: лишнее не входит в ряд")
        if not a.get("feature"):
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: нет признака-причины")
        props = a.get("props")
        if not props or len(props) != 4:
            raise InvariantError(f"Задание {task.id}, ряд {i + 1}: нет props для проверки однозначности")
        odd = odd_one_out_index(props)
        if odd is None:
            raise InvariantError(
                f"Задание {task.id}, ряд {i + 1}: лишнее неоднозначно (props={props})")
        if norm_word(words[odd]) != norm_word(a["extra"]):
            raise InvariantError(
                f"Задание {task.id}, ряд {i + 1}: «лишнее» не совпадает с отличающимся словом")


def _validate_phonetics(task: Task) -> None:
    # Число хранится, не вычисляется на лету.
    if "count" not in task.answer or not isinstance(task.answer["count"], int):
        raise InvariantError(f"Задание {task.id}: число звука должно быть сохранено в банке")


def _validate_word_formation(task: Task) -> None:
    a = task.answer
    if not a.get("chain"):
        raise InvariantError(f"Задание {task.id}: пустая цепочка")
    # Каждый шаг цепочки должен иметь ссылку-подтверждение из Викисловаря.
    steps = a.get("steps", [])
    if len(steps) < len(a["chain"]) - 1:
        raise InvariantError(f"Задание {task.id}: не у всех шагов есть подтверждение Викисловаря")
    for st in steps:
        if not st.get("source"):
            raise InvariantError(f"Задание {task.id}: шаг {st} без ссылки-подтверждения")
    if not a.get("morphemes"):
        raise InvariantError(f"Задание {task.id}: нет морфемного разбора")


def validate_bank(conn: sqlite3.Connection) -> list[str]:
    """Проверяет все verified=1 задания. Возвращает список ошибок (пустой = ОК)."""
    errors: list[str] = []
    for task in db.all_tasks(conn, verified_only=True):
        try:
            validate_task(task)
        except InvariantError as e:
            errors.append(str(e))
    return errors


# --------------------------------------------------------------------------- #
# Адаптивный подбор дневного набора
# --------------------------------------------------------------------------- #
def weak_topics(chat_id: int) -> dict[str, float]:
    """Средний балл по темам для ученика (меньше — слабее, выше приоритет).

    Данные попыток ученика хранятся в KV (userstore), а не в банке SQLite.
    """
    return userstore.weak_topics(chat_id)


def recent_task_ids(chat_id: int, days: int) -> set[int]:
    return userstore.recent_task_ids(chat_id, days)


def build_daily_set(conn: sqlite3.Connection, chat_id: int,
                    n_min: Optional[int] = None, n_max: Optional[int] = None) -> list[Task]:
    """Собирает дневной набор: разные типы, приоритет слабым темам, без повторов.

    Банк заданий читается из SQLite (conn), история ученика — из KV (userstore).
    Все возвращаемые задания проходят validate_task — иначе исключаются.
    """
    n_min = n_min or config.DAILY_TASK_MIN
    n_max = n_max or config.DAILY_TASK_MAX
    avg = weak_topics(chat_id)
    recent = recent_task_ids(chat_id, config.NO_REPEAT_DAYS)

    candidates = [t for t in db.all_tasks(conn, verified_only=True) if t.id not in recent]
    # подстраховка инвариантами
    valid: list[Task] = []
    for t in candidates:
        try:
            validate_task(t)
            valid.append(t)
        except InvariantError:
            continue

    # сортировка: сначала слабые темы (низкий средний балл), темы без попыток считаем слабыми
    def priority(t: Task) -> float:
        return avg.get(t.topic, -1.0)   # -1 => тема ещё не встречалась, высший приоритет

    valid.sort(key=priority)

    # набираем, по возможности по одному заданию на тип
    selected: list[Task] = []
    seen_types: set[int] = set()
    for t in valid:
        if len(selected) >= n_max:
            break
        if t.task_type in seen_types and len(seen_types) < 12:
            continue
        selected.append(t)
        seen_types.add(int(t.task_type))
    # добиваем до минимума, если типов не хватило
    if len(selected) < n_min:
        for t in valid:
            if t in selected:
                continue
            selected.append(t)
            if len(selected) >= n_min:
                break
    return selected[:n_max]


# --------------------------------------------------------------------------- #
# Курс из 15 дней по тестовым заданиям (QUIZ)
#
# План дня: по 1 вопросу из тем 1, 2, 3 и по 2 вопроса из тем 4, 5, 6 = 9 шт.
# Темы 1–3 идут по порядку (день d → вопрос d). Темы 4–6 перемешиваются один раз
# детерминированно (фикс. seed), затем берутся по 2 в день. Всего 15 дней.
# --------------------------------------------------------------------------- #
import random as _random

COURSE_DAYS = 15
SEQ_THEMES = (1, 2, 3)        # по 1 вопросу в день, по порядку
RANDOM_THEMES = (4, 5, 6)     # по 2 вопроса в день, перемешанные
_SHUFFLE_SEED = 20240601


def _quiz_tasks_by_theme(conn: sqlite3.Connection) -> dict[int, list[Task]]:
    """Все QUIZ-задания, сгруппированные по теме и упорядоченные по idx."""
    by_theme: dict[int, list[Task]] = {}
    for t in db.all_tasks(conn, verified_only=True):
        if int(t.task_type) != int(TaskType.QUIZ):
            continue
        theme = int(t.payload.get("theme", 0))
        by_theme.setdefault(theme, []).append(t)
    for theme, lst in by_theme.items():
        lst.sort(key=lambda x: int(x.payload.get("idx", 0)))
    return by_theme


def course_day_set(conn: sqlite3.Connection, day: int) -> list[Task]:
    """Возвращает 9 заданий для дня `day` (0-based, 0..14). Пустой список вне курса."""
    if day < 0 or day >= COURSE_DAYS:
        return []
    by_theme = _quiz_tasks_by_theme(conn)
    selected: list[Task] = []
    # темы 1–3: по одному по порядку
    for th in SEQ_THEMES:
        lst = by_theme.get(th, [])
        if day < len(lst):
            selected.append(lst[day])
    # темы 4–6: перемешать детерминированно, взять по 2
    for th in RANDOM_THEMES:
        lst = by_theme.get(th, [])[:]
        _random.Random(_SHUFFLE_SEED + th).shuffle(lst)
        for pos in (2 * day, 2 * day + 1):
            if pos < len(lst):
                selected.append(lst[pos])
    return selected


def get_course_day(chat_id: int) -> int:
    """Текущий день курса ученика (хранится в KV)."""
    return userstore.get_course_day(chat_id)


def advance_course_day(chat_id: int) -> None:
    userstore.advance_course_day(chat_id)


def build_course_today(conn: sqlite3.Connection, chat_id: int) -> list[Task]:
    """Набор текущего дня курса для пользователя (по его course_day).

    День курса берётся из KV; сами задания дня — из банка (conn).
    """
    return course_day_set(conn, get_course_day(chat_id))


# --------------------------------------------------------------------------- #
# Дополнительные задания «догона» (вне основного курса)
#
# Это отдельный набор QUIZ-вопросов (фразеологизмы + автопроверяемые задания
# по типам 2/4/6/7/10), который выдаётся ученику ТОЛЬКО в дни догона, поверх
# набора курса. Хранится в data/catchup_questions.json: ключ — номер дня догона
# (1..CATCHUP_DAYS), значение — список вопросов. В основной 15-дневный курс
# (темы 1–6) эти задания не попадают.
# --------------------------------------------------------------------------- #
import json as _json

CATCHUP_DAYS = 7
# Сколько вопросов курса оставлять в наборе дня догона (остальное — доп. задания).
# Курсовой день — 9 вопросов; берём 7, чтобы 7 + 8 доп. = 15 заданий в день.
CATCHUP_COURSE_KEEP = 7
_CATCHUP_JSON = config.DATA_DIR / "catchup_questions.json"
_catchup_cache: Optional[dict] = None


def _load_catchup() -> dict:
    global _catchup_cache
    if _catchup_cache is None:
        try:
            with open(_CATCHUP_JSON, encoding="utf-8") as f:
                _catchup_cache = _json.load(f)
        except (OSError, ValueError) as e:  # файла нет/битый — работаем без доп. заданий
            print(f"[assembler] не удалось загрузить {_CATCHUP_JSON}: {e}")
            _catchup_cache = {}
    return _catchup_cache


def catchup_extra_set(day_number: int) -> list[Task]:
    """Доп. задания (QUIZ) для дня догона day_number (1..CATCHUP_DAYS).

    Возвращает готовые Task-объекты типа QUIZ с автопроверкой. Вне диапазона
    дней — пустой список.
    """
    if day_number < 1 or day_number > CATCHUP_DAYS:
        return []
    items = _load_catchup().get(str(day_number)) or []
    tasks: list[Task] = []
    for pos, it in enumerate(items):
        tasks.append(Task(
            id=700000 + day_number * 100 + pos,   # синтетический id, не пересекается с банком
            task_type=TaskType.QUIZ,
            topic=it.get("topic", "Дополнительно"),
            difficulty=1,
            payload={"stem": it["stem"], "options": it.get("options", [])},
            answer={"correct": it.get("correct"),
                    "answer_text": it.get("answer_text"),   # для открытых вопросов (инфинитив)
                    "explanation": it.get("expl", "")},
            source="Догон: дополнительные задания (data/catchup_questions.json)",
            verified=True,
        ))
    return tasks


def trim_course_keep(tasks: list[Task], n: int) -> list[Task]:
    """Обрезает набор курса до n вопросов, сохраняя представленность всех тем.

    Сначала берёт по одному вопросу каждой темы (в исходном порядке), затем
    добивает до n остальными. Порядок выдачи сохраняется. Если задач ≤ n —
    возвращает как есть.
    """
    if len(tasks) <= n:
        return tasks
    keep_ids: set[int] = set()
    seen_themes: set[int] = set()
    for t in tasks:                       # по одному на тему
        th = int(t.payload.get("theme", 0))
        if th not in seen_themes:
            seen_themes.add(th)
            keep_ids.add(t.id)
    for t in tasks:                       # добиваем до n
        if len(keep_ids) >= n:
            break
        keep_ids.add(t.id)
    return [t for t in tasks if t.id in keep_ids][:n]
