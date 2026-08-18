"""Ejecutores que permiten usar acciones/condiciones dentro del DAG existente."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from styler.execution.base import StepExecutor, emit_step_progress
from styler.planning.models import ExecutionContext, Status, StepDefinition, StepResult

from .actions import (
    ActionContext,
    DesktopClickAction,
    LaunchProcessAction,
    SleepAction,
    WaitAction,
)
from .conditions import Condition, FileExistsCondition, ProcessRunningCondition
from .desktop import AutoDesktopDriver, ElementLocator
from .specs import BuildContext, ConditionSpec, SpecError, default_condition_registry
from .controller import ApplicationController
from .profiles import ApplicationProfile


class ConditionResolutionError(ValueError):
    pass


def _build_context(ctx: ExecutionContext) -> BuildContext:
    raw_home = ctx.values.get("home")
    home = Path(raw_home).expanduser() if raw_home else Path.home()
    driver = ctx.values.get("desktop_driver") or AutoDesktopDriver()
    profiles = ctx.values.get("application_profiles", {})
    applications = ctx.values.get("applications")
    commands = ctx.values.get("automation_commands", {})
    kwargs: dict[str, Any] = {
        "home": home,
        "variables": ctx.values,
        "profiles": profiles if isinstance(profiles, dict) else {},
        "commands": commands if isinstance(commands, dict) else {},
        "desktop_driver": driver,
    }
    if isinstance(applications, dict):
        kwargs["applications"] = applications
    return BuildContext(**kwargs)


def _condition_from_mapping(raw: Any, ctx: ExecutionContext) -> Condition:
    if not isinstance(raw, dict):
        raise ConditionResolutionError("config.condition debe ser un objeto declarativo.")
    try:
        spec = ConditionSpec.from_dict(raw)
        return default_condition_registry().build(spec, _build_context(ctx))
    except (SpecError, TypeError, ValueError) as exc:
        raise ConditionResolutionError(str(exc)) from exc


def resolve_condition(step: StepDefinition, ctx: ExecutionContext) -> Condition:
    direct = step.config.get("condition")
    if direct is not None and hasattr(direct, "evaluate") and hasattr(direct, "diagnostic"):
        return direct
    if isinstance(direct, dict):
        return _condition_from_mapping(direct, ctx)

    key = str(step.config.get("condition_key") or "")
    if key:
        registry = ctx.values.get("automation_conditions", {})
        condition = registry.get(key) if isinstance(registry, dict) else None
        if condition is None:
            raise ConditionResolutionError(f"No existe la condición registrada {key!r}.")
        return condition

    # Compatibilidad con grafos antiguos. Los paquetes nuevos deben
    # preferir config.condition con el mismo formato declarativo de las recetas declarativas.
    kind = str(step.config.get("condition_type") or "")
    if kind == "file_exists":
        raw = str(step.config.get("path") or "")
        if not raw:
            raise ConditionResolutionError("file_exists requiere config.path.")
        return FileExistsCondition(Path(raw).expanduser())
    if kind == "process_running":
        name = str(step.config.get("process_name") or "")
        if not name:
            raise ConditionResolutionError("process_running requiere config.process_name.")
        return ProcessRunningCondition(name)
    raise ConditionResolutionError(
        "Declara condition, condition_key o un condition_type compatible."
    )


def resolve_abort_conditions(step: StepDefinition, ctx: ExecutionContext) -> tuple[Condition, ...]:
    raw_items = step.config.get("abort_conditions") or []
    if not isinstance(raw_items, list):
        raise ConditionResolutionError("config.abort_conditions debe ser una lista.")
    return tuple(_condition_from_mapping(item, ctx) for item in raw_items)


class SleepStepExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "sleep"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        try:
            seconds = float(step.config.get("seconds", 0.0))
            result = SleepAction(seconds).execute(
                ActionContext(dry_run=ctx.dry_run, variables=ctx.values, workdir=ctx.root)
            )
        except (TypeError, ValueError) as exc:
            return StepResult.failed(step, str(exc), "INVALID_SLEEP_CONFIG")
        status = Status.DRY_RUN if ctx.dry_run else Status.OK
        return StepResult(step.id, step.step_type, result.success, status, result.message, data=dict(result.data))


class WaitUntilStepExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "wait_until"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        try:
            condition = resolve_condition(step, ctx)
            timeout = float(step.config.get("timeout_seconds", step.timeout or 20.0))
            poll = float(step.config.get("poll_interval_seconds", 0.1))
            stable = float(step.config.get("success_stable_seconds", 0.0))
            abort_conditions = resolve_abort_conditions(step, ctx)
        except (ConditionResolutionError, TypeError, ValueError) as exc:
            return StepResult.failed(step, str(exc), "WAIT_CONDITION_CONFIG_ERROR")

        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                f"Se esperaría hasta {timeout:g} s a que se cumpla: {condition.name}.",
                data={
                    "condition": condition.name,
                    "timeout_seconds": timeout,
                    "poll_interval_seconds": poll,
                    "success_stable_seconds": stable,
                    "abort_conditions": [item.name for item in abort_conditions],
                },
            )

        def on_poll(attempt: int, elapsed: float, diagnostic: str) -> None:
            progress = min(0.95, elapsed / timeout) if timeout > 0 else None
            emit_step_progress(
                ctx,
                step,
                progress,
                f"Esperando {condition.name} · intento {attempt}",
                message=diagnostic,
            )

        result = WaitAction(
            condition,
            timeout_seconds=timeout,
            poll_interval_seconds=poll,
            on_poll=on_poll,
            abort_conditions=abort_conditions,
            success_stable_seconds=stable,
        ).execute(ActionContext(dry_run=ctx.dry_run, variables=ctx.values, workdir=ctx.root))
        if not result.success:
            status = Status.TIMEOUT if result.data.get("reason") == "timeout" else Status.FAILED
            return StepResult(
                step.id,
                step.step_type,
                False,
                status,
                result.message,
                data={"error_code": "WAIT_CONDITION_FAILED", **dict(result.data)},
            )
        return StepResult(step.id, step.step_type, True, Status.OK, result.message, data=dict(result.data))


class DesktopClickStepExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "desktop_click"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        try:
            locator = ElementLocator.from_mapping(step.config.get("locator"))
            driver = ctx.values.get("desktop_driver") or AutoDesktopDriver()
            result = DesktopClickAction(driver, locator).execute(
                ActionContext(dry_run=ctx.dry_run, variables=ctx.values, workdir=ctx.root)
            )
        except (TypeError, ValueError) as exc:
            return StepResult.failed(step, str(exc), "DESKTOP_CLICK_CONFIG_ERROR")
        status = Status.DRY_RUN if ctx.dry_run else (Status.OK if result.success else Status.FAILED)
        data = dict(result.data)
        if not result.success:
            data.setdefault("error_code", "DESKTOP_CLICK_FAILED")
        return StepResult(
            step.id, step.step_type, result.success, status, result.message, data=data
        )


class LaunchApplicationStepExecutor(StepExecutor):
    @property
    def step_type(self) -> str:
        return "launch_application"

    def run(self, step: StepDefinition, ctx: ExecutionContext) -> StepResult:
        argv = list(step.config.get("argv") or [])
        profile_id = str(step.config.get("profile_id") or "")
        profiles = ctx.values.get("application_profiles", {})
        profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
        if not argv or not isinstance(profile, ApplicationProfile):
            return StepResult.failed(
                step,
                "launch_application requiere argv y un ApplicationProfile registrado.",
                "APPLICATION_LAUNCH_CONFIG_ERROR",
            )
        if ctx.dry_run:
            return StepResult(
                step.id,
                step.step_type,
                True,
                Status.DRY_RUN,
                f"Se abriría {profile.id} y se esperaría hasta que alcance READY.",
                data={
                    "argv": argv,
                    "profile_id": profile.id,
                    "startup_timeout_seconds": profile.startup_timeout_seconds,
                    "settle_seconds": profile.settle_seconds,
                },
            )

        action_ctx = ActionContext(dry_run=False, variables=ctx.values, workdir=ctx.root)
        controller = ApplicationController(source=profile.id)
        report = controller.launch_wait_and_settle(
            LaunchProcessAction(argv),
            profile,
            action_ctx,
        )
        process = action_ctx.variables.get("last_process")
        stopped_after_failure = False
        if not report.success and bool(step.config.get("terminate_on_failure", True)):
            if process is not None and getattr(process, "poll", lambda: 0)() is None:
                try:
                    process.terminate()
                    process.wait(timeout=float(step.config.get("terminate_timeout_seconds", 8)))
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=3)
                    except Exception:
                        pass
                stopped_after_failure = True
        data: dict[str, Any] = {
            "state": report.final_state.value,
            "elapsed_seconds": report.elapsed_seconds,
            "launch": dict(report.launch.data),
            "readiness": dict(report.readiness.data) if report.readiness else None,
            "settle": dict(report.settle.data) if report.settle else None,
            "stopped_after_failure": stopped_after_failure,
        }
        if not report.success:
            return StepResult(
                step.id,
                step.step_type,
                False,
                Status.FAILED,
                report.readiness.message if report.readiness else report.launch.message,
                data={"error_code": "APPLICATION_NOT_READY", **data},
            )
        return StepResult(
            step.id,
            step.step_type,
            True,
            Status.DRY_RUN if ctx.dry_run else Status.OK,
            f"{profile.id} abrió y alcanzó READY.",
            data=data,
        )
