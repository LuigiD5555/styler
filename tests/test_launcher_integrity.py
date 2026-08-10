from pathlib import Path
import os

from styler.launcher_integrity import normalize_and_inspect


def test_restored_script_keeps_executable_mode_and_rewrites_home(tmp_path, monkeypatch):
    home = tmp_path / "newuser"
    script = home / ".local/bin/affinity-wine.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        '#!/usr/bin/env bash\nexport WINEPREFIX="$HOME/.wineAffinity3"\n'
        'wine "/home/olduser/Downloads/Affinity/App/Affinity.exe"\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o644)
    monkeypatch.setattr("styler.launcher_integrity.shutil.which", lambda cmd: None if cmd == "wine" else "/bin/x")

    result = normalize_and_inspect(script, home, 0o775)

    assert script.stat().st_mode & 0o777 == 0o775
    assert "/home/olduser" not in script.read_text(encoding="utf-8")
    assert str(home) in script.read_text(encoding="utf-8")
    assert result.changed
    assert "wine" in result.missing_commands
    assert any("Affinity.exe" in item for item in result.missing_paths)


def test_desktop_launcher_rewrites_exec_and_reports_missing_target(tmp_path):
    home = tmp_path / "newuser"
    desktop = home / ".local/share/applications/affinity.desktop"
    desktop.parent.mkdir(parents=True)
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=Affinity\n"
        "Exec=/home/olduser/.local/bin/affinity-wine.sh\n"
        "Icon=/home/olduser/Downloads/Affinity/icon.png\n",
        encoding="utf-8",
    )

    result = normalize_and_inspect(desktop, home, 0o775)

    text = desktop.read_text(encoding="utf-8")
    assert str(home / ".local/bin/affinity-wine.sh") in text
    assert result.changed
    assert str(home / ".local/bin/affinity-wine.sh") in result.missing_paths
    assert str(home / "Downloads/Affinity/icon.png") in result.missing_paths
