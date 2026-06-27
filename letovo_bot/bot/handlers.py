"""Хендлеры aiogram 3.x: выдача заданий, приём ответов, обратная связь.

Задания с множеством номеров (зад. 2, 6, 11) — inline-кнопки-тогглы + «Готово».
Открытые ответы — текстом. После каждого ответа — мгновенная проверка
конвейером §3 с разбором по критериям, эталоном и ссылкой на правило.

Хранилище: банк заданий читается из SQLite (read-only), а данные ученика
(профиль, попытки, активная сессия) — из KV (userstore), т.к. на serverless
файловая система эфемерная.
"""
from __future__ import annotations

import re
import sqlite3
from html import escape as _esc

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from .. import config
from ..core import assembler, checker, db, userstore
from ..core.llm import LLMJudge
from ..core.models import MULTI_NUMBER_TYPES, Task, TaskType, Verdict
from .session import SessionStore

router = Router()
store = SessionStore()
_judge = LLMJudge() if config.ENABLE_LLM_JUDGE else None


def _conn() -> sqlite3.Connection:
    """Подключение к банку только для чтения (запись идёт в KV)."""
    return db.connect(config.BANK_PATH, read_only=True)


def _remember(chat_id: int, from_user) -> None:
    """Создать/обновить профиль ученика, запомнив имя и Telegram-ник.

    Ник нужен, чтобы в уведомлениях администратору показывать @username.
    """
    if from_user is None:
        userstore.ensure_user(chat_id)
        return
    userstore.remember_user(
        chat_id,
        name=getattr(from_user, "full_name", "") or "",
        username=getattr(from_user, "username", "") or "",
    )


# --------------------------------------------------------------------------- #
# Рендеринг
# --------------------------------------------------------------------------- #
def render_task(task: Task, idx: int, total: int) -> str:
    """Текст задания для Telegram. Весь динамический материал из банка
    экранируется (_esc), чтобы символы < > & в данных не ломали HTML-разметку."""
    p = task.payload
    if int(task.task_type) == int(TaskType.QUIZ):
        head = f"<b>Вопрос {idx + 1}/{total}</b>  ({_esc(task.topic)})\n\n"
        body = _esc(p.get("stem", ""))
        opts = p.get("options") or []
        if opts:
            body += "\n\n" + "\n".join(f"  {i + 1}) {_esc(o)}" for i, o in enumerate(opts))
            body += "\n\n<i>Выберите вариант кнопкой ниже.</i>"
        else:
            body += "\n\n<i>Напишите ответ одним словом.</i>"
        return head + body
    head = (f"<b>Задание {idx + 1}/{total}</b> "
            f"(тип {int(task.task_type)}, тема: {_esc(task.topic)})\n")
    body = _esc(p.get("instruction", ""))
    tt = TaskType(task.task_type)
    if tt == TaskType.THIRD_EXTRA:
        rows = "\n".join(f"{i + 1}) " + ", ".join(_esc(w) for w in r["words"])
                         + f"  — {_esc(r['principle'])}"
                         for i, r in enumerate(p["rows"]))
        body += "\n\n" + rows + "\n\n<i>Ответ: по одному слову на строку, в верном написании.</i>"
    elif tt == TaskType.CONJUGATION:
        body += "\n\n" + ", ".join(_esc(f) for f in p["forms"])
    elif tt == TaskType.SCHEMES:
        body += "\n\n" + "\n".join(f"{i + 1}) {_esc(s)}" for i, s in enumerate(p["sentences"]))
        body += "\n\n<i>Схемы — по одной на строку.</i>"
    elif tt == TaskType.PUNCTUATION:
        body += "\n\n" + "\n".join(f"{i + 1}) {_esc(s)}" for i, s in enumerate(p["sentences"]))
    elif tt == TaskType.CONSTRUCT:
        words = "\n".join(f"• {_esc(w['word'])} — {_esc(w['meaning'])}" for w in p["words"])
        phr = "\n".join(f"• {_esc(x['phraseme'])} — {_esc(x['meaning'])}" for x in p["phrasemes"])
        body += f"\n\nСлова:\n{words}\n\nФразеологизмы:\n{phr}"
    elif tt == TaskType.GRAMMAR_FIX:
        body += "\n\n" + "\n".join(f"{i + 1}) {_esc(s)}" for i, s in enumerate(p["sentences"]))
        body += "\n\n<i>Отметьте ошибочные кнопками, затем пришлите исправленные варианты текстом.</i>"
    elif tt == TaskType.PHONETICS:
        body += f"\n\nПредложение: «{_esc(p['sentence'])}»\nЗвук: {_esc(p['sound'])}"
    elif tt == TaskType.SYNONYMS:
        body += f"\n\nКонтекст: «{_esc(p['context'])}»\n<i>5 синонимов через запятую.</i>"
    elif tt == TaskType.WORD_FORMATION:
        body += "\n\nСлова: " + ", ".join(_esc(w) for w in p["words"]) \
            + f"\nРазобрать: «{_esc(p['target_word'])}»"
    elif tt == TaskType.FOURTH_EXTRA:
        rows = "\n".join(f"{i + 1}) " + ", ".join(_esc(w) for w in r["words"])
                         for i, r in enumerate(p["rows"]))
        body += "\n\n" + rows + "\n\n<i>По одному лишнему слову на строку.</i>"
    elif tt == TaskType.TEXT_STATEMENTS:
        st = "\n".join(f"{i + 1}) {_esc(s)}" for i, s in enumerate(p["statements"]))
        body += f"\n\n{_esc(p['text'])}\n\nУтверждения:\n{st}"
    elif tt == TaskType.PHRASEME:
        body += f"\n\n{_esc(p['text'])}\n\nАбзац № {p['paragraph']}."
    return head + body


def numbers_keyboard(task: Task, selected: set[int]) -> InlineKeyboardMarkup:
    """Кнопки-тогглы для заданий с выбором номеров."""
    p = task.payload
    count = len(p.get("sentences") or p.get("statements") or [])
    buttons = []
    row = []
    for n in range(1, count + 1):
        mark = "✅" if n in selected else "▫️"
        row.append(InlineKeyboardButton(text=f"{mark}{n}", callback_data=f"tog:{task.id}:{n}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="Готово ✓", callback_data=f"done:{task.id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def quiz_keyboard(task: Task) -> InlineKeyboardMarkup:
    """Кнопки одиночного выбора для теста (нажатие сразу отправляет ответ)."""
    opts = task.payload.get("options") or []
    rows = [[InlineKeyboardButton(text=str(i + 1), callback_data=f"qz:{task.id}:{i + 1}")]
            for i in range(len(opts))]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_verdict(v: Verdict) -> str:
    lines = [_esc(v.summary_line())]
    for c in v.criteria:
        icon = "✓" if c.passed else "✗"
        detail = f" — {_esc(c.detail)}" if c.detail else ""
        lines.append(f"  {icon} {_esc(c.name)}{detail}")
    if v.reference_answer:
        lines.append(f"\n<b>Образец:</b>\n{_esc(v.reference_answer)}")
    if v.rule_source:
        lines.append(f"\n📖 Правило: {_esc(v.rule_source)}")
    if v.needs_review:
        lines.append("\n<i>Часть ответа отправлена на ручную проверку преподавателю.</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    _remember(message.chat.id, message.from_user)
    await message.answer(
        "Привет! Я тренажёр по русскому языку — курс из 15 дней.\n"
        f"Каждый день в {config.DEFAULT_DAILY_TIME} ({config.DEFAULT_TIMEZONE}) я присылаю "
        "набор из 9 вопросов (по 1 из тем 1–3 и по 2 из тем 4–6) и сразу проверяю их "
        "с объяснением.\n\n"
        "Команды: /today — вопросы сейчас, /stats — прогресс, "
        "/theory «тема», /restart — начать курс заново, /settings."
    )


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    _remember(message.chat.id, message.from_user)
    await send_daily(message.chat.id, message.bot)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    rows = userstore.topic_stats(message.chat.id)
    if not rows:
        await message.answer("Пока нет попыток. Нажми /today, чтобы начать.")
        return
    lines = ["<b>Прогресс по темам</b> (от слабых к сильным):"]
    for topic, n, avg in rows:
        lines.append(f"• {_esc(topic)}: {avg:.0%} (попыток: {n})")
    await message.answer("\n".join(lines))


@router.message(Command("theory"))
async def cmd_theory(message: Message) -> None:
    topic = (message.text or "").partition(" ")[2].strip()
    conn = _conn()
    if topic:
        rows = conn.execute("SELECT DISTINCT topic, source FROM tasks WHERE topic LIKE ? AND verified=1",
                            (f"%{topic}%",)).fetchall()
    else:
        rows = conn.execute("SELECT DISTINCT topic, source FROM tasks WHERE verified=1").fetchall()
    conn.close()
    if not rows:
        await message.answer("Не нашёл такой темы. Доступные темы — в /stats.")
        return
    lines = ["<b>Правила и источники</b>:"]
    for r in rows:
        lines.append(f"• {_esc(r['topic'])}: {_esc(r['source'])}")
    await message.answer("\n".join(lines))


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    userstore.ensure_user(message.chat.id)
    await message.answer(
        f"Часовой пояс: {config.DEFAULT_TIMEZONE}\n"
        f"Время рассылки: {config.DEFAULT_DAILY_TIME} (фиксировано для всех).\n\n"
        "Набор приходит автоматически раз в день. Получить вопросы прямо сейчас — /today."
    )


# --------------------------------------------------------------------------- #
# Выдача набора и приём ответов
# --------------------------------------------------------------------------- #
async def send_daily(chat_id: int, bot) -> None:
    userstore.ensure_user(chat_id)
    # если набор уже идёт и не завершён — не начинаем новый день, продолжаем
    active = store.get(chat_id)
    if active is not None and not active.finished:
        await _send_current(chat_id, bot)
        return
    day = assembler.get_course_day(chat_id)              # 0-based
    conn = _conn()
    tasks = assembler.build_course_today(conn, chat_id)
    conn.close()
    if not tasks:
        await bot.send_message(
            chat_id, "🎓 Курс из 15 дней пройден! Можно начать заново — напишите /restart.")
        return
    store.start(chat_id, tasks)
    await bot.send_message(
        chat_id, f"📚 День {day + 1} из {assembler.COURSE_DAYS}: {len(tasks)} вопросов. Поехали!")
    await _send_current(chat_id, bot)


async def send_catchup(chat_id: int, bot) -> None:
    """Догоняющая рассылка: один новый набор в день, пока счётчик catchup > 0.

    В отличие от send_daily, продвигает ученика вперёд, даже если предыдущий
    набор не завершён: незаконченную сессию прошлого дня закрываем и переходим
    к следующему дню курса. Так за неделю выдаются пропущенные дни — по одному
    в день — вместо того чтобы бесконечно пересылать один и тот же застрявший
    набор. Завершение набора учеником обрабатывается обычным путём
    (_finish_day): прогресс и уведомление администратору работают как всегда.
    """
    userstore.ensure_user(chat_id)
    active = store.get(chat_id)
    if active is not None and not active.finished:
        # предыдущий догоняющий набор не доведён до конца — двигаемся дальше
        assembler.advance_course_day(chat_id)
        store.clear(chat_id)
    # какой это по счёту день догона (1..CATCHUP_DAYS): счётчик идёт от CATCHUP_DAYS к 0
    remaining = userstore.get_catchup(chat_id)
    catchup_day = assembler.CATCHUP_DAYS - remaining + 1
    day = assembler.get_course_day(chat_id)
    conn = _conn()
    tasks = assembler.build_course_today(conn, chat_id)   # полный набор дня курса
    conn.close()
    extras = assembler.catchup_extra_set(catchup_day)     # полный блок доп. заданий дня догона
    all_tasks = tasks + extras
    if not all_tasks:
        userstore.set_catchup(chat_id, 0)
        await bot.send_message(
            chat_id, "🎓 Курс из 15 дней пройден! Можно начать заново — напишите /restart.")
        return
    store.start(chat_id, all_tasks)
    if tasks:
        intro = (f"📚 Догоняем пропущенное — день {day + 1} из {assembler.COURSE_DAYS}: "
                 f"{len(tasks)} вопросов курса")
        intro += (f" + {len(extras)} доп. заданий. Поехали!" if extras else ". Поехали!")
    else:
        intro = f"📚 Дополнительные задания на сегодня: {len(extras)}. Поехали!"
    await bot.send_message(chat_id, intro)
    await _send_current(chat_id, bot)
    userstore.decrement_catchup(chat_id)


async def _send_current(chat_id: int, bot) -> None:
    s = store.get(chat_id)
    if s is None or s.finished:
        await _finish_day(chat_id, bot)
        return
    task = s.current
    text = render_task(task, s.index, len(s.tasks))
    if int(task.task_type) == int(TaskType.QUIZ) and (task.payload.get("options")):
        await bot.send_message(chat_id, text, reply_markup=quiz_keyboard(task), parse_mode="HTML")
    elif task.task_type in MULTI_NUMBER_TYPES:
        kb = numbers_keyboard(task, s.selected_numbers.get(task.id, set()))
        await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, text, parse_mode="HTML")


async def _finish_day(chat_id: int, bot) -> None:
    s = store.get(chat_id)
    if s and s.day_scores:
        avg = sum(s.day_scores) / len(s.day_scores)
        assembler.advance_course_day(chat_id)             # переходим к следующему дню курса
        new_day = assembler.get_course_day(chat_id)
        tail = ("Это был последний день курса! 🎓" if new_day >= assembler.COURSE_DAYS
                else "Возвращайтесь завтра за следующим набором — или сразу /today.")
        await bot.send_message(
            chat_id, f"🏁 Итог дня: {avg:.0%} верных. {tail}\n/stats — прогресс по темам.")
        await _notify_admin(bot, chat_id, s, avg)
    store.clear(chat_id)


async def _notify_admin(bot, chat_id: int, s, avg: float) -> None:
    """Прислать преподавателю (ADMIN_CHAT_ID) ПОЛНЫЙ отчёт за день ученика.

    Не сводка по темам, а весь набор: каждый вопрос, ответ ученика, верно/неверно
    и правильный ответ для ошибочных. Не шлётся, если набор завершил сам админ.
    Длинный отчёт отправляется несколькими сообщениями (лимит Telegram ~4096).
    """
    admin = config.ADMIN_CHAT_ID
    if not admin or chat_id == admin:
        return
    profile = userstore.get_user(chat_id) or {}
    uname = profile.get("username")
    who = f"@{uname}" if uname else (profile.get("name") or f"id {chat_id}")

    recs = s.records or []
    total = len(recs) or len(s.day_scores)
    correct = (sum(1 for r in recs if r.get("correct"))
               if recs else sum(1 for sc in s.day_scores if sc >= 1.0))

    header = (f"🧑‍🎓 <b>{_esc(who)}</b> — полный отчёт за день\n"
              f"Итог: {avg:.0%} ({correct} из {total} верно)")

    blocks: list[str] = []
    for i, r in enumerate(recs, 1):
        icon = "✅" if r.get("correct") else "❌"
        ans = _esc(r.get("answer", "")) or "—"
        part = (f"\n<b>{i}. {icon}</b> <i>{_esc(r.get('topic', ''))}</i>\n"
                f"{_esc(r.get('stem', ''))}\n"
                f"Ответ ученика: {ans}")
        if not r.get("correct") and r.get("ref"):
            part += f"\n✔ Правильно: {_esc(r['ref'])}"
        blocks.append(part)

    await _send_admin_chunks(bot, admin, header, blocks)


async def _send_admin_chunks(bot, admin: int, header: str, blocks: list[str],
                             limit: int = 3500) -> None:
    """Собирает header + блоки в сообщения ≤ limit символов и шлёт их по очереди."""
    messages: list[str] = []
    cur = header
    for b in blocks:
        if len(cur) + len(b) + 1 > limit:
            messages.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + b
    if cur:
        messages.append(cur)
    for msg in messages:
        try:
            await bot.send_message(admin, msg, parse_mode="HTML")
        except Exception as e:  # отчёт не должен ломать поток ученика
            print(f"[notify_admin] не удалось отправить администратору {admin}: {e}")


@router.callback_query(F.data.startswith("tog:"))
async def on_toggle(cb: CallbackQuery) -> None:
    _, tid, n = cb.data.split(":")
    _remember(cb.message.chat.id, cb.from_user)
    s = store.get(cb.message.chat.id)
    if s is None or s.current is None or s.current.id != int(tid):
        await cb.answer("Это задание уже не активно.")
        return
    selected = s.toggle(int(tid), int(n))
    store.save(s)
    await cb.message.edit_reply_markup(reply_markup=numbers_keyboard(s.current, selected))
    await cb.answer()


@router.callback_query(F.data.startswith("done:"))
async def on_done(cb: CallbackQuery) -> None:
    _, tid = cb.data.split(":")
    _remember(cb.message.chat.id, cb.from_user)
    s = store.get(cb.message.chat.id)
    if s is None or s.current is None or s.current.id != int(tid):
        await cb.answer("Это задание уже не активно.")
        return
    task = s.current
    selected = sorted(s.selected_numbers.get(task.id, set()))
    answer = ",".join(str(x) for x in selected)
    await cb.answer()
    await _grade_and_advance(cb.message.chat.id, cb.bot, answer)


@router.callback_query(F.data.startswith("qz:"))
async def on_quiz_answer(cb: CallbackQuery) -> None:
    _, tid, opt = cb.data.split(":")
    _remember(cb.message.chat.id, cb.from_user)
    s = store.get(cb.message.chat.id)
    if s is None or s.current is None or s.current.id != int(tid):
        await cb.answer("Этот вопрос уже не активен.")
        return
    await cb.answer()
    # убираем кнопки у отвеченного вопроса
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _grade_and_advance(cb.message.chat.id, cb.bot, opt)


@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    userstore.ensure_user(message.chat.id)
    userstore.reset_course_day(message.chat.id)
    store.clear(message.chat.id)
    await message.answer("Курс сброшен на день 1. Напишите /today, чтобы начать заново.")


@router.message(F.text)
async def on_text_answer(message: Message) -> None:
    _remember(message.chat.id, message.from_user)
    s = store.get(message.chat.id)
    if s is None or s.finished:
        return  # вне сессии — игнор (команды обрабатываются выше)
    await _grade_and_advance(message.chat.id, message.bot, message.text or "")


async def _grade_and_advance(chat_id: int, bot, answer: str) -> None:
    s = store.get(chat_id)
    if s is None or s.current is None:
        return
    task = s.current
    verdict = checker.check(task, answer, judge=_judge)
    userstore.save_attempt(chat_id, task.id, int(task.task_type), task.topic,
                           verdict.score, verdict.needs_review)
    s.records.append(_answer_record(task, answer, verdict))   # для отчёта преподавателю
    await bot.send_message(chat_id, render_verdict(verdict), parse_mode="HTML")
    s.advance(verdict.score)
    store.save(s)
    await _send_current(chat_id, bot)


def _answer_record(task: Task, answer: str, verdict: Verdict) -> dict:
    """Запись одного ответа для подробного отчёта: вопрос, ответ ученика, верность, эталон."""
    p = task.payload
    opts = p.get("options") or []
    if opts:                                  # MCQ — показываем выбранный вариант текстом
        m = re.search(r"\d+", answer or "")
        idx = int(m.group()) if m else 0
        user_disp = opts[idx - 1] if 0 < idx <= len(opts) else (answer or "")
    else:                                     # открытый ответ — как ввёл ученик
        user_disp = (answer or "").strip()
    return {
        "topic": task.topic,
        "stem": p.get("stem") or p.get("instruction") or "",
        "answer": user_disp,
        "correct": bool(verdict.correct),
        "ref": verdict.reference_answer or "",
    }
