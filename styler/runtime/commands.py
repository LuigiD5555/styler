"""
styler.runtime.commands
=======================
Frontera única de PipeCraft con los procesos externos.

`PipeCraftRunner` ejecuta de verdad. `FakeRunner` **simula un equipo completo**
(paquetes instalados, paquetes ofrecidos por cada gestor, remotos configurados),
para que las pruebas comprueben comportamiento real y no solo que no se cae.
"""
from __future__ import annotations

import codecs
import json
import shlex
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()
    pid: int | None = None
    elapsed_seconds: float = 0.0
    log_path: str = ""
    timed_out: bool = False
    cancelled: bool = False




def command_failure_summary(result: CommandResult, *, max_lines: int = 8, max_chars: int = 1600) -> str:
    """Devuelve la parte útil de la salida de un comando fallido.

    ``run_streaming`` combina stdout/stderr para poder mostrarlo en vivo, así
    que el diagnóstico debe mirar ambos campos. Se limita la cola para no
    convertir la pantalla de resultado en un volcado completo del gestor.
    """
    text = (result.stderr or result.stdout or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    return tail

OutputCallback = Optional[Callable[[str], None]]
HeartbeatCallback = Optional[Callable[[float], None]]
StartCallback = Optional[Callable[[int], None]]


class Runner(Protocol):
    def available(self, program: str) -> bool: ...
    def run(self, argv: list[str], timeout: float | None = None) -> CommandResult: ...


@dataclass
class PipeCraftRunner:
    """Ejecuta comandos reales y puede transmitir su salida mientras siguen vivos.

    La restauración puede tardar varios minutos al instalar un escritorio completo.
    ``subprocess.run(capture_output=True)`` ocultaba toda la actividad hasta el final,
    de modo que una descarga sana parecía un bloqueo. ``run_streaming`` conserva la
    misma seguridad (stdin cerrado) pero entrega líneas y latidos al observador.
    """

    timeout: float = 900.0
    heartbeat_interval: float = 2.0
    interactive_prompt_grace: float = 8.0
    terminate_grace: float = 10.0
    _active_process: subprocess.Popen[bytes] | None = field(
        default=None, init=False, repr=False
    )
    _active_pgid: int | None = field(default=None, init=False, repr=False)
    _active_prefix: list[str] = field(default_factory=list, init=False, repr=False)
    _active_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    _cancel_requested: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )

    def available(self, program: str) -> bool:
        return bool(shutil.which(program))

    def begin_operation(self) -> None:
        """Limpia una cancelación anterior antes de comenzar una restauración nueva."""
        self._cancel_requested.clear()

    @property
    def cancellation_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def request_cancel(self) -> bool:
        """Solicita cancelación y detiene el árbol activo, no solo su proceso padre.

        APT suele ejecutar ``sudo -> apt-get -> dpkg -> scripts``. Terminar solo
        ``apt-get`` puede dejar a ``dpkg`` vivo y conservar ``lock-frontend``.
        Cada comando streaming se crea en un grupo propio, conservando la sesión
        del terminal, y aquí se señala todo ese grupo de procesos.
        """
        self._cancel_requested.set()
        with self._active_lock:
            process = self._active_process
            pgid = self._active_pgid
            prefix = list(self._active_prefix)
        if process is None or pgid is None:
            return False
        # No bloquea el event loop de Textual. El worker recoge el grupo y
        # escala a SIGKILL si hace falta.
        self._signal_group(process, pgid, signal.SIGTERM, prefix)
        return True

    # Nombre explícito para servicios/UI; conserva request_cancel para pruebas.
    cancel_active = request_cancel

    def _set_active(self, process: subprocess.Popen[bytes], argv: list[str]) -> int:
        # El proceso vive en un grupo propio, pero conserva la misma sesión y
        # terminal controlador que Styler. Esto permite cancelar todo el árbol
        # con killpg sin invalidar el ticket sudo asociado al TTY.
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError):
            pgid = process.pid
        with self._active_lock:
            self._active_process = process
            self._active_pgid = pgid
            self._active_prefix = self._privilege_prefix(argv)
        return pgid

    @staticmethod
    def _process_group_kwargs() -> dict[str, object]:
        """Crea un grupo cancelable sin perder el terminal controlador.

        ``start_new_session=True`` era tentador porque facilita ``killpg``, pero
        también ejecuta el instalador fuera de la sesión del terminal. En
        sistemas donde sudo usa tickets por TTY, ``sudo -v`` era aceptado y el
        siguiente ``sudo -n apt-get`` fallaba inmediatamente por no compartir
        ese TTY.

        Python 3.11 añadió ``process_group``. Para Python 3.10 se usa
        ``os.setpgrp`` justo antes de exec. Ambos crean un grupo nuevo dentro de
        la sesión actual y conservan el terminal controlador.
        """
        if os.name != "posix":
            return {"start_new_session": True}
        if sys.version_info >= (3, 11):
            return {"process_group": 0}
        return {"preexec_fn": os.setpgrp}

    def _clear_active(self, process: subprocess.Popen[bytes]) -> None:
        with self._active_lock:
            if self._active_process is process:
                self._active_process = None
                self._active_pgid = None
                self._active_prefix = []

    @staticmethod
    def _signal_group(
        process: subprocess.Popen[bytes],
        pgid: int,
        sig: signal.Signals,
        prefix: list[str],
    ) -> None:
        """Señala también descendientes root creados por sudo.

        Un ``killpg`` hecho por el usuario puede terminar al proceso ``sudo``
        pero no a ``apt-get``/``dpkg`` ya ejecutados como root. Cuando el comando
        nació con ``sudo -n``, se envía además la señal con esa misma credencial
        vigente hacia el PGID completo.
        """
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

        if prefix[:2] == ["sudo", "-n"]:
            try:
                subprocess.run(
                    [*prefix, "kill", f"-{sig.name}", "--", f"-{pgid}"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif process.poll() is None:
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass

    def _terminate_group(
        self,
        process: subprocess.Popen[bytes],
        pgid: int,
        *,
        grace: float | None = None,
        prefix: list[str] | None = None,
    ) -> None:
        """Termina el grupo completo y recoge al proceso principal."""
        wait_for = self.terminate_grace if grace is None else grace
        active_prefix = list(prefix or [])
        self._signal_group(process, pgid, signal.SIGTERM, active_prefix)

        try:
            process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._signal_group(process, pgid, signal.SIGKILL, active_prefix)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # El líder pudo morir aunque un descendiente extraño haya cambiado
                # de sesión. No bloqueamos indefinidamente la interfaz.
                pass

    def run(
        self,
        argv: list[str],
        timeout: float | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        input_text: str | None = None,
        inherit_stdio: bool = False,
        quiet: bool = False,
    ) -> CommandResult:
        """Ejecuta un comando corto bajo la única frontera de PipeCraft.

        ``inherit_stdio`` se reserva para interacciones deliberadas con el
        terminal real, como ``sudo -v``. El resto de comandos conserva stdin
        cerrado y devuelve su salida estructurada. ``quiet`` descarta la salida
        únicamente cuando el llamador necesita un sondeo silencioso.
        """
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "timeout": timeout or self.timeout,
            "check": False,
            "env": env,
            "cwd": cwd,
            "text": True,
        }
        if inherit_stdio:
            kwargs["stdin"] = None
        elif input_text is not None:
            kwargs["input"] = input_text
        else:
            kwargs["stdin"] = subprocess.DEVNULL

        if quiet:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        elif not inherit_stdio:
            kwargs["capture_output"] = True

        try:
            process = subprocess.run(argv, **kwargs)
            return CommandResult(
                process.returncode,
                getattr(process, "stdout", "") or "",
                getattr(process, "stderr", "") or "",
                command=tuple(argv),
                elapsed_seconds=max(0.0, time.monotonic() - started),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(
                124, stdout, stderr or "La operación excedió el tiempo permitido.",
                command=tuple(argv), elapsed_seconds=max(0.0, time.monotonic() - started),
                timed_out=True,
            )
        except OSError as exc:
            return CommandResult(
                127, "", str(exc), command=tuple(argv),
                elapsed_seconds=max(0.0, time.monotonic() - started),
            )

    def run_interactive(
        self,
        argv: list[str],
        timeout: float | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> CommandResult:
        """Ejecuta heredando el terminal; PipeCraft no captura contraseñas."""
        return self.run(
            argv, timeout=timeout, env=env, cwd=cwd, inherit_stdio=True
        )

    def spawn(
        self,
        argv: list[str],
        *,
        stdin: Any = subprocess.DEVNULL,
        stdout: Any = subprocess.PIPE,
        stderr: Any = subprocess.PIPE,
        text: bool = False,
        encoding: str | None = None,
        errors: str | None = None,
        bufsize: int = 0,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        process_group: bool = False,
    ) -> Any:
        """Abre un proceso cuando un protocolo necesita stdin/stdout vivos.

        Ningún módulo de Styler crea procesos por su cuenta; los protocolos de
        larga vida (motor Rust, aplicaciones observadas, lanzadores) pasan por
        este método y por tanto conservan una sola frontera auditable.
        """
        kwargs: dict[str, Any] = {
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
            "text": text,
            "bufsize": bufsize,
            "env": env,
            "cwd": cwd,
            "shell": False,
        }
        if encoding is not None:
            kwargs["encoding"] = encoding
        if errors is not None:
            kwargs["errors"] = errors
        if process_group:
            kwargs.update(self._process_group_kwargs())
        return subprocess.Popen(argv, **kwargs)  # noqa: S603

    def spawn_protocol(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> Any:
        """Abre un protocolo JSON/JSONL con stdin, stdout y stderr separados."""
        return self.spawn(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=cwd,
            process_group=True,
        )

    def spawn_detached(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> Any:
        """Inicia un proceso de escritorio desacoplado y sin salida capturada."""
        return self.spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            process_group=True,
        )

    @staticmethod
    def wait_process(process: Any, timeout: float | None = None) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    @staticmethod
    def stop_process(process: Any, *, grace: float = 1.0) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass

    def run_streaming(
        self,
        argv: list[str],
        timeout: float | None = None,
        on_output: OutputCallback = None,
        on_heartbeat: HeartbeatCallback = None,
        on_start: StartCallback = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> CommandResult:
        if self._cancel_requested.is_set():
            return CommandResult(
                130, "", "La operación fue cancelada antes de iniciar el comando."
            )

        limit = timeout or self.timeout
        started = time.monotonic()
        try:
            process = subprocess.Popen(  # noqa: S603
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
                cwd=cwd,
                **self._process_group_kwargs(),
            )
        except OSError as exc:
            return CommandResult(127, "", str(exc))

        pgid = self._set_active(process, argv)
        if on_start is not None:
            try:
                on_start(process.pid)
            except Exception:
                # Un observador defectuoso no puede abortar el comando real.
                pass
        prefix = self._privilege_prefix(argv)
        chunks: queue.Queue[bytes | None] = queue.Queue()

        def read_pipe() -> None:
            try:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(4096)
                    if not chunk:
                        break
                    chunks.put(chunk)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read_pipe, name="styler-command-output", daemon=True)
        reader.start()

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        raw = bytearray()
        pending = ""
        last_heartbeat = started
        timed_out = False
        cancelled = False
        interactive_prompt = ""
        interactive_prompt_at = 0.0

        try:
            while True:
                now = time.monotonic()
                if self._cancel_requested.is_set() and process.poll() is None:
                    cancelled = True
                    if on_output is not None:
                        on_output(
                            "Cancelación solicitada: deteniendo el instalador y sus procesos hijos…"
                        )
                    self._terminate_group(process, pgid, prefix=prefix)

                if limit and now - started >= limit and process.poll() is None:
                    timed_out = True
                    self._terminate_group(process, pgid, grace=5, prefix=prefix)

                try:
                    chunk = chunks.get(timeout=0.2)
                except queue.Empty:
                    chunk = ...

                if chunk is None:
                    break
                if chunk is not ...:
                    raw.extend(chunk)
                    pending += decoder.decode(chunk)
                    pieces = re.split(r"[\r\n]+", pending)
                    pending = pieces.pop()
                    if on_output is not None:
                        for line in pieces:
                            if line.strip():
                                on_output(line.rstrip())
                    for line in pieces:
                        if line.strip() and _looks_like_interactive_prompt(line):
                            interactive_prompt = line.strip()
                            interactive_prompt_at = time.monotonic()

                if (
                    interactive_prompt
                    and process.poll() is None
                    and time.monotonic() - interactive_prompt_at >= self.interactive_prompt_grace
                ):
                    self._terminate_group(process, pgid, grace=5, prefix=prefix)

                now = time.monotonic()
                if on_heartbeat is not None and now - last_heartbeat >= self.heartbeat_interval:
                    on_heartbeat(now - started)
                    last_heartbeat = now
        finally:
            # Una excepción en el observador o en la lectura nunca debe dejar
            # apt/dpkg vivo en segundo plano.
            if process.poll() is None:
                self._terminate_group(process, pgid, grace=5, prefix=prefix)
            self._clear_active(process)

        pending += decoder.decode(b"", final=True)
        if pending.strip() and on_output is not None:
            on_output(pending.rstrip())

        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_group(process, pgid, grace=1, prefix=prefix)
            returncode = process.poll() if process.poll() is not None else 137

        stdout = raw.decode("utf-8", errors="replace")
        if cancelled or self._cancel_requested.is_set():
            repair = self._repair_dpkg_after_cancel(argv, on_output)
            detail = "La instalación fue cancelada de forma segura."
            if repair:
                detail += " " + repair
            return CommandResult(
                130, stdout, detail, command=tuple(argv), pid=process.pid,
                elapsed_seconds=max(0.0, time.monotonic() - started), cancelled=True,
            )
        if timed_out:
            return CommandResult(
                124, stdout, "La operación excedió el tiempo permitido.",
                command=tuple(argv), pid=process.pid,
                elapsed_seconds=max(0.0, time.monotonic() - started), timed_out=True,
            )
        if interactive_prompt:
            return CommandResult(
                125,
                stdout,
                "El instalador intentó abrir una pregunta interactiva que Styler no puede "
                "contestar de forma segura. Se detuvo el árbol completo antes de tocar tus "
                "archivos. "
                f"Última pregunta detectada: {interactive_prompt}",
                command=tuple(argv), pid=process.pid,
                elapsed_seconds=max(0.0, time.monotonic() - started),
            )
        return CommandResult(
            returncode, stdout, "", command=tuple(argv), pid=process.pid,
            elapsed_seconds=max(0.0, time.monotonic() - started),
        )

    @staticmethod
    def _is_dpkg_command(argv: list[str]) -> bool:
        names = {Path(value).name for value in argv}
        return bool(names & {"apt", "apt-get", "dpkg"})

    @staticmethod
    def _privilege_prefix(argv: list[str]) -> list[str]:
        if argv[:2] == ["sudo", "-n"]:
            return ["sudo", "-n"]
        if argv[:1] == ["pkexec"]:
            return ["pkexec"]
        return []

    def _repair_dpkg_after_cancel(
        self,
        argv: list[str],
        on_output: OutputCallback = None,
    ) -> str:
        """Repara únicamente si la cancelación dejó paquetes pendientes.

        Se ejecuta después de haber recogido el grupo anterior. No borra locks ni
        mata procesos ajenos. Si otro gestor legítimo posee dpkg, la reparación
        falla de forma visible y se deja para un intento posterior.
        """
        if not self._is_dpkg_command(argv) or not shutil.which("dpkg"):
            return ""
        try:
            audit = subprocess.run(
                ["dpkg", "--audit"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"No se pudo comprobar dpkg después de cancelar: {exc}"

        audit_text = (audit.stdout + "\n" + audit.stderr).strip()
        if not audit_text:
            if on_output is not None:
                on_output("Cancelación completada; dpkg no reporta paquetes pendientes.")
            return "dpkg quedó consistente."

        if on_output is not None:
            on_output("dpkg reporta una configuración pendiente; intentando repararla…")
        command = [
            *self._privilege_prefix(argv),
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "DEBCONF_NONINTERACTIVE_SEEN=true",
            "APT_LISTCHANGES_FRONTEND=none",
            "NEEDRESTART_MODE=a",
            "dpkg",
            "--configure",
            "-a",
        ]
        try:
            repaired = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"dpkg requiere reparación manual: {exc}"

        if repaired.returncode == 0:
            if on_output is not None:
                on_output("dpkg terminó de configurar los paquetes pendientes.")
            return "dpkg fue reparado automáticamente."
        tail = (repaired.stderr or repaired.stdout or "").strip().splitlines()
        reason = tail[-1] if tail else f"código {repaired.returncode}"
        return f"dpkg todavía requiere reparación manual: {reason}"


_INTERACTIVE_PROMPT_PATTERNS = (
    "<aceptar>",
    "<ok>",
    "¿desea continuar?",
    "do you want to continue?",
    "seleccione cuál gestor de sesiones",
    "select which display manager",
)


def _looks_like_interactive_prompt(line: str) -> bool:
    """Detecta preguntas incompatibles con un proceso cuyo stdin está cerrado.

    PipeCraft nunca debe fingir que puede contestar diálogos de ncurses/debconf.
    La detección es una red de seguridad: los comandos APT se preparan como no
    interactivos, pero si un paquete ignora esa política el proceso se corta con
    un error claro en vez de quedar congelado indefinidamente.
    """

    normalized = line.casefold()
    return any(pattern in normalized for pattern in _INTERACTIVE_PROMPT_PATTERNS)


@dataclass
class FakeRunner:
    """Un equipo simulado: qué programas hay, qué está instalado, qué se ofrece.

    - `programs`: ejecutables presentes en el PATH.
    - `installed`: identificadores «gestor:paquete» ya instalados.
    - `offered`: lo que cada gestor puede entregar. `None` = lo tiene todo.
    - `failing`: nombres cuya instalación falla.
    - `remotes`: remotos de Flatpak configurados.
    - `provides`: al instalar «gestor:paquete», aparecen estos ejecutables.
    """

    programs: set[str] = field(default_factory=set)
    installed: set[str] = field(default_factory=set)
    offered: set[str] | None = None
    failing: set[str] = field(default_factory=set)
    remotes: set[str] = field(default_factory=set)
    provides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    upgradable: set[str] = field(default_factory=set)
    lying: set[str] = field(default_factory=set)   # sale 0 pero no deja nada instalado
    calls: list[list[str]] = field(default_factory=list)

    # -- API de Runner ---------------------------------------------------
    def available(self, program: str) -> bool:
        return program in self.programs

    def run(self, argv: list[str], timeout: float | None = None) -> CommandResult:
        self.calls.append(list(argv))
        if not argv:
            # Un comando vacío es un fallo del planificador, no un éxito.
            return CommandResult(127, "", "Comando vacío")
        words = [item for item in argv if not item.startswith("-")]
        program = next((word for word in words if word in _PROGRAMS), "")
        package = words[-1] if len(words) > 1 else ""

        handler = _HANDLERS.get(program)
        if handler is None:
            return CommandResult(0)
        return handler(self, argv, words, package)

    # -- ayudas ----------------------------------------------------------
    def _offers(self, manager: str, package: str) -> bool:
        if self.offered is None:
            return True
        return f"{manager}:{package}" in self.offered

    def _install(self, manager: str, package: str) -> CommandResult:
        if package in self.failing or f"{manager}:{package}" in self.failing:
            return CommandResult(100, "", f"E: no se pudo instalar {package}")
        if not self._offers(manager, package):
            return CommandResult(100, "", f"E: Unable to locate package {package}")
        if package in self.lying or f"{manager}:{package}" in self.lying:
            return CommandResult(0, f"Setting up {package} ...")   # miente: no instala
        self.installed.add(f"{manager}:{package}")
        for program in self.provides.get(f"{manager}:{package}", ()):
            self.programs.add(program)
        return CommandResult(0, f"Setting up {package} ...")

    def _upgrade(self, manager: str, package: str) -> CommandResult:
        key = f"{manager}:{package}"
        if package in self.failing or key in self.failing:
            return CommandResult(100, "", f"E: no se pudo actualizar {package}")
        if key in self.upgradable:
            self.upgradable.discard(key)
            return CommandResult(0, f"1 upgraded, 0 newly installed. {package}")
        return CommandResult(0, "0 upgraded, 0 newly installed, 0 to remove.")


def _apt(runner: FakeRunner, argv: list[str], words: list[str], package: str) -> CommandResult:
    if "update" in words:
        return CommandResult(0, "Reading package lists...")
    if "install" in words:
        if "--only-upgrade" in argv:
            return runner._upgrade("apt", package)
        return runner._install("apt", package)
    return CommandResult(0)


def _apt_cache(runner: FakeRunner, argv, words, package) -> CommandResult:
    if runner._offers("apt", package):
        return CommandResult(0, f"{package}:\n  Installed: (none)\n  Candidate: 1.0\n")
    return CommandResult(0, f"{package}:\n  Installed: (none)\n  Candidate: (none)\n")


def _dpkg_query(runner: FakeRunner, argv, words, package) -> CommandResult:
    if f"apt:{package}" in runner.installed:
        return CommandResult(0, "installed")
    return CommandResult(1, "", "no packages found")


def _pacman(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "-Q" in argv:
        return CommandResult(0) if f"pacman:{package}" in runner.installed else CommandResult(1)
    if "-Si" in argv:
        return CommandResult(0) if runner._offers("pacman", package) else CommandResult(1)
    if "-Sy" in argv:
        return CommandResult(0)
    if "-S" in argv:
        if f"pacman:{package}" in runner.installed:
            return runner._upgrade("pacman", package)
        return runner._install("pacman", package)
    return CommandResult(0)


def _dnf(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "makecache" in words:
        return CommandResult(0, "Metadata cache created.")
    if "info" in words:
        return CommandResult(0) if runner._offers("dnf", package) else CommandResult(1)
    if "install" in words:
        return runner._install("dnf", package)
    if "upgrade" in words:
        return runner._upgrade("dnf", package)
    return CommandResult(0)


def _rpm(runner: FakeRunner, argv, words, package) -> CommandResult:
    present = f"dnf:{package}" in runner.installed or f"zypper:{package}" in runner.installed
    return CommandResult(0) if present else CommandResult(1)


def _zypper(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "refresh" in words:
        return CommandResult(0, "Repository metadata refreshed.")
    if "search" in words:
        return CommandResult(0) if runner._offers("zypper", package) else CommandResult(104)
    if "install" in words:
        return runner._install("zypper", package)
    if "update" in words:
        return runner._upgrade("zypper", package)
    return CommandResult(0)


def _flatpak(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "remotes" in words:
        return CommandResult(0, "\n".join(sorted(runner.remotes)))
    if "remote-add" in words:
        runner.remotes.add(words[-2] if len(words) > 2 else package)
        return CommandResult(0)
    if "remote-info" in words:
        return CommandResult(0) if runner._offers("flatpak", package) else CommandResult(1)
    if "info" in words:
        return (
            CommandResult(0)
            if f"flatpak:{package}" in runner.installed
            else CommandResult(1)
        )
    if "install" in words:
        return runner._install("flatpak", package)
    if "update" in words:
        return runner._upgrade("flatpak", package)
    return CommandResult(0)


def _snap(runner: FakeRunner, argv, words, package) -> CommandResult:
    if "list" in words:
        return CommandResult(0) if f"snap:{package}" in runner.installed else CommandResult(1)
    if "info" in words:
        return CommandResult(0) if runner._offers("snap", package) else CommandResult(1)
    if "install" in words:
        return runner._install("snap", package)
    if "refresh" in words:
        return runner._upgrade("snap", package)
    return CommandResult(0)


_HANDLERS = {
    "apt-get": _apt,
    "apt-cache": _apt_cache,
    "dpkg-query": _dpkg_query,
    "pacman": _pacman,
    "dnf": _dnf,
    "rpm": _rpm,
    "zypper": _zypper,
    "flatpak": _flatpak,
    "snap": _snap,
}

_PROGRAMS = set(_HANDLERS)


# ---------------------------------------------------------------------------
# PipeCraft process events
# ---------------------------------------------------------------------------

def _publish_process_event(ctx: Any, step: Any, payload: dict[str, Any]) -> None:
    """Publica un evento de proceso por el mismo canal que usa PipeCraft.

    No hay un bus paralelo para la terminal: progreso, comandos, salida, latidos
    y finalización viajan por ``progress_callback`` y el motor los persiste en
    ``events.jsonl``. La TUI y la CLI son observadores del mismo hilo.
    """
    callback = getattr(ctx, "values", {}).get("progress_callback")
    if not callable(callback):
        return
    event = {
        "step_id": str(getattr(step, "id", "")),
        "status": "running",
        "phase_progress": None,
        "event_type": "process",
        **payload,
    }
    event.setdefault("operation", event.get("message", "Proceso externo"))
    event.setdefault("message", event.get("operation", "Proceso externo"))
    try:
        callback(event)
    except Exception:
        # La observabilidad nunca debe tumbar el trabajo real.
        pass


def _safe_step_name(step_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", step_id).strip("-") or "command"


def run_step_command(
    ctx: Any,
    step: Any,
    argv: list[str],
    *,
    timeout: float | None = None,
    label: str = "",
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> CommandResult:
    """Ejecuta un comando como parte de un nodo PipeCraft y transmite todo.

    Esta es la única ruta admitida para comandos de efecto dentro de los DAG de
    Styler. La salida combinada se escribe línea a línea en un log durable y se
    publica en vivo. No se usa ``capture_output=True`` ni se espera al final
    para saber qué hizo el gestor de paquetes.
    """
    values = getattr(ctx, "values", {})
    runner = values.get("command_runner")
    if not isinstance(runner, PipeCraftRunner):
        runner = PipeCraftRunner()
        values["command_runner"] = runner

    command_index = int(values.get("_command_index", 0)) + 1
    values["_command_index"] = command_index
    step_id = str(getattr(step, "id", "command"))
    logs_dir = Path(getattr(ctx, "logs_dir"))
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{_safe_step_name(step_id)}-{command_index:02d}.log"
    rendered = shlex.join(argv)
    started = time.monotonic()
    last_output = started
    log = log_path.open("w", encoding="utf-8", buffering=1)
    log.write(json.dumps({"argv": argv, "cwd": str(cwd or ""), "started_monotonic": started}, ensure_ascii=False) + "\n")
    log.write(f"$ {rendered}\n")

    _publish_process_event(
        ctx,
        step,
        {
            "event_type": "command_started",
            "operation": label or f"Ejecutando {Path(argv[0]).name}",
            "message": f"$ {rendered}",
            "command": rendered,
            "argv": list(argv),
            "log_path": str(log_path),
            "elapsed_seconds": 0.0,
        },
    )

    def on_start(pid: int) -> None:
        _publish_process_event(
            ctx,
            step,
            {
                "event_type": "command_spawned",
                "operation": label or f"Ejecutando {Path(argv[0]).name}",
                "message": f"Proceso iniciado con PID {pid}.",
                "command": rendered,
                "pid": pid,
                "log_path": str(log_path),
                "elapsed_seconds": max(0.0, time.monotonic() - started),
            },
        )

    def on_output(line: str) -> None:
        nonlocal last_output
        last_output = time.monotonic()
        log.write(line + "\n")
        _publish_process_event(
            ctx,
            step,
            {
                "event_type": "command_output",
                "operation": label or f"Ejecutando {Path(argv[0]).name}",
                "message": line,
                "terminal_line": line,
                "stream": "combined",
                "command": rendered,
                "log_path": str(log_path),
                "elapsed_seconds": max(0.0, time.monotonic() - started),
                "quiet_seconds": 0.0,
            },
        )

    def on_heartbeat(elapsed: float) -> None:
        quiet = max(0.0, time.monotonic() - last_output)
        message = (
            f"Proceso activo · {elapsed:.0f} s transcurridos · "
            f"última salida hace {quiet:.0f} s"
        )
        log.write(f"[heartbeat] {message}\n")
        _publish_process_event(
            ctx,
            step,
            {
                "event_type": "command_heartbeat",
                "operation": label or f"Ejecutando {Path(argv[0]).name}",
                "message": message,
                "command": rendered,
                "log_path": str(log_path),
                "elapsed_seconds": elapsed,
                "quiet_seconds": quiet,
            },
        )

    # ``PipeCraftRunner`` no necesita shell y conserva stdin cerrado. ``env`` y
    # ``cwd`` se aplican temporalmente dentro de una instancia dedicada para el
    # comando; el runner general se mantiene deliberadamente pequeño.
    result = runner.run_streaming(
        argv,
        timeout=timeout,
        on_output=on_output,
        on_heartbeat=on_heartbeat,
        on_start=on_start,
        env=env,
        cwd=cwd,
    )
    elapsed = max(0.0, time.monotonic() - started)
    result.command = tuple(argv)
    result.elapsed_seconds = elapsed
    result.log_path = str(log_path)
    result.timed_out = result.returncode == 124
    result.cancelled = result.returncode == 130
    log.write(f"[finished] returncode={result.returncode} elapsed={elapsed:.3f}s\n")
    if result.stderr:
        log.write("[stderr] " + result.stderr.rstrip() + "\n")
    log.close()

    _publish_process_event(
        ctx,
        step,
        {
            "event_type": "command_finished",
            "status": "completed" if result.returncode == 0 else "failed",
            "operation": label or f"Finalizó {Path(argv[0]).name}",
            "message": f"Comando terminado con código {result.returncode} en {elapsed:.1f} s.",
            "command": rendered,
            "argv": list(argv),
            "returncode": result.returncode,
            "pid": result.pid,
            "log_path": str(log_path),
            "elapsed_seconds": elapsed,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
        },
    )
    return result

class ObservedProcess:
    """Proceso de larga vida observado por PipeCraft.

    Se usa para aplicaciones que deben permanecer abiertas mientras otro nodo
    espera condiciones (por ejemplo GIMP). Comparte el mismo canal de eventos y
    el mismo formato de log que ``run_step_command``.
    """

    def __init__(
        self,
        ctx: Any,
        step: Any,
        argv: list[str],
        *,
        label: str,
        runner: PipeCraftRunner | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.step = step
        self.argv = list(argv)
        self.label = label
        self.runner = runner or PipeCraftRunner()
        self.rendered = shlex.join(argv)
        self.started = time.monotonic()
        self._finished = False
        self._finish_lock = threading.Lock()

        values = getattr(ctx, "values", {})
        index = int(values.get("_command_index", 0)) + 1
        values["_command_index"] = index
        logs_dir = Path(getattr(ctx, "logs_dir"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        step_id = str(getattr(step, "id", "command"))
        self.log_path = logs_dir / f"{_safe_step_name(step_id)}-{index:02d}.log"
        self._log = self.log_path.open("w", encoding="utf-8", buffering=1)
        self._log.write(json.dumps({"argv": argv, "cwd": str(cwd or "")}, ensure_ascii=False) + "\n")
        self._log.write(f"$ {self.rendered}\n")

        _publish_process_event(
            ctx,
            step,
            {
                "event_type": "command_started",
                "operation": label,
                "message": f"$ {self.rendered}",
                "command": self.rendered,
                "argv": list(argv),
                "log_path": str(self.log_path),
                "elapsed_seconds": 0.0,
            },
        )
        self.process = self.runner.spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
            cwd=cwd,
        )

        _publish_process_event(
            ctx,
            step,
            {
                "event_type": "command_spawned",
                "operation": label,
                "message": f"Proceso iniciado con PID {self.pid}.",
                "command": self.rendered,
                "pid": self.pid,
                "log_path": str(self.log_path),
                "elapsed_seconds": max(0.0, time.monotonic() - self.started),
            },
        )

        stream = getattr(self.process, "stdout", None)
        self._reader: threading.Thread | None = None
        if stream is not None and hasattr(stream, "read"):
            self._reader = threading.Thread(
                target=self._read_output,
                name=f"pipecraft-process-{self.pid}",
                daemon=True,
            )
            self._reader.start()

    @property
    def pid(self) -> int:
        return int(getattr(self.process, "pid", 0) or 0)

    @property
    def returncode(self) -> int | None:
        return getattr(self.process, "returncode", None)

    def _read_output(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        stream = getattr(self.process, "stdout", None)
        try:
            while stream is not None:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    text = chunk
                else:
                    text = decoder.decode(chunk)
                pending += text
                pieces = re.split(r"[\r\n]+", pending)
                pending = pieces.pop()
                for line in pieces:
                    if line.strip():
                        self._emit_line(line.rstrip())
            pending += decoder.decode(b"", final=True)
            if pending.strip():
                self._emit_line(pending.rstrip())
        except Exception as exc:
            self._emit_line(f"[observador] No se pudo leer la salida: {exc}")

    def _emit_line(self, line: str) -> None:
        self._log.write(line + "\n")
        _publish_process_event(
            self.ctx,
            self.step,
            {
                "event_type": "command_output",
                "operation": self.label,
                "message": line,
                "terminal_line": line,
                "stream": "combined",
                "command": self.rendered,
                "pid": self.pid,
                "log_path": str(self.log_path),
                "elapsed_seconds": max(0.0, time.monotonic() - self.started),
            },
        )

    def poll(self) -> int | None:
        code = self.process.poll()
        if code is not None:
            self.finish_observation(code)
        return code

    def wait(self, timeout: float | None = None) -> int | None:
        code = self.process.wait(timeout=timeout)
        self.finish_observation(code)
        return code

    def terminate(self) -> None:
        self.process.terminate()

    def kill(self) -> None:
        self.process.kill()

    def send_signal(self, sig: int) -> None:
        sender = getattr(self.process, "send_signal", None)
        if callable(sender):
            sender(sig)
        elif sig == signal.SIGTERM:
            self.terminate()
        else:
            self.kill()

    def finish_observation(self, code: int | None = None) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
            if self._reader is not None and self._reader.is_alive():
                self._reader.join(timeout=1)
            final_code = self.returncode if code is None else code
            elapsed = max(0.0, time.monotonic() - self.started)
            self._log.write(f"[finished] returncode={final_code} elapsed={elapsed:.3f}s\n")
            self._log.close()
            _publish_process_event(
                self.ctx,
                self.step,
                {
                    "event_type": "command_finished",
                    "status": "completed" if final_code in (None, 0) else "failed",
                    "operation": self.label,
                    "message": f"Proceso terminado con código {final_code} en {elapsed:.1f} s.",
                    "command": self.rendered,
                    "pid": self.pid,
                    "returncode": final_code,
                    "log_path": str(self.log_path),
                    "elapsed_seconds": elapsed,
                },
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.process, name)


def start_step_process(
    ctx: Any,
    step: Any,
    argv: list[str],
    *,
    label: str,
    runner: PipeCraftRunner | None = None,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> ObservedProcess:
    return ObservedProcess(
        ctx,
        step,
        argv,
        label=label,
        runner=runner,
        env=env,
        cwd=cwd,
    )
