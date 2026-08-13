"""Cliente del motor Rust de Styler.

El motor vive en un proceso compañero para mantener una frontera estable entre
la interfaz Python y el núcleo Rust. Los comandos tradicionales devuelven un
sobre JSON; la ejecución de planes emite eventos JSONL para que la TUI pueda
mostrar progreso sin bloquearse.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from styler.runtime.commands import PipeCraftRunner

PROTOCOL_VERSION = 1
EVENT_PROTOCOL_VERSION = 1
ENGINE_ENV = "STYLER_ENGINE_BIN"
EXECUTION_ENV = "STYLER_ENABLE_EXECUTION"
EXECUTION_CONFIRMATION = "I_UNDERSTAND_STYLER_WILL_CHANGE_MY_SYSTEM"


class EngineUnavailableError(RuntimeError):
    """El binario Rust no está instalado o no es ejecutable."""


class EngineProtocolError(RuntimeError):
    """El motor respondió con JSON inválido o con un contrato incompatible."""


class EngineCommandError(RuntimeError):
    """El motor rechazó una solicitud válida del cliente."""


class EngineExecutionError(RuntimeError):
    """La transmisión de ejecución terminó de forma incompleta o fallida."""


@dataclass(frozen=True)
class EngineStatus:
    available: bool
    binary: str = ""
    engine_version: str = ""
    protocol_version: int = 0
    event_protocol_version: int = 0
    hash_algorithm: str = ""
    execution_enabled: bool = False
    execution_default_mode: str = "dry_run"
    reason: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    summary: dict[str, Any]
    events: tuple[dict[str, Any], ...]


class EngineClient:
    """Invoca ``styler-engine`` sin ``shell=True`` y valida sus protocolos."""

    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        *,
        timeout: float = 120.0,
        runner: PipeCraftRunner | None = None,
    ) -> None:
        self.binary = str(binary or find_engine_binary() or "")
        self.timeout = timeout
        self.runner = runner or PipeCraftRunner(timeout=timeout)

    @property
    def available(self) -> bool:
        return bool(self.binary and os.path.isfile(self.binary) and os.access(self.binary, os.X_OK))

    def status(self) -> EngineStatus:
        if not self.available:
            return EngineStatus(
                available=False,
                reason=(
                    "No se encontró styler-engine. Compílalo con "
                    "./scripts/build-rust-engine.sh o define STYLER_ENGINE_BIN."
                ),
            )
        try:
            result = self._run("version")
        except (EngineProtocolError, EngineCommandError, OSError) as exc:
            return EngineStatus(available=False, binary=self.binary, reason=str(exc))
        return EngineStatus(
            available=True,
            binary=self.binary,
            engine_version=str(result.get("engine_version", "")),
            protocol_version=int(result.get("protocol_version", 0) or 0),
            event_protocol_version=int(result.get("event_protocol_version", 0) or 0),
            hash_algorithm=str(result.get("hash_algorithm", "")),
            execution_enabled=bool(result.get("execution_enabled", False)),
            execution_default_mode=str(result.get("execution_default_mode", "dry_run")),
        )

    def host(self) -> dict[str, Any]:
        return self._run("host")

    def hash_file(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        return self._run("hash-file", os.fspath(path))

    def scan(self, paths: Iterable[str | os.PathLike[str]]) -> dict[str, Any]:
        normalized = [os.fspath(path) for path in paths]
        if not normalized:
            return {"entries": [], "failures": [], "scanned_files": 0, "total_bytes": 0}
        return self._run("scan", *normalized)

    def diagnose(self, catalog_root: str | os.PathLike[str]) -> dict[str, Any]:
        return self._run("diagnose", os.fspath(catalog_root))

    def plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._run("plan", "-", stdin=json.dumps(dict(request), ensure_ascii=False))

    def journal_summary(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        return self._run("journal-summary", os.fspath(path))

    def registry_list(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        return self._run("registry-list", *([os.fspath(path)] if path else []))

    def registry_show(self, record_id: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        args = [record_id]
        if path:
            args.append(os.fspath(path))
        return self._run("registry-show", *args)

    def registry_audit(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        return self._run("registry-audit", *([os.fspath(path)] if path else []))

    def uninstall_plan(self, record_id: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        args = [record_id]
        if path:
            args.append(os.fspath(path))
        return self._run("uninstall-plan", *args)

    def reconcile(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        return self._run("reconcile", *([os.fspath(path)] if path else []))

    def reconcile_show(self, record_id: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        args = [record_id]
        if path:
            args.append(os.fspath(path))
        return self._run("reconcile-show", *args)

    def repair_plan(self, record_id: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        args = [record_id]
        if path:
            args.append(os.fspath(path))
        return self._run("repair-plan", *args)

    def adoption_preview(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._run("adoption-preview", "-", stdin=json.dumps(dict(request), ensure_ascii=False))

    def registry_adopt(self, request: Mapping[str, Any], path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        args = ["-"]
        if path:
            args.append(os.fspath(path))
        return self._run("registry-adopt", *args, stdin=json.dumps(dict(request), ensure_ascii=False))

    def stream_execute(
        self,
        request: Mapping[str, Any],
        *,
        allow_system_changes: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Transmite eventos de una ejecución.

        El cliente fuerza ``dry_run`` salvo que el llamador pase
        ``allow_system_changes=True``. La ejecución real añade las dos llaves
        que el motor exige: variable de entorno y confirmación exacta.
        """
        if not self.available:
            raise EngineUnavailableError(
                "styler-engine no está disponible; compílalo o define STYLER_ENGINE_BIN"
            )

        payload = json.loads(json.dumps(dict(request), ensure_ascii=False))
        if not isinstance(payload, dict):
            raise EngineProtocolError("la solicitud de ejecución no es un objeto JSON")
        options = payload.setdefault("options", {})
        if not isinstance(options, dict):
            raise EngineProtocolError("execution.options debe ser un objeto JSON")

        requested_mode = str(options.get("mode", "dry_run"))
        if requested_mode == "apply" and not allow_system_changes:
            raise EngineExecutionError(
                "la solicitud pide apply, pero el cliente no recibió allow_system_changes=True"
            )
        if allow_system_changes:
            options["mode"] = "apply"
            options["confirmation"] = EXECUTION_CONFIRMATION
        else:
            options["mode"] = "dry_run"
            options.pop("confirmation", None)

        with tempfile.TemporaryDirectory(prefix="styler-engine-") as temporary:
            configured_cancel = str(options.get("cancel_file", "")).strip()
            cancel_path = (
                Path(configured_cancel).expanduser()
                if configured_cancel
                else Path(temporary) / "cancel.requested"
            )
            options["cancel_file"] = str(cancel_path)
            process_env = os.environ.copy()
            if allow_system_changes:
                process_env[EXECUTION_ENV] = "1"
            else:
                process_env.pop(EXECUTION_ENV, None)

            process = self.runner.spawn_protocol(
                [self.binary, "execute", "-"],
                env=process_env,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            stderr_lines: list[str] = []
            stderr_thread = threading.Thread(
                target=_collect_lines,
                args=(process.stderr, stderr_lines),
                name="styler-engine-stderr",
                daemon=True,
            )
            stderr_thread.start()

            stop_watcher = threading.Event()
            watcher: threading.Thread | None = None
            if cancel_event is not None:
                watcher = threading.Thread(
                    target=_watch_cancellation,
                    args=(cancel_event, stop_watcher, cancel_path),
                    name="styler-engine-cancel",
                    daemon=True,
                )
                watcher.start()

            seen_events = 0
            saw_finished = False
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False))
                process.stdin.close()
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    event = _parse_event(line)
                    seen_events += 1
                    if event.get("event") == "run_finished":
                        saw_finished = True
                    yield event
                if not self.runner.wait_process(process, timeout=self.timeout):
                    self.runner.stop_process(process)
                    raise EngineExecutionError(
                        f"styler-engine excedió el timeout de {self.timeout:g} s"
                    )
                return_code = int(process.returncode or 0)
            except GeneratorExit:
                _cancel_then_stop(process, cancel_path, self.runner)
                raise
            except BaseException:
                _cancel_then_stop(process, cancel_path, self.runner)
                raise
            finally:
                stop_watcher.set()
                if watcher is not None:
                    watcher.join(timeout=1.0)
                stderr_thread.join(timeout=1.0)
                if process.poll() is None:
                    _terminate_process(process, self.runner)

            stderr = "".join(stderr_lines).strip()
            if return_code != 0:
                raise EngineExecutionError(_format_engine_stream_error(return_code, stderr))
            if seen_events == 0:
                raise EngineExecutionError(
                    "styler-engine terminó sin emitir eventos de ejecución"
                )
            if not saw_finished:
                raise EngineExecutionError(
                    "styler-engine terminó sin el evento final run_finished"
                )

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        allow_system_changes: bool = False,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> ExecutionResult:
        events: list[dict[str, Any]] = []
        summary: dict[str, Any] | None = None
        for event in self.stream_execute(
            request,
            allow_system_changes=allow_system_changes,
            cancel_event=cancel_event,
        ):
            events.append(event)
            if on_event is not None:
                on_event(event)
            if event.get("event") == "run_finished":
                data = event.get("data")
                if isinstance(data, dict):
                    summary = data
        if summary is None:
            raise EngineExecutionError("la ejecución no produjo un resumen final")
        return ExecutionResult(summary=summary, events=tuple(events))

    def _run(self, *args: str, stdin: str | None = None) -> dict[str, Any]:
        if not self.available:
            raise EngineUnavailableError(
                "styler-engine no está disponible; compílalo o define STYLER_ENGINE_BIN"
            )
        completed = self.runner.run(
            [self.binary, *args],
            timeout=self.timeout,
            input_text=stdin,
        )
        raw = completed.stdout.strip() or completed.stderr.strip()
        if not raw:
            raise EngineProtocolError(
                f"styler-engine terminó con código {completed.returncode} sin devolver JSON"
            )
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EngineProtocolError(
                f"styler-engine devolvió una respuesta no JSON: {raw[:300]}"
            ) from exc
        if not isinstance(envelope, dict):
            raise EngineProtocolError("el sobre del motor no es un objeto JSON")
        protocol = int(envelope.get("protocol_version", 0) or 0)
        if protocol != PROTOCOL_VERSION:
            raise EngineProtocolError(
                f"protocolo incompatible: motor={protocol}, cliente={PROTOCOL_VERSION}"
            )
        if not envelope.get("ok", False):
            error = envelope.get("error") or {}
            raise EngineCommandError(
                f"{error.get('code', 'ENGINE_ERROR')}: {error.get('message', 'error desconocido')}"
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise EngineProtocolError("el resultado del motor no es un objeto JSON")
        if completed.returncode != 0:
            raise EngineCommandError(
                f"styler-engine terminó con código {completed.returncode}: {completed.stderr.strip()}"
            )
        return result


def _parse_event(raw: str) -> dict[str, Any]:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EngineProtocolError(
            f"styler-engine emitió una línea JSONL inválida: {raw[:300]}"
        ) from exc
    if not isinstance(event, dict):
        raise EngineProtocolError("un evento del motor no es un objeto JSON")
    protocol = int(event.get("event_protocol_version", 0) or 0)
    if protocol != EVENT_PROTOCOL_VERSION:
        raise EngineProtocolError(
            f"protocolo de eventos incompatible: motor={protocol}, cliente={EVENT_PROTOCOL_VERSION}"
        )
    if not event.get("event") or not event.get("run_id"):
        raise EngineProtocolError("el evento no contiene event o run_id")
    return event


def _collect_lines(stream: Any, target: list[str]) -> None:
    for line in stream:
        target.append(line)


def _watch_cancellation(
    requested: threading.Event,
    stop: threading.Event,
    cancel_path: Path,
) -> None:
    while not stop.wait(0.05):
        if requested.is_set():
            _request_cancel(cancel_path)
            return


def _request_cancel(cancel_path: Path) -> None:
    try:
        cancel_path.parent.mkdir(parents=True, exist_ok=True)
        cancel_path.touch(exist_ok=True)
    except OSError:
        pass


def _cancel_then_stop(
    process: Any,
    cancel_path: Path,
    runner: PipeCraftRunner,
    *,
    grace_seconds: float = 1.5,
) -> None:
    """Solicita cancelación cooperativa y delega el cierre a PipeCraft."""
    _request_cancel(cancel_path)
    if not runner.wait_process(process, timeout=grace_seconds):
        runner.stop_process(process)


def _terminate_process(process: Any, runner: PipeCraftRunner) -> None:
    runner.stop_process(process)


def _format_engine_stream_error(return_code: int, stderr: str) -> str:
    if stderr:
        last_line = stderr.splitlines()[-1]
        try:
            envelope = json.loads(last_line)
        except json.JSONDecodeError:
            return f"styler-engine terminó con código {return_code}: {stderr[-500:]}"
        if isinstance(envelope, dict):
            error = envelope.get("error") or {}
            message = error.get("message")
            if message:
                return f"{error.get('code', 'ENGINE_ERROR')}: {message}"
    return f"styler-engine terminó con código {return_code}"


def find_engine_binary() -> Path | None:
    """Localiza el motor sin asumir una forma de instalación concreta."""
    explicit = os.environ.get(ENGINE_ENV, "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate.resolve()

    found = shutil.which("styler-engine")
    if found:
        return Path(found).resolve()

    project_root = Path(__file__).resolve().parent.parent
    candidates = (
        project_root / "bin" / "styler-engine",
        project_root / "rust" / "styler-engine" / "target" / "release" / "styler-engine",
        project_root / "rust" / "styler-engine" / "target" / "debug" / "styler-engine",
        Path.home() / ".local" / "lib" / "styler" / "styler-engine",
    )
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
