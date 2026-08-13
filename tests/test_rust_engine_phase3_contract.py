from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
RUST = ROOT / "rust" / "styler-engine"


def test_phase3_dependencies_are_declared():
    cargo = tomllib.loads((RUST / "Cargo.toml").read_text())
    deps = cargo["dependencies"]
    for name in ("sha2", "reqwest", "zip", "tar", "flate2", "tempfile"):
        assert name in deps


def test_artifact_module_has_mandatory_integrity_and_path_guards():
    source = (RUST / "src" / "artifact.rs").read_text()
    assert "checksum_sha256 obligatorio" in source
    assert "https://" in source
    assert "constant_time_eq" in source
    assert "entry.enclosed_name" in source
    assert "TAR contiene entrada especial no permitida" in source
    assert "reject_dangerous_destination" in source
    assert "backup_existing" in source
    assert "restore_backup" in source


def test_executor_supports_artifacts_without_catalog_commands():
    actions = (RUST / "src" / "execution" / "actions.rs").read_text()
    assert "StepAction::Artifact" in actions
    assert "ArtifactWorkspace::new" in actions
    assert "artifact_verified" in actions
    assert "local_package_install_started" in actions
    assert "shell=True" not in actions


def test_catalog_provider_can_describe_artifact_policy():
    catalog = (RUST / "src" / "catalog.rs").read_text()
    for field in ("checksum_sha256", "artifact_kind", "destination", "strip_components", "max_size_bytes"):
        assert f"pub {field}" in catalog
