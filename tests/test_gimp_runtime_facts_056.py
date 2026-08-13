from __future__ import annotations

import subprocess
from pathlib import Path

from styler.component_catalog.compiler import compile_workflow
from styler.component_catalog.executors import (
    BackupConfigExecutor,
    InitializeFlatpakAppExecutor,
    ResolveFlatpakAppFactsExecutor,
)
from styler.component_catalog.loader import load
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import resolve
from styler.flatpak_facts import (
    FlatpakApplicationFacts,
    config_schema_from_version,
    inspect_flatpak_application,
    load_flatpak_facts,
    save_flatpak_facts,
)
from styler.runtime.models import ExecutionContext, Status, StepDefinition


def test_config_schema_is_derived_from_installed_version() -> None:
    assert config_schema_from_version("3.0.4") == "3.0"
    assert config_schema_from_version("2.10.38") == "2.10"
    assert config_schema_from_version("GIMP 3.2.0-rc1") == "3.2"
    assert config_schema_from_version("stable") == ""


def test_flatpak_facts_parse_version_branch_ref_and_commit() -> None:
    def runner(argv):
        if argv[:3] == ["flatpak", "list", "--app"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "org.gimp.GIMP\t3.0.4\tstable\tx86_64\tflathub\tuser\n",
                "",
            )
        if "--show-ref" in argv:
            return subprocess.CompletedProcess(argv, 0, "app/org.gimp.GIMP/x86_64/stable\n", "")
        if "--show-commit" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        return subprocess.CompletedProcess(argv, 0, "Name: GIMP\nVersion: 3.0.4\n", "")

    facts = inspect_flatpak_application("org.gimp.GIMP", runner=runner)

    assert facts.installed is True
    assert facts.version == "3.0.4"
    assert facts.config_schema == "3.0"
    assert facts.branch == "stable"
    assert facts.ref == "app/org.gimp.GIMP/x86_64/stable"
    assert facts.commit == "abc123"


def test_flatpak_facts_fall_back_to_gimp_version_command() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv):
        calls.append(tuple(argv))
        if argv[:3] == ["flatpak", "list", "--app"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "org.gimp.GIMP\t\tstable\tx86_64\tflathub\tuser\n",
                "",
            )
        if argv == ["flatpak", "info", "org.gimp.GIMP"]:
            return subprocess.CompletedProcess(argv, 0, "Name: GIMP\nBranch: stable\n", "")
        if argv == ["flatpak", "run", "org.gimp.GIMP", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "GNU Image Manipulation Program version 3.0.6\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "unsupported")

    facts = inspect_flatpak_application("org.gimp.GIMP", runner=runner)

    assert facts.version == "3.0.6"
    assert facts.config_schema == "3.0"
    assert ("flatpak", "run", "org.gimp.GIMP", "--version") in calls


def test_resolve_facts_executor_persists_exact_config_path(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    root = tmp_path / "library"
    facts = FlatpakApplicationFacts(
        application_id="org.gimp.GIMP",
        installed=True,
        version="3.0.4",
        branch="stable",
        architecture="x86_64",
        origin="flathub",
        ref="app/org.gimp.GIMP/x86_64/stable",
        commit="abc123",
        config_schema="3.0",
        observed_at=1.0,
    )
    monkeypatch.setattr(
        "styler.component_catalog.executors.inspect_flatpak_application",
        lambda app_id: facts,
    )
    step = StepDefinition(
        "app.gimp.resolve-facts",
        "resolve_flatpak_app_facts",
        config={
            "application_id": "org.gimp.GIMP",
            "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        },
    )

    result = ResolveFlatpakAppFactsExecutor().run(
        step,
        ExecutionContext(root=root, dry_run=False, values={"home": home}),
    )

    assert result.success is True
    assert result.data["config_schema"] == "3.0"
    assert result.data["config_path"] == str(
        home / ".var/app/org.gimp.GIMP/config/GIMP/3.0"
    )
    stored = load_flatpak_facts(root, "org.gimp.GIMP")
    assert stored is not None
    assert stored["version"] == "3.0.4"
    assert stored["config_path"] == result.data["config_path"]


def test_backup_uses_versioned_path_from_saved_facts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = tmp_path / "library"
    config_root = home / ".var/app/org.gimp.GIMP/config/GIMP"
    config_path = config_root / "2.10"
    config_path.mkdir(parents=True)
    (config_path / "gimprc").write_text("original", encoding="utf-8")
    save_flatpak_facts(
        root,
        FlatpakApplicationFacts(
            application_id="org.gimp.GIMP",
            installed=True,
            version="2.10.38",
            branch="stable",
            config_schema="2.10",
            observed_at=1.0,
        ),
        config_root=str(config_root),
        config_path=str(config_path),
    )
    step = StepDefinition(
        "app.photogimp.backup",
        "backup_config",
        config={
            "backup_source": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
            "runtime_facts_application_id": "org.gimp.GIMP",
        },
    )

    result = BackupConfigExecutor().run(
        step,
        ExecutionContext(root=root, dry_run=False, values={"home": home}),
    )

    assert result.success is True
    assert result.data["source"] == str(config_path)
    assert Path(result.data["backup"]).joinpath("gimprc").read_text() == "original"


def test_initialize_reconcile_persists_real_discovered_path(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    root = tmp_path / "library"
    config_root = home / ".var/app/org.gimp.GIMP/config/GIMP"
    actual = config_root / "3.0"
    actual.mkdir(parents=True)
    save_flatpak_facts(
        root,
        FlatpakApplicationFacts(
            application_id="org.gimp.GIMP",
            installed=True,
            version="3.0.6",
            branch="stable",
            config_schema="3.0",
            observed_at=1.0,
        ),
        config_root=str(config_root),
        config_path=str(actual),
        initialization_completed=True,
        initialized_application_version="3.0.6",
        initialized_config_schema="3.0",
        initialized_config_path=str(actual),
    )
    monkeypatch.setattr(
        InitializeFlatpakAppExecutor,
        "_flatpak_state",
        classmethod(lambda cls, app_id: (False, False, "cerrado")),
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    step = StepDefinition(
        "app.gimp.initialize",
        "initialize_flatpak_app",
        config={
            "application_id": "org.gimp.GIMP",
            "config_root": "${HOME}/.var/app/org.gimp.GIMP/config/GIMP",
        },
    )

    result = InitializeFlatpakAppExecutor().reconcile(
        step,
        ExecutionContext(root=root, dry_run=False, values={"home": home}),
    )

    assert result is not None and result.success is True
    stored = load_flatpak_facts(root, "org.gimp.GIMP")
    assert stored is not None
    assert stored["config_path"] == str(actual)


def test_photogimp_dag_resolves_version_before_initialization() -> None:
    registry = ComponentRegistry.from_report(load(root="."))
    resolution = resolve(registry, ["app.photogimp"], family="ubuntu")
    compiled = compile_workflow(registry, resolution)
    by_id = {step.id: step for step in compiled.workflow.steps}

    facts = by_id["app.gimp.resolve-facts"]
    initialize = by_id["app.gimp.initialize"]
    backup = by_id["app.photogimp.backup"]
    install = by_id["app.photogimp.install"]

    assert facts.needs == ["app.gimp.install"]
    assert facts.config["application_id"] == "org.gimp.GIMP"
    assert initialize.needs == [facts.id]
    assert "expected_config_schema" not in initialize.config
    assert backup.config["runtime_facts_application_id"] == "org.gimp.GIMP"
    assert install.config["runtime_facts_application_id"] == "org.gimp.GIMP"
