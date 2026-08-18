"""Condiciones observables y esperas inteligentes.

`wait_until` expresa un tiempo máximo. No añade una pausa fija después del éxito;
para eso existe `SleepAction` en `actions.py`.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from styler.execution.processes import ProcessRunner


class Condition(Protocol):
    name: str

    def evaluate(self) -> bool:
        ...

    def diagnostic(self) -> str:
        ...


class ConditionState(str, Enum):
    """Resultado de tres estados de una condición.

    ``PENDING`` significa "todavía no, sigue esperando".
    ``UNSATISFIABLE`` significa "ya no va a pasar": el proceso murió, la ruta
    padre desapareció, el comando no existe. Permite abortar sin gastar el
    timeout completo, que es la diferencia entre fallar en 0.4 s y fallar en 20.
    """

    PENDING = "pending"
    SATISFIED = "satisfied"
    UNSATISFIABLE = "unsatisfiable"


def evaluate_state(condition: "Condition") -> ConditionState:
    """Evalúa una condición y conserva un snapshot para diagnóstico.

    Las condiciones antiguas que solo implementan ``evaluate()`` siguen
    funcionando. El snapshot evita que capturar un fallo vuelva a ejecutar
    comandos, consultas D-Bus o sondeos de ventanas.
    """

    state_fn = getattr(condition, "state", None)
    if callable(state_fn):
        state = ConditionState(state_fn())
    else:
        state = ConditionState.SATISFIED if condition.evaluate() else ConditionState.PENDING
    try:
        detail = condition.diagnostic()
    except Exception as exc:
        detail = f"error de diagnóstico: {type(exc).__name__}: {exc}"
    try:
        setattr(condition, "_styler_last_state", state.value)
        setattr(condition, "_styler_last_diagnostic", detail)
        setattr(condition, "_styler_last_evaluated_at", time.time())
    except Exception:
        pass
    return state


class ConditionAborted(RuntimeError):
    """La espera ya no puede tener éxito, por ejemplo porque el proceso murió."""


@dataclass(frozen=True)
class WaitResult:
    satisfied: bool
    condition: str
    elapsed_seconds: float
    attempts: int
    diagnostic: str
    reason: str = "satisfied"  # satisfied | timeout | aborted | error
    history: tuple[str, ...] = ()


class CallableCondition:
    def __init__(
        self,
        name: str,
        predicate: Callable[[], bool],
        detail: Callable[[], str] | None = None,
        activity: Callable[[], Any] | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("El nombre de la condición no puede estar vacío.")
        self.name = name
        self._predicate = predicate
        self._detail = detail
        self._activity = activity
        self._last = "sin evaluar"

    def evaluate(self) -> bool:
        value = bool(self._predicate())
        self._last = f"predicate={value}"
        return value

    def diagnostic(self) -> str:
        return self._detail() if self._detail else self._last

    def activity_token(self) -> Any:
        return self._activity() if self._activity is not None else None


class LatchedCondition:
    """Conserva un hito una vez observado como satisfecho.

    Las señales de una interfaz gráfica no siempre permanecen verdaderas. Por
    ejemplo, una ventana puede estar activa durante el splash y perder el foco
    al aparecer la ventana principal. Para un proceso de arranque interesa
    saber que el hito *ocurrió*, no exigir que todos los hitos sigan verdaderos
    exactamente en el mismo sondeo.

    Antes de alcanzar el hito conserva los tres estados de la condición
    interior. Después de alcanzarlo permanece ``SATISFIED``; la condición viva
    del proceso exterior sigue detectando si la aplicación terminó.
    """

    def __init__(self, condition: Condition, *, name: str = "") -> None:
        self.condition = condition
        self.name = name or f"hito observado: {condition.name}"
        self._latched = False
        self._latched_at: float | None = None
        self._last_inner_state = ConditionState.PENDING
        self._last_inner_diagnostic = "sin evaluar"

    def state(self) -> ConditionState:
        if self._latched:
            return ConditionState.SATISFIED
        state = evaluate_state(self.condition)
        self._last_inner_state = state
        try:
            self._last_inner_diagnostic = self.condition.diagnostic()
        except Exception as exc:
            self._last_inner_diagnostic = f"error de diagnóstico: {type(exc).__name__}: {exc}"
        if state is ConditionState.SATISFIED:
            self._latched = True
            self._latched_at = time.monotonic()
            return ConditionState.SATISFIED
        return state

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        if self._latched:
            return (
                f"latched=True at={self._latched_at}; "
                f"última_observación=({self._last_inner_diagnostic})"
            )
        return (
            f"latched=False inner={self._last_inner_state.value}; "
            f"{self._last_inner_diagnostic}"
        )


class FileExistsCondition:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.name = f"existe archivo o ruta: {self.path}"

    def evaluate(self) -> bool:
        return self.path.exists()

    def diagnostic(self) -> str:
        return f"exists={self.path.exists()} path={self.path}"


class ProcessRunningCondition:
    """Detecta procesos Linux mediante `/proc` sin exigir `psutil`."""

    def __init__(self, process_name: str) -> None:
        if not process_name.strip():
            raise ValueError("process_name no puede estar vacío.")
        self.process_name = process_name.strip()
        self.name = f"proceso activo: {self.process_name}"
        self._matches: tuple[int, ...] = ()

    def evaluate(self) -> bool:
        matches: list[int] = []
        proc = Path("/proc")
        if not proc.exists():
            self._matches = ()
            return False
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(errors="replace").strip()
                cmdline = (
                    (entry / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode(errors="replace")
                )
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if self.process_name == comm or self.process_name in cmdline:
                matches.append(int(entry.name))
        self._matches = tuple(sorted(matches))
        return bool(self._matches)

    def diagnostic(self) -> str:
        return f"process={self.process_name} pids={list(self._matches)}"


class AllCondition:
    def __init__(self, name: str, conditions: Sequence[Condition]) -> None:
        if not conditions:
            raise ValueError("AllCondition requiere al menos una condición.")
        self.name = name
        self.conditions = tuple(conditions)
        self._last: list[tuple[Condition, bool]] = []
        self._states: list[tuple[Condition, ConditionState]] = []

    def state(self) -> ConditionState:
        self._states = [(c, evaluate_state(c)) for c in self.conditions]
        self._last = [
            (c, value is ConditionState.SATISFIED) for c, value in self._states
        ]
        if any(value is ConditionState.UNSATISFIABLE for _, value in self._states):
            return ConditionState.UNSATISFIABLE
        if all(value is ConditionState.SATISFIED for _, value in self._states):
            return ConditionState.SATISFIED
        return ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        if not self._last:
            return "sin evaluar"
        return "; ".join(
            f"{condition.name}={value} ({condition.diagnostic()})"
            for condition, value in self._last
        )


class AnyCondition:
    def __init__(self, name: str, conditions: Sequence[Condition]) -> None:
        if not conditions:
            raise ValueError("AnyCondition requiere al menos una condición.")
        self.name = name
        self.conditions = tuple(conditions)
        self._last: list[tuple[Condition, bool]] = []
        self._states: list[tuple[Condition, ConditionState]] = []

    def state(self) -> ConditionState:
        # Una condición ``any`` debe comportarse como las expected conditions
        # de Selenium: en cuanto una alternativa se satisface no se ejecutan
        # sondeos más caros (D-Bus, AT-SPI, procesos externos). La versión
        # anterior evaluaba siempre todos los hijos, incluso después de haber
        # detectado una ventana válida.
        states: list[tuple[Condition, ConditionState]] = []
        for condition in self.conditions:
            value = evaluate_state(condition)
            states.append((condition, value))
            if value is ConditionState.SATISFIED:
                self._states = states
                self._last = [
                    (item, state is ConditionState.SATISFIED)
                    for item, state in states
                ]
                return ConditionState.SATISFIED
        self._states = states
        self._last = [
            (c, value is ConditionState.SATISFIED) for c, value in self._states
        ]
        if all(value is ConditionState.UNSATISFIABLE for _, value in self._states):
            return ConditionState.UNSATISFIABLE
        return ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        if not self._last:
            return "sin evaluar"
        return "; ".join(
            f"{condition.name}={value} ({condition.diagnostic()})"
            for condition, value in self._last
        )


class StableValueCondition:
    """Se satisface cuando un valor deja de cambiar durante un intervalo.

    El `probe` puede representar una captura hash, tamaño de archivo, geometría de
    ventana o cualquier observación comparable. No impone un backend visual.
    """

    def __init__(
        self,
        name: str,
        probe: Callable[[], Any],
        *,
        stable_for_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if stable_for_seconds < 0:
            raise ValueError("stable_for_seconds debe ser >= 0.")
        self.name = name
        self._probe = probe
        self._stable_for = stable_for_seconds
        self._monotonic = monotonic
        self._last_value: Any = object()
        self._changed_at: float | None = None
        self._stable_elapsed = 0.0

    def evaluate(self) -> bool:
        now = self._monotonic()
        value = self._probe()
        if self._changed_at is None or value != self._last_value:
            self._last_value = value
            self._changed_at = now
            self._stable_elapsed = 0.0
            return self._stable_for == 0
        self._stable_elapsed = max(0.0, now - self._changed_at)
        return self._stable_elapsed >= self._stable_for

    def diagnostic(self) -> str:
        return (
            f"stable={self._stable_elapsed:.3f}s required={self._stable_for:.3f}s "
            f"value={self._last_value!r}"
        )

    def activity_token(self) -> Any:
        return self._last_value


def wait_until(
    condition: Condition,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.1,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    on_poll: Callable[[int, float, str], None] | None = None,
    abort_conditions: Sequence[Condition] = (),
    success_stable_seconds: float = 0.0,
    history_limit: int = 50,
    inactivity_timeout_seconds: float | None = None,
) -> WaitResult:
    """Consulta una condición hasta que se cumpla, aborte o venza el timeout.

    ``abort_conditions`` modela errores observables (proceso muerto, diálogo de
    error, permiso denegado). ``success_stable_seconds`` evita declarar éxito
    por un estado fugaz: la condición debe permanecer satisfecha durante ese
    intervalo, como las condiciones esperadas robustas de Selenium.
    """

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds debe ser >= 0.")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds debe ser > 0.")
    if success_stable_seconds < 0:
        raise ValueError("success_stable_seconds debe ser >= 0.")
    if history_limit < 1:
        raise ValueError("history_limit debe ser >= 1.")
    if inactivity_timeout_seconds is not None and inactivity_timeout_seconds < 0:
        raise ValueError("inactivity_timeout_seconds debe ser >= 0.")

    started = monotonic()
    last_activity_at = started
    last_activity_token: Any = object()
    attempts = 0
    satisfied_since: float | None = None
    history: list[str] = []

    def remember(text: str) -> tuple[str, ...]:
        history.append(text)
        if len(history) > history_limit:
            del history[:-history_limit]
        return tuple(history)

    while True:
        attempts += 1
        elapsed = max(0.0, monotonic() - started)
        try:
            for abort in abort_conditions:
                abort_state = evaluate_state(abort)
                abort_detail = abort.diagnostic()
                if abort_state is ConditionState.SATISFIED:
                    diagnostic = f"abort={abort.name}: {abort_detail}"
                    remember(f"{elapsed:.3f}s {diagnostic}")
                    return WaitResult(
                        False, condition.name, elapsed, attempts, diagnostic,
                        "aborted", tuple(history)
                    )

            condition_state = evaluate_state(condition)
            diagnostic = condition.diagnostic()
            if condition_state is ConditionState.UNSATISFIABLE:
                raise ConditionAborted(diagnostic)
        except ConditionAborted as exc:
            elapsed = max(0.0, monotonic() - started)
            remember(f"{elapsed:.3f}s aborted: {exc}")
            return WaitResult(
                False, condition.name, elapsed, attempts, str(exc), "aborted", tuple(history)
            )
        except Exception as exc:
            elapsed = max(0.0, monotonic() - started)
            detail = f"{type(exc).__name__}: {exc}"
            remember(f"{elapsed:.3f}s error: {detail}")
            return WaitResult(
                False, condition.name, elapsed, attempts, detail, "error", tuple(history)
            )

        activity_probe = getattr(condition, "activity_token", None)
        if callable(activity_probe):
            try:
                token = activity_probe()
                if token != last_activity_token:
                    last_activity_token = token
                    last_activity_at = monotonic()
            except Exception:
                # La actividad es una ayuda para ampliar una espera sana; nunca
                # puede convertir por sí sola una condición válida en error.
                pass

        elapsed = max(0.0, monotonic() - started)
        satisfied = condition_state is ConditionState.SATISFIED
        if satisfied:
            if satisfied_since is None:
                satisfied_since = monotonic()
            stable_elapsed = max(0.0, monotonic() - satisfied_since)
            poll_detail = (
                f"{diagnostic}; stable={stable_elapsed:.3f}s/"
                f"{success_stable_seconds:.3f}s"
            )
        else:
            satisfied_since = None
            stable_elapsed = 0.0
            poll_detail = diagnostic
        remember(f"{elapsed:.3f}s {condition_state.value}: {poll_detail}")
        if on_poll:
            on_poll(attempts, elapsed, poll_detail)
        if satisfied and stable_elapsed >= success_stable_seconds:
            return WaitResult(
                True, condition.name, elapsed, attempts, poll_detail, "satisfied", tuple(history)
            )
        idle_elapsed = max(0.0, monotonic() - last_activity_at)
        if (
            inactivity_timeout_seconds is not None
            and inactivity_timeout_seconds > 0
            and idle_elapsed >= inactivity_timeout_seconds
        ):
            detail = (
                f"{poll_detail}; sin actividad observable durante "
                f"{idle_elapsed:.3f}s/{inactivity_timeout_seconds:.3f}s"
            )
            remember(f"{elapsed:.3f}s idle-timeout: {detail}")
            return WaitResult(
                False, condition.name, elapsed, attempts, detail, "idle_timeout", tuple(history)
            )
        if elapsed >= timeout_seconds:
            return WaitResult(
                False, condition.name, elapsed, attempts, poll_detail, "timeout", tuple(history)
            )
        sleeper(min(poll_interval_seconds, max(0.0, timeout_seconds - elapsed)))


# ---------------------------------------------------------------------------
# Catálogo de condiciones observables
#
# Inspirado en `expected_conditions` de Selenium: predicados pequeños, puros y
# componibles. Están ordenados de más barato a más caro de evaluar.
# ---------------------------------------------------------------------------


class PathGlobCondition:
    """Existe al menos una ruta que coincide con un patrón.

    Es la generalización de `FileExistsCondition` para configuraciones que
    llevan la versión en la ruta (``GIMP/3.*``, ``blender/4.*/config``).
    """

    def __init__(self, base: str | Path, pattern: str, *, require_dir: bool = False) -> None:
        if not pattern.strip():
            raise ValueError("pattern no puede estar vacío.")
        self.base = Path(base)
        self.pattern = pattern
        self.require_dir = require_dir
        self.name = f"existe {self.base}/{pattern}"
        self._matches: tuple[str, ...] = ()

    def matches(self) -> tuple[str, ...]:
        if not self.base.is_dir():
            return ()
        found = [
            str(item)
            for item in sorted(self.base.glob(self.pattern))
            if not self.require_dir or item.is_dir()
        ]
        return tuple(found)

    def state(self) -> ConditionState:
        self._matches = self.matches()
        return ConditionState.SATISFIED if self._matches else ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return f"base={self.base} pattern={self.pattern} matches={list(self._matches)}"


class ProcessAliveCondition:
    """El PID sigue vivo. Si murió, la espera es imposible, no pendiente.

    Es la condición que evita gastar los 20 segundos completos cuando la
    aplicación se cerró sola por un error de arranque.
    """

    def __init__(
        self,
        pid: int | Callable[[], int | None],
        *,
        proc_root: str | Path = "/proc",
    ) -> None:
        self._pid_source = pid
        self.proc_root = Path(proc_root)
        self.name = "proceso vivo"
        self._last_pid: int | None = None
        self._alive = False

    def _pid(self) -> int | None:
        value = self._pid_source() if callable(self._pid_source) else self._pid_source
        return int(value) if value is not None else None

    def state(self) -> ConditionState:
        pid = self._pid()
        self._last_pid = pid
        if pid is None:
            self._alive = False
            return ConditionState.UNSATISFIABLE
        self._alive = (self.proc_root / str(pid)).exists()
        return ConditionState.SATISFIED if self._alive else ConditionState.UNSATISFIABLE

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        if self._last_pid is None:
            return "pid=desconocido (el proceso nunca se inició)"
        return f"pid={self._last_pid} alive={self._alive}"


class GoneCondition:
    """Se satisface cuando la condición interior deja de cumplirse.

    Equivale a `staleness_of` / `invisibility_of` de Selenium: esperar a que un
    archivo de bloqueo desaparezca, a que una ventana se cierre o a que un
    proceso termine.
    """

    def __init__(self, condition: Condition, *, name: str = "") -> None:
        self.condition = condition
        self.name = name or f"desaparece: {condition.name}"

    def state(self) -> ConditionState:
        inner = evaluate_state(self.condition)
        if inner is ConditionState.SATISFIED:
            return ConditionState.PENDING
        return ConditionState.SATISFIED

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return f"interior={self.condition.name} ({self.condition.diagnostic()})"


class FileStableCondition:
    """El tamaño y la fecha de una ruta dejan de cambiar durante un intervalo.

    Sirve para descargas, exportaciones y archivos que una aplicación escribe
    poco a poco.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        stable_for_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.name = f"ruta estable: {self.path}"
        self._inner = StableValueCondition(
            self.name, self._probe, stable_for_seconds=stable_for_seconds, monotonic=monotonic
        )

    def _probe(self) -> Any:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_size, int(info.st_mtime_ns))

    def state(self) -> ConditionState:
        if not self.path.exists():
            return ConditionState.PENDING
        return ConditionState.SATISFIED if self._inner.evaluate() else ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return f"path={self.path} exists={self.path.exists()} {self._inner.diagnostic()}"


class DirectoryQuiescentCondition:
    """Un directorio deja de recibir escrituras durante un intervalo.

    Es el análogo real, en un escritorio Linux, de esperar a que una página
    termine de cargar: la aplicación dejó de escribir su configuración.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        stable_for_seconds: float = 2.0,
        max_entries: int = 4000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self.name = f"directorio en reposo: {self.path}"
        self._inner = StableValueCondition(
            self.name, self._probe, stable_for_seconds=stable_for_seconds, monotonic=monotonic
        )

    def _probe(self) -> Any:
        if not self.path.is_dir():
            return None
        signature: list[tuple[str, int, int]] = []
        for index, item in enumerate(sorted(self.path.rglob("*"))):
            if index >= self.max_entries:
                break
            try:
                info = item.stat()
            except OSError:
                continue
            signature.append((item.name, info.st_size, int(info.st_mtime_ns)))
        return tuple(signature)

    def state(self) -> ConditionState:
        if not self.path.is_dir():
            return ConditionState.PENDING
        return ConditionState.SATISFIED if self._inner.evaluate() else ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return f"path={self.path} is_dir={self.path.is_dir()} {self._inner.diagnostic()}"

    def activity_token(self) -> Any:
        # Se sondea directamente para que una escritura renueve el presupuesto
        # de inactividad aunque el directorio todavía no haya quedado estable.
        return self._probe()


CommandRunner = Callable[[Sequence[str]], tuple[int, str, str]]


def default_command_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    process = ProcessRunner(timeout=10).run(list(argv), timeout=10)
    return process.returncode, process.stdout, process.stderr



class CommandOutputCondition:
    """La salida de un comando coincide con un patrón."""

    def __init__(
        self,
        argv: Sequence[str],
        pattern: str = "",
        *,
        expect_returncode: int | None = 0,
        runner: CommandRunner = default_command_runner,
        name: str = "",
    ) -> None:
        if not argv:
            raise ValueError("argv no puede estar vacío.")
        self.argv = tuple(argv)
        self.pattern = pattern
        self.expect_returncode = expect_returncode
        self._runner = runner
        self.name = name or f"salida de {' '.join(self.argv)}"
        self._last = "sin evaluar"

    def state(self) -> ConditionState:
        if shutil.which(self.argv[0]) is None and not Path(self.argv[0]).exists():
            self._last = f"comando no disponible: {self.argv[0]}"
            return ConditionState.UNSATISFIABLE
        code, out, err = self._runner(self.argv)
        self._last = f"rc={code} stdout={out.strip()[:200]!r} stderr={err.strip()[:200]!r}"
        if self.expect_returncode is not None and code != self.expect_returncode:
            return ConditionState.PENDING
        if self.pattern and not re.search(self.pattern, out):
            return ConditionState.PENDING
        return ConditionState.SATISFIED

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return self._last


class DBusNameOwnedCondition:
    """Alguien posee un nombre en el bus de sesión.

    En un escritorio Linux es el mejor indicador de "la aplicación terminó de
    cargar": GIMP, VLC y todo KDE registran su nombre justo al quedar listos.
    Funciona igual en X11 y en Wayland y no depende del servidor gráfico.
    """

    def __init__(
        self,
        bus_name: str,
        *,
        runner: CommandRunner = default_command_runner,
    ) -> None:
        if not bus_name.strip():
            raise ValueError("bus_name no puede estar vacío.")
        self.bus_name = bus_name.strip()
        self._runner = runner
        self.name = f"nombre D-Bus registrado: {self.bus_name}"
        self._last = "sin evaluar"

    def _candidates(self) -> list[Sequence[str]]:
        return [
            ("busctl", "--user", "--no-pager", "--list", "list"),
            ("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.ListNames"),
        ]

    def state(self) -> ConditionState:
        for argv in self._candidates():
            if shutil.which(argv[0]) is None:
                continue
            code, out, err = self._runner(argv)
            if code != 0:
                self._last = f"{argv[0]} rc={code} {err.strip()[:120]}"
                continue
            self._last = f"consultado con {argv[0]}"
            return (
                ConditionState.SATISFIED
                if self.bus_name in out
                else ConditionState.PENDING
            )
        self._last = "no hay busctl ni gdbus disponibles"
        return ConditionState.UNSATISFIABLE

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return f"bus_name={self.bus_name} {self._last}"


class WindowPresentCondition:
    """Existe una ventana cuya clase o título coincide.

    En X11 usa `wmctrl`/`xdotool`. En Wayland con Plasma usa `kdotool` cuando
    está disponible. Si no hay ninguna herramienta, la condición se declara
    imposible en vez de fingir que la ventana no existe todavía.
    """

    def __init__(
        self,
        pattern: str,
        *,
        runner: CommandRunner = default_command_runner,
    ) -> None:
        if not pattern.strip():
            raise ValueError("pattern no puede estar vacío.")
        self.pattern = pattern.strip()
        self._runner = runner
        self.name = f"ventana presente: {self.pattern}"
        self._last = "sin evaluar"

    def state(self) -> ConditionState:
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        candidates = (
            (("kdotool", "search", self.pattern), ("wmctrl", "-lx"),
             ("xdotool", "search", "--name", self.pattern))
            if session == "wayland"
            else (("wmctrl", "-lx"), ("xdotool", "search", "--name", self.pattern),
                  ("kdotool", "search", self.pattern))
        )
        observations: list[str] = []
        available = False
        for argv in candidates:
            if shutil.which(argv[0]) is None:
                continue
            available = True
            code, out, err = self._runner(argv)
            observations.append(
                f"{argv[0]} rc={code} salida={out.strip()[:120]!r} error={err.strip()[:80]!r}"
            )
            if argv[0] == "wmctrl":
                found = code == 0 and re.search(self.pattern, out, re.IGNORECASE) is not None
            else:
                found = code == 0 and bool(out.strip())
            if found:
                self._last = "; ".join(observations)
                return ConditionState.SATISFIED
        if not available:
            self._last = "no hay wmctrl, xdotool ni kdotool disponibles"
            return ConditionState.UNSATISFIABLE
        self._last = "; ".join(observations)
        return ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return f"pattern={self.pattern} {self._last}"


class CpuBelowCondition:
    """El proceso baja de un umbral de CPU durante un intervalo.

    Heurística barata para "terminó de cargar" cuando no hay nombre D-Bus ni
    ruta observable. Lee `/proc/<pid>/stat`, sin dependencias externas.
    """

    def __init__(
        self,
        pid: int | Callable[[], int | None],
        *,
        threshold_percent: float = 5.0,
        stable_for_seconds: float = 2.0,
        proc_root: str | Path = "/proc",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold_percent <= 0:
            raise ValueError("threshold_percent debe ser > 0.")
        self._pid_source = pid
        self.threshold_percent = threshold_percent
        self.stable_for_seconds = stable_for_seconds
        self.proc_root = Path(proc_root)
        self._monotonic = monotonic
        self.name = "uso de CPU por debajo del umbral"
        self._ticks_per_second = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0
        self._last_sample: tuple[float, float] | None = None
        self._below_since: float | None = None
        self._last_percent = -1.0

    def _pid(self) -> int | None:
        value = self._pid_source() if callable(self._pid_source) else self._pid_source
        return int(value) if value is not None else None

    def _cpu_seconds(self, pid: int) -> float | None:
        try:
            fields = (self.proc_root / str(pid) / "stat").read_text().rsplit(") ", 1)[-1].split()
        except (OSError, IndexError):
            return None
        try:
            utime, stime = float(fields[11]), float(fields[12])
        except (IndexError, ValueError):
            return None
        return (utime + stime) / self._ticks_per_second

    def state(self) -> ConditionState:
        pid = self._pid()
        if pid is None or not (self.proc_root / str(pid)).exists():
            return ConditionState.UNSATISFIABLE
        now = self._monotonic()
        cpu = self._cpu_seconds(pid)
        if cpu is None:
            return ConditionState.UNSATISFIABLE
        previous, self._last_sample = self._last_sample, (now, cpu)
        if previous is None:
            return ConditionState.PENDING
        elapsed = max(1e-6, now - previous[0])
        self._last_percent = max(0.0, (cpu - previous[1]) / elapsed * 100.0)
        if self._last_percent > self.threshold_percent:
            self._below_since = None
            return ConditionState.PENDING
        if self._below_since is None:
            self._below_since = now
        if now - self._below_since >= self.stable_for_seconds:
            return ConditionState.SATISFIED
        return ConditionState.PENDING

    def evaluate(self) -> bool:
        return self.state() is ConditionState.SATISFIED

    def diagnostic(self) -> str:
        return (
            f"cpu={self._last_percent:.1f}% umbral={self.threshold_percent:.1f}% "
            f"estable_desde={self._below_since}"
        )
