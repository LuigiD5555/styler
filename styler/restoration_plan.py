"""Plan declarativo de restauración Mint/XFCE/X11 → KDE/Wayland.

Esta primera integración prepara y verifica; nunca elimina XFCE/X11 ni cambia
la sesión predeterminada antes de la barrera ``wayland.verified``.
"""
from __future__ import annotations

from styler.runtime.models import ErrorPolicy, NodeKind, PhaseDefinition, StepDefinition, WorkflowDefinition
from styler.session_profile import SessionProfile


def build_restoration_plan(profile: SessionProfile, kde_package: str = "kde-plasma-desktop") -> WorkflowDefinition:
    if profile.desired.desktop != "kde":
        raise ValueError("El plan actual solo soporta KDE como escritorio deseado.")
    if profile.desired.preferred_session != "wayland":
        raise ValueError("El plan actual requiere Wayland como sesión preferida.")

    steps = [
        StepDefinition(
            "system.detect", "note", "Detectar distribución, escritorio y sesión actuales.",
            provides=["system.detected"], shared_resources=["system:read"], phase="discovery",
        ),
        StepDefinition(
            "recovery.prepare", "note", "Preparar journal y conservar XFCE/X11 como recuperación.",
            needs=["system.detect"], requires=["system.detected"], provides=["recovery.ready"],
            exclusive_resources=["recovery"], phase="preparation", barrier=True,
            rollback={"level": "full", "strategy": "file_journal"},
        ),
        StepDefinition(
            "apt.metadata", "note", "Actualizar metadatos APT antes de instalar.",
            needs=["recovery.prepare"], requires=["recovery.ready"], provides=["apt.ready"],
            exclusive_resources=["apt", "dpkg"], shared_resources=["network"],
            phase="base",
        ),
        StepDefinition(
            "desktop.kde.install", "install_package", "Instalar KDE Plasma.",
            needs=["apt.metadata"], requires=["apt.ready"],
            provides=["desktop.kde.installed", "session.wayland.candidate", "session.x11.fallback"],
            exclusive_resources=["apt", "dpkg"], shared_resources=["network"], provider="apt",
            requires_approval=True, retries=1, timeout=1800, phase="base",
            config={"package": {"manager": "apt", "name": kde_package}},
            session_support={"wayland": "candidate", "xwayland": "not_applicable", "x11": "fallback"},
            rollback={"level": "best_effort", "strategy": "keep_packages_and_restore_files"},
        ),
        StepDefinition(
            "desktop.kde.verify", "note", "Verificar KDE y las sesiones disponibles.",
            needs=["desktop.kde.install"], requires=["desktop.kde.installed"],
            provides=["desktop.kde.verified"], shared_resources=["system:read"],
            phase="base", barrier=True, kind=NodeKind.CHECK,
        ),
        StepDefinition(
            "configuration.apply", "note", "Aplicar únicamente configuraciones con proveedor verificado.",
            needs=["desktop.kde.verify"], requires=["desktop.kde.verified"],
            provides=["configuration.applied"], exclusive_resources=["user-config:kde"],
            phase="configuration",
        ),
        StepDefinition(
            "wayland.prepare", "note", "Preparar portales, PipeWire y XWayland sin cambiar la sesión predeterminada.",
            needs=["configuration.apply"], requires=["configuration.applied", "session.wayland.candidate"],
            provides=["wayland.prepared", "x11.preserved"],
            exclusive_resources=["session-manager"], phase="wayland",
            session_support={"wayland": "preferred", "xwayland": "allowed", "x11": "preserved"},
        ),
        StepDefinition(
            "wayland.logout", "note", "Guardar estado y solicitar iniciar Plasma (Wayland).",
            needs=["wayland.prepare"], requires=["wayland.prepared", "x11.preserved"],
            provides=["logout.requested"], exclusive_resources=["logout"],
            phase="wayland", barrier=True,
        ),
        StepDefinition(
            "wayland.verify", "note", "Verificar sesión, portales, PipeWire, portapapeles y aplicaciones críticas.",
            needs=["wayland.logout"], requires=["logout.requested"], provides=["wayland.verified"],
            shared_resources=["system:read"], phase="verification", barrier=True, kind=NodeKind.CHECK,
        ),
        StepDefinition(
            "wayland.activate", "note", "Ofrecer Wayland como preferida manteniendo X11 y XFCE.",
            needs=["wayland.verify"], requires=["wayland.verified", "x11.preserved"],
            provides=["migration.completed"], exclusive_resources=["session-manager", "display-manager"],
            phase="activation",
        ),
    ]
    return WorkflowDefinition(
        "restore.linuxmint.kde.wayland.v1", steps,
        description="Restauración segura con Wayland preferido y X11/XFCE preservados.",
        metadata={"max_workers": 4, "session_profile": profile.to_dict(), "safe_first_migration": True},
        phases={
            "discovery": PhaseDefinition("Descubrir el sistema actual"),
            "preparation": PhaseDefinition("Preparar recuperación"),
            "base": PhaseDefinition("Instalar y verificar KDE"),
            "configuration": PhaseDefinition("Aplicar configuración"),
            "wayland": PhaseDefinition("Preparar la sesión Wayland"),
            "verification": PhaseDefinition("Verificar la sesión", tags=["verification"]),
            "activation": PhaseDefinition("Activar el resultado"),
        },
        on_error=ErrorPolicy(
            default="continue",
            nodes={
                "recovery.prepare": "stop",
                "apt.metadata": "stop",
                "desktop.kde.install": "stop",
                "desktop.kde.verify": "stop",
                "wayland.prepare": "stop",
                "wayland.verify": "stop",
                "wayland.activate": "stop",
            },
        ),
    )
