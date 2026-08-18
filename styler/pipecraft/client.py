"""Cliente IPC del servicio PipeCraft.

No implementa scheduling, DAGs ni procesos. Sólo transporta solicitudes y
respuestas estructuradas al runtime Rust.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class PipeCraftIpcError(RuntimeError):
    pass


@dataclass
class Job:
    run_id: str
    status: str
    message: str = ""
    report_path: str = ""
    warning: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled", "interrupted"}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Job":
        return cls(
            run_id=str(raw.get("run_id", "")),
            status=str(raw.get("status", "")),
            message=str(raw.get("message", "")),
            report_path=str(raw.get("report_path") or ""),
            warning=str(raw.get("warning") or ""),
        )


class PipeCraftClient:
    def __init__(self, workspace: Path, endpoint: str | None = None) -> None:
        self.workspace = Path(workspace)
        self.endpoint = endpoint or os.environ.get("PIPECRAFT_ENDPOINT") or self._default_endpoint()
        self._info: dict[str, Any] | None = None

    def _default_endpoint(self) -> str:
        if os.name == "nt":
            return "tcp://127.0.0.1:47831"
        return str(self.workspace / ".pipelines" / "pipecraft.sock")

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            if self.endpoint.startswith("tcp://"):
                host, port = self.endpoint.removeprefix("tcp://").rsplit(":", 1)
                sock = socket.create_connection((host, int(port)), timeout=5)
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(self.endpoint)
            with sock:
                sock.sendall(raw)
                response = bytearray()
                while not response.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if len(response) > 8 * 1024 * 1024:
                        raise PipeCraftIpcError("La respuesta IPC de PipeCraft excedió 8 MiB.")
        except OSError as exc:
            raise PipeCraftIpcError(f"No se pudo contactar PipeCraft en {self.endpoint}: {exc}") from exc
        try:
            value = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipeCraftIpcError("PipeCraft devolvió JSON IPC inválido.") from exc
        if not isinstance(value, dict):
            raise PipeCraftIpcError("PipeCraft devolvió una respuesta IPC no estructurada.")
        if not value.get("ok", False):
            detail = value.get("error") or {}
            if isinstance(detail, dict):
                raise PipeCraftIpcError(
                    f"{detail.get('code', 'PIPECRAFT_ERROR')}: {detail.get('message', 'falló la solicitud')}"
                )
            raise PipeCraftIpcError(str(detail))
        data = value.get("data", {})
        return data if isinstance(data, dict) else {"value": data}

    def ping(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._info is None or refresh:
            self._info = self.request({"op": "ping"})
        return dict(self._info)

    def capabilities(self, *, refresh: bool = False) -> frozenset[str]:
        raw = self.ping(refresh=refresh).get("capabilities", [])
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(str(item) for item in raw if isinstance(item, str) and item)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities()

    def submit(
        self,
        pipeline: str,
        *,
        execute: bool,
        approve: bool,
        labels: list[str],
        max_workers: int,
        only: list[str] | None = None,
        from_step: str | None = None,
    ) -> str:
        data = self.request({
            "op": "submit",
            "pipeline": pipeline,
            "execute": execute,
            "approve": approve,
            "labels": labels,
            "max_workers": max(1, int(max_workers)),
            "from_step": from_step,
            "only": list(only or []),
        })
        return str(data["run_id"])

    def submit_spec(
        self,
        spec: dict[str, Any],
        *,
        execute: bool,
        approve: bool,
        labels: list[str],
        max_workers: int,
        only: list[str] | None = None,
        from_step: str | None = None,
    ) -> str:
        data = self.request({
            "op": "submit_spec",
            "spec": spec,
            "execute": execute,
            "approve": approve,
            "labels": labels,
            "max_workers": max(1, int(max_workers)),
            "from_step": from_step,
            "only": list(only or []),
        })
        return str(data["run_id"])

    def validate_spec(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.request({"op": "validate_spec", "spec": spec})

    def plan_spec(
        self,
        spec: dict[str, Any],
        *,
        only: list[str] | None = None,
        from_step: str | None = None,
    ) -> dict[str, Any]:
        return self.request({
            "op": "plan_spec",
            "spec": spec,
            "from_step": from_step,
            "only": list(only or []),
        })

    def status(self, run_id: str) -> Job:
        return Job.from_dict(self.request({"op": "status", "run_id": run_id}))

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self.request({"op": "cancel", "run_id": run_id})

    def resume(self, run_id: str) -> dict[str, Any]:
        return self.request({"op": "resume", "run_id": run_id})

    def report(self, run_id: str) -> dict[str, Any]:
        return self.request({"op": "report", "run_id": run_id})

    def events_page(self, run_id: str, *, after: int = 0, limit: int = 250) -> dict[str, Any]:
        return self.request({"op": "events", "run_id": run_id, "after": max(0, after), "limit": max(1, min(1000, limit))})

    def events(self, run_id: str, *, after: int = 0) -> Iterator[dict[str, Any]]:
        cursor = after
        while True:
            page = self.events_page(run_id, after=cursor)
            events = page.get("events", [])
            for event in events:
                if isinstance(event, dict):
                    yield event
            next_cursor = int(page.get("next", cursor) or cursor)
            if not events or next_cursor <= cursor:
                break
            cursor = next_cursor

    def wait(self, run_id: str, *, progress=None, poll_interval: float = 0.15, timeout: float | None = None) -> Job:
        started = time.monotonic()
        cursor = 0
        while True:
            page = self.events_page(run_id, after=cursor)
            events = page.get("events", [])
            for event in events:
                if isinstance(event, dict) and callable(progress):
                    progress(event)
            cursor = int(page.get("next", cursor) or cursor)
            job = self.status(run_id)
            if job.terminal:
                page = self.events_page(run_id, after=cursor)
                for event in page.get("events", []):
                    if isinstance(event, dict) and callable(progress):
                        progress(event)
                return job
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError(f"PipeCraft job {run_id} excedió {timeout}s")
            time.sleep(max(0.03, poll_interval))
