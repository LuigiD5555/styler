from __future__ import annotations

from pathlib import Path

from styler.pipecraft.client import PipeCraftClient
from styler.pipecraft.contract import IPC_PROTOCOL, MIN_VERSION, check_runtime


def test_runtime_16_requires_spec_capabilities() -> None:
    caps = ["submit_spec", "validate_spec", "plan_spec", "command", "plugin", "resume"]
    result = check_runtime("1.6.0-alpha.1", IPC_PROTOCOL, caps)
    assert result.compatible is True
    assert result.legacy is False
    assert MIN_VERSION == "1.6.0-alpha.1"


def test_runtime_15_is_explicit_legacy_compatibility() -> None:
    result = check_runtime("1.5.0-alpha.1", IPC_PROTOCOL, [], allow_legacy=True)
    assert result.compatible is True
    assert result.legacy is True
    assert "adaptador YAML" in result.reason


def test_runtime_16_without_capabilities_is_rejected_when_legacy_disabled() -> None:
    result = check_runtime("1.6.0-alpha.1", IPC_PROTOCOL, [], allow_legacy=False)
    assert result.compatible is False
    assert set(result.missing_capabilities) == {"submit_spec", "validate_spec", "plan_spec"}


def test_client_submit_spec_uses_structured_ipc(monkeypatch, tmp_path: Path) -> None:
    client = PipeCraftClient(tmp_path)
    seen = {}

    def fake_request(payload):
        seen.update(payload)
        return {"run_id": "r-123"}

    monkeypatch.setattr(client, "request", fake_request)
    run_id = client.submit_spec(
        {"schema_version": "pipecraft/v1", "name": "demo", "steps": []},
        execute=True,
        approve=False,
        labels=["styler"],
        max_workers=3,
    )
    assert run_id == "r-123"
    assert seen["op"] == "submit_spec"
    assert seen["spec"]["name"] == "demo"
    assert seen["max_workers"] == 3


def test_client_capabilities_are_negotiated_from_ping(monkeypatch, tmp_path: Path) -> None:
    client = PipeCraftClient(tmp_path)
    monkeypatch.setattr(
        client,
        "request",
        lambda payload: {
            "service": "pipecraft",
            "version": "1.6.0-alpha.1",
            "protocol": IPC_PROTOCOL,
            "capabilities": ["submit_spec", "plan_spec"],
        },
    )
    assert client.supports("submit_spec") is True
    assert client.supports("validate_spec") is False
