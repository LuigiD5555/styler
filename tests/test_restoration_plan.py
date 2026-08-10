from styler.restoration_plan import build_restoration_plan
from styler.session_profile import DesiredSession, SessionProfile, SessionState


def profile():
    return SessionProfile(
        source=SessionState("kde", "x11"),
        target_current=SessionState("xfce", "x11"),
        desired=DesiredSession("kde", "wayland", True, True, True),
    )


def test_restoration_plan_preserves_x11_and_does_not_activate_before_verification():
    workflow = build_restoration_plan(profile())
    by_id = {step.id: step for step in workflow.steps}
    assert "wayland.verify" in by_id["wayland.activate"].needs
    assert "wayland.verified" in by_id["wayland.activate"].requires
    assert "x11.preserved" in by_id["wayland.activate"].requires
    assert workflow.metadata["safe_first_migration"] is True


def test_restoration_plan_serializes_apt_and_configuration_waits_for_kde():
    workflow = build_restoration_plan(profile())
    by_id = {step.id: step for step in workflow.steps}
    install = by_id["desktop.kde.install"]
    assert set(install.exclusive_resources) >= {"apt", "dpkg"}
    assert by_id["configuration.apply"].needs == ["desktop.kde.verify"]
    assert "desktop.kde.verified" in by_id["configuration.apply"].requires


def test_restoration_plan_keeps_source_target_and_desired_separate():
    workflow = build_restoration_plan(profile())
    data = workflow.metadata["session_profile"]
    assert data["source"]["session"] == "x11"
    assert data["target_current"]["desktop"] == "xfce"
    assert data["desired"]["preferred_session"] == "wayland"
    assert data["desired"]["keep_x11_fallback"] is True
