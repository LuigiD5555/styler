"""
styler.provenance.detectors.brew
================================
Homebrew / Linuxbrew.

Evidencia usada:

* ``brew leaves --installed-on-request`` — fórmulas que la persona pidió. Es el
  filtro de alto nivel propio de este gestor: ``brew list`` incluiría también
  cada dependencia arrastrada.
* ``brew info --json=v2 --installed`` — versión, tap y homepage declarada.

El tap es el remote real: ``homebrew/core`` es reproducible; un tap de terceros
también, pero solo si sigue existiendo.
"""
from __future__ import annotations

import json

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    AppCategory,
    ApplicationRecord,
    Confidence,
    InstallReason,
    Origin,
    OriginKind,
)
from styler.provenance.upstream import upstream_from_metadata


class BrewDetector(Detector):
    name = "brew"
    manager = "brew"

    def applies(self) -> bool:
        return self.runner.available("brew")

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        requested = self._requested()
        details = self._details()

        records: list[ApplicationRecord] = []
        for name in sorted(requested):
            info = details.get(name, {})
            tap = info.get("tap", "")
            version = info.get("version", "")

            if tap:
                origin = Origin(
                    kind=OriginKind.BREW,
                    remote_name=tap,
                    confidence=Confidence.CONFIRMED,
                    evidence="brew info --json=v2 --installed",
                )
            else:
                origin = Origin(
                    kind=OriginKind.BREW,
                    confidence=Confidence.UNKNOWN,
                    evidence="brew leaves --installed-on-request",
                )

            records.append(
                ApplicationRecord(
                    app_id=f"brew:{name}",
                    name=name,
                    display_name=name,
                    manager="brew",
                    version=version,
                    install_method="repository" if tap else "unknown",
                    install_reason=InstallReason.EXPLICIT,
                    category=AppCategory.DEV_TOOL,
                    origin=origin,
                    upstream=upstream_from_metadata(
                        homepage=info.get("homepage", ""), evidence="brew homepage"
                    ),
                )
            )
        return records

    def _requested(self) -> set[str]:
        try:
            out = self.runner.run(["brew", "leaves", "--installed-on-request"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"brew: no se pudo listar leaves ({exc})")
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _details(self) -> dict[str, dict[str, str]]:
        try:
            out = self.runner.run(["brew", "info", "--json=v2", "--installed"])
        except Exception as exc:  # noqa: BLE001
            self.problems.append(f"brew: no se pudo leer info ({exc})")
            return {}
        return parse_brew_info(out)


def parse_brew_info(text: str) -> dict[str, dict[str, str]]:
    """Extrae nombre → {version, tap, homepage} del JSON v2 de Homebrew."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}

    details: dict[str, dict[str, str]] = {}
    for formula in data.get("formulae", []) or []:
        if not isinstance(formula, dict):
            continue
        name = str(formula.get("name", ""))
        if not name:
            continue
        installed = formula.get("installed", []) or []
        version = ""
        if installed and isinstance(installed[0], dict):
            version = str(installed[0].get("version", ""))
        details[name] = {
            "version": version,
            "tap": str(formula.get("tap", "")),
            "homepage": str(formula.get("homepage", "")),
        }
    return details
