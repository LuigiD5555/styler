"""Contrato de cambios AppImage incorporados como YAML declarativo."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from styler.appimage_actions import AppImageIntegrateExecutor, AppImageVerifyExecutor, ReleaseFetchExecutor, artifact_path
from styler.changes.service import ChangeService
from styler.declarative_changes import load_declarative_changes
from styler.receipts import ReceiptJournal, ReceiptKind
from styler.runtime.models import ExecutionContext, StepDefinition


APPIMAGELAUNCHER_ASSET = "appimagelauncher_3.0.0-beta-2-gha287.96cb937_amd64.deb"
AFFINITY_ASSET = "Affinity-3-x86_64.AppImage"
AFFINITY_URL = (
    "https://github.com/ryzendew/Linux-Affinity-Installer/releases/download/"
    "Affinity-wine-10.10-Appimage/Affinity-3-x86_64.AppImage"
)


def test_builtin_yaml_catalog_contains_appimagelauncher_and_affinity():
    changes = load_declarative_changes()
    assert {"appimagelauncher", "affinity-linux"} <= set(changes)
    assert changes["affinity-linux"].requires_changes == ("appimagelauncher",)


def test_yaml_keeps_release_coordinates_outside_python():
    changes = load_declarative_changes()
    launcher = changes["appimagelauncher"].recipe.operations[0].config
    affinity = changes["affinity-linux"].recipe.operations[0].config
    assert launcher["repository"] == "TheAssassin/AppImageLauncher"
    assert launcher["tag"] == "v3.0.0-beta-3"
    assert launcher["asset"] == APPIMAGELAUNCHER_ASSET
    assert affinity["repository"] == "ryzendew/Linux-Affinity-Installer"
    assert affinity["tag"] == "Affinity-wine-10.10-Appimage"
    assert affinity["asset"] == AFFINITY_ASSET


def test_affinity_plan_composes_appimagelauncher_before_affinity(tmp_path):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    plan = service.build_plan("affinity-linux")
    ids = [step.id for step in plan.workflow.steps]
    launcher_verify = "yaml.appimagelauncher.op.verify"
    affinity_download = "yaml.affinity-linux.op.download"
    affinity_integrate = next(step for step in plan.workflow.steps if step.id == "yaml.affinity-linux.op.integrate")
    assert ids.index(launcher_verify) < ids.index(affinity_download)
    assert "appimage.integration.ready" in affinity_integrate.requires
    provider = next(step for step in plan.workflow.steps if step.id == launcher_verify)
    assert "appimage.integration.ready" in provider.provides
    install = next(step for step in plan.workflow.steps if step.id == "yaml.appimagelauncher.op.install")
    assert install.config["retain_on_rollback"] is True
    assert service._workflow_requires_admin(plan.workflow) is True


def test_appimagelauncher_standalone_is_reversible_package_install(tmp_path):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    plan = service.build_plan("appimagelauncher")
    install = next(step for step in plan.workflow.steps if step.step_type == "install_package_artifact")
    assert "retain_on_rollback" not in install.config


def test_yaml_changes_are_normal_changes_in_changes_service(tmp_path):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    cards = {item.change_id: item for item in service.available_changes()}
    assert cards["appimagelauncher"].provider_id == "yaml"
    assert cards["affinity-linux"].provider_id == "yaml"
    assert cards["affinity-linux"].automation_level == "automatic"


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self.payload[self.offset:]
        else:
            chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


def test_release_fetch_downloads_to_styler_cache(tmp_path, monkeypatch):
    import styler.appimage_actions as module

    payload = b"appimage-binary"
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: _Response(payload))
    ctx = ExecutionContext(root=tmp_path / "library", run_id="run", dry_run=False, values={"home": str(tmp_path / "home")})
    step = StepDefinition(
        id="download", step_type="fetch_release_artifact",
        config={"url": AFFINITY_URL, "artifact_id": "affinity", "filename": "Affinity.AppImage"},
    )
    result = ReleaseFetchExecutor().run(step, ctx)
    assert result.success
    path = artifact_path(ctx, "affinity", "Affinity.AppImage")
    assert path.read_bytes() == payload


def test_appimage_integration_records_desktop_and_appimage_paths(tmp_path, monkeypatch):
    import styler.appimage_actions as module

    home = tmp_path / "home"
    root = tmp_path / "library"
    home.mkdir()
    ctx = ExecutionContext(
        root=root, run_id="run-1", dry_run=False,
        values={"home": str(home), "change_id": "affinity-linux", "receipts_root": str(root)},
    )
    source = artifact_path(ctx, "affinity", "Affinity.AppImage")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake")

    real_which = module.shutil.which
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/ail-cli" if name == "ail-cli" else real_which(name))

    def fake_run(_ctx, _step, _argv, **_kwargs):
        assert _argv == ["/usr/bin/ail-cli", "integrate", str(source)]
        apps = home / "Applications"
        desktops = home / ".local" / "share" / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        desktops.mkdir(parents=True, exist_ok=True)
        integrated = apps / "Affinity.AppImage"
        integrated.write_bytes(b"fake")
        integrated.chmod(0o755)
        icons = home / ".local" / "share" / "icons"
        icons.mkdir(parents=True, exist_ok=True)
        (icons / "affinity.png").write_bytes(b"png")
        desktop = desktops / "affinity.desktop"
        desktop.write_text(
            "[Desktop Entry]\nName=Affinity\nExec=" + str(integrated) + "\nIcon=affinity\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="integrated", stderr="", log_path=str(root / "log.txt"))

    monkeypatch.setattr(module, "run_step_command", fake_run)
    step = StepDefinition(
        id="integrate", step_type="integrate_appimage",
        config={
            "artifact_id": "affinity", "filename": "Affinity.AppImage",
            "name_hint": "Affinity", "backend": "appimagelauncher",
        },
    )
    result = AppImageIntegrateExecutor().run(step, ctx)
    assert result.success
    receipts = ReceiptJournal(root, "affinity-linux").entries()
    assert any(item.kind == ReceiptKind.PATHS_WRITTEN for item in receipts)
    paths = [path for item in receipts for path in item.data.get("created_paths", [])]
    assert any(path.endswith("affinity.desktop") for path in paths)
    assert any(path.endswith("Affinity.AppImage") for path in paths)
    assert any(path.endswith("affinity.png") for path in paths)

    verify = StepDefinition(
        id="verify", step_type="verify_appimage_integration",
        config={"name_hint": "Affinity"},
    )
    verified = AppImageVerifyExecutor().run(verify, ctx)
    assert verified.success


def test_yaml_declares_verified_upstream_checksums():
    changes = load_declarative_changes()
    launcher = changes["appimagelauncher"].recipe.operations[0].config
    affinity = changes["affinity-linux"].recipe.operations[0].config
    assert launcher["sha256"] == "4117552105968a8011955d065b5fd55f547a2ed21dac1fe0a046ee9b60220c36"
    assert affinity["sha256"] == "87e5e0c4acbb9fda012feeab9ac7f7e8301ce2893e130244dca37fea350c55b6"


def test_builtin_appimage_changes_are_scoped_to_apt_x86_64():
    changes = load_declarative_changes()
    for change_id in ("appimagelauncher", "affinity-linux"):
        change = changes[change_id]
        assert change.compatible_with(family="ubuntu", architecture="amd64")
        assert change.compatible_with(family="debian", architecture="x86_64")
        assert not change.compatible_with(family="arch", architecture="x86_64")
        assert not change.compatible_with(family="ubuntu", architecture="aarch64")


def test_github_release_resolution_uses_exact_asset(tmp_path, monkeypatch):
    import json
    import styler.appimage_actions as module

    asset = "demo.AppImage"
    release = {
        "assets": [{
            "name": asset,
            "browser_download_url": "https://github.com/acme/demo/releases/download/v1/demo.AppImage",
        }]
    }
    payloads = [json.dumps(release).encode("utf-8"), b"binary"]

    def fake_open(*_args, **_kwargs):
        return _Response(payloads.pop(0))

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_open)
    ctx = ExecutionContext(root=tmp_path / "library", run_id="run", dry_run=False, values={"home": str(tmp_path / "home")})
    step = StepDefinition(
        id="download", step_type="fetch_release_artifact",
        config={
            "source": "github", "repository": "acme/demo", "tag": "v1",
            "asset": asset, "artifact_id": "demo", "filename": asset,
        },
    )
    result = ReleaseFetchExecutor().run(step, ctx)
    assert result.success
    assert artifact_path(ctx, "demo", asset).read_bytes() == b"binary"


def test_appimagelauncher_yaml_checks_existing_ail_before_download_or_install():
    changes = load_declarative_changes()
    operations = {item.operation_id: item for item in changes["appimagelauncher"].recipe.operations}
    assert operations["download"].config["satisfied_by"] == {"executable": "ail-cli"}
    assert operations["install"].config["satisfied_by"] == {"executable": "ail-cli"}


def test_release_fetch_skips_network_when_declared_executable_already_exists(tmp_path, monkeypatch):
    import styler.appimage_actions as module

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/ail-cli" if name == "ail-cli" else None)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("No se debe consultar ni descargar la release si ail-cli ya existe")

    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden_network)
    ctx = ExecutionContext(root=tmp_path / "library", run_id="run", dry_run=False, values={"home": str(tmp_path / "home")})
    step = StepDefinition(
        id="download", step_type="fetch_release_artifact",
        config={
            "source": "github", "repository": "TheAssassin/AppImageLauncher",
            "tag": "v3.0.0-beta-3", "asset": APPIMAGELAUNCHER_ASSET,
            "artifact_id": "appimagelauncher-deb", "filename": APPIMAGELAUNCHER_ASSET,
            "satisfied_by": {"executable": "ail-cli"},
        },
    )
    result = ReleaseFetchExecutor().run(step, ctx)
    assert result.success
    assert result.status == "reconciled"
    assert result.data["download_skipped"] is True
    assert result.data["path"] == "/usr/bin/ail-cli"


def test_package_artifact_install_skips_missing_artifact_when_ail_already_exists(tmp_path, monkeypatch):
    import styler.appimage_actions as module

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/ail-cli" if name == "ail-cli" else None)
    ctx = ExecutionContext(root=tmp_path / "library", run_id="run", dry_run=False, values={"home": str(tmp_path / "home")})
    step = StepDefinition(
        id="install", step_type="install_package_artifact",
        config={
            "manager": "apt", "package_name": "appimagelauncher",
            "artifact_id": "appimagelauncher-deb", "filename": APPIMAGELAUNCHER_ASSET,
            "satisfied_by": {"executable": "ail-cli"},
        },
    )
    # No existe ningún .deb en caché. Si el ejecutor intentara reinstalarlo,
    # terminaría con ARTIFACT_NOT_FOUND. La capacidad existente debe ganar.
    result = module.PackageInstallArtifactExecutor().run(step, ctx)
    assert result.success
    assert result.status == "reconciled"
    assert result.data["install_skipped"] is True
    assert result.data["path"] == "/usr/bin/ail-cli"


def test_affinity_does_not_request_admin_for_launcher_when_ail_already_exists(tmp_path, monkeypatch):
    import styler.changes.service as service_module

    monkeypatch.setattr(service_module.shutil, "which", lambda name: "/usr/bin/ail-cli" if name == "ail-cli" else None)
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    plan = service.build_plan("affinity-linux")
    assert service._workflow_requires_admin(plan.workflow) is False


def test_affinity_still_requests_admin_when_appimagelauncher_is_missing(tmp_path, monkeypatch):
    import styler.changes.service as service_module

    monkeypatch.setattr(service_module.shutil, "which", lambda _name: None)
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    plan = service.build_plan("affinity-linux")
    assert service._workflow_requires_admin(plan.workflow) is True
