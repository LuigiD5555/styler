"""
styler.diff
=============
Compara "instalación base" contra "sistema personalizado" y produce
diferencias crudas (RawChange). No interpreta nada todavía — eso es
trabajo de interpreter.py.
"""

from __future__ import annotations

from styler.models import State, RawChange, ChangeKind


def diff_states(base: State, target: State) -> list[RawChange]:
    changes: list[RawChange] = []
    changes.extend(_diff_packages(base, target))
    changes.extend(_diff_files(base, target))
    changes.extend(_diff_services(base, target))
    return changes


def _diff_packages(base: State, target: State) -> list[RawChange]:
    base_pkgs = {(p.manager, p.name): p for p in base.packages}
    target_pkgs = {(p.manager, p.name): p for p in target.packages}

    changes = []
    for key, pkg in target_pkgs.items():
        if key not in base_pkgs:
            changes.append(RawChange(
                kind=ChangeKind.PACKAGE_ADDED,
                subject=pkg.name,
                detail={"manager": pkg.manager, "version": pkg.version, "architecture": pkg.architecture},
            ))
    for key, pkg in base_pkgs.items():
        if key not in target_pkgs:
            changes.append(RawChange(
                kind=ChangeKind.PACKAGE_REMOVED,
                subject=pkg.name,
                detail={"manager": pkg.manager},
            ))
    return changes


def _diff_files(base: State, target: State) -> list[RawChange]:
    base_files = {f.path: f for f in base.files}
    target_files = {f.path: f for f in target.files}

    changes = []
    for path, entry in target_files.items():
        if path not in base_files:
            changes.append(RawChange(
                kind=ChangeKind.FILE_ADDED,
                subject=path,
                detail={"checksum": entry.checksum, "size": entry.size, "owner_hint": entry.owner_hint},
            ))
        elif base_files[path].checksum != entry.checksum:
            changes.append(RawChange(
                kind=ChangeKind.FILE_MODIFIED,
                subject=path,
                detail={"checksum": entry.checksum, "size": entry.size, "owner_hint": entry.owner_hint},
            ))
    for path in base_files:
        if path not in target_files:
            changes.append(RawChange(kind=ChangeKind.FILE_REMOVED, subject=path))
    return changes


def _diff_services(base: State, target: State) -> list[RawChange]:
    base_svc = {(s.name, s.scope) for s in base.services}
    target_svc = {(s.name, s.scope) for s in target.services}

    changes = []
    for name, scope in target_svc - base_svc:
        changes.append(RawChange(kind=ChangeKind.SERVICE_ADDED, subject=name, detail={"scope": scope}))
    for name, scope in base_svc - target_svc:
        changes.append(RawChange(kind=ChangeKind.SERVICE_REMOVED, subject=name, detail={"scope": scope}))
    return changes
