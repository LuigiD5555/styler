from __future__ import annotations

from ._support import *  # noqa: F401,F403

from .models import *  # noqa: F401,F403
from .models import _FILE_STAGE_BY_PART
from .sources import apply_restorable_base, _restore_source

def _resolve(item: RestoreItem, target: target_mod.Target, runner: Runner, root: str) -> Resolution:
    """Vuelve a preguntar al equipo cómo satisfacer este requisito, AHORA."""
    requirement = item.requirement
    if requirement is None:
        return Resolution(reason="Requisito sin definir.")
    if requirement.kind == "desktop":
        capability = catalogs.cached(root).desktop(requirement.key.split(":", 1)[1])
        return resolution_mod.resolve_capability(capability, target, runner)
    if requirement.kind == "manager":
        capability = catalogs.cached(root).manager(requirement.key.split(":", 1)[1])
        return resolution_mod.resolve_capability(capability, target, runner)
    if requirement.kind == "application":
        return resolution_mod.resolve_application(requirement, target, runner, root)
    return Resolution(reason="")

def _plan_item(
    item: RestoreItem,
    target: target_mod.Target,
    runner: Runner,
    root: str,
    prefix: list[str],
    is_root: bool,
) -> RestoreItem:
    """Decide el estado y el comando de un requisito según el estado real del equipo."""
    if item.kind == "remote":
        remote = item.key.split(":", 1)[1]
        if remote in target_mod.configured_flatpak_remotes(runner):
            item.status = ItemStatus.ALREADY_PRESENT
            item.detail = "Ya configurado."
            item.argv = []
            return item
        argv = target_mod.remote_add_argv(remote, root)
        if argv is None:
            item.status = ItemStatus.MANUAL_REQUIRED
            item.detail = (
                "Styler solo añade remotos declarados como oficiales en el catálogo. "
                "Añade este a mano con «flatpak remote-add»."
            )
            item.argv = []
            return item
        item.status = ItemStatus.WILL_ADD
        item.detail = "Remoto oficial y firmado."
        item.argv = argv
        return item

    if item.kind == "repository":
        return item  # se decide en build_plan; Styler nunca instala llaves ajenas

    result = _resolve(item, target, runner, root)
    item.candidate = result.candidate

    if not result.resolved:
        reproducible = item.app.reproducible if item.app is not None else True
        item.status = (
            ItemStatus.MANAGER_MISSING
            if result.no_manager and reproducible
            else ItemStatus.MANUAL_REQUIRED
        )
        item.detail = result.reason
        item.argv = []
        return item

    candidate = result.candidate
    installed = resolution_mod.is_installed(candidate, runner)
    policy = item.requirement.version_policy if item.requirement else "present"

    if installed and policy != "latest":
        item.status = ItemStatus.ALREADY_PRESENT
        item.detail = f"Ya está en este equipo ({candidate.key})."
        item.argv = []
        return item

    if installed and policy == "latest":
        item.status = ItemStatus.WILL_UPDATE
        item.detail = f"Ya está ({candidate.key}); se pedirá la versión más reciente."
        item.argv = resolution_mod.upgrade_argv(candidate, prefix)
        return item

    argv = resolution_mod.install_argv(candidate, prefix)
    if not argv:
        item.status = ItemStatus.UNSUPPORTED
        item.detail = f"Styler no sabe instalar con «{candidate.manager}»."
        item.argv = []
        return item

    if candidate.privileged and not prefix and not is_root:
        item.status = ItemStatus.PERMISSION_DENIED
        item.detail = (
            "Hace falta permiso de administrador y no hay «sudo» ni «pkexec». "
            "Ejecuta Styler desde una terminal con sudo."
        )
        item.argv = []
        return item

    item.status = ItemStatus.WILL_INSTALL
    item.detail = result.reason
    item.argv = argv
    return item

def build_plan(
    source: RestoreSource,
    root: str = ".",
    runner: Runner | None = None,
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    skip: Iterable[str] = (),
    install_desktop: bool = True,
    apt_root: str = "/etc/apt",
    is_root: bool | None = None,
) -> RestorePlan:
    """Traduce la intención de una configuración en requisitos de ESTE equipo.

    Es una foto del estado actual. Al ejecutar, cada etapa se vuelve a resolver:
    instalar Flatpak cambia lo que el equipo sabe hacer.
    """
    import os

    runner = runner or ProcessRunner()
    target = target or target_mod.detect_target(root=root)
    catalog = catalogs.cached(root)
    source = apply_restorable_base(source, root)
    skipped = set(skip)
    root_now = (os.geteuid() == 0) if is_root is None else is_root
    prefix = apps_mod.privilege_prefix(runner, privilege, is_root)

    plan = RestorePlan(
        source_type=source.source_type,
        source_id=source.source_id,
        target=target,
        privilege=list(prefix),
    )
    if not target.known:
        plan.warnings.append(
            "No se reconoció la distribución de este equipo. Añádela a un catálogo de "
            "familias (~/.config/styler/catalog/) para que Styler pueda resolver paquetes."
        )

    # -- 1. Escritorio ------------------------------------------------------
    if source.environment_id and install_desktop:
        environment = source.environment_id
        capability = catalog.desktop(environment)
        title = capability.title if capability else f"Escritorio {environment}"
        check = verify_mod.verify_desktop(environment, runner, root)

        if capability is None or not capability.verifiable:
            # Desconocido NO es presente.
            plan.items.append(
                RestoreItem(
                    kind="desktop",
                    key=f"desktop:{environment}",
                    title=title,
                    stage=STAGE_DESKTOP,
                    status=ItemStatus.MANUAL_REQUIRED,
                    detail=check.detail,
                )
            )
        else:
            requirement = Requirement(
                kind="desktop",
                key=f"desktop:{environment}",
                title=title,
                identity=capability.identity,
                version_policy=capability.version_policy,
            )
            item = RestoreItem(
                kind="desktop",
                key=requirement.key,
                title=title,
                stage=STAGE_DESKTOP,
                status=ItemStatus.PENDING,
                requirement=requirement,
            )
            if check.ok and capability.version_policy != "latest":
                item.status = ItemStatus.ALREADY_PRESENT
                item.detail = check.detail
            else:
                _plan_item(item, target, runner, root, prefix, root_now)
                if check.ok and item.status == ItemStatus.WILL_INSTALL:
                    # Está en el sistema pero el gestor no lo ve: no reinstalar a ciegas.
                    item.status = ItemStatus.ALREADY_PRESENT
                    item.detail = check.detail
            plan.items.append(item)

    # -- 2. Gestores necesarios --------------------------------------------
    needed = sorted({
        app.manager for app in source.applications if catalog.manager(app.manager) is not None
    })
    for manager in needed:
        capability = catalog.manager(manager)
        program = target_mod.manager_binary(manager, root)
        if runner.available(program):
            plan.items.append(
                RestoreItem(
                    kind="manager",
                    key=f"manager:{manager}",
                    title=capability.title,
                    stage=STAGE_MANAGERS,
                    status=ItemStatus.ALREADY_PRESENT,
                    detail="Disponible.",
                )
            )
            continue
        requirement = Requirement(
            kind="manager", key=f"manager:{manager}", title=capability.title
        )
        item = RestoreItem(
            kind="manager",
            key=requirement.key,
            title=capability.title,
            stage=STAGE_MANAGERS,
            status=ItemStatus.PENDING,
            requirement=requirement,
        )
        plan.items.append(_plan_item(item, target, runner, root, prefix, root_now))

    # -- 3. Remotos de Flatpak ---------------------------------------------
    remotes = sorted({
        (app.remote or "flathub").lower()
        for app in source.applications
        if app.manager == "flatpak"
    })
    for remote in remotes:
        item = RestoreItem(
            kind="remote",
            key=f"remote:{remote}",
            title=f"Remoto {remote}",
            stage=STAGE_REMOTES,
            status=ItemStatus.PENDING,
        )
        plan.items.append(_plan_item(item, target, runner, root, prefix, root_now))

    # -- 4. Repositorios de terceros ---------------------------------------
    # Styler NO añade llaves ni repositorios de terceros: eso es confianza, no
    # automatización. Los declara y se detiene.
    seen: set[str] = set()
    for app in source.applications:
        if app.manager != "apt" or not app.remote_url or app.remote_url in seen:
            continue
        if not target_mod.is_third_party_apt(app.remote_url, root):
            continue
        seen.add(app.remote_url)
        key = f"repo:{app.remote_url}"
        configured = target_mod.apt_repository_configured(app.remote_url, apt_root)
        # Si el paquete ya está disponible por otra vía en este equipo, el
        # repositorio original deja de ser obligatorio.
        alternative = resolution_mod.resolve_application(
            apps_mod._requirement(app, root), target, runner, root
        )
        needed_here = target.family in ("ubuntu", "debian") and not alternative.resolved
        plan.items.append(
            RestoreItem(
                kind="repository",
                key=key,
                title=f"Repositorio {app.remote_url}",
                stage=STAGE_REPOSITORIES,
                status=ItemStatus.ALREADY_PRESENT if configured else ItemStatus.MANUAL_REQUIRED,
                detail=(
                    "Ya está configurado en este equipo."
                    if configured
                    else (
                        f"«{app.title}» vino de este repositorio, que no está configurado aquí. "
                        "Añádelo con su llave oficial; Styler no instala llaves de terceros por ti."
                    )
                ),
                mandatory=needed_here and key not in skipped,
            )
        )

    # -- 5. Aplicaciones ----------------------------------------------------
    for app in source.applications:
        key = f"app:{app.app_id}"
        if app.app_id in skipped or key in skipped:
            plan.items.append(
                RestoreItem(
                    kind="application",
                    key=key,
                    title=app.title,
                    stage=STAGE_APPLICATIONS,
                    status=ItemStatus.SKIPPED_BY_USER,
                    detail="La omitiste a propósito.",
                    app=app,
                    mandatory=False,
                )
            )
            continue
        requirement = apps_mod._requirement(app, root)
        item = RestoreItem(
            kind="application",
            key=key,
            title=app.title,
            stage=STAGE_APPLICATIONS,
            status=ItemStatus.PENDING,
            requirement=requirement,
            app=app,
        )
        _plan_item(item, target, runner, root, prefix, root_now)

        # Si su gestor todavía no existe pero SE VA A INSTALAR en una etapa
        # anterior, no es un bloqueo: es orden. El comando se calculará después,
        # cuando el gestor exista de verdad (replanificación por etapas).
        if item.status in (ItemStatus.MANAGER_MISSING, ItemStatus.MANUAL_REQUIRED) and any(
            other.kind == "manager" and other.pending_install for other in plan.items
        ):
            item.status = ItemStatus.WILL_INSTALL
            item.argv = []
            item.detail = "Se resolverá cuando su gestor esté instalado."
        plan.items.append(item)

    # -- 6. Archivos, por etapas -------------------------------------------
    grouped: dict[int, list[FileEntry]] = {}
    for entry in source.files:
        part = classify(entry.path).part_id
        index = _FILE_STAGE_BY_PART.get(part, len(FILE_STAGES) - 1)
        grouped.setdefault(index, []).append(entry)
    for index, (key, title, _parts) in enumerate(FILE_STAGES):
        entries = grouped.get(index, [])
        if entries:
            plan.file_stages.append(FileStage(key=key, title=title, entries=entries))

    return plan
def ordered_entries(plan: RestorePlan) -> list[FileEntry]:
    """Archivos en el orden de las etapas: escritorio primero, recursos al final."""
    result: list[FileEntry] = []
    for stage in plan.file_stages:
        result.extend(stage.entries)
    return result

def plan_restore(
    source_type: str,
    source_id: str,
    root: str = ".",
    runner: Runner | None = None,
    target: target_mod.Target | None = None,
    privilege: str = "auto",
    skip: Iterable[str] = (),
    install_apps: bool = True,
    is_root: bool | None = None,
) -> RestorePlan:
    """Calcula el plan completo que la persona revisa y aprueba."""
    source = _restore_source(source_type, source_id, root)
    skipped = set(skip)
    if not install_apps:
        skipped.update(app.app_id for app in source.applications)
    plan = build_plan(
        source,
        root=root,
        runner=runner,
        target=target,
        privilege=privilege,
        skip=skipped,
        install_desktop=install_apps,
        is_root=is_root,
    )
    if not install_apps and source.applications:
        plan.warnings.append(
            f"Se pidió no instalarlas: {len(source.applications)} aplicaciones quedan "
            "fuera. La configuración puede quedar sin efecto donde la app no exista."
        )
    return plan
