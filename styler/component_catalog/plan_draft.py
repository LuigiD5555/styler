"""Borrador editable de un plan de componentes.

El catálogo y el resolver siguen siendo la fuente de verdad. Este módulo
conserva únicamente decisiones humanas (incluir, excluir y proveedor) y vuelve
a ejecutar el resolver después de cada cambio. El DAG compilado nunca se
edita ni se persiste como autoridad.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from styler.component_catalog.compiler import CompileResult, compile_workflow
from styler.component_catalog.models import ComponentDefinition, ProviderDefinition
from styler.component_catalog.registry import ComponentRegistry
from styler.component_catalog.resolver import ResolutionResult, resolve
from styler.validation import safe_record_path, validate_identifier

PLAN_DIR = Path(".styler") / "component-plans"
PLAN_SCHEMA_VERSION = 3
# Solo se anuncian como ejecutables los proveedores que el runtime puede aplicar hoy.
EXECUTABLE_PROVIDER_TYPES = frozenset({"apt", "flatpak", "archive"})


@dataclass
class PlanDraft:
    profile_id: str
    desired_components: list[str]
    excluded_components: set[str] = field(default_factory=set)
    preferred_providers: dict[str, str] = field(default_factory=dict)
    preferred_components: dict[str, str] = field(default_factory=dict)
    revision: int = 0
    confirmed_revision: int = -1
    schema_version: int = PLAN_SCHEMA_VERSION
    catalog_signature: str = ""
    confirmed_catalog_signature: str = ""

    @property
    def confirmed(self) -> bool:
        return (
            self.confirmed_revision == self.revision
            and bool(self.catalog_signature)
            and self.confirmed_catalog_signature == self.catalog_signature
        )

    @property
    def effective_components(self) -> tuple[str, ...]:
        return tuple(
            component_id
            for component_id in self.desired_components
            if component_id not in self.excluded_components
        )

    def touch(self) -> None:
        self.revision += 1
        self.confirmed_revision = -1

    def confirm(self) -> None:
        self.confirmed_revision = self.revision
        self.confirmed_catalog_signature = self.catalog_signature

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "desired_components": list(self.desired_components),
            "excluded_components": sorted(self.excluded_components),
            "preferred_providers": dict(sorted(self.preferred_providers.items())),
            "preferred_components": dict(sorted(self.preferred_components.items())),
            "revision": self.revision,
            "confirmed_revision": self.confirmed_revision,
            "catalog_signature": self.catalog_signature,
            "confirmed_catalog_signature": self.confirmed_catalog_signature,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanDraft":
        version = int(data.get("schema_version", 1))
        if version not in (1, 2, PLAN_SCHEMA_VERSION):
            raise ValueError(f"Versión de plan de componentes no soportada: {version}")
        profile_id = validate_identifier(str(data.get("profile_id", "")), "ID de perfil")
        desired = [str(item) for item in data.get("desired_components", [])]
        return cls(
            profile_id=profile_id,
            desired_components=list(dict.fromkeys(desired)),
            excluded_components={str(item) for item in data.get("excluded_components", [])},
            preferred_providers={
                str(key): str(value)
                for key, value in dict(data.get("preferred_providers", {})).items()
            },
            preferred_components={
                str(key): str(value)
                for key, value in dict(data.get("preferred_components", {})).items()
            },
            revision=int(data.get("revision", 0)),
            confirmed_revision=int(data.get("confirmed_revision", -1)),
            schema_version=PLAN_SCHEMA_VERSION,
            catalog_signature=str(data.get("catalog_signature", "")),
            confirmed_catalog_signature=str(data.get("confirmed_catalog_signature", "")),
        )


@dataclass(frozen=True)
class ExclusionPreview:
    component_id: str
    affected_components: tuple[str, ...]
    dependent_components: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    can_exclude_only: bool


@dataclass(frozen=True)
class ProviderOption:
    provider_id: str
    provider_type: str
    label: str
    compatible: bool
    executable: bool
    selected: bool
    reason: str = ""

    @property
    def selectable(self) -> bool:
        return self.compatible and self.executable


@dataclass(frozen=True)
class ReplacementOption:
    component_id: str
    label: str
    description: str
    selected: bool
    selectable: bool = True


@dataclass(frozen=True)
class ReplacementDecision:
    consumer_id: str
    requirement: str
    chosen_component: str
    candidates: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.consumer_id}|{self.requirement}"


@dataclass
class DraftResolution:
    draft: PlanDraft
    resolution: ResolutionResult
    compiled: CompileResult
    excluded_but_required: tuple[str, ...] = ()
    invalid_preferences: tuple[str, ...] = ()
    catalog_changed: bool = False
    changes: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[str, ...]:
        messages: list[str] = []
        if self.excluded_but_required:
            messages.append(
                "No se pueden excluir por separado porque todavía son necesarios: "
                + ", ".join(self.excluded_but_required)
            )
        if self.catalog_changed:
            messages.append("El catálogo cambió desde la última revisión; confirma nuevamente el plan.")
        if self.invalid_preferences:
            messages.append(
                "Hay proveedores elegidos que ya no son compatibles: "
                + ", ".join(self.invalid_preferences)
            )
        messages.extend(issue.message for issue in self.compiled.errors)
        if self.resolution.missing_capabilities:
            messages.append(
                "Faltan capacidades: " + ", ".join(self.resolution.missing_capabilities)
            )
        return tuple(dict.fromkeys(messages))

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def can_execute(self) -> bool:
        return self.ok and self.draft.confirmed


class PlanDraftStore:
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root)

    @property
    def directory(self) -> Path:
        path = self.root / PLAN_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, profile_id: str) -> Path:
        validate_identifier(profile_id, "ID de perfil")
        return safe_record_path(self.directory, profile_id)

    def load(self, profile_id: str) -> PlanDraft | None:
        path = self.path_for(profile_id)
        if not path.is_file():
            return None
        try:
            return PlanDraft.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"El plan de componentes de {profile_id} está dañado.") from exc

    def save(self, draft: PlanDraft) -> Path:
        path = self.path_for(draft.profile_id)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(draft.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def delete(self, profile_id: str) -> None:
        path = self.path_for(profile_id)
        if path.exists():
            path.unlink()


class ComponentPlanEditor:
    """Aplica decisiones al borrador y recalcula resolución + DAG."""

    def __init__(
        self,
        registry: ComponentRegistry,
        family: str,
        *,
        executable_provider_types: frozenset[str] = EXECUTABLE_PROVIDER_TYPES,
    ) -> None:
        self.registry = registry
        self.family = family or "unknown"
        self.executable_provider_types = executable_provider_types
        self.catalog_signature = self._catalog_signature()

    def create(self, profile_id: str, desired_components: Iterable[str]) -> PlanDraft:
        desired = [
            component_id
            for component_id in dict.fromkeys(desired_components)
            if self.registry.contains(component_id)
        ]
        return PlanDraft(profile_id=profile_id, desired_components=desired, catalog_signature=self.catalog_signature)

    def evaluate(self, draft: PlanDraft) -> DraftResolution:
        previous_signature = draft.catalog_signature
        catalog_changed = bool(previous_signature and previous_signature != self.catalog_signature)
        draft.catalog_signature = self.catalog_signature
        invalid_preferences: list[str] = []
        for component_id, provider_id in draft.preferred_providers.items():
            component = self.registry.get(component_id)
            provider = self._provider(component, provider_id) if component else None
            if provider is None or not self._provider_compatible(provider):
                invalid_preferences.append(f"{component_id}:{provider_id}")

        resolution = resolve(
            self.registry,
            draft.effective_components,
            family=self.family,
            preferred_providers=draft.preferred_providers,
            preferred_components=self._decoded_component_preferences(draft),
            allowed_provider_types=self.executable_provider_types,
        )
        excluded_but_required = tuple(
            sorted(set(resolution.selected_components) & draft.excluded_components)
        )
        compiled = compile_workflow(self.registry, resolution, name=f"profile:{draft.profile_id}")
        return DraftResolution(
            draft=draft,
            resolution=resolution,
            compiled=compiled,
            excluded_but_required=excluded_but_required,
            invalid_preferences=tuple(sorted(invalid_preferences)),
            catalog_changed=catalog_changed,
            changes=self.change_summary(draft, resolution),
        )


    def change_summary(self, draft: PlanDraft, resolution: ResolutionResult | None = None) -> tuple[str, ...]:
        resolution = resolution or self.evaluate(draft).resolution
        changes: list[str] = []
        for component_id in sorted(draft.excluded_components):
            component = self.registry.get(component_id)
            changes.append(f"Excluir {component.name if component else component_id}")
        for component_id, provider_id in sorted(draft.preferred_providers.items()):
            component = self.registry.get(component_id)
            if component is None:
                continue
            default = resolve(
                self.registry, [component_id], family=self.family,
                allowed_provider_types=self.executable_provider_types,
            ).selected_providers.get(component_id, "")
            if provider_id and provider_id != default:
                provider = self._provider(component, provider_id)
                changes.append(f"Usar {self._provider_label(provider) if provider else provider_id} para {component.name}")
        for key, component_id in sorted(draft.preferred_components.items()):
            if "|" not in key:
                continue
            consumer_id, requirement = key.split("|", 1)
            consumer = self.registry.get(consumer_id)
            chosen = self.registry.get(component_id)
            changes.append(
                f"Usar {chosen.name if chosen else component_id} para {consumer.name if consumer else consumer_id} ({requirement})"
            )
        return tuple(dict.fromkeys(changes))

    def _catalog_signature(self) -> str:
        rows: list[str] = []
        for component in sorted(self.registry.all(), key=lambda item: item.id):
            rows.append(component.id + ":" + ",".join(sorted(component.provides)))
            for provider in sorted(component.providers, key=lambda item: item.id):
                rows.append(
                    f"{component.id}|{provider.id}|{provider.type}|{','.join(sorted(provider.families))}|{','.join(provider.packages)}|{provider.application_id}"
                )
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def preview_exclusion(self, draft: PlanDraft, component_id: str) -> ExclusionPreview:
        if not self.registry.contains(component_id):
            raise KeyError(component_id)
        dependents = self._transitive_dependents(component_id)
        trial = PlanDraft.from_dict(draft.to_dict())
        trial.excluded_components.add(component_id)
        evaluated = self.evaluate(trial)
        affected = tuple(
            item for item in (component_id, *dependents)
            if item in draft.desired_components or item in evaluated.resolution.selected_components
        )
        return ExclusionPreview(
            component_id=component_id,
            affected_components=tuple(dict.fromkeys(affected)),
            dependent_components=dependents,
            missing_capabilities=evaluated.resolution.missing_capabilities,
            can_exclude_only=component_id not in evaluated.excluded_but_required,
        )

    def exclude(self, draft: PlanDraft, component_id: str, *, cascade: bool = False) -> DraftResolution:
        targets = {component_id}
        if cascade:
            targets.update(self._transitive_dependents(component_id))
        before = set(draft.excluded_components)
        draft.excluded_components.update(targets)
        if draft.excluded_components != before:
            draft.touch()
        return self.evaluate(draft)

    def include(self, draft: PlanDraft, component_id: str) -> DraftResolution:
        changed = component_id in draft.excluded_components
        draft.excluded_components.discard(component_id)
        if component_id not in draft.desired_components and self.registry.contains(component_id):
            draft.desired_components.append(component_id)
            changed = True
        if changed:
            draft.touch()
        return self.evaluate(draft)

    def provider_options(self, draft: PlanDraft, component_id: str) -> tuple[ProviderOption, ...]:
        component = self.registry.get(component_id)
        if component is None:
            raise KeyError(component_id)
        evaluated = self.evaluate(draft)
        selected_id = draft.preferred_providers.get(
            component_id,
            evaluated.resolution.selected_providers.get(component_id, ""),
        )
        options: list[ProviderOption] = []
        for provider in sorted(component.providers, key=lambda item: (-item.priority, item.id)):
            compatible = self._provider_compatible(provider, check_executable=False)
            executable = provider.type in self.executable_provider_types
            if not compatible:
                reason = f"No es compatible con {self.family}."
            elif not executable:
                reason = "Styler puede describirlo, pero todavía no ejecutarlo."
            else:
                reason = "Disponible para este equipo."
            options.append(
                ProviderOption(
                    provider_id=provider.id,
                    provider_type=provider.type,
                    label=self._provider_label(provider),
                    compatible=compatible,
                    executable=executable,
                    selected=provider.id == selected_id,
                    reason=reason,
                )
            )
        return tuple(options)

    def select_provider(
        self,
        draft: PlanDraft,
        component_id: str,
        provider_id: str,
    ) -> DraftResolution:
        component = self.registry.get(component_id)
        provider = self._provider(component, provider_id) if component else None
        if provider is None:
            raise ValueError(f"'{provider_id}' no es un proveedor de '{component_id}'.")
        if not self._provider_compatible(provider, check_executable=False):
            raise ValueError(f"'{provider_id}' no es compatible con {self.family}.")
        if provider.type not in self.executable_provider_types:
            raise ValueError(
                f"Styler todavía no puede ejecutar proveedores de tipo '{provider.type}'."
            )
        if draft.preferred_providers.get(component_id) != provider_id:
            draft.preferred_providers[component_id] = provider_id
            draft.touch()
        return self.evaluate(draft)

    def replacement_decisions(self, draft: PlanDraft) -> tuple[ReplacementDecision, ...]:
        evaluated = self.evaluate(draft)
        return tuple(
            ReplacementDecision(
                consumer_id=decision.component_id,
                requirement=decision.requirement,
                chosen_component=decision.chosen_component,
                candidates=decision.candidates,
            )
            for decision in evaluated.resolution.decisions
            if decision.requirement and len(decision.candidates) > 1
        )

    def replacement_options(
        self, draft: PlanDraft, consumer_id: str, requirement: str
    ) -> tuple[ReplacementOption, ...]:
        candidates = self.registry.providers_for(requirement)
        if not candidates:
            return ()
        evaluated = self.evaluate(draft)
        chosen = next((
            decision.chosen_component
            for decision in evaluated.resolution.decisions
            if decision.component_id == consumer_id and decision.requirement == requirement
        ), "")
        return tuple(
            ReplacementOption(
                component_id=item.id,
                label=item.name,
                description=(item.description or f"Proporciona {requirement}"),
                selected=item.id == chosen,
            )
            for item in candidates
        )

    def select_replacement(
        self, draft: PlanDraft, consumer_id: str, requirement: str, component_id: str
    ) -> DraftResolution:
        consumer = self.registry.get(consumer_id)
        if consumer is None or requirement not in (*consumer.requires, *consumer.optional_requires):
            raise ValueError(f"'{consumer_id}' no requiere '{requirement}'.")
        candidates = {item.id for item in self.registry.providers_for(requirement)}
        if component_id not in candidates:
            raise ValueError(f"'{component_id}' no proporciona '{requirement}'.")
        key = self._preference_key(consumer_id, requirement)
        if draft.preferred_components.get(key) != component_id:
            draft.preferred_components[key] = component_id
            draft.touch()
        return self.evaluate(draft)

    @staticmethod
    def _preference_key(consumer_id: str, requirement: str) -> str:
        return f"{consumer_id}|{requirement}"

    def _decoded_component_preferences(self, draft: PlanDraft) -> dict[tuple[str, str], str]:
        result: dict[tuple[str, str], str] = {}
        for key, value in draft.preferred_components.items():
            if "|" not in key:
                continue
            consumer_id, requirement = key.split("|", 1)
            result[(consumer_id, requirement)] = value
        return result

    def confirm(self, draft: PlanDraft) -> DraftResolution:
        evaluated = self.evaluate(draft)
        if not evaluated.ok:
            raise ValueError("El plan no puede confirmarse mientras tenga bloqueos.")
        draft.confirm()
        return self.evaluate(draft)

    def reset(self, draft: PlanDraft) -> DraftResolution:
        changed = bool(draft.excluded_components or draft.preferred_providers or draft.preferred_components or draft.confirmed)
        draft.excluded_components.clear()
        draft.preferred_providers.clear()
        draft.preferred_components.clear()
        if changed:
            draft.touch()
        return self.evaluate(draft)

    def _transitive_dependents(self, component_id: str) -> tuple[str, ...]:
        result: list[str] = []
        pending = [component_id]
        seen = {component_id}
        while pending:
            current = pending.pop(0)
            for dependent in self.registry.dependents_of(current):
                if dependent.id in seen:
                    continue
                seen.add(dependent.id)
                result.append(dependent.id)
                pending.append(dependent.id)
        return tuple(result)

    def _provider_compatible(
        self,
        provider: ProviderDefinition,
        *,
        check_executable: bool = True,
    ) -> bool:
        family_ok = (
            not provider.families
            or self.family in provider.families
            or "*" in provider.families
        )
        if not family_ok:
            return False
        return not check_executable or provider.type in self.executable_provider_types

    @staticmethod
    def _provider(component: ComponentDefinition | None, provider_id: str) -> ProviderDefinition | None:
        if component is None:
            return None
        return next((item for item in component.providers if item.id == provider_id), None)

    @staticmethod
    def _provider_label(provider: ProviderDefinition) -> str:
        identity = provider.application_id or (provider.packages[0] if provider.packages else "")
        return f"{provider.type.upper()}" + (f" · {identity}" if identity else "")
