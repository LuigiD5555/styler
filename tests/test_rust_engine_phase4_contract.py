from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_phase4_registry_module_exists():
    source = read("rust/styler-engine/src/registry.rs")
    for token in (
        "InstallationRecord",
        "RegistryEvent",
        "register_execution",
        "uninstall_plan",
        "el registro no puede ser un enlace simbólico",
        "styler_managed",
    ):
        assert token in source


def test_engine_exposes_registry_commands():
    source = read("rust/styler-engine/src/main.rs")
    for command in ("registry-list", "registry-show", "registry-audit", "uninstall-plan"):
        assert f'"{command}"' in source


def test_executor_supports_evidence_based_uninstall():
    source = read("rust/styler-engine/src/execution/actions.rs")
    for action in (
        "uninstall_apt",
        "uninstall_pacman",
        "uninstall_dnf",
        "uninstall_zypper",
        "uninstall_snap",
        "uninstall_flatpak",
        "remove_managed_artifact",
        "managed_installation_removed",
    ):
        assert action in source


def test_python_bridge_exposes_inventory():
    client = read("styler/engine_client.py")
    cli = read("styler/engine_cli.py")
    for method in ("registry_list", "registry_show", "registry_audit", "uninstall_plan"):
        assert f"def {method}" in client
    assert "registry-list" in cli
    assert "uninstall-plan" in cli
