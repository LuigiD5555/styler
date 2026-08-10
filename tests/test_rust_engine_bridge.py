from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from styler.engine_client import (
    EngineClient,
    EngineCommandError,
    EngineProtocolError,
    EngineUnavailableError,
    EngineExecutionError,
    PROTOCOL_VERSION,
)
from styler.hashing import _hash_file_python


def _fake_engine(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "styler-engine"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_python_hash_contract_is_blake2b_128(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"styler-rust-parity\x00\xff")
    checksum, size = _hash_file_python(str(sample))
    expected = hashlib.blake2b(sample.read_bytes(), digest_size=16).hexdigest()
    assert checksum == expected
    assert size == sample.stat().st_size
    assert len(checksum) == 32


def test_engine_client_accepts_versioned_envelope(tmp_path: Path) -> None:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "engine_version": "0.1.0-test",
        "ok": True,
        "result": {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": "0.1.0-test",
            "hash_algorithm": "blake2b-128",
            "execution_enabled": False,
        },
        "error": None,
    }
    engine = _fake_engine(tmp_path, f"import json\nprint(json.dumps({payload!r}))\n")
    status = EngineClient(engine).status()
    assert status.available is True
    assert status.protocol_version == PROTOCOL_VERSION
    assert status.hash_algorithm == "blake2b-128"
    assert status.execution_enabled is False


def test_engine_client_rejects_incompatible_protocol(tmp_path: Path) -> None:
    payload = {
        "protocol_version": 99,
        "engine_version": "broken",
        "ok": True,
        "result": {},
        "error": None,
    }
    engine = _fake_engine(tmp_path, f"import json\nprint(json.dumps({payload!r}))\n")
    with pytest.raises(EngineProtocolError, match="protocolo incompatible"):
        EngineClient(engine).host()


def test_engine_client_surfaces_structured_error(tmp_path: Path) -> None:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "engine_version": "0.1.0-test",
        "ok": False,
        "result": None,
        "error": {"code": "BAD_PLAN", "message": "plan inválido"},
    }
    engine = _fake_engine(tmp_path, f"import json, sys\nprint(json.dumps({payload!r}))\nsys.exit(2)\n")
    with pytest.raises(EngineCommandError, match="BAD_PLAN"):
        EngineClient(engine).plan({"catalog_root": "missing"})


def test_missing_engine_is_explicit(tmp_path: Path) -> None:
    client = EngineClient(tmp_path / "not-installed")
    assert client.available is False
    with pytest.raises(EngineUnavailableError):
        client.host()


def test_scan_sends_paths_without_shell(tmp_path: Path) -> None:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "engine_version": "0.1.0-test",
        "ok": True,
        "result": {"entries": [], "failures": [], "scanned_files": 0, "total_bytes": 0},
        "error": None,
    }
    log = tmp_path / "args.json"
    body = (
        "import json, sys\n"
        f"open({str(log)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        f"print(json.dumps({payload!r}))\n"
    )
    engine = _fake_engine(tmp_path, body)
    dangerous = tmp_path / "name;touch-should-not-run"
    EngineClient(engine).scan([dangerous])
    assert json.loads(log.read_text(encoding="utf-8")) == ["scan", str(dangerous)]
    assert not (tmp_path / "touch-should-not-run").exists()


def _execution_event(kind: str, *, data: dict | None = None) -> dict:
    return {
        "event_protocol_version": 1,
        "engine_version": "0.2.0-test",
        "sequence": 1 if kind != "run_finished" else 2,
        "timestamp_ms": 0,
        "run_id": "run-test",
        "event": kind,
        "step_id": "",
        "message": kind,
        "data": data or {},
    }


def test_execute_forces_dry_run_by_default(tmp_path: Path) -> None:
    log = tmp_path / "request.json"
    started = _execution_event("run_started")
    finished = _execution_event(
        "run_finished",
        data={"run_id": "run-test", "status": "simulated", "dry_run": True},
    )
    body = (
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'w', encoding='utf-8').write(json.dumps(request))\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        f"print(json.dumps({finished!r}), flush=True)\n"
    )
    engine = _fake_engine(tmp_path, body)
    result = EngineClient(engine).execute({"plan": {"name": "test"}})
    request = json.loads(log.read_text(encoding="utf-8"))
    assert request["options"]["mode"] == "dry_run"
    assert "confirmation" not in request["options"]
    assert result.summary["status"] == "simulated"


def test_apply_requires_explicit_client_permission(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path, "raise SystemExit('must not execute')\n")
    with pytest.raises(EngineExecutionError, match="allow_system_changes"):
        list(
            EngineClient(engine).stream_execute(
                {"plan": {}, "options": {"mode": "apply"}}
            )
        )


def test_apply_sets_both_rust_safety_keys(tmp_path: Path) -> None:
    log = tmp_path / "apply.json"
    started = _execution_event("run_started")
    finished = _execution_event(
        "run_finished",
        data={"run_id": "run-test", "status": "completed", "dry_run": False},
    )
    body = (
        "import json, os, sys\n"
        "request = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'w', encoding='utf-8').write(json.dumps({{'request': request, 'enabled': os.environ.get('STYLER_ENABLE_EXECUTION')}}))\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        f"print(json.dumps({finished!r}), flush=True)\n"
    )
    engine = _fake_engine(tmp_path, body)
    EngineClient(engine).execute(
        {"plan": {"name": "test"}}, allow_system_changes=True
    )
    recorded = json.loads(log.read_text(encoding="utf-8"))
    assert recorded["enabled"] == "1"
    assert recorded["request"]["options"]["mode"] == "apply"
    assert (
        recorded["request"]["options"]["confirmation"]
        == "I_UNDERSTAND_STYLER_WILL_CHANGE_MY_SYSTEM"
    )


def test_execute_rejects_invalid_event_protocol(tmp_path: Path) -> None:
    bad = _execution_event("run_started")
    bad["event_protocol_version"] = 99
    body = f"import json, sys\njson.load(sys.stdin)\nprint(json.dumps({bad!r}), flush=True)\n"
    engine = _fake_engine(tmp_path, body)
    with pytest.raises(EngineProtocolError, match="protocolo de eventos incompatible"):
        list(EngineClient(engine).stream_execute({"plan": {}}))


def test_cancel_event_creates_cooperative_cancel_file(tmp_path: Path) -> None:
    import threading

    finished = _execution_event(
        "run_finished",
        data={"run_id": "run-test", "status": "cancelled", "dry_run": False},
    )
    body = (
        "import json, os, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "cancel = request['options']['cancel_file']\n"
        "deadline = time.time() + 3\n"
        "while not os.path.exists(cancel) and time.time() < deadline:\n"
        "    time.sleep(0.02)\n"
        f"print(json.dumps({finished!r}), flush=True)\n"
    )
    engine = _fake_engine(tmp_path, body)
    cancel_event = threading.Event()
    cancel_event.set()
    result = EngineClient(engine, timeout=5).execute(
        {"plan": {}}, cancel_event=cancel_event
    )
    assert result.summary["status"] == "cancelled"
