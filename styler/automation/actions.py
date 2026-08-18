"""Comandos reutilizables del motor de automatización."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from styler.execution.processes import ProcessRunner

from .desktop import DesktopDriver, ElementLocator
from .conditions import (
    Condition,
    ConditionState,
    WaitResult,
    evaluate_state,
    wait_until,
)


def condition_is_satisfied(condition: Condition) -> bool:
    return evaluate_state(condition) is ConditionState.SATISFIED


@dataclass
class ActionContext:
    dry_run: bool = True
    variables: dict[str, Any] = field(default_factory=dict)
    workdir: Path | None = None


@dataclass(frozen=True)
class ActionResult:
    success: bool
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)


class Action(Protocol):
    name: str

    def execute(self, context: ActionContext) -> ActionResult:
        ...


class SleepAction:
    """Pausa fija. No comprueba que una aplicación esté lista."""

    def __init__(self, seconds: float, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        if seconds < 0:
            raise ValueError("seconds debe ser >= 0.")
        self.seconds = seconds
        self._sleeper = sleeper
        self.name = f"sleep {seconds:g}s"

    def execute(self, context: ActionContext) -> ActionResult:
        if not context.dry_run:
            self._sleeper(self.seconds)
        return ActionResult(
            True,
            f"Pausa fija completada: {self.seconds:g} s.",
            {"seconds": self.seconds, "dry_run": context.dry_run},
        )
class WaitAction:
    """Espera basada en condición; deliberadamente distinta de `SleepAction`."""

    def __init__(
        self,
        condition: Condition,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.1,
        on_poll: Callable[[int, float, str], None] | None = None,
        abort_conditions: Sequence[Condition] = (),
        success_stable_seconds: float = 0.0,
    ) -> None:
        self.condition = condition
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.on_poll = on_poll
        self.abort_conditions = tuple(abort_conditions)
        self.success_stable_seconds = success_stable_seconds
        self.name = f"wait until {condition.name}"

    def execute(self, context: ActionContext) -> ActionResult:
        timeout = self.timeout_seconds
        deadline = context.variables.get("deadline")
        if deadline is not None:
            remaining = max(0.0, float(deadline) - time.monotonic())
            timeout = min(timeout, remaining)
        result: WaitResult = wait_until(
            self.condition,
            timeout_seconds=timeout,
            poll_interval_seconds=self.poll_interval_seconds,
            on_poll=self.on_poll,
            abort_conditions=self.abort_conditions,
            success_stable_seconds=self.success_stable_seconds,
        )
        return ActionResult(
            result.satisfied,
            (
                f"Condición satisfecha después de {result.elapsed_seconds:.3f} s."
                if result.satisfied
                else f"Espera terminada ({result.reason}) después de "
                f"{result.elapsed_seconds:.3f} s: {result.diagnostic}"
            ),
            {
                "condition": result.condition,
                "attempts": result.attempts,
                "elapsed_seconds": result.elapsed_seconds,
                "diagnostic": result.diagnostic,
                "reason": result.reason,
                "history": list(result.history),
            },
        )


class DesktopClickAction:
    """Hace clic en un control localizado semánticamente por el driver."""

    def __init__(self, driver: DesktopDriver, locator: ElementLocator) -> None:
        self.driver = driver
        self.locator = locator
        self.name = f"desktop click {locator.to_dict()}"

    def execute(self, context: ActionContext) -> ActionResult:
        if context.dry_run:
            available = self.driver.available()
            matches = self.driver.find_all(self.locator) if available else ()
            return ActionResult(
                True,
                (
                    "Vista previa: el control fue localizado; no se hizo clic."
                    if matches
                    else (
                        "Vista previa válida, pero el control no está visible en este momento."
                        if available
                        else f"Vista previa válida; backend de escritorio no disponible: {self.driver.unavailable_reason()}"
                    )
                ),
                {
                    "locator": self.locator.to_dict(),
                    "matches": len(matches),
                    "backend_available": available,
                    "dry_run": True,
                },
            )
        try:
            snapshot = self.driver.click(self.locator)
        except Exception as exc:
            return ActionResult(
                False,
                f"No se pudo activar el control: {type(exc).__name__}: {exc}",
                {"locator": self.locator.to_dict()},
            )
        return ActionResult(
            True,
            f"Control activado mediante {self.driver.name}.",
            {"locator": self.locator.to_dict(), "element": snapshot.to_dict()},
        )


class LaunchProcessAction:
    """Inicia una aplicación mediante la frontera única de PipeCraft."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not argv:
            raise ValueError("argv no puede estar vacío.")
        self.argv = tuple(argv)
        self.env = dict(env) if env else None
        self.name = f"launch {self.argv[0]}"

    def execute(self, context: ActionContext) -> ActionResult:
        if context.dry_run:
            return ActionResult(True, "Dry-run: el proceso no se inició.", {"argv": self.argv})
        try:
            process = ProcessRunner().spawn_detached(
                list(self.argv),
                cwd=str(context.workdir) if context.workdir else None,
                env=self.env,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return ActionResult(False, f"No se pudo iniciar el proceso: {exc}", {"argv": self.argv})
        context.variables["last_process"] = process
        return ActionResult(
            True,
            f"Proceso iniciado con PID {process.pid}.",
            {"pid": process.pid, "argv": self.argv},
        )


class FunctionAction:
    """Adapter para servicios y ejecutores ya existentes en Styler 0.1."""

    def __init__(self, name: str, function: Callable[[ActionContext], Any]) -> None:
        self.name = name
        self._function = function

    def execute(self, context: ActionContext) -> ActionResult:
        try:
            value = self._function(context)
        except Exception as exc:
            return ActionResult(False, f"{type(exc).__name__}: {exc}")
        if isinstance(value, ActionResult):
            return value
        if value is False:
            return ActionResult(False, f"La acción devolvió False: {self.name}")
        return ActionResult(True, f"Acción completada: {self.name}", {"value": value})


class SequenceAction:
    """Composite para secuencias locales pequeñas, no para dependencias DAG."""

    def __init__(self, name: str, actions: Sequence[Action]) -> None:
        if not actions:
            raise ValueError("actions no puede estar vacío.")
        self.name = name
        self.actions = tuple(actions)

    def execute(self, context: ActionContext) -> ActionResult:
        results: list[ActionResult] = []
        for action in self.actions:
            result = action.execute(context)
            results.append(result)
            if not result.success:
                return ActionResult(
                    False,
                    f"{self.name} falló en {action.name}: {result.message}",
                    {"results": results},
                )
        return ActionResult(True, f"Secuencia completada: {self.name}", {"results": results})


# ---------------------------------------------------------------------------
# Combinadores
#
# Una acción compuesta es una acción. Con estos combinadores, cualquier flujo
# —incluida una receta generada— se expresa como un árbol de acciones sin escribir
# código nuevo, y por tanto puede serializarse, editarse y volver a componerse.
# ---------------------------------------------------------------------------


class NoteAction:
    """Deja un mensaje en el resultado sin producir ningún efecto."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.name = f"nota: {message[:40]}"

    def execute(self, context: ActionContext) -> ActionResult:
        return ActionResult(True, self.message, {"message": self.message})


class RetryAction:
    """Repite una acción hasta que tenga éxito o se agoten los intentos."""

    def __init__(
        self,
        action: Action,
        *,
        attempts: int = 2,
        delay_seconds: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts debe ser >= 1.")
        if delay_seconds < 0:
            raise ValueError("delay_seconds debe ser >= 0.")
        self.action = action
        self.attempts = attempts
        self.delay_seconds = delay_seconds
        self._sleeper = sleeper
        self.name = f"retry({attempts}) {action.name}"

    def execute(self, context: ActionContext) -> ActionResult:
        results: list[ActionResult] = []
        for attempt in range(1, self.attempts + 1):
            result = self.action.execute(context)
            results.append(result)
            if result.success:
                return ActionResult(
                    True,
                    f"{self.action.name} tuvo éxito en el intento {attempt}.",
                    {"attempts": attempt, "results": results},
                )
            if attempt < self.attempts and self.delay_seconds and not context.dry_run:
                self._sleeper(self.delay_seconds)
        return ActionResult(
            False,
            f"{self.action.name} falló después de {self.attempts} intentos: {results[-1].message}",
            {"attempts": self.attempts, "results": results},
        )


class TimeoutAction:
    """Impone un plazo máximo a una acción compuesta.

    No interrumpe por la fuerza una operación bloqueante —eso exigiría matar el
    proceso y no siempre es seguro—, pero sí publica un plazo en el contexto:
    las esperas interiores recortan su propio timeout para respetarlo, y si el
    conjunto se pasa del plazo la acción se declara fallida en vez de fingir
    éxito tardío.
    """

    def __init__(
        self,
        action: Action,
        *,
        seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if seconds <= 0:
            raise ValueError("seconds debe ser > 0.")
        self.action = action
        self.seconds = seconds
        self._monotonic = monotonic
        self.name = f"timeout({seconds:g}s) {action.name}"

    def execute(self, context: ActionContext) -> ActionResult:
        started = self._monotonic()
        previous = context.variables.get("deadline")
        deadline = started + self.seconds
        context.variables["deadline"] = min(deadline, previous) if previous else deadline
        try:
            result = self.action.execute(context)
        finally:
            if previous is None:
                context.variables.pop("deadline", None)
            else:
                context.variables["deadline"] = previous
        elapsed = max(0.0, self._monotonic() - started)
        if elapsed > self.seconds:
            return ActionResult(
                False,
                f"{self.action.name} superó el plazo de {self.seconds:g} s ({elapsed:.3f} s).",
                {"elapsed_seconds": elapsed, "limit_seconds": self.seconds, "inner": result.data},
            )
        return result


class ConditionalAction:
    """Ejecuta una rama u otra según una condición observable."""

    def __init__(
        self,
        condition: Condition,
        then_action: Action,
        else_action: Action | None = None,
    ) -> None:
        self.condition = condition
        self.then_action = then_action
        self.else_action = else_action
        self.name = f"si {condition.name}"

    def execute(self, context: ActionContext) -> ActionResult:
        try:
            satisfied = condition_is_satisfied(self.condition)
        except Exception as exc:
            return ActionResult(False, f"No se pudo evaluar {self.condition.name}: {exc}")
        branch = self.then_action if satisfied else self.else_action
        if branch is None:
            return ActionResult(
                True,
                f"Condición {self.condition.name}={satisfied}; no había rama que ejecutar.",
                {"condition": self.condition.name, "satisfied": satisfied, "skipped": True},
            )
        result = branch.execute(context)
        return ActionResult(
            result.success,
            result.message,
            {"condition": self.condition.name, "satisfied": satisfied, **dict(result.data)},
        )


class TryFinallyAction:
    """Ejecuta una limpieza pase lo que pase.

    Es imprescindible para el ciclo abrir → trabajar → cerrar: si el cuerpo
    falla, la aplicación no puede quedarse abierta bloqueando su configuración.
    """

    def __init__(self, name: str, body: Action, cleanup: Action) -> None:
        self.name = name
        self.body = body
        self.cleanup = cleanup

    def execute(self, context: ActionContext) -> ActionResult:
        body_result = self.body.execute(context)
        cleanup_result = self.cleanup.execute(context)
        data = {"body": body_result.data, "cleanup": cleanup_result.data}
        if not body_result.success:
            return ActionResult(False, f"{self.name}: {body_result.message}", data)
        if not cleanup_result.success:
            return ActionResult(
                False, f"{self.name}: la limpieza falló: {cleanup_result.message}", data
            )
        return ActionResult(True, f"{self.name} completada.", data)


class ForEachAction:
    """Repite una acción sobre una lista de valores.

    El valor de cada vuelta queda en ``context.variables[variable]`` para que la
    acción interior lo use.
    """

    def __init__(self, variable: str, values: Sequence[Any], action: Action) -> None:
        if not variable.strip():
            raise ValueError("variable no puede estar vacía.")
        self.variable = variable
        self.values = tuple(values)
        self.action = action
        self.name = f"para cada {variable} ({len(self.values)})"

    def execute(self, context: ActionContext) -> ActionResult:
        results: list[ActionResult] = []
        previous = context.variables.get(self.variable)
        try:
            for value in self.values:
                context.variables[self.variable] = value
                result = self.action.execute(context)
                results.append(result)
                if not result.success:
                    return ActionResult(
                        False,
                        f"{self.name} falló con {self.variable}={value!r}: {result.message}",
                        {"results": results, "failed_value": value},
                    )
        finally:
            if previous is None:
                context.variables.pop(self.variable, None)
            else:
                context.variables[self.variable] = previous
        return ActionResult(True, f"{self.name} completada.", {"results": results})


class StopProcessAction:
    """Cierra el proceso lanzado: primero `terminate`, después `kill`."""

    def __init__(
        self,
        *,
        variable: str = "last_process",
        grace_seconds: float = 5.0,
    ) -> None:
        if grace_seconds < 0:
            raise ValueError("grace_seconds debe ser >= 0.")
        self.variable = variable
        self.grace_seconds = grace_seconds
        self.name = "detener aplicación"

    def execute(self, context: ActionContext) -> ActionResult:
        process = context.variables.get(self.variable)
        if process is None:
            return ActionResult(True, "No había ningún proceso que detener.", {"stopped": False})
        if context.dry_run:
            return ActionResult(True, "Dry-run: el proceso no se detuvo.", {"stopped": False})
        try:
            process.terminate()
            try:
                process.wait(timeout=self.grace_seconds)
                forced = False
            except Exception:
                process.kill()
                process.wait(timeout=self.grace_seconds)
                forced = True
        except Exception as exc:
            return ActionResult(False, f"No se pudo detener el proceso: {exc}", {"stopped": False})
        context.variables.pop(self.variable, None)
        return ActionResult(
            True,
            "Proceso detenido por la fuerza." if forced else "Proceso detenido.",
            {"stopped": True, "forced": forced},
        )
