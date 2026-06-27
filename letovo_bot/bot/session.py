"""Состояние активной дневной сессии ученика.

На serverless (Vercel) каждый входящий апдминг Telegram — отдельный вызов
функции без общей памяти, поэтому состояние сессии (текущий набор заданий,
индекс, выбранные номера, баллы дня) нельзя держать в памяти процесса — оно
сериализуется в KV (Upstash) под ключом session:{chat_id}.

Задания хранятся в сессии целиком (как JSON модели Task), чтобы не зависеть от
повторного обращения к банку при каждом ответе. После любой мутации сессию
нужно сохранить: store.save(session).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core import userstore
from ..core.models import Task

# Сессия живёт ограниченное время (день курса не растягивается на недели).
_SESSION_TTL_SECONDS = 3 * 24 * 3600


def _session_key(chat_id: int) -> str:
    return f"session:{chat_id}"


@dataclass
class DailySession:
    chat_id: int
    tasks: list[Task]
    index: int = 0
    # выбранные номера для заданий с inline-тогглами (по task_id)
    selected_numbers: dict[int, set[int]] = field(default_factory=dict)
    day_scores: list[float] = field(default_factory=list)
    # подробные записи ответов для отчёта преподавателю (вопрос, ответ, верно, эталон)
    records: list[dict] = field(default_factory=list)

    @property
    def current(self) -> Task | None:
        return self.tasks[self.index] if self.index < len(self.tasks) else None

    @property
    def finished(self) -> bool:
        return self.index >= len(self.tasks)

    def advance(self, score: float) -> None:
        self.day_scores.append(score)
        self.index += 1

    def toggle(self, task_id: int, number: int) -> set[int]:
        s = self.selected_numbers.setdefault(task_id, set())
        if number in s:
            s.discard(number)
        else:
            s.add(number)
        return s

    # --- сериализация для KV --- #
    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "tasks": [t.model_dump(mode="json") for t in self.tasks],
            "index": self.index,
            "selected_numbers": {str(k): sorted(v) for k, v in self.selected_numbers.items()},
            "day_scores": self.day_scores,
            "records": self.records,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DailySession":
        return cls(
            chat_id=int(data["chat_id"]),
            tasks=[Task.model_validate(t) for t in data.get("tasks", [])],
            index=int(data.get("index", 0)),
            selected_numbers={int(k): set(v)
                              for k, v in (data.get("selected_numbers") or {}).items()},
            day_scores=list(data.get("day_scores") or []),
            records=list(data.get("records") or []),
        )


class SessionStore:
    """Реестр активных сессий по chat_id поверх KV (Upstash) с in-memory fallback."""

    def start(self, chat_id: int, tasks: list[Task]) -> DailySession:
        s = DailySession(chat_id=chat_id, tasks=tasks)
        self.save(s)
        return s

    def get(self, chat_id: int) -> DailySession | None:
        data = userstore.kv_get_json(_session_key(chat_id))
        return DailySession.from_dict(data) if data else None

    def save(self, session: DailySession) -> None:
        userstore.kv_set_json(_session_key(session.chat_id),
                              session.to_dict(), ex=_SESSION_TTL_SECONDS)

    def clear(self, chat_id: int) -> None:
        userstore.kv_del(_session_key(chat_id))
