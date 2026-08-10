from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_reconciliation_module_exists_and_is_wired():
    main = (ROOT / "rust/styler-engine/src/main.rs").read_text(encoding="utf-8")
    module = (ROOT / "rust/styler-engine/src/reconciliation.rs").read_text(encoding="utf-8")
    assert "mod reconciliation;" in main
    for command in ("reconcile", "reconcile-show", "repair-plan", "adoption-preview", "registry-adopt"):
        assert f'"{command}"' in main
    for status in (
        "healthy", "version_changed", "content_modified", "externally_removed",
        "partially_present", "unverifiable", "provider_unavailable",
    ):
        assert f'"{status}"' in module


def test_reconciliation_uses_typed_read_only_queries():
    module = (ROOT / "rust/styler-engine/src/reconciliation.rs").read_text(encoding="utf-8")
    assert "Command::new(program).args(args).output()" in module
    assert "shell" not in module.lower()
    for tool in ("dpkg-query", "pacman", "rpm", "snap", "flatpak"):
        assert f'"{tool}"' in module


def test_adoption_is_external_and_does_not_invent_rollback():
    module = (ROOT / "rust/styler-engine/src/reconciliation.rs").read_text(encoding="utf-8")
    registry = (ROOT / "rust/styler-engine/src/registry.rs").read_text(encoding="utf-8")
    assert '"external_detected"' in module
    assert 'rollback_available": false' in module
    assert 'ownership:"external_detected"' in registry
    assert 'rollback_path:String::new()' in registry


def test_python_bridge_exposes_phase5_commands():
    client = (ROOT / "styler/engine_client.py").read_text(encoding="utf-8")
    cli = (ROOT / "styler/engine_cli.py").read_text(encoding="utf-8")
    for name in ("reconcile", "reconcile_show", "repair_plan", "adoption_preview", "registry_adopt"):
        assert f"def {name}" in client
    for command in ("reconcile", "reconcile-show", "repair-plan", "adoption-preview", "registry-adopt"):
        assert command in cli
