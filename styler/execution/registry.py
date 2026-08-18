"""Composición explícita del registro de ejecutores de Styler.

La composición vive fuera de las clases base para que automatización, undo y
operaciones normales dependan de contratos, no unos de otros.
"""
from __future__ import annotations

from .base import ExecutorRegistry


def default_registry() -> ExecutorRegistry:
    from styler.automation.executors import (
        DesktopClickStepExecutor,
        LaunchApplicationStepExecutor,
        SleepStepExecutor,
        WaitUntilStepExecutor,
    )
    from styler.execution.executors import (
        AptReconcileExecutor,
        FileOverlayExecutor,
        NoteExecutor,
        PackageInstallExecutor,
        ServiceEnableExecutor,
    )
    from styler.execution.undo import (
        PackageUninstallExecutor,
        RemovePathsExecutor,
        RestoreBackupExecutor,
        RestoreCheckpointExecutor,
        RestoreSettingExecutor,
        UndoNoteExecutor,
    )

    registry = ExecutorRegistry()
    for executor in (
        NoteExecutor(),
        PackageInstallExecutor(),
        AptReconcileExecutor(),
        FileOverlayExecutor(),
        ServiceEnableExecutor(),
        SleepStepExecutor(),
        WaitUntilStepExecutor(),
        DesktopClickStepExecutor(),
        LaunchApplicationStepExecutor(),
        RestoreBackupExecutor(),
        RestoreCheckpointExecutor(),
        RemovePathsExecutor(),
        PackageUninstallExecutor(),
        RestoreSettingExecutor(),
        UndoNoteExecutor(),
    ):
        registry.register(executor)
    return registry
