"""
styler.provenance.detectors.snap
================================
Snap declara revisión, canal y publisher. La revisión es el identificador
exacto del artefacto, igual que el commit en Flatpak.

Evidencia usada: ``snap list --color=never --unicode=never``.
"""
from __future__ import annotations

from styler.provenance.detectors.base import Detector, Runner
from styler.provenance.models import (
    ApplicationRecord,
    Confidence,
    Integrity,
    Origin,
    OriginKind,
    Upstream,
    InstallReason,
)

SNAP_STORE_URL = "https://snapcraft.io"


class SnapDetector(Detector):
    name = "snap"
    manager = "snap"

    def __init__(self, runner: Runner | None = None) -> None:
        super().__init__(runner)

    def applies(self) -> bool:
        return self.runner.available("snap")

    def _detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        out = self.runner.run(["snap", "list", "--color=never", "--unicode=never"])
        records: list[ApplicationRecord] = []

        for index, line in enumerate(out.splitlines()):
            if not line.strip():
                continue
            if index == 0 and line.split()[:1] == ["Name"]:
                continue
            fields = line.split()
            if len(fields) < 3:
                continue
            name = fields[0]
            version = fields[1]
            revision = fields[2]
            tracking = fields[3] if len(fields) > 3 else ""
            publisher = fields[4] if len(fields) > 4 else ""
            notes = fields[5] if len(fields) > 5 else ""

            warnings: list[str] = []
            if "classic" in notes:
                warnings.append(
                    "Este snap corre en modo clásico: no está aislado del sistema."
                )
            verified = publisher.endswith("**")
            channel = tracking.split("/")[-1] if tracking else ""

            origin = Origin(
                kind=OriginKind.SNAP,
                remote_name="snap store",
                remote_url=f"{SNAP_STORE_URL}/{name}",
                branch=tracking,
                channel=channel,
                commit=revision,
                vendor=publisher.rstrip("*"),
                signed=True,  # el store solo entrega snaps con assertions firmadas
                confidence=Confidence.CONFIRMED if revision else Confidence.INFERRED,
                evidence="snap list",
            )

            records.append(
                ApplicationRecord(
                    app_id=f"snap:{name}",
                    name=name,
                    display_name=name,
                    manager="snap",
                    version=version,
                    architecture="",
                    install_method="snap",
                    install_reason=InstallReason.EXPLICIT,
                    origin=origin,
                    upstream=Upstream(
                        confidence=Confidence.UNKNOWN,
                        evidence="snap (el store no declara el repositorio upstream)",
                    ),
                    integrity=Integrity(signature_verified=verified or None),
                    warnings=warnings,
                )
            )
        return records
