"""Eventos append-only y explicaciones causales de ejecuciones."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from styler.planning.models import PlanNode, StepResult


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunEvent:
    sequence: int
    timestamp: str
    run_id: str
    pipeline: str
    kind: str
    node_id: str = ""
    node_kind: str = ""
    phase: str = ""
    block: str = ""
    attempt: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class EventWriter:
    def __init__(self, path: Path, run_id: str, pipeline: str) -> None:
        self.path = path
        self.run_id = run_id
        self.pipeline = pipeline
        self.sequence = 0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def emit(
        self,
        kind: str,
        *,
        node: PlanNode | None = None,
        result: StepResult | None = None,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        with self._lock:
            self.sequence += 1
            event = RunEvent(
                sequence=self.sequence,
                timestamp=now(),
                run_id=self.run_id,
                pipeline=self.pipeline,
                kind=kind,
                node_id=(node.id if node else (result.node_id if result else "")),
                node_kind=(node.kind if node else (result.node_kind if result else "")),
                phase=(node.phase if node else (result.phase if result else "")),
                block=(node.block if node else (result.block if result else "")),
                attempt=(result.attempts if result else None),
                data=dict(data or {}),
            )
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return event


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events
