"""
styler.provenance.detectors.aur
===============================
``PacmanDetector`` ya encuentra los paquetes foráneos con ``pacman -Qm`` y los
deja, correctamente, con confianza ``UNKNOWN``: pacman sabe que ningún
repositorio configurado los entrega, pero no sabe si vinieron de AUR o de una
compilación local.

Este módulo sube esa confianza a ``INFERRED`` consultando la API RPC de AUR.

**Rompe deliberadamente una garantía de la capa de procedencia**: usa la red.
Por eso vive aparte, no se ejecuta durante un escaneo normal y hay que pedirlo
de forma explícita. Sin red, o si la consulta falla, los registros se devuelven
intactos. Nunca se inventa un origen.
"""
from __future__ import annotations

import json
from typing import Callable, Iterable, Sequence
from urllib.parse import urlencode

from styler.provenance.models import (
    ApplicationRecord,
    Confidence,
    Origin,
    OriginKind,
)

AUR_RPC_URL = "https://aur.archlinux.org/rpc/v5/info"
AUR_PACKAGE_URL = "https://aur.archlinux.org/packages"
QUERY_CHUNK = 50
DEFAULT_TIMEOUT = 10

Fetcher = Callable[[str], str]


def _default_fetcher(url: str) -> str:
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "styler-provenance"})
    with urlopen(request, timeout=DEFAULT_TIMEOUT) as response:  # noqa: S310
        return response.read().decode("utf-8")


def foreign_records(records: Iterable[ApplicationRecord]) -> list[ApplicationRecord]:
    """Registros de pacman que ningún repositorio configurado entrega."""
    return [
        record
        for record in records
        if record.manager == "pacman" and record.origin.confidence == Confidence.UNKNOWN
    ]


def enrich_with_aur(
    records: Sequence[ApplicationRecord],
    fetcher: Fetcher | None = None,
) -> tuple[list[ApplicationRecord], list[str]]:
    """Marca como AUR los foráneos que existen en AUR.

    Devuelve ``(registros, problemas)``. Los registros se modifican en sitio
    solo cuando hay evidencia; el resto se deja como estaba.
    """
    fetch = fetcher or _default_fetcher
    problems: list[str] = []
    pending = foreign_records(records)
    if not pending:
        return list(records), problems

    by_name = {record.name: record for record in pending}
    known: dict[str, dict] = {}

    for chunk in _chunks(sorted(by_name), QUERY_CHUNK):
        query = urlencode([("arg[]", name) for name in chunk])
        try:
            payload = fetch(f"{AUR_RPC_URL}?{query}")
            data = json.loads(payload)
        except Exception as exc:  # noqa: BLE001 — sin red se queda como estaba
            problems.append(f"aur: no se pudo consultar la API ({exc})")
            return list(records), problems
        for item in data.get("results", []) or []:
            if isinstance(item, dict) and item.get("Name"):
                known[str(item["Name"])] = item

    for name, record in by_name.items():
        info = known.get(name)
        if info is None:
            continue
        maintainer = str(info.get("Maintainer") or "")
        packager = record.origin.vendor or ""
        confirmed = bool(maintainer) and maintainer.lower() in packager.lower()
        record.origin = Origin(
            kind=OriginKind.AUR,
            remote_name="aur",
            remote_url=f"{AUR_PACKAGE_URL}/{name}",
            vendor=record.origin.vendor,
            confidence=Confidence.CONFIRMED if confirmed else Confidence.INFERRED,
            evidence="pacman -Qm + AUR RPC info",
        )
        record.install_method = "aur"
        record.warnings = [
            warning
            for warning in record.warnings
            if "ningún repositorio configurado" not in warning
        ]
        if not confirmed:
            record.warnings.append(
                "Existe un paquete con este nombre en AUR, pero no se pudo "
                "confirmar que sea el mismo que está instalado."
            )
    return list(records), problems


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
