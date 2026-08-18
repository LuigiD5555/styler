from __future__ import annotations

from ._service_support import *  # noqa: F401,F403
from ._service_support import _CONFIG_VERSION_DIR, _change_name

class DiscoveryOperations:
    def available_changes(self) -> tuple[ChangeCard, ...]:
        """Todos los cambios aplicables, sin exponer cómo fueron definidos.

        PhotoGIMP sigue usando su pipeline YAML/catálogo de componentes. Los
        ``.stylerpkg`` aportan DAG portables. Ambos convergen aquí como
        ``ChangeCard`` y, desde este punto, usan el mismo flujo de Cambios.
        """
        cards: list[ChangeCard] = [self._photogimp_available_card()]
        records = read_json(self._records_path)
        for change_id, change in self._declarative_changes.items():
            compatibility_error = change.compatibility_error(
                family=self._target.family, architecture=platform.machine(),
            )
            if compatibility_error:
                continue
            record = records.get(change_id, {})
            status = str(record.get("status") or "") if isinstance(record, dict) else ""
            retry = status in CONTINUATION_STATUSES
            requirement_note = (
                " Requiere: " + ", ".join(change.requires_changes) + "."
                if change.requires_changes else ""
            )
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=change.recipe.name,
                    description=change.description,
                    category=change.category,
                    status=ChangeStatus.AVAILABLE,
                    status_label="Reintento disponible" if retry else "Disponible",
                    provider_id="yaml",
                    provider_label=change.provider_label,
                    automation_level=AutomationLevel.AUTOMATIC,
                    detail=f"DAG declarado en {change.source.name}.{requirement_note}",
                    warning="",
                    reversible=self.can_rollback(change_id),
                    continuation_available=False,
                )
            )
        for change_id, package, graph in self._portable_change_sources():
            record = records.get(change_id, {})
            status = str(record.get("status") or "") if isinstance(record, dict) else ""
            retry = status in CONTINUATION_STATUSES
            detail = (
                f"DAG «{graph.title}» · {len(graph.workflow.steps)} paso(s) · "
                f"{package.identity}."
            )
            if status == ChangeStatus.INTEGRATED:
                detail += " Ya fue integrado; puedes volver a aplicarlo como reparación."
            elif retry:
                detail += " Existe una ejecución incompleta; puedes revisar el DAG y volver a intentarlo."
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=(package.manifest.name if self._package_graph_count(package) == 1 else graph.title),
                    description=graph.description or package.manifest.description or "Cambio importado en formato .stylerpkg.",
                    category="Paquete .stylerpkg · DAG",
                    status=ChangeStatus.AVAILABLE,
                    status_label="Reintento disponible" if retry else "Disponible",
                    provider_id="stylerpkg",
                    provider_label="DAG de paquete .stylerpkg",
                    automation_level=AutomationLevel.AUTOMATIC,
                    detail=detail,
                    warning="",
                    reversible=self.can_rollback(change_id),
                    continuation_available=False,
                )
            )
        return tuple(cards)

    def integrated_changes(self) -> tuple[ChangeCard, ...]:
        cards = list(self._photogimp_integrated_cards())
        records = read_json(self._records_path)
        live_sources = {change_id: (package, graph) for change_id, package, graph in self._portable_change_sources()}
        for change_id, record in records.items():
            if not change_id.startswith("pkg.") or not isinstance(record, dict):
                continue
            status = str(record.get("status", ChangeStatus.UNKNOWN))
            if status not in {
                ChangeStatus.INTEGRATED,
                ChangeStatus.PREPARED,
                ChangeStatus.FAILED,
                ChangeStatus.NEEDS_ATTENTION,
                ChangeStatus.PARTIALLY_REVERTED,
                ChangeStatus.REVERTING,
                ChangeStatus.INTEGRATING,
            }:
                continue
            package_graph = live_sources.get(change_id)
            if package_graph is not None:
                package, graph = package_graph
                name = str(record.get("name") or package.manifest.name or graph.title)
                description = graph.description or package.manifest.description or "Cambio portable registrado por Styler."
                detail = str(record.get("message") or f"DAG «{graph.title}» desde {package.identity}.")
            else:
                name = str(record.get("name") or _change_name(change_id))
                description = "Cambio portable registrado por Styler."
                detail = str(record.get("message") or "El paquete ya no está en la biblioteca local.")
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=name,
                    description=description,
                    category="Paquete .stylerpkg · DAG",
                    status=status,
                    status_label=self._status_label(status),
                    provider_id="stylerpkg",
                    provider_label="DAG de paquete .stylerpkg",
                    automation_level=str(record.get("automation_level", AutomationLevel.AUTOMATIC)),
                    detail=detail,
                    reversible=self.can_rollback(change_id),
                    detected_at=float(record.get("updated_at", 0.0)),
                )
            )
        for change_id, change in self._declarative_changes.items():
            record = records.get(change_id, {})
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or ChangeStatus.UNKNOWN)
            if status not in {
                ChangeStatus.INTEGRATED, ChangeStatus.PREPARED, ChangeStatus.FAILED,
                ChangeStatus.NEEDS_ATTENTION, ChangeStatus.PARTIALLY_REVERTED,
                ChangeStatus.REVERTING, ChangeStatus.INTEGRATING,
            }:
                continue
            cards.append(
                ChangeCard(
                    change_id=change_id,
                    name=str(record.get("name") or change.recipe.name),
                    description=change.description,
                    category=change.category,
                    status=status,
                    status_label=self._status_label(status),
                    provider_id="yaml",
                    provider_label=change.provider_label,
                    automation_level=str(record.get("automation_level") or AutomationLevel.AUTOMATIC),
                    detail=str(record.get("message") or f"DAG YAML incorporado: {change.source.name}."),
                    reversible=self.can_rollback(change_id),
                    detected_at=float(record.get("updated_at", 0.0)),
                )
            )
        return tuple(cards)

    def _photogimp_available_card(self) -> ChangeCard:
        provider = self.provider_for("photogimp")
        option = self._provider_option(provider)
        integrated = {item.change_id: item for item in self._photogimp_integrated_cards()}
        current = integrated.get("photogimp")
        record = read_json(self._records_path).get("photogimp", {})
        previous_status = str(record.get("status") or "") if isinstance(record, dict) else ""
        continuation = previous_status in CONTINUATION_STATUSES
        if current and current.status == ChangeStatus.INTEGRATED:
            detail = (
                "PhotoGIMP ya está integrado; puedes revisar su estrategia o volver a "
                "integrarlo si necesitas reparar la configuración."
            )
        elif continuation:
            detail = (
                "Hay una integración incompleta. Styler comprobará el estado real, "
                "reutilizará lo ya terminado y continuará desde el primer paso pendiente."
            )
        else:
            detail = "Instala GIMP y adapta su interfaz mediante un pipeline verificable y reversible."
        return ChangeCard(
            change_id="photogimp",
            name="PhotoGIMP",
            description="Convierte GIMP en una experiencia de trabajo similar a Photoshop.",
            category="Creatividad · GIMP · Interfaz",
            status=ChangeStatus.AVAILABLE,
            status_label="Continuación disponible" if continuation else "Disponible",
            provider_id=provider,
            provider_label=option.label,
            automation_level=option.automation_level,
            detail=detail,
            warning=option.warning,
            reversible=provider == "flatpak",
            continuation_available=continuation,
        )

    def _photogimp_integrated_cards(self) -> tuple[ChangeCard, ...]:
        detected = self._detect_photogimp()
        records = read_json(self._records_path)
        record = records.get("photogimp", {})
        if detected is not None:
            provider_id, marker = detected
            return (
                ChangeCard(
                    change_id="photogimp",
                    name="PhotoGIMP",
                    description="Adaptación de GIMP detectada en este equipo.",
                    category="Creatividad · GIMP · Interfaz",
                    status=ChangeStatus.INTEGRATED,
                    status_label="Integrado",
                    provider_id=provider_id,
                    provider_label=PROVIDER_LABELS.get(provider_id, provider_id),
                    automation_level=(
                        AutomationLevel.AUTOMATIC
                        if provider_id == "flatpak"
                        else AutomationLevel.ASSISTED
                    ),
                    detail=f"Marcador verificado en {marker}",
                    reversible=bool(record.get("reversible", provider_id == "flatpak")),
                    detected_at=float(record.get("updated_at", marker.stat().st_mtime)),
                ),
            )
        if record:
            status = str(record.get("status", ChangeStatus.UNKNOWN))
            if status in {
                ChangeStatus.PREPARED,
                ChangeStatus.FAILED,
                ChangeStatus.NEEDS_ATTENTION,
                ChangeStatus.PARTIALLY_REVERTED,
                ChangeStatus.REVERTING,
                ChangeStatus.INTEGRATING,
            }:
                provider = str(record.get("provider_id", ""))
                handoff = str(record.get("handoff_path", ""))
                return (
                    ChangeCard(
                        change_id="photogimp",
                        name="PhotoGIMP",
                        description="Estado registrado por Styler.",
                        category="Creatividad · GIMP · Interfaz",
                        status=status,
                        status_label=self._status_label(status),
                        provider_id=provider,
                        provider_label=PROVIDER_LABELS.get(provider, provider),
                        automation_level=str(record.get("automation_level", AutomationLevel.ASSISTED)),
                        detail=(
                            f"Archivo preparado en {handoff}"
                            if handoff
                            else str(record.get("message", ""))
                        ),
                        reversible=bool(record.get("reversible", False)),
                        detected_at=float(record.get("updated_at", 0.0)),
                    ),
                )
        return ()

    def provider_options(self, change_id: str) -> tuple[ProviderOption, ...]:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return (
                ProviderOption(
                    provider_id="stylerpkg",
                    label="DAG de paquete .stylerpkg",
                    description="El cambio ejecuta el DAG declarativo contenido en el paquete importado.",
                    automation_level=AutomationLevel.AUTOMATIC,
                    recommended=True,
                    available=True,
                ),
            )
        component = self._registry.get("app.gimp")
        if component is None:
            return ()
        options: list[ProviderOption] = []
        for provider in component.providers:
            if provider.id == "appimage":
                # No existe todavía una fuente AppImage oficial declarada que
                # Styler pueda descargar de manera reproducible.
                continue
            if provider.families and self._target.family not in provider.families and "*" not in provider.families:
                continue
            automatic = provider.id == "flatpak"
            available = self._provider_command_available(provider.id)
            warning = ""
            if not automatic:
                warning = (
                    "PhotoGIMP recomienda GIMP desde Flathub. Con esta fuente Styler "
                    "instalará GIMP y descargará PhotoGIMP, pero la integración quedará manual."
                )
            if not available:
                warning = (warning + " " if warning else "") + (
                    "El gestor correspondiente no fue detectado; Styler mostrará el fallo de preparación si sigue ausente."
                )
            options.append(
                ProviderOption(
                    provider_id=provider.id,
                    label=PROVIDER_LABELS.get(provider.id, provider.id),
                    description=(
                        "Integración automática, respaldo y verificación completos."
                        if automatic
                        else "Instalación asistida: GIMP automático y PhotoGIMP preparado en Descargas."
                    ),
                    automation_level=(
                        AutomationLevel.AUTOMATIC if automatic else AutomationLevel.ASSISTED
                    ),
                    recommended=automatic,
                    available=available,
                    warning=warning,
                )
            )
        options.sort(key=lambda item: (not item.recommended, item.label.lower()))
        return tuple(options)

    def provider_for(self, change_id: str) -> str:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return "stylerpkg"
        preferences = read_json(self._preferences_path)
        selected = str(preferences.get("providers", {}).get(change_id, ""))
        valid = {option.provider_id for option in self.provider_options(change_id)}
        return selected if selected in valid else "flatpak"

    def set_provider(self, change_id: str, provider_id: str) -> None:
        if self._is_portable_change(change_id):
            if provider_id != "stylerpkg":
                raise ValueError("Un cambio portable usa el DAG contenido en su .stylerpkg.")
            return
        valid = {option.provider_id for option in self.provider_options(change_id)}
        if provider_id not in valid:
            raise ValueError(f"El proveedor '{provider_id}' no está disponible para este equipo.")
        preferences = read_json(self._preferences_path)
        providers = dict(preferences.get("providers", {}))
        providers[change_id] = provider_id
        preferences["providers"] = providers
        write_json(self._preferences_path, preferences)

    PHOTOGIMP_OPTIONS: tuple[ChangeOption, ...] = (
        ChangeOption(
            "backup",
            "Respaldar la configuración actual de GIMP",
            "Copia tu configuración antes de tocarla. Sin respaldo, deshacer solo "
            "puede quitar lo que Styler escribió, no devolver lo que había.",
            default=True,
        ),
        ChangeOption(
            "rewrite_launchers",
            "Adaptar el acceso del menú",
            "Reescribe el lanzador de PhotoGIMP para que abra GIMP Flatpak.",
            default=True,
            advanced=True,
        ),
        ChangeOption(
            "startup_timeout_seconds",
            "Tiempo máximo de arranque de GIMP",
            "Cuánto esperar a que GIMP complete su primer arranque. Styler conserva "
            "cada hito observado; este límite solo se agota si falta uno de ellos.",
            kind="number",
            default=90.0,
            minimum=15.0,
            maximum=300.0,
            advanced=True,
        ),
    )

    def options_for(self, change_id: str) -> tuple[ChangeOption, ...]:
        self._require_change(change_id)
        if self._is_portable_change(change_id):
            return ()
        return self.PHOTOGIMP_OPTIONS

    def default_options(self, change_id: str) -> dict[str, Any]:
        return {option.option_id: option.default for option in self.options_for(change_id)}

    def normalize_options(self, change_id: str, values: dict[str, Any] | None) -> dict[str, Any]:
        """Toda opción desconocida se descarta y toda opción fuera de rango se
        recorta: un paquete importado no puede introducir ajustes nuevos."""
        resolved = self.default_options(change_id)
        for option in self.options_for(change_id):
            if values and option.option_id in values:
                resolved[option.option_id] = option.coerce(values[option.option_id])
        return resolved

    def _detect_photogimp(self) -> tuple[str, Path] | None:
        candidates = (
            ("flatpak", self.home / ".var" / "app" / "org.gimp.GIMP" / "config" / "GIMP"),
            ("snap", self.home / "snap" / "gimp" / "current" / ".config" / "GIMP"),
            ("native", self.home / ".config" / "GIMP"),
        )
        for provider, root in candidates:
            markers = [root / ".photogimp-marker"]
            if root.is_dir():
                markers.extend(
                    path / ".photogimp-marker"
                    for path in root.iterdir()
                    if path.is_dir() and _CONFIG_VERSION_DIR.fullmatch(path.name)
                )
            marker = next((path for path in markers if path.is_file()), None)
            if marker is not None:
                if provider == "native":
                    try:
                        metadata = dict(
                            line.split("=", 1)
                            for line in marker.read_text(encoding="utf-8").splitlines()
                            if "=" in line
                        )
                    except OSError:
                        metadata = {}
                    provider = str(metadata.get("provider") or self._target.native_manager or "native")
                return provider, marker
        return None

    @staticmethod
    def _portable_change_id(package_id: str, graph_id: str) -> str:
        """Identidad estable del cambio sin colisionar con DAG incorporados.

        El usuario nunca necesita ver este prefijo. Internamente evita que un
        paquete pueda reemplazar por accidente a ``photogimp`` u otro cambio
        incorporado que use el mismo ``graph_id``.
        """
        candidate = f"pkg.{package_id}.{graph_id}"
        if len(candidate) <= 128:
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:20]
        return f"pkg.{package_id[:80]}.{digest}"[:128].rstrip(".-_")

    @staticmethod
    def _package_graph_count(package: InstalledPackage) -> int:
        return sum(1 for artifact in package.manifest.artifacts if artifact.kind == "graph")

    def _portable_change_sources(self) -> tuple[tuple[str, InstalledPackage, GraphDefinition], ...]:
        """DAG aportados por paquetes ``change`` registrados.

        Importar un paquete solo registra el artefacto. La ejecución sigue
        ocurriendo exclusivamente por ``ChangeService`` desde la pestaña
        Cambios, igual que PhotoGIMP.
        """
        sources: list[tuple[str, InstalledPackage, GraphDefinition]] = []
        latest: dict[str, InstalledPackage] = {}
        for package in self._portable_library.list_packages():
            if package.manifest.package_type is not PackageType.CHANGE:
                continue
            current = latest.get(package.manifest.package_id)
            if current is None or package.imported_at >= current.imported_at:
                latest[package.manifest.package_id] = package
        for package in latest.values():
            for artifact in package.manifest.artifacts:
                if artifact.kind != "graph":
                    continue
                raw = self._portable_library.read_artifact(package, artifact)
                graph = GraphDefinition.from_dict(json.loads(raw.decode("utf-8")))
                change_id = self._portable_change_id(package.manifest.package_id, graph.graph_id)
                sources.append((change_id, package, graph))
        return tuple(sources)

    def _portable_source(self, change_id: str) -> tuple[InstalledPackage, GraphDefinition] | None:
        for candidate, package, graph in self._portable_change_sources():
            if candidate == change_id:
                return package, graph
        return None

    def _is_portable_change(self, change_id: str) -> bool:
        return self._portable_source(change_id) is not None

    def _provider_option(self, provider_id: str) -> ProviderOption:
        option = next((item for item in self.provider_options("photogimp") if item.provider_id == provider_id), None)
        if option is None:
            raise ValueError(f"El proveedor '{provider_id}' no es compatible con este equipo.")
        return option

    @staticmethod
    def _provider_command_available(provider_id: str) -> bool:
        commands = PROVIDER_COMMANDS.get(provider_id, ())
        if not commands:
            return provider_id == "appimage"
        return any(shutil.which(command) for command in commands)

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            ChangeStatus.PREPARED: "Preparado · falta integración manual",
            ChangeStatus.FAILED: "Falló",
            ChangeStatus.NEEDS_ATTENTION: "Necesita atención",
            ChangeStatus.PARTIALLY_REVERTED: "Revertido parcialmente",
            ChangeStatus.REVERTED: "Revertido",
            ChangeStatus.REVERTING: "Deshaciendo",
            ChangeStatus.INTEGRATING: "Integrando",
            ChangeStatus.INTEGRATED: "Integrado",
        }.get(status, "Estado desconocido")

    @staticmethod
    def _validate_change_id(change_id: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", change_id):
            raise ValueError(f"Identificador de cambio inválido: {change_id!r}.")

    def _require_change(self, change_id: str) -> None:
        self._validate_change_id(change_id)
        if change_id == "photogimp" or self._is_portable_change(change_id) or change_id in self._declarative_changes:
            return
        raise ValueError(f"El cambio '{change_id}' no está disponible en este Styler.")
