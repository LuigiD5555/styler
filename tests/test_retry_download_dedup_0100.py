from __future__ import annotations

from styler.appimage_actions import ReleaseFetchExecutor, artifact_path
from styler.changes.service import ChangeService
from styler.runtime.executors import PackageInstallExecutor
from styler.runtime.models import ExecutionContext, Status, StepDefinition


def test_release_fetch_reuses_completed_cache_without_second_request(tmp_path, monkeypatch):
    import styler.appimage_actions as module

    class Response:
        headers = {"Content-Length": "7"}

        def __init__(self):
            self._done = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            if self._done:
                return b""
            self._done = True
            return b"payload"

    calls = []

    def fake_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    ctx = ExecutionContext(root=tmp_path / "library", dry_run=False, values={"home": str(tmp_path / "home")})
    step = StepDefinition(
        "download",
        "fetch_release_artifact",
        config={
            "url": "https://example.invalid/Affinity.AppImage",
            "artifact_id": "affinity",
            "filename": "Affinity.AppImage",
        },
    )

    first = ReleaseFetchExecutor().run(step, ctx)
    second = ReleaseFetchExecutor().run(step, ctx)

    assert first.success
    assert second.success
    assert second.status == Status.RECONCILED
    assert second.data["cache_hit"] is True
    assert second.data["download_skipped"] is True
    assert len(calls) == 1
    assert artifact_path(ctx, "affinity", "Affinity.AppImage").read_bytes() == b"payload"


def test_affinity_safe_steps_have_one_automatic_retry(tmp_path):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    plan = service.build_plan("affinity-linux")
    steps = {step.id: step for step in plan.workflow.steps}

    for step_id in (
        "yaml.affinity-linux.op.download",
        "yaml.affinity-linux.op.integrate",
        "yaml.affinity-linux.op.verify",
    ):
        assert steps[step_id].retries == 1
        assert steps[step_id].retry_delay > 0


def test_gimp_package_install_has_retry_and_run_skips_if_already_installed(tmp_path, monkeypatch):
    service = ChangeService(root=tmp_path / "library", home=tmp_path / "home")
    plan = service.build_plan("photogimp", "flatpak")
    install = next(step for step in plan.workflow.steps if step.id == "app.gimp.install")
    assert install.retries == 1
    assert install.retry_delay > 0

    executor = PackageInstallExecutor()
    monkeypatch.setattr(executor, "_is_installed", lambda manager, name: True)

    def should_not_build_installer(*_args, **_kwargs):
        raise AssertionError("no debe invocar otra instalación para un paquete ya presente")

    monkeypatch.setattr(executor, "_install_argv", should_not_build_installer)
    result = executor.run(
        StepDefinition(
            "gimp",
            "install_package",
            config={"package": {"manager": "flatpak", "name": "org.gimp.GIMP"}},
        ),
        ExecutionContext(root=tmp_path, dry_run=False),
    )

    assert result.success
    assert result.status == Status.RECONCILED
    assert result.data["install_skipped"] is True


def test_release_fetch_resumes_partial_download_with_http_range(tmp_path, monkeypatch):
    import styler.appimage_actions as module

    class BrokenResponse:
        status = 200
        headers = {"Content-Length": "6"}

        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            self.calls += 1
            if self.calls == 1:
                return b"abc"
            raise OSError("network interrupted")

    class ResumeResponse:
        status = 206
        headers = {"Content-Length": "3"}

        def __init__(self):
            self.done = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            if self.done:
                return b""
            self.done = True
            return b"def"

    requests = []

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return BrokenResponse() if len(requests) == 1 else ResumeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    ctx = ExecutionContext(root=tmp_path / "library", dry_run=False)
    step = StepDefinition(
        "download",
        "fetch_release_artifact",
        config={
            "url": "https://example.invalid/Affinity.AppImage",
            "artifact_id": "affinity",
            "filename": "Affinity.AppImage",
        },
    )

    first = ReleaseFetchExecutor().run(step, ctx)
    assert not first.success
    assert first.data["resume_available"] is True
    assert first.data["partial_bytes"] == 3

    second = ReleaseFetchExecutor().run(step, ctx)
    assert second.success
    assert requests[1].get_header("Range") == "bytes=3-"
    assert artifact_path(ctx, "affinity", "Affinity.AppImage").read_bytes() == b"abcdef"
