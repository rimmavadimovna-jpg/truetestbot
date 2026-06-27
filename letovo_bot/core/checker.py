"""Конвейер проверки ответов (§3): уровень A → B → C.

check(task, user_answer, judge) -> Verdict

  A. Детерминированные детекторы (detectors.py).
  B. Сверка с эталоном/списком допустимых вариантов из банка (answer_json).
  C. LLM-судья (llm.py) — только остаточные субъективные критерии, по рубрике
     и с эталоном. При недоступности/невалидном ответе → needs_review.

Каждый грейдер возвращает Verdict с разбором по критериям, эталонным/образцовым
ответом и ссылкой на правило — обратная связь важнее жёсткого балла.
"""
from __future__ import annotations

import re
from typing import Optional

from . import detectors as D
from . import llm as L
from .models import CriterionResult, Task, TaskType, Verdict


def check(task: Task, user_answer: str, judge: Optional[L.LLMJudge] = None) -> Verdict:
    grader = _GRADERS.get(TaskType(task.task_type))
    if grader is None:
        return Verdict(score=0.0, needs_review=True, comment="Нет грейдера для типа задания")
    return grader(task, user_answer or "", judge)


def _mk(score: float, criteria: list[CriterionResult], ref: Optional[str] = None,
        rule: Optional[str] = None, comment: str = "", needs_review: bool = False) -> Verdict:
    score = max(0.0, min(1.0, score))
    return Verdict(
        score=score,
        correct=score >= 0.999 and not needs_review,
        partial=0.0 < score < 0.999,
        needs_review=needs_review,
        criteria=criteria,
        reference_answer=ref,
        rule_source=rule,
        comment=comment,
    )


# --------------------------------------------------------------------------- #
# Задание 1 — «Третий лишний»
# --------------------------------------------------------------------------- #
def grade_third_extra(task: Task, ans: str, judge=None) -> Verdict:
    rows = task.answer["rows"]
    user_lines = [ln for ln in ans.splitlines() if ln.strip()]
    crit: list[CriterionResult] = []
    correct = 0
    for i, row in enumerate(rows):
        user_word = user_lines[i] if i < len(user_lines) else ""
        word_ok, spell_ok = D.extra_word_matches(user_word, row["extra"], row.get("spelling"))
        ok = word_ok and spell_ok
        correct += 1 if ok else 0
        crit.append(CriterionResult(
            name=f"Ряд {i + 1}", passed=ok, source="reference",
            detail=("верно" if ok else f"эталон: {row['extra']} → {row.get('spelling', row['extra'])}"),
        ))
    ref = "\n".join(f"{i + 1}) {r['extra']} → {r.get('spelling', r['extra'])}" for i, r in enumerate(rows))
    return _mk(correct / len(rows), crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 2 — глаголы заданного спряжения
# --------------------------------------------------------------------------- #
def grade_conjugation(task: Task, ans: str, judge=None) -> Verdict:
    a = task.answer
    expected_forms = {D.norm_word(f) for f in a["expected_forms"]}   # формы с буквой
    expected_infs = {D.norm_word(x) for x in a["expected_infinitives"]}
    user_words = set(D.word_sequence(ans))
    sel_hits = len(expected_forms & user_words)
    sel_extra = len(user_words & {D.norm_word(f) for f in a.get("distractor_forms", [])})
    sel_score = max(0.0, (sel_hits - sel_extra) / max(1, len(expected_forms)))
    inf_hits = sum(1 for inf in expected_infs if inf in user_words)
    inf_score = inf_hits / max(1, len(expected_infs))
    score = 0.6 * sel_score + 0.4 * inf_score
    crit = [
        CriterionResult(name="Выписаны нужные формы", passed=sel_score >= 0.999,
                        source="reference", detail=f"{sel_hits}/{len(expected_forms)}"),
        CriterionResult(name="Указаны инфинитивы", passed=inf_score >= 0.999,
                        source="reference", detail=f"{inf_hits}/{len(expected_infs)}"),
    ]
    ref = "Формы: " + ", ".join(a["expected_forms"]) + "\nИнфинитивы: " + ", ".join(a["expected_infinitives"])
    return _mk(score, crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 3 — схемы предложений
# --------------------------------------------------------------------------- #
def grade_schemes(task: Task, ans: str, judge=None) -> Verdict:
    items = task.answer["items"]   # [{canonical, equivalents:[...]}]
    user_lines = [ln for ln in ans.splitlines() if ln.strip()]
    crit: list[CriterionResult] = []
    correct = 0
    for i, it in enumerate(items):
        allowed = {D.normalize_scheme(it["canonical"])}
        allowed |= {D.normalize_scheme(e) for e in it.get("equivalents", [])}
        user_line = user_lines[i] if i < len(user_lines) else ""
        ok = D.normalize_scheme(user_line) in allowed
        correct += 1 if ok else 0
        crit.append(CriterionResult(name=f"Предложение {i + 1}", passed=ok,
                                    source="reference",
                                    detail="верно" if ok else f"эталон: {it['canonical']}"))
    ref = "\n".join(f"{i + 1}) {it['canonical']}" for i, it in enumerate(items))
    return _mk(correct / len(items), crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 4 — пунктуация + основы + объяснение
# --------------------------------------------------------------------------- #
def grade_punctuation(task: Task, ans: str, judge=None) -> Verdict:
    items = task.answer["items"]   # [{reference, bases:[[..]], rule, source}]
    user_lines = [ln for ln in ans.splitlines() if ln.strip()]
    crit: list[CriterionResult] = []
    punct_scores: list[float] = []
    base_scores: list[float] = []
    for i, it in enumerate(items):
        user_line = user_lines[i] if i < len(user_lines) else ""
        ps, notes = D.punctuation_score(user_line, it["reference"])
        bs = D.grammatical_bases_score(D.word_sequence(user_line), it["bases"])
        punct_scores.append(ps)
        base_scores.append(bs)
        crit.append(CriterionResult(name=f"Пунктуация (предл. {i + 1})", passed=ps >= 0.999,
                                    source="detector", detail="; ".join(notes) or "верно"))
        crit.append(CriterionResult(name=f"Грам. основы (предл. {i + 1})", passed=bs >= 0.999,
                                    source="detector", detail=f"{bs:.0%}"))
    score = 0.6 * (sum(punct_scores) / len(items)) + 0.4 * (sum(base_scores) / len(items))

    # Уровень C: объяснение причины знаков (необязательный балл).
    rule = items[0].get("rule", "")
    if judge and judge.enabled and task.rubric and task.rubric.get("check_explanation"):
        res = judge.judge(L.prompt_task4_explanation(ans, rule), ["explanation_valid", "comment"])
        if res is not None:
            crit.append(CriterionResult(name="Объяснение", passed=bool(res["explanation_valid"]),
                                        source="llm", detail=str(res.get("comment", ""))))
    ref = "\n".join(f"{i + 1}) {it['reference']}" for i, it in enumerate(items))
    return _mk(score, crit, ref=ref, rule=rule)


# --------------------------------------------------------------------------- #
# Задание 5 — конструирование предложений (гибрид)
# --------------------------------------------------------------------------- #
def grade_construct(task: Task, ans: str, judge=None) -> Verdict:
    p = task.payload
    words = [w["word"] for w in p["words"]]
    phrasemes = [ph["phraseme"] for ph in p["phrasemes"]]
    crit: list[CriterionResult] = []

    enough_words, used = D.words_used(ans, words, p.get("min_words", 2))
    ph_match = D.find_matching_phraseme(ans, phrasemes)
    crit.append(CriterionResult(name="≥2 слов из списка", passed=enough_words,
                                source="detector", detail="использованы: " + ", ".join(used)))
    crit.append(CriterionResult(name="Фразеологизм использован", passed=ph_match is not None,
                                source="detector", detail=ph_match or "не найден"))

    interrog = D.is_interrogative(ans)
    author = D.has_author_words_after_speech(ans)
    homo = D.has_homogeneous_members(ans)
    base_score = (int(enough_words) + int(ph_match is not None)) / 2.0

    needs_review = False
    if judge and judge.enabled and task.rubric:
        res = judge.judge(
            L.prompt_task5(ans, words, p["phrasemes"], p.get("requirements", "")),
            ["author_words_after_speech", "has_homogeneous_members",
             "words_used_appropriately", "comment"],
        )
        if res is not None:
            author = bool(res["author_words_after_speech"])
            homo = bool(res["has_homogeneous_members"])
            crit.append(CriterionResult(name="Слова уместны", passed=bool(res["words_used_appropriately"]),
                                        source="llm", detail=str(res.get("comment", ""))))
        else:
            needs_review = True
    crit.append(CriterionResult(name="Структура предложений", source="detector",
                                passed=(author or homo or interrog),
                                detail=f"вопрос={interrog}, слова автора после речи={author}, однородные={homo}"))
    struct_score = (int(author) + int(homo or interrog)) / 2.0
    score = 0.6 * base_score + 0.4 * struct_score
    ref = task.answer.get("example", "")
    return _mk(score, crit, ref=ref, rule=task.source, needs_review=needs_review)


# --------------------------------------------------------------------------- #
# Задание 6 — исправление грамматических ошибок
# --------------------------------------------------------------------------- #
def grade_grammar_fix(task: Task, ans: str, judge=None) -> Verdict:
    a = task.answer
    sel_score = D.number_set_partial(ans, a["wrong"])
    crit = [CriterionResult(name="Найдены ошибочные предложения", passed=sel_score >= 0.999,
                            source="reference", detail=f"эталон: {a['wrong']}")]
    # Проверка исправлений: неверный фрагмент исчез, появился один из верных.
    ans_n = D.norm_text(ans)
    fix_hits = 0
    fixes = a.get("fixes", {})
    for num, fx in fixes.items():
        correct_fragments = fx.get("correct_fragments") or [fx["corrected"]]
        wrong = fx.get("wrong_fragment")
        has_correct = any(D.norm_text(c) in ans_n for c in correct_fragments)
        wrong_gone = (wrong is None) or (D.norm_text(wrong) not in ans_n)
        ok = has_correct and wrong_gone
        fix_hits += 1 if ok else 0
    fix_score = fix_hits / max(1, len(fixes))
    crit.append(CriterionResult(name="Исправления", passed=fix_score >= 0.999,
                                source="reference", detail=f"{fix_hits}/{len(fixes)}"))
    score = 0.5 * sel_score + 0.5 * fix_score
    ref = "\n".join(f"{n}) {fx['corrected']}" for n, fx in fixes.items())
    return _mk(score, crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 7 — фонетика (подсчёт звука)
# --------------------------------------------------------------------------- #
def grade_phonetics(task: Task, ans: str, judge=None) -> Verdict:
    a = task.answer
    ok = D.exact_int(ans, a["count"])   # число хранится, не вычисляется на лету
    crit = [CriterionResult(name="Количество звука", passed=ok, source="reference",
                            detail=f"эталон: {a['count']}")]
    ref = f"{a['count']}. {a.get('explanation', '')}"
    return _mk(1.0 if ok else 0.0, crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 8 — синонимы с разными корнями (гибрид)
# --------------------------------------------------------------------------- #
def grade_synonyms(task: Task, ans: str, judge=None) -> Verdict:
    p, a = task.payload, task.answer
    syns = D.split_synonyms(ans)
    allowed = {D.norm_word(x["syn"]): x for x in a["allowed"]}
    roots_map = {D.norm_word(x["syn"]): x.get("root", "") for x in a["allowed"] if x.get("root")}
    crit: list[CriterionResult] = []

    count_ok = len(syns) >= 5
    crit.append(CriterionResult(name="Пять синонимов", passed=count_ok, source="detector",
                                detail=f"дано: {len(syns)}"))

    valid: list[str] = []
    needs_review = False
    for s in syns:
        sn = D.norm_word(s)
        if sn in allowed:                       # уровень B: членство в банке
            valid.append(s)
        elif judge and judge.enabled:           # уровень C: синоним вне банка
            res = judge.judge(L.prompt_task8_synonym(s, p["word"], p.get("context", "")),
                              ["is_synonym", "different_root"])
            if res is None:
                needs_review = True
            elif res["is_synonym"] and res["different_root"]:
                valid.append(s)
                roots_map.setdefault(sn, sn[:4])
        else:
            needs_review = True
    distinct, roots = D.distinct_roots(valid, roots_map)
    crit.append(CriterionResult(name="Синонимы засчитаны", passed=len(valid) >= 5,
                                source="reference", detail=f"{len(valid)} из {len(syns)}"))
    crit.append(CriterionResult(name="Разные корни", passed=distinct, source="detector",
                                detail=", ".join(roots)))
    score = min(len(valid), 5) / 5.0
    if not distinct:
        score *= 0.7
    ref = ", ".join(x["syn"] for x in a["allowed"])
    return _mk(score, crit, ref=ref, rule=task.source, needs_review=needs_review and not valid)


# --------------------------------------------------------------------------- #
# Задание 9 — словообразовательная цепочка + морфемика
# --------------------------------------------------------------------------- #
def grade_word_formation(task: Task, ans: str, judge=None) -> Verdict:
    a = task.answer
    crit: list[CriterionResult] = []
    # 1) цепочка
    user_words = [D.norm_word(w) for w in D.tokens(ans) if D.norm_word(w)]
    chain = [D.norm_word(w) for w in a["chain"]]
    # ищем подпоследовательность совпадения порядка
    chain_ok = _ordered_subsequence(chain, user_words)
    crit.append(CriterionResult(name="Порядок цепочки", passed=chain_ok, source="reference",
                                detail=" → ".join(a["chain"])))
    # 2) способ словообразования
    method_ok = D.norm_text(a["method"]) in D.norm_text(ans)
    crit.append(CriterionResult(name="Способ словообразования", passed=method_ok,
                                source="reference", detail=a["method"]))
    # 3) морфемный разбор: сверяем эталонные морфемы с разбором ученика.
    # Разбиваем и эталон, и ответ на «атомы» морфем (учительница → уч/и/тель/ниц/а)
    # и считаем долю эталонных атомов, найденных у ученика (порядок не важен).
    morphemes = a["morphemes"]
    parts = [v for v in (morphemes.get("prefix"), morphemes.get("root"),
                         morphemes.get("suffix"), morphemes.get("ending")) if v]
    expected_atoms = D.parse_morphemes("-".join(parts))
    user_atoms = set(D.parse_morphemes(ans))
    ans_norm = D.norm_text(ans)
    found = sum(1 for atom in expected_atoms
                if atom in user_atoms or atom in ans_norm)
    morph_score = found / max(1, len(expected_atoms))
    crit.append(CriterionResult(name="Морфемный разбор", passed=morph_score >= 0.999,
                                source="reference", detail=_format_morphemes(morphemes)))
    score = (int(chain_ok) + int(method_ok) + morph_score) / 3.0
    ref = f"Цепочка: {' → '.join(a['chain'])}\nСпособ: {a['method']}\nРазбор: {_format_morphemes(morphemes)}"
    return _mk(score, crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 10 — «Четвёртое лишнее» по морфологии
# --------------------------------------------------------------------------- #
def grade_fourth_extra(task: Task, ans: str, judge=None) -> Verdict:
    rows = task.answer["rows"]
    user_lines = [ln for ln in ans.splitlines() if ln.strip()]
    crit: list[CriterionResult] = []
    correct = 0
    for i, row in enumerate(rows):
        user_word = user_lines[i] if i < len(user_lines) else ""
        ok = D.norm_word(row["extra"]) in D.word_sequence(user_word)
        correct += 1 if ok else 0
        crit.append(CriterionResult(name=f"Ряд {i + 1}", passed=ok, source="reference",
                                    detail=f"лишнее: {row['extra']} ({row['feature']})"))
    ref = "\n".join(f"{i + 1}) {r['extra']} — {r['feature']}" for i, r in enumerate(rows))
    return _mk(correct / len(rows), crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 11 — верные/ошибочные утверждения
# --------------------------------------------------------------------------- #
def grade_text_statements(task: Task, ans: str, judge=None) -> Verdict:
    a = task.answer
    match, got, exp = D.number_set_matches(ans, a["key"])
    score = D.number_set_partial(ans, a["key"])
    crit = [CriterionResult(name="Номера утверждений", passed=match, source="reference",
                            detail=f"эталон: {sorted(exp)}, ваш ответ: {sorted(got)}")]
    ref = f"Ключ: {sorted(exp)}"
    return _mk(score, crit, ref=ref, rule=task.source)


# --------------------------------------------------------------------------- #
# Задание 12 — фразеологизм в текст + толкование (гибрид)
# --------------------------------------------------------------------------- #
def grade_phraseme(task: Task, ans: str, judge=None) -> Verdict:
    a = task.answer
    allowed = a["allowed"]   # [{phraseme, meaning, fits}]
    crit: list[CriterionResult] = []
    matched = None
    for it in allowed:
        if D.phraseme_present(ans, it["phraseme"]):
            matched = it
            break
    crit.append(CriterionResult(name="Фразеологизм из допустимых", passed=matched is not None,
                                source="reference",
                                detail=(matched["phraseme"] if matched else
                                        "допустимы: " + ", ".join(x["phraseme"] for x in allowed))))
    needs_review = False
    def_ok = False
    if matched is not None and judge and judge.enabled:
        res = judge.judge(L.prompt_task12(ans, matched["meaning"], matched["phraseme"]),
                          ["phraseme_fits_meaning", "definition_matches_reference", "comment"])
        if res is None:
            needs_review = True
        else:
            def_ok = bool(res["definition_matches_reference"]) and bool(res["phraseme_fits_meaning"])
            crit.append(CriterionResult(name="Толкование верно", passed=def_ok, source="llm",
                                        detail=str(res.get("comment", ""))))
    elif matched is not None:
        needs_review = True   # толкование требует судьи
    score = (int(matched is not None) + int(def_ok)) / 2.0 if (judge and judge.enabled) \
        else (1.0 if matched is not None else 0.0)
    ref = "\n".join(f"{x['phraseme']} — {x['meaning']}" for x in allowed)
    return _mk(score, crit, ref=ref, rule=task.source, needs_review=needs_review)


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
def _ordered_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Идут ли элементы needle в haystack в том же относительном порядке."""
    it = iter(haystack)
    return all(any(x == y for y in it) for x in needle)


def _format_morphemes(m: dict) -> str:
    order = [("приставка", m.get("prefix")), ("корень", m.get("root")),
             ("суффикс", m.get("suffix")), ("окончание", m.get("ending"))]
    return ", ".join(f"{name}: {val}" for name, val in order if val)


_GRADERS = {
    TaskType.THIRD_EXTRA: grade_third_extra,
    TaskType.CONJUGATION: grade_conjugation,
    TaskType.SCHEMES: grade_schemes,
    TaskType.PUNCTUATION: grade_punctuation,
    TaskType.CONSTRUCT: grade_construct,
    TaskType.GRAMMAR_FIX: grade_grammar_fix,
    TaskType.PHONETICS: grade_phonetics,
    TaskType.SYNONYMS: grade_synonyms,
    TaskType.WORD_FORMATION: grade_word_formation,
    TaskType.FOURTH_EXTRA: grade_fourth_extra,
    TaskType.TEXT_STATEMENTS: grade_text_statements,
    TaskType.PHRASEME: grade_phraseme,
    TaskType.QUIZ: None,  # назначается ниже (определён после _GRADERS)
}


# --------------------------------------------------------------------------- #
# QUIZ — тест с выбором варианта (1 из N) или открытый ответ
# --------------------------------------------------------------------------- #
def grade_quiz(task: Task, ans: str, judge=None) -> Verdict:
    """Проверка тестового вопроса.

    MCQ: answer={'correct': N (1-based)} — сверяем выбранный номер.
    Открытый: answer={'answer_text': '...'} — нормализованное сравнение текста.
    Объяснение из банка всегда показывается как обратная связь.
    """
    a = task.answer
    p = task.payload
    expl = a.get("explanation") or a.get("expl") or ""
    options = p.get("options") or []

    if a.get("correct"):  # MCQ
        correct = int(a["correct"])
        chosen = None
        m = re.search(r"\d+", ans or "")
        if m:
            chosen = int(m.group())
        ok = chosen == correct
        correct_text = options[correct - 1] if 0 < correct <= len(options) else ""
        ref = f"{correct}) {correct_text}".strip()
        if expl:
            ref += f"\n{expl}"
        crit = [CriterionResult(
            name="Выбор варианта", passed=ok, source="reference",
            detail=("верно" if ok else f"правильный ответ: {correct}) {correct_text}"))]
        # rule=None: у QUIZ источник — внутренний путь к файлу, не правило для ученика
        return _mk(1.0 if ok else 0.0, crit, ref=ref, rule=None)

    # Открытый ответ
    expected = a.get("answer_text", "")
    ok = D.norm_text(ans) == D.norm_text(expected)
    ref = expected + (f"\n{expl}" if expl else "")
    crit = [CriterionResult(name="Ответ", passed=ok, source="reference",
                            detail=("верно" if ok else f"правильный ответ: {expected}"))]
    return _mk(1.0 if ok else 0.0, crit, ref=ref, rule=None)


_GRADERS[TaskType.QUIZ] = grade_quiz
