"""Perfil explícito de origen, destino, deseo y resultado de sesión."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SessionState:
    desktop: str = ""
    session: str = ""


@dataclass
class DesiredSession:
    desktop: str = "kde"
    preferred_session: str = "wayland"
    allow_xwayland: bool = True
    keep_x11_fallback: bool = True
    preserve_current_desktop: bool = True


@dataclass
class CompatibilityPolicy:
    temporary_x11_allowed: bool = True
    blocking_components: list[str] = field(default_factory=list)


@dataclass
class SessionResult:
    desktop: str = ""
    active_session: str = ""
    fallback_session: str = ""
    native_wayland_components: list[str] = field(default_factory=list)
    xwayland_components: list[str] = field(default_factory=list)
    x11_only_components: list[str] = field(default_factory=list)
    unresolved_components: list[str] = field(default_factory=list)


@dataclass
class SessionProfile:
    source: SessionState
    target_current: SessionState
    desired: DesiredSession = field(default_factory=DesiredSession)
    compatibility: CompatibilityPolicy = field(default_factory=CompatibilityPolicy)
    result: SessionResult = field(default_factory=SessionResult)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
