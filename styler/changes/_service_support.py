"""Servicio de cambios semánticos de Styler.

PhotoGIMP es el primer cambio completamente descrito. La implementación
reutiliza el catálogo, el resolver, el compilador y el motor DAG existentes,
pero presenta una sola intención al usuario y elige una estrategia automática
o asistida según el proveedor de GIMP.
"""
from __future__ import annotations

import json
import hashlib
import os
import errno
import tempfile
import platform
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from styler.component_catalog.compiler import compile_workflow
from styler.change_recipe import compile_recipe
from styler.declarative_changes import dependency_order, load_declarative_changes
from styler.component_catalog.executors import PHOTOGIMP_RELEASE_PREFIX, extended_registry
from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.receipts import (
    ReceiptJournal,
    ReceiptKind,
    StepReceipt,
    all_checkpoint_receipts,
    compile_rollback_workflow,
    prune_system_checkpoints,
)
from styler.methods import (
    MethodContext,
    MethodPolicy,
    annotate_workflow_methods,
    default_method_registry,
)
from styler.portable import GraphDefinition, InstalledPackage, PackageType, PortableLibrary
from styler.privileges import keepalive_for
from styler import workflow as workflow_runtime
from styler.planning.graph import drop_step, topological_order
from styler.planning.models import ExecutionContext, PhaseDefinition, StepDefinition, WorkflowDefinition
from styler.target import detect_target

from .storage import (
    ChangeStateWriteError,
    is_storage_failure,
    mount_status,
    probe_directory_writable,
    read_json,
    save_record,
    storage_error,
    write_json,
)

from .models import (
    AutomationLevel,
    BatchProgressCallback,
    ChangeBatchExecutionResult,
    ChangeBatchPlan,
    ChangeBatchProgressEvent,
    ChangeCard,
    ChangeExecutionResult,
    ChangeOption,
    ChangePhase,
    ChangePlan,
    ChangeProgressEvent,
    ChangeWorkflowPair,
    ChangeStatus,
    ProgressCallback,
    ProviderOption,
)


PROVIDER_LABELS = {
    "flatpak": "Flathub (Flatpak)",
    "apt": "APT",
    "pacman": "Pacman",
    "aur": "AUR",
    "rpm": "DNF/RPM",
    "zypper": "Zypper",
    "snap": "Snap",
    "appimage": "AppImage",
}

PROVIDER_COMMANDS = {
    "flatpak": ("flatpak",),
    "apt": ("apt-get", "dpkg-query"),
    "pacman": ("pacman",),
    "aur": ("yay", "paru"),
    "rpm": ("dnf", "rpm"),
    "zypper": ("zypper",),
    "snap": ("snap",),
    "appimage": (),
}

# Carpetas de configuración de GIMP: 3.0, 3.2, 4.0, 10.4… No se codifica la
# familia 3.x, para que una versión mayor futura se detecte igual.
_CONFIG_VERSION_DIR = re.compile(r"\d+\.\d+")



CONTINUATION_STATUSES = {
    ChangeStatus.FAILED,
    ChangeStatus.INTEGRATING,
    ChangeStatus.NEEDS_ATTENTION,
}


CHANGE_NAMES = {
    "photogimp": "PhotoGIMP",
}


def _change_name(change_id: str) -> str:
    """Nombre legible sin acoplar la reversión a un pipeline concreto."""
    return CHANGE_NAMES.get(change_id, change_id.replace("-", " ").title())
