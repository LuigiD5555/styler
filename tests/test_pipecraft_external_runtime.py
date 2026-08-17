from pathlib import Path

from styler.pipecraft.contract import IPC_PROTOCOL, MIN_VERSION, check_runtime
from styler.pipecraft.service import diagnose


def test_runtime_contract_accepts_pipecraft_15_and_newer_minor() -> None:
    assert check_runtime("1.5.0-alpha.1", IPC_PROTOCOL).compatible
    assert check_runtime("1.6.2", IPC_PROTOCOL).compatible


def test_runtime_contract_rejects_old_or_wrong_protocol() -> None:
    assert not check_runtime("1.4.9", IPC_PROTOCOL).compatible
    assert not check_runtime("2.0.0", IPC_PROTOCOL).compatible
    assert not check_runtime("1.5.0", "pipecraft.ipc/v2").compatible


def test_repository_does_not_vendor_pipecraft_source() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "vendor" / "pipecraft-1.5.0-alpha.1").exists()
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
    assert "vendor/pipecraft" not in manifest


def test_diagnose_does_not_start_service(monkeypatch, tmp_path: Path) -> None:
    import styler.pipecraft.service as service

    monkeypatch.setattr(service, "locate_binary", lambda: None)
    monkeypatch.setattr(service.PipeCraftClient, "ping", lambda self: (_ for _ in ()).throw(RuntimeError("offline")))
    info = diagnose(tmp_path)
    assert info["binary_available"] is False
    assert info["service_active"] is False
    assert info["required_version"] == MIN_VERSION
