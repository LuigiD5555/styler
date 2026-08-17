"""Regression tests for the user-level source installer."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "local" / "install.sh"


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)
    return path


def _installer_env(tmp_path: Path, python: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_BIN_HOME": str(tmp_path / "bin"),
            "STYLER_PYTHON": str(python),
            "TMPDIR": str(tmp_path / "tmp"),
            "PATH": "/usr/bin:/bin",
        }
    )
    (tmp_path / "tmp").mkdir()
    return env


def test_installer_has_valid_bash_syntax_and_help():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "--install-dependencies" in result.stdout
    assert "--yes" in result.stdout


def test_missing_venv_is_detected_before_user_installation_is_touched(tmp_path):
    fake_python = _write_executable(
        tmp_path / "fake-python",
        r"""
        #!/usr/bin/env bash
        if [[ "${1:-}" == "-" ]]; then cat >/dev/null; exit 0; fi
        if [[ "${1:-}" == "--version" ]]; then echo 'Python 3.11.0'; exit 0; fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
          mkdir -p "${3:-/tmp/fake}"
          echo 'ensurepip failed intentionally' >&2
          exit 1
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "virtualenv" ]]; then exit 1; fi
        exit 1
        """,
    )
    env = _installer_env(tmp_path, fake_python)

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    known_venv_hints = (
        "python3-venv",
        "python-virtualenv",
        "python3-virtualenv",
        "venv/ensurepip",
    )
    assert result.returncode != 0
    assert any(hint in result.stderr for hint in known_venv_hints)
    assert "No se modificó la instalación" in result.stderr
    assert not (tmp_path / "data" / "styler-app").exists()
    assert not list((tmp_path / "tmp").glob("styler-venv-check.*"))


def _fake_python_that_builds_venvs(path: Path, *, fail_install: bool = False) -> Path:
    install_branch = "exit 31" if fail_install else r"""
        cat > "$(dirname "$0")/styler" <<'STYLER'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo 'Styler 0.11.0'; exit 0; fi
exit 0
STYLER
        chmod +x "$(dirname "$0")/styler"
        exit 0
    """
    return _write_executable(
        path,
        rf"""
        #!/usr/bin/env bash
        if [[ "${{1:-}}" == "-" ]]; then cat >/dev/null; exit 0; fi
        if [[ "${{1:-}}" == "--version" ]]; then echo 'Python 3.11.0'; exit 0; fi
        if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
          target="${{3}}"
          mkdir -p "$target/bin"
          cat > "$target/bin/python" <<'VENV_PY'
#!/usr/bin/env bash
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" && "${{3:-}}" == "--version" ]]; then
  echo 'pip 24.0'; exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" && "${{3:-}}" == "install" ]]; then
  {textwrap.dedent(install_branch).strip()}
fi
exit 1
VENV_PY
          chmod +x "$target/bin/python"
          exit 0
        fi
        exit 1
        """,
    )


def test_installer_activates_only_a_fully_verified_staged_installation(tmp_path):
    fake_python = _fake_python_that_builds_venvs(tmp_path / "fake-python")
    env = _installer_env(tmp_path, fake_python)

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    launcher = tmp_path / "bin" / "styler"
    assert launcher.exists()
    installed = subprocess.run(
        [str(launcher), "--version"],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )
    assert "0.11.0" in installed.stdout
    assert not list((tmp_path / "data").glob("styler-install.*"))


def test_failed_staged_install_preserves_previous_version(tmp_path):
    fake_python = _fake_python_that_builds_venvs(
        tmp_path / "fake-python", fail_install=True
    )
    env = _installer_env(tmp_path, fake_python)
    old_venv = tmp_path / "data" / "styler-app" / "venv"
    old_venv.mkdir(parents=True)
    marker = old_venv / "old-version.txt"
    marker.write_text("keep me", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert "permanece intacta" in result.stderr
    assert not list((tmp_path / "data").glob("styler-install.*"))

SIMPLE_INSTALLER = ROOT / "scripts" / "local" / "install-styler.sh"
RUNNER = ROOT / "scripts" / "local" / "run-styler.sh"
UNINSTALLER = ROOT / "scripts" / "local" / "uninstall.sh"


def test_beginner_scripts_have_valid_bash_syntax_and_clear_help():
    for script in (SIMPLE_INSTALLER, RUNNER, UNINSTALLER):
        subprocess.run(["bash", "-n", str(script)], check=True)

    result = subprocess.run(
        ["bash", str(SIMPLE_INSTALLER), "--help"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "Miniconda" in result.stdout
    assert "Python 3" in result.stdout
    assert "--yes" in result.stdout


def test_simple_installer_delegates_dependency_bootstrap(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    wrapper = source / "install-styler.sh"
    wrapper.write_text(SIMPLE_INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    target = _write_executable(
        source / "install.sh",
        """
        #!/usr/bin/env bash
        printf '%s\\n' "$@"
        """,
    )

    result = subprocess.run(
        [str(wrapper), "--yes"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.splitlines() == ["--install-dependencies", "--yes"]


def test_runner_updates_an_older_installed_release_before_launch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    runner = source / "run-styler.sh"
    runner.write_text(RUNNER.read_text(encoding="utf-8"), encoding="utf-8")
    runner.chmod(0o755)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "styler"\nversion = "0.11.0"\n', encoding="utf-8"
    )

    bin_home = tmp_path / "bin"
    bin_home.mkdir()
    styler = _write_executable(
        bin_home / "styler",
        """
        #!/usr/bin/env bash
        if [[ "${1:-}" == "--version" ]]; then
          echo "Styler 0.11.0~previous"
        else
          echo "old-release"
        fi
        """,
    )
    marker = tmp_path / "updated"
    installer = _write_executable(
        source / "install-styler.sh",
        f"""
        #!/usr/bin/env bash
        touch {marker!s}
        cat > {styler!s} <<'SCRIPT'
#!/usr/bin/env bash
if [[ "${{1:-}}" == "--version" ]]; then
  echo "Styler 0.11.0"
else
  echo "new-release"
fi
SCRIPT
        chmod +x {styler!s}
        """,
    )

    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["XDG_BIN_HOME"] = str(bin_home)
    result = subprocess.run(
        [str(runner)],
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )

    assert marker.exists()
    assert "Actualizando Styler 0.11.0~previous → 0.11.0" in result.stdout
    assert result.stdout.rstrip().endswith("new-release")


def test_installer_persists_user_bin_in_profile_and_bashrc_without_duplicates(tmp_path):
    fake_python = _fake_python_that_builds_venvs(tmp_path / "fake-python")
    env = _installer_env(tmp_path, fake_python)
    env["SHELL"] = "/bin/bash"

    for _ in range(2):
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr

    home = Path(env["HOME"])
    bin_home = env["XDG_BIN_HOME"]
    expected = f'export PATH="{bin_home}:$PATH"'
    for target in (home / ".profile", home / ".bashrc"):
        text = target.read_text(encoding="utf-8")
        assert text.count(expected) == 1
        assert text.count("# >>> Styler user commands >>>") == 1


def test_installer_process_itself_exports_user_bin_before_post_install_commands(tmp_path):
    # Contrato estático: install.sh debe añadir BIN_HOME a PATH antes de ejecutar
    # cualquier fase de instalación, no limitarse a escribir ~/.profile al final.
    text = INSTALLER.read_text(encoding="utf-8")
    export_pos = text.index('export PATH="$BIN_HOME:$PATH"')
    preflight_pos = text.index("venv_preflight()")
    assert export_pos < preflight_pos


def test_sourcing_simple_installer_updates_current_shell_path(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    wrapper = source / "install-styler.sh"
    wrapper.write_text(SIMPLE_INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    _write_executable(
        source / "install.sh",
        """
        #!/usr/bin/env bash
        exit 0
        """,
    )
    home = tmp_path / "home"
    bin_home = home / ".local" / "bin"
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"})
    result = subprocess.run(
        ["bash", "-c", 'source "$1" --yes; printf "%s" "$PATH"', "bash", str(wrapper)],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.split(":", 1)[0] == str(bin_home)


def test_sourcing_simple_installer_does_not_enable_strict_shell_options(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    wrapper = source / "install-styler.sh"
    wrapper.write_text(SIMPLE_INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    _write_executable(source / "install.sh", "#!/usr/bin/env bash\nexit 0\n")
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update({"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"})
    result = subprocess.run(
        [
            "bash", "-c",
            'set +e +u; set +o pipefail; source "$1" --yes; '
            '[[ $- != *e* && $- != *u* ]]; a=$?; '
            'set -o | grep -q "^pipefail[[:space:]]*off$"; b=$?; exit $((a || b))',
            "bash", str(wrapper),
        ],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_installer_builds_from_sanitized_staging_copy_not_source_tree():
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'BUILD_SOURCE_DIR="$STAGE_DIR/source"' in text
    assert 'prepare_build_source "$BUILD_SOURCE_DIR"' in text
    assert 'pip install "${PIP_ARGS[@]}" "$BUILD_SOURCE_DIR"' in text
    assert 'pip install "${PIP_ARGS[@]}" "$SOURCE_DIR"' not in text
    # Los residuos que provocaron el fallo anterior deben excluirse de la copia.
    assert '"build"' in text
    assert 'name.endswith(".egg-info")' in text
    assert 'copy_function=shutil.copyfile' in text


def test_bundled_official_baseline_is_readable_as_package_data():
    baselines = list((ROOT / "styler" / "baselines" / "catalog").glob("*.stylerpkg"))
    assert baselines
    for baseline in baselines:
        mode = baseline.stat().st_mode & 0o777
        assert mode & 0o444 == 0o444, f"{baseline} debe ser legible en una distribución"



def test_installer_creates_immediate_bridge_in_active_conda_path(tmp_path):
    fake_python = _fake_python_that_builds_venvs(tmp_path / "fake-python")
    env = _installer_env(tmp_path, fake_python)
    conda = tmp_path / "home" / "miniconda3"
    conda_bin = conda / "bin"
    conda_bin.mkdir(parents=True)
    env["CONDA_PREFIX"] = str(conda)
    env["PATH"] = f"{conda_bin}:/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    bridge = conda_bin / "styler"
    assert bridge.exists()
    assert "STYLER_MANAGED_COMMAND_BRIDGE=1" in bridge.read_text(encoding="utf-8")
    assert str(Path(env["XDG_BIN_HOME"]) / "styler") in bridge.read_text(encoding="utf-8")
    assert "Comando inmediato disponible" in result.stdout

    # Simula exactamente el shell padre: su PATH original no contiene
    # XDG_BIN_HOME, pero sí el bin de Conda. El comando debe resolver ya.
    resolved = subprocess.run(
        ["bash", "-c", 'command -v styler && styler --version'],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert resolved.stdout.splitlines()[0] == str(bridge)
    assert "0.11.0" in resolved.stdout


def test_uninstaller_removes_only_recorded_managed_bridge(tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "data"
    bin_home = tmp_path / "bin"
    app_dir = data / "styler-app"
    conda_bin = home / "miniconda3" / "bin"
    for path in (app_dir, conda_bin, bin_home):
        path.mkdir(parents=True, exist_ok=True)

    bridge = conda_bin / "styler"
    bridge.write_text(
        "#!/bin/sh\n# STYLER_MANAGED_COMMAND_BRIDGE=1\nexit 0\n",
        encoding="utf-8",
    )
    bridge.chmod(0o755)
    (app_dir / "command-bridge.path").write_text(str(bridge) + "\n", encoding="utf-8")
    (bin_home / "styler").write_text("launcher\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(data),
            "XDG_BIN_HOME": str(bin_home),
            "PATH": f"{conda_bin}:/usr/bin:/bin",
        }
    )
    result = subprocess.run(
        ["bash", str(UNINSTALLER)],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert not bridge.exists()
    assert not (bin_home / "styler").exists()


def test_uninstaller_does_not_remove_foreign_command_even_if_recorded(tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "data"
    bin_home = tmp_path / "bin"
    app_dir = data / "styler-app"
    command_dir = home / "bin"
    for path in (app_dir, command_dir, bin_home):
        path.mkdir(parents=True, exist_ok=True)

    foreign = command_dir / "styler"
    foreign.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    foreign.chmod(0o755)
    (app_dir / "command-bridge.path").write_text(str(foreign) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "XDG_DATA_HOME": str(data),
            "XDG_BIN_HOME": str(bin_home),
            "PATH": f"{command_dir}:/usr/bin:/bin",
        }
    )
    subprocess.run(["bash", str(UNINSTALLER)], env=env, check=True)
    assert foreign.exists()


def test_installer_immediate_command_with_plain_python_and_system_path(tmp_path):
    """Python del sistema, sin Conda/venv: usa un bin local ya visible."""
    fake_python = _fake_python_that_builds_venvs(tmp_path / "fake-python")
    env = _installer_env(tmp_path, fake_python)
    system_bin = tmp_path / "usr-local-bin"
    system_bin.mkdir()
    env.pop("CONDA_PREFIX", None)
    env.pop("VIRTUAL_ENV", None)
    env["STYLER_SYSTEM_BIN"] = str(system_bin)
    env["PATH"] = f"{system_bin}:/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    bridge = system_bin / "styler"
    assert bridge.exists()
    assert "STYLER_MANAGED_COMMAND_BRIDGE=1" in bridge.read_text(encoding="utf-8")

    resolved = subprocess.run(
        ["bash", "-c", "command -v styler && styler --version"],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert resolved.stdout.splitlines()[0] == str(bridge)
    assert "0.11.0" in resolved.stdout


def test_installer_reuses_generic_user_bin_without_conda(tmp_path):
    """No hay lógica especial por Conda: cualquier ~/.../bin seguro sirve."""
    fake_python = _fake_python_that_builds_venvs(tmp_path / "fake-python")
    env = _installer_env(tmp_path, fake_python)
    user_bin = tmp_path / "home" / ".python-tools" / "bin"
    user_bin.mkdir(parents=True)
    env.pop("CONDA_PREFIX", None)
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = f"{user_bin}:/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    bridge = user_bin / "styler"
    assert bridge.exists()
    resolved = subprocess.run(
        ["bash", "-c", "command -v styler && styler --version"],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    assert resolved.stdout.splitlines()[0] == str(bridge)
    assert "0.11.0" in resolved.stdout
