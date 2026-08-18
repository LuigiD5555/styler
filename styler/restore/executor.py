from __future__ import annotations

from ._support import *  # noqa: F401,F403

from .models import *  # noqa: F401,F403
from .planner import _plan_item, ordered_entries, plan_restore
from .sources import _restore_source
from .verification import _verify_item, environment_gate

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_SENSITIVE_FLAGS = {"--password", "--passwd", "--token", "--secret", "--auth"}

def execute(
    plan: RestorePlan,
    root: str = ".",
    home: str | Path | None = None,
    execute_real: bool = False,
    approve: bool = False,
    runner: Runner | None = None,
    refresh_index: bool = True,
    progress: ProgressCallback = None,
    label: str = "",
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    is_root: bool | None = None,
    apply_files: bool = True,
) -> RestoreReport:
    """Ejecuta el plan **replanificando cada etapa** contra el estado real.

    Con `apply_files=False` corre solo el PIPELINE 1 (entorno): instala y
    verifica el sistema sin tocar ni un archivo del usuario.

    Instalar Flatpak cambia lo que el equipo puede hacer: por eso el comando de
    una aplicación de Flatpak no se calcula al principio, sino justo antes de
    ejecutarla, cuando su gestor ya existe.
    """
    import os

    runner = runner or ProcessRunner()
    target = target or plan.target or target_mod.detect_target(root=root)
    root_now = (os.geteuid() == 0) if is_root is None else is_root
    prefix = apps_mod.privilege_prefix(runner, privilege, is_root)
    run_id = f"restore-{uuid.uuid4().hex[:8]}"

    report = RestoreReport(
        plan=plan, dry_run=not execute_real, started_at=time.time(), run_id=run_id
    )
    report.warnings.extend(plan.warnings)

    if plan.blocking():
        report.aborted_reason = (
            "Faltan requisitos obligatorios: "
            + "; ".join(
                f"{item.title} ({HUMAN.get(item.status, item.status)})"
                for item in plan.blocking()
            )
            + ". Styler no copió ningún archivo. Resuélvelos y vuelve a intentarlo."
        )
        report.finished_at = time.time()
        return report

    if not execute_real:
        report.finished_at = time.time()
        return report

    if not approve:
        report.aborted_reason = (
            "Instalar programas y escribir archivos requiere tu aprobación explícita."
        )
        report.finished_at = time.time()
        return report

    logs_dir = Path(root) / ".styler" / "runs" / run_id / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Una restauración nueva limpia el estado de cancelación del intento anterior.
    begin = getattr(runner, "begin_operation", None)
    if callable(begin):
        begin()

    # ===================== PIPELINE 1: ENTORNO ============================== #
    # Instala y verifica el sistema. No toca ni un archivo del usuario.
    # Puede ejecutarse solo (`styler pipeline entorno`) y repetirse sin daño.

    # La persona autoriza UNA vez, fuera de la TUI. Styler nunca lee contraseñas.
    authorization_error = _authorize_privileged_commands(plan, runner, progress)
    if authorization_error:
        report.aborted_reason = authorization_error
        report.finished_at = time.time()
        return report

    # APT abre diálogos de debconf aunque se use «-y» (p. ej. elegir el gestor de
    # inicio de sesión). Se conserva el actual y se repara un dpkg interrumpido
    # antes de instalar nada.
    apt_error, apt_warnings, apt_logs = _prepare_apt_noninteractive(
        plan, runner, logs_dir, progress
    )
    report.warnings.extend(apt_warnings)
    report.logs.update(apt_logs)
    if apt_error:
        report.aborted_reason = apt_error
        report.finished_at = time.time()
        return report

    # Una instalación de escritorio completo dura más que el ticket de sudo (15
    # min por omisión). Sin esto, la persona autoriza, Styler empieza, y a mitad
    # de camino falla por «permiso rechazado» sin haber hecho nada mal.
    # El mantenedor usa el mismo runner: así se puede probar sin tocar el sistema.
    ticket = privileges.keepalive_for(
        prefix, run=lambda argv: runner.run(list(argv), timeout=15).returncode
    )
    if ticket is not None:
        ticket.start()
        report.warnings.append(
            "La autorización se mantendrá vigente durante la instalación; no tendrás "
            "que volver a escribir la contraseña."
        )

    try:
        _run_installation_stages(
            plan, report, runner, target, root, prefix, root_now,
            logs_dir, refresh_index, progress, ticket,
        )
    finally:
        if ticket is not None:
            ticket.stop()
            if ticket.lost:
                report.warnings.append(
                    "La autorización de administrador se perdió durante la instalación "
                    "(¿alguien ejecutó «sudo -k»?)."
                )

    return _finish_environment_and_files(
        plan, report, runner, root, home, label, progress, files=apply_files,
    )

def _run_installation_stages(
    plan: RestorePlan,
    report: RestoreReport,
    runner: Runner,
    target: target_mod.Target,
    root: str,
    prefix: list[str],
    root_now: bool,
    logs_dir: Path,
    refresh_index: bool,
    progress: ProgressCallback,
    ticket: "privileges.SudoTicket | None" = None,
) -> None:
    """Las etapas de instalación, replanificadas contra el equipo real."""
    if refresh_index:
        _refresh(plan, runner, prefix, logs_dir, target, root, progress)

    total = len(plan.pending())
    done = 0
    stopped = False

    for stage in INSTALL_STAGES:
        if stopped:
            break
        for item in plan.by_stage(stage):
            if item.status in (ItemStatus.ALREADY_PRESENT, ItemStatus.SKIPPED_BY_USER):
                continue

            # --- REPLANIFICACIÓN: el equipo cambió desde que se hizo el plan ---
            if item.kind != "repository":
                _plan_item(item, target, runner, root, prefix, root_now)

            if item.status in (ItemStatus.ALREADY_PRESENT, ItemStatus.SKIPPED_BY_USER):
                continue
            if item.blocking:
                stopped = True
                break
            if not item.pending_install:
                continue
            if not item.argv:
                # Nunca se ejecuta un comando vacío: eso es un fallo del plan.
                item.status = ItemStatus.MANUAL_REQUIRED
                item.detail = (
                    "Styler no supo con qué comando satisfacer este requisito en este equipo."
                )
                stopped = True
                break

            # La autorización se renueva antes de cada comando privilegiado: una
            # instalación larga nunca debe morir por un ticket caducado.
            if ticket is not None and item.argv[:1] == ["sudo"] and not ticket.ensure():
                item.status = ItemStatus.PERMISSION_DENIED
                item.detail = (
                    "La autorización de administrador ya no está vigente. "
                    "Vuelve a aplicar: lo ya instalado no se repite."
                )
                stopped = True
                break

            done += 1
            updating = item.status == ItemStatus.WILL_UPDATE
            apps_mod._emit(
                progress, "install", done, max(total, done),
                f"{stage}: {item.title}" + (" (actualizando)" if updating else ""),
            )
            log = logs_dir / f"{_slug(item.key)}.log"
            result = _run_observable(
                runner,
                item.argv,
                timeout=1800,
                progress=progress,
                current=done,
                total=max(total, done),
                title=item.title,
                log_path=log,
            )
            report.logs[item.key] = str(log)

            if result.returncode != 0:
                text = (result.stderr or result.stdout or "").lower()
                if result.returncode == 130:
                    item.status = ItemStatus.FAILED
                    item.detail = result.stderr or "La instalación fue cancelada de forma segura."
                    stopped = True
                    break
                if any(
                    marker in text
                    for marker in (
                        "could not get lock",
                        "unable to acquire the dpkg frontend lock",
                        "no se pudo obtener el bloqueo",
                        "lock-frontend",
                    )
                ):
                    item.status = ItemStatus.FAILED
                    item.detail = (
                        "Otro proceso está usando APT/dpkg. Styler esperó sin borrar "
                        "archivos de bloqueo. Cierra el Gestor de actualizaciones o espera "
                        "a que termine y vuelve a aplicar. No elimines /var/lib/dpkg/lock*. "
                        + _tail(result)
                    )
                elif "permission" in text or "password" in text or "sudo:" in text:
                    item.status = ItemStatus.PERMISSION_DENIED
                    item.detail = (
                        "No se obtuvo autorización de administrador. Styler no lee "
                        "contraseñas en la interfaz. Acepta el diálogo del sistema, o "
                        "cierra Styler y ejecuta «sudo -v && styler» en esta misma terminal."
                    )
                else:
                    item.status = ItemStatus.FAILED
                    item.detail = _tail(result)
                stopped = True
                break

            if item.kind == "remote":
                item.status = ItemStatus.ADDED
                item.detail = "Remoto añadido."
            elif updating:
                manager = item.candidate.manager if item.candidate else ""
                if resolution_mod.up_to_date(manager, result):
                    item.status = ItemStatus.ALREADY_PRESENT
                    item.detail = "Ya estaba en la versión más reciente."
                else:
                    item.status = ItemStatus.UPDATED
                    item.detail = "Actualizado a la versión más reciente del repositorio."
            else:
                item.status = ItemStatus.INSTALLED
                item.detail = f"Instalado ahora ({item.candidate.key if item.candidate else ''})."

            # --- verificación inmediata de la etapa ---
            check = _verify_item(item, runner, root)
            if check is not None and not check.ok:
                item.status = ItemStatus.VERIFICATION_FAILED
                item.detail = check.detail
                stopped = True
                break

    # Lo que quedó sin ejecutar es PENDIENTE, nunca un éxito.
    for item in plan.items:
        if item.pending_install:
            item.status = ItemStatus.PENDING
            item.detail = "No se llegó a ejecutar por un fallo anterior."

def _finish_environment_and_files(
    plan: RestorePlan,
    report: RestoreReport,
    runner: Runner,
    root: str,
    home: str | Path | None,
    label: str,
    progress: ProgressCallback,
    files: bool = True,
) -> RestoreReport:
    """Cierra el pipeline del entorno, abre la compuerta y corre el de archivos."""
    # Una cancelación es terminal para este intento: no se ejecutan verificadores
    # nuevos ni se copia ningún archivo.
    if bool(getattr(runner, "cancellation_requested", False)):
        report.aborted_reason = (
            "La restauración fue cancelada de forma segura. Styler detuvo el instalador "
            "y NO copió ningún archivo de configuración."
        )
        report.finished_at = time.time()
        return report

    # --- Verificación global antes de tocar archivos --------------------------
    apps_mod._emit(progress, "verify", 1, 1, "Verificando el entorno antes de restaurar")
    report.verification = verify_mod.verify_requirements(
        environment_id=next(
            (item.key.split(":", 1)[1] for item in plan.items if item.kind == "desktop"), ""
        ),
        managers=[item.key.split(":", 1)[1] for item in plan.items if item.kind == "manager"],
        remotes=[item.key.split(":", 1)[1] for item in plan.items if item.kind == "remote"],
        candidates=[
            (item.key, item.title, item.candidate, item.mandatory)
            for item in plan.items
            if item.kind == "application" and item.status != ItemStatus.SKIPPED_BY_USER
        ],
        runner=runner,
        root=root,
    )

    failed_items = [item for item in plan.items if item.blocking]
    if failed_items or not report.verification.ok:
        reasons = [
            f"{item.title} ({HUMAN.get(item.status, item.status)})" for item in failed_items
        ]
        reasons.extend(check.title for check in report.verification.failures())
        report.aborted_reason = (
            "El entorno no quedó completo: "
            + "; ".join(dict.fromkeys(reasons))
            + ". Styler NO copió ningún archivo de configuración. "
            "Corrige lo que falta y vuelve a ejecutar: lo ya instalado no se repite."
        )
        report.finished_at = time.time()
        return report

    # ===================== COMPUERTA ======================================== #
    # El entorno quedó instalado y verificado. Solo ahora se abre el pipeline de
    # personalización. Si no se pidió, el trabajo del entorno igual queda hecho.
    report.environment_ready = True
    if not files:
        report.finished_at = time.time()
        return report

    # ===================== PIPELINE 2: PERSONALIZACIÓN ====================== #
    # Transaccional: punto de recuperación, escritura por etapas y rollback.
    entries = ordered_entries(plan)
    if entries:
        apps_mod._emit(
            progress, "files", 1, 1, "Creando punto de recuperación y aplicando archivos"
        )
        run, record = transaction_mod.apply_entries_transactional(
            entries,
            source_type=plan.source_type,
            source_id=plan.source_id,
            root=root,
            execute=True,
            approve=True,
            label=label or plan.source_id,
            home=home,
        )
        report.recovery_point = record.backup_snapshot
        report.transaction_id = record.transaction_id
        report.rollback_status = record.rollback_status
        report.files_applied = bool(record.applied)
        for stage in plan.file_stages:
            stage.status = ItemStatus.INSTALLED if record.applied else ItemStatus.FAILED
        if not record.applied:
            report.aborted_reason = (
                "La escritura de archivos falló. "
                + (
                    "Styler restauró el estado anterior."
                    if record.rolled_back
                    else "ATENCIÓN: revisa el journal antes de continuar."
                )
            )
            report.warnings.append(record.error)
    else:
        report.files_applied = True

    if report.installed or report.updated:
        report.warnings.append(apps_mod.UNDO_DOES_NOT_UNINSTALL)
    if report.needs_relogin:
        report.warnings.append(
            "Cierra la sesión y vuelve a entrar para que el escritorio cargue la configuración."
        )
    report.finished_at = time.time()
    return report

def _refresh(
    plan: RestorePlan,
    runner: Runner,
    prefix: list[str],
    logs_dir: Path,
    target: target_mod.Target,
    root: str,
    progress: ProgressCallback = None,
) -> None:
    """Refresca el índice de TODOS los gestores implicados: apt, pacman, dnf y zypper."""
    managers = {
        item.candidate.manager for item in plan.pending() if item.candidate is not None
    }
    commands: list[tuple[str, list[str]]] = []
    for manager in sorted(managers):
        argv = resolution_mod.refresh_argv(manager, prefix)
        if argv:
            commands.append((f"Actualizar catálogo de {manager}", argv))
    for index, (title, argv) in enumerate(commands, start=1):
        _run_observable(
            runner,
            argv,
            timeout=600,
            progress=progress,
            current=index,
            total=max(1, len(commands)),
            title=title,
            log_path=logs_dir / f"refresh-{index}.log",
        )

def _tail(result) -> str:
    text = (result.stderr or result.stdout or "").strip().splitlines()
    return text[-1] if text else f"código {result.returncode}"

def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-").lower()

def _authorize_privileged_commands(
    plan: RestorePlan, runner: Runner, progress: ProgressCallback = None
) -> str:
    """Autoriza una sola vez sin compartir stdin con Textual.

    Los comandos ``sudo`` del plan son siempre no interactivos (``sudo -n``).
    ``pkexec`` usa el agente gráfico de PolicyKit. Si PolicyKit no puede
    autorizar, se intenta una credencial sudo ya validada, pero nunca se pide ni
    se canaliza una contraseña desde Styler.
    """
    privileged = [
        item for item in plan.pending()
        if item.argv and item.argv[0] in {"pkexec", "sudo"}
    ]
    if not privileged:
        return ""

    apps_mod._emit(
        progress, "authorize", 0, max(1, len(plan.pending())),
        "Esperando autorización del sistema",
    )

    uses_pkexec = any(item.argv[0] == "pkexec" for item in privileged)
    if uses_pkexec:
        result = runner.run(["pkexec", "true"], timeout=300)
        if result.returncode == 0:
            return ""

        # En una sesión sin agente gráfico, una autorización sudo previamente
        # validada sigue siendo segura porque no lee del terminal de la TUI.
        if runner.available("sudo"):
            sudo = runner.run(["sudo", "-n", "-v"], timeout=30)
            if sudo.returncode == 0:
                for item in privileged:
                    if item.argv and item.argv[0] == "pkexec":
                        item.argv = ["sudo", "-n", *item.argv[1:]]
                return ""

        detail = _tail(result)
        return (
            "No se pudo completar la autorización del sistema. Styler no copió "
            "ningún archivo. Acepta el diálogo de PolicyKit; si no aparece, cierra "
            "Styler y ejecuta «sudo -v && styler» en esta misma terminal. Styler "
            f"nunca rellena ni guarda tu contraseña. Detalle: {detail}"
        )

    result = runner.run(["sudo", "-n", "-v"], timeout=30)
    if result.returncode == 0:
        return ""
    return (
        "La instalación necesita autorización de administrador, pero sudo no tiene "
        "una credencial vigente. Styler no copió ningún archivo. Cierra Styler y "
        "ejecuta «sudo -v && styler» en esta misma terminal; escribe la contraseña "
        "antes de que se abra la interfaz. No ejecutes Styler completo como root."
    )

def _privilege_prefix_from_argv(argv: list[str]) -> list[str]:
    if argv[:2] == ["sudo", "-n"]:
        return ["sudo", "-n"]
    if argv[:1] == ["pkexec"]:
        return ["pkexec"]
    return []

def _current_display_manager(
    path: str | Path = "/etc/X11/default-display-manager",
) -> str:
    """Devuelve el gestor actual sin asumir LightDM, SDDM, GDM u otro nombre."""

    try:
        value = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    name = Path(value).name
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name):
        return ""
    return name

def _prepare_apt_noninteractive(
    plan: RestorePlan,
    runner: Runner,
    logs_dir: Path,
    progress: ProgressCallback = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Prepara APT para una restauración sin preguntas invisibles.

    La selección del display manager no es una condición especial para KDE:
    es una pregunta genérica de paquetes Debian cuando ya existe otro gestor.
    Styler conserva dinámicamente el gestor que el equipo usa hoy. Si una
    instalación anterior dejó dpkg a medias, reanuda su configuración con la
    misma política no interactiva antes de instalar algo nuevo.
    """

    # Lo que importa es el gestor RESUELTO en este equipo, no el del equipo
    # original: un perfil de Mint restaurado en Arch no necesita nada de APT.
    apt_items = [
        item
        for item in plan.pending()
        if item.candidate is not None and item.candidate.manager == "apt"
    ]
    if not apt_items:
        return "", [], {}

    prefix = _privilege_prefix_from_argv(apt_items[0].argv)
    warnings: list[str] = []
    logs: dict[str, str] = {}
    total = max(1, len(plan.pending()))

    display_manager = _current_display_manager()
    if display_manager:
        if runner.available("debconf-set-selections"):
            seed_path = logs_dir / "display-manager.seed"
            seed_path.write_text(
                f"{display_manager} shared/default-x-display-manager "
                f"select {display_manager}\n",
                encoding="utf-8",
            )
            log_path = logs_dir / "prepare-display-manager.log"
            result = _run_observable(
                runner,
                [*prefix, "debconf-set-selections", str(seed_path)],
                timeout=60,
                progress=progress,
                current=0,
                total=total,
                title=f"Conservar {display_manager} como gestor de inicio de sesión",
                log_path=log_path,
            )
            logs["prepare:display-manager"] = str(log_path)
            if result.returncode != 0:
                warnings.append(
                    "No se pudo registrar previamente el gestor de inicio de sesión "
                    f"«{display_manager}». APT seguirá en modo no interactivo y "
                    "conservará la respuesta disponible en debconf."
                )
        else:
            warnings.append(
                f"Se detectó «{display_manager}» como gestor de inicio de sesión, "
                "pero no está disponible debconf-set-selections. APT seguirá en "
                "modo no interactivo."
            )

    # Un cierre forzado en medio de apt puede dejar paquetes desempaquetados.
    # Sólo se ejecuta dpkg --configure -a cuando dpkg --audit informa pendientes.
    if runner.available("dpkg"):
        audit = runner.run(["dpkg", "--audit"], timeout=30)
        audit_text = (audit.stdout + "\n" + audit.stderr).strip()
        if audit_text:
            log_path = logs_dir / "repair-dpkg.log"
            result = _run_observable(
                runner,
                dpkg_configure_argv(prefix),
                timeout=1800,
                progress=progress,
                current=0,
                total=total,
                title="Completar una instalación de paquetes interrumpida",
                log_path=log_path,
            )
            logs["prepare:dpkg"] = str(log_path)
            if result.returncode != 0:
                return (
                    "dpkg quedó incompleto y no pudo repararse automáticamente. "
                    "Styler no instaló más paquetes ni copió archivos. Revisa el "
                    f"registro: {log_path}",
                    warnings,
                    logs,
                )

    return "", warnings, logs

def _safe_command(argv: list[str]) -> str:
    """Comando legible sin exponer credenciales accidentales en pantalla o logs."""
    safe: list[str] = []
    redact_next = False
    for value in argv:
        if redact_next:
            safe.append("***")
            redact_next = False
            continue
        lower = value.lower()
        if lower in _SENSITIVE_FLAGS:
            safe.append(value)
            redact_next = True
            continue
        matched = next((flag for flag in _SENSITIVE_FLAGS if lower.startswith(flag + "=")), None)
        if matched:
            safe.append(value.split("=", 1)[0] + "=***")
            continue
        # URL con usuario:contraseña@host. Conserva el host para diagnosticar.
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", value)
        safe.append(value)
    return shlex.join(safe)

def _display_line(value: str, limit: int = 900) -> str:
    value = _ANSI.sub("", value).replace("\x00", "")
    value = "".join(character for character in value if character == "\t" or ord(character) >= 32)
    return value[-limit:]

def _program_name(argv: list[str]) -> str:
    for value in argv:
        name = Path(value).name
        if (
            name in {"sudo", "pkexec", "env"}
            or value in {"-n", "--"}
            or value.startswith("-")
            or ("=" in value and not value.startswith(("/", "./")))
        ):
            continue
        return name
    return "instalador"

def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def _run_observable(
    runner: Runner,
    argv: list[str],
    *,
    timeout: float,
    progress: ProgressCallback,
    current: int,
    total: int,
    title: str,
    log_path: Path,
):
    """Ejecuta un paso dejando salida viva, latidos y un registro persistente."""
    safe_command = _safe_command(argv)
    program = _program_name(argv)
    started = time.monotonic()
    last_output_at = started
    log_path.parent.mkdir(parents=True, exist_ok=True)
    apps_mod._emit(progress, "command", current, total, f"{title}\n$ {safe_command}")

    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(f"$ {safe_command}\n")
        handle.write(f"[inicio] {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        handle.flush()

        def output(line: str) -> None:
            nonlocal last_output_at
            clean = _display_line(line)
            if not clean.strip():
                return
            handle.write(clean + "\n")
            handle.flush()
            last_output_at = time.monotonic()
            apps_mod._emit(progress, "output", current, total, clean)

        def heartbeat(elapsed: float) -> None:
            quiet_for = max(0.0, time.monotonic() - last_output_at)
            quiet = (
                f" · sin salida nueva {_clock(quiet_for)}"
                if quiet_for >= 5
                else ""
            )
            apps_mod._emit(
                progress,
                "heartbeat",
                current,
                total,
                f"Proceso activo · {_clock(elapsed)} transcurridos · {program}{quiet}",
            )

        streaming = getattr(runner, "run_streaming", None)
        if callable(streaming):
            result = streaming(
                argv, timeout=timeout, on_output=output, on_heartbeat=heartbeat
            )
        else:
            # Compatibilidad con runners externos y dobles de prueba antiguos.
            result = runner.run(argv, timeout=timeout)
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                output(line)

        elapsed = time.monotonic() - started
        handle.write(f"\n[fin] código={result.returncode} duración={_clock(elapsed)}\n")
        handle.flush()

    state = "terminó correctamente" if result.returncode == 0 else f"falló (código {result.returncode})"
    apps_mod._emit(
        progress, "command_done", current, total,
        f"{title}: {state} después de {_clock(elapsed)}",
    )
    apps_mod._emit(progress, "logfile", current, total, f"Registro completo: {log_path}")
    return result

def _apply_source(
    source_type: str,
    source_id: str,
    root: str,
    execute_real: bool,
    approve: bool,
    home: str | Path | None,
    install_apps: bool,
    runner: Runner | None,
    privilege: str,
    refresh_index: bool,
    skip: Iterable[str],
    progress: ProgressCallback,
    target: target_mod.Target | None = None,
    is_root: bool | None = None,
) -> ApplyOutcome:
    source = _restore_source(source_type, source_id, root)
    resolved_target = target or target_mod.detect_target(root=root)
    plan = plan_restore(
        source_type,
        source_id,
        root=root,
        runner=runner,
        target=resolved_target,
        privilege=privilege,
        skip=skip,
        install_apps=install_apps,
        is_root=is_root,
    )
    app_plan = apps_mod.plan_installation(
        source.applications if install_apps else [],
        runner=runner,
        privilege=privilege,
        target=resolved_target,
        root=root,
        is_root=is_root,
    )
    report = execute(
        plan,
        root=root,
        home=home,
        execute_real=execute_real,
        approve=approve,
        runner=runner,
        refresh_index=refresh_index,
        progress=progress,
        label=source.label,
        target=resolved_target,
        privilege=privilege,
        is_root=is_root,
    )
    outcome = ApplyOutcome(
        source_type=source_type,
        source_id=source_id,
        dry_run=not execute_real,
        plan=plan,
        install_plan=app_plan,
        report=report,
    )
    outcome.warnings = list(dict.fromkeys([*plan.warnings, *report.warnings]))
    return outcome

def apply_profile(
    profile_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    home: str | Path | None = None,
    install_apps: bool = True,
    runner: Runner | None = None,
    privilege: str = "auto",
    refresh_index: bool = True,
    skip: Iterable[str] = (),
    progress: ProgressCallback = None,
    target: target_mod.Target | None = None,
    is_root: bool | None = None,
) -> ApplyOutcome:
    return _apply_source(
        "profile", profile_id, root, execute, approve, home, install_apps,
        runner, privilege, refresh_index, skip, progress, target, is_root,
    )

def apply_snapshot(
    snapshot_id: str,
    root: str = ".",
    execute: bool = False,
    approve: bool = False,
    home: str | Path | None = None,
    install_apps: bool = True,
    runner: Runner | None = None,
    privilege: str = "auto",
    refresh_index: bool = True,
    skip: Iterable[str] = (),
    progress: ProgressCallback = None,
    target: target_mod.Target | None = None,
    is_root: bool | None = None,
) -> ApplyOutcome:
    return _apply_source(
        "snapshot", snapshot_id, root, execute, approve, home, install_apps,
        runner, privilege, refresh_index, skip, progress, target, is_root,
    )

def run_pipeline(
    pipeline: str,
    source_type: str,
    source_id: str,
    root: str = ".",
    home: str | Path | None = None,
    execute: bool = False,
    approve: bool = False,
    runner: Runner | None = None,
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    refresh_index: bool = True,
    skip: Iterable[str] = (),
    install_apps: bool = True,
    is_root: bool | None = None,
    progress: ProgressCallback = None,
) -> RestoreReport:
    """Ejecuta entorno, personalización o la restauración completa."""
    if pipeline not in RESTORE_PIPELINES:
        raise ValueError(f"Pipeline de restauración desconocido: {pipeline}")

    runner = runner or ProcessRunner()
    plan = plan_restore(
        source_type,
        source_id,
        root=root,
        runner=runner,
        target=target,
        privilege=privilege,
        skip=skip,
        install_apps=install_apps,
        is_root=is_root,
    )
    source = _restore_source(source_type, source_id, root)

    if pipeline == PERSONALIZATION:
        report = RestoreReport(plan=plan, dry_run=not execute, started_at=time.time())
        decision = environment_gate(plan, runner, root)
        report.verification = decision.verification
        report.environment_ready = decision.open
        if not decision.open:
            report.aborted_reason = decision.reason
            report.finished_at = time.time()
            return report
        if not execute:
            report.finished_at = time.time()
            return report
        if not approve:
            report.aborted_reason = "Escribir archivos requiere tu aprobación explícita."
            report.finished_at = time.time()
            return report
        return _finish_environment_and_files(
            plan, report, runner, root, home, source.label, progress, files=True
        )

    return _execute_plan(
        plan,
        root=root,
        home=home,
        execute_real=execute,
        approve=approve,
        runner=runner,
        refresh_index=refresh_index,
        progress=progress,
        label=source.label,
        target=target,
        privilege=privilege,
        is_root=is_root,
        apply_files=(pipeline == ALL),
    )


_execute_plan = execute
