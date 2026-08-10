from __future__ import annotations

from styler.privileges import authorize_sudo_interactive


def test_interactive_sudo_uses_real_validation_command():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return 0

    result = authorize_sudo_interactive(
        run=run, is_root=False, sudo_path="/usr/bin/sudo"
    )

    assert result.ok is True
    assert result.method == "sudo"
    assert calls == [["/usr/bin/sudo", "-v"]]


def test_rejected_password_is_not_reported_as_success():
    result = authorize_sudo_interactive(
        run=lambda _argv: 1, is_root=False, sudo_path="/usr/bin/sudo"
    )

    assert result.ok is False
    assert result.returncode == 1
    assert "rechazada" in result.message


def test_root_never_prompts_for_a_password():
    called = False

    def run(_argv):
        nonlocal called
        called = True
        return 0

    result = authorize_sudo_interactive(run=run, is_root=True)

    assert result.ok is True
    assert result.method == "root"
    assert called is False


def test_authorization_error_is_visible_without_opening_technical_details():
    from styler.services import AuthorizationError
    from styler.ui.errors import to_user_error

    error = to_user_error(
        AuthorizationError(
            "No se obtuvo autorización; no se instaló KDE Plasma.",
            "sudo -v terminó con código 1",
        )
    )

    assert error.title == "No se pudo obtener autorización administrativa"
    assert "KDE Plasma" in error.message
    assert "sudo -v" in error.technical_detail
