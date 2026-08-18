"""Conformidad con el instructivo oficial de PhotoGIMP.

El README del proyecto describe: instalar GIMP desde Flathub, abrirlo y
cerrarlo una vez, descargar **la última** publicación, extraerla sobre el HOME
sobrescribiendo lo existente y volver a abrir GIMP para ver el resultado.

Dos matices que estas pruebas fijan:

- La ruta que el README indica para Flatpak (``~/.config/GIMP/3.0``) no es la
  que GIMP en sandbox lee. Styler escribe en la carpeta real de la versión
  instalada bajo ``~/.var/app/org.gimp.GIMP/config/GIMP``.
- «La última publicación» se resuelve de verdad; la fuente del catálogo es solo
  el respaldo cuando la API no responde.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from styler.changes import ChangeService
from styler.component_catalog.photogimp_overlay import (
    PHOTOGIMP_RELEASE_PREFIX, _find_photogimp_payload, _photogimp_template_root,
    resolve_photogimp_source,
)

PINNED = "https://github.com/Diolinux/PhotoGIMP/releases/download/3.0/PhotoGIMP-linux.zip"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _api(payload: dict):
    def opener(request, timeout=None):
        return _Response(json.dumps(payload).encode("utf-8"))

    return opener


# ------------------------------------------------------ «descarga la última»
def test_latest_linux_asset_is_preferred_over_the_pinned_tag():
    source, evidence = resolve_photogimp_source(
        PINNED,
        opener=_api({
            "tag_name": "3.1",
            "assets": [
                {
                    "name": "PhotoGIMP-windows.zip",
                    "browser_download_url": (
                        "https://github.com/Diolinux/PhotoGIMP/releases/download/3.1/"
                        "PhotoGIMP-windows.zip"
                    ),
                },
                {
                    "name": "PhotoGIMP-linux.zip",
                    "browser_download_url": (
                        "https://github.com/Diolinux/PhotoGIMP/releases/download/3.1/"
                        "PhotoGIMP-linux.zip"
                    ),
                },
            ],
        }),
    )

    assert source.endswith("3.1/PhotoGIMP-linux.zip")
    assert evidence["resolution"] == "latest-release"
    assert evidence["release_tag"] == "3.1"


def test_unreachable_api_falls_back_to_the_catalog_source():
    def broken(request, timeout=None):
        raise OSError("sin red")

    source, evidence = resolve_photogimp_source(PINNED, opener=broken)

    assert source == PINNED
    assert evidence["resolution"] == "pinned"
    assert "resolution_error" in evidence


def test_asset_outside_the_official_repository_is_rejected():
    source, evidence = resolve_photogimp_source(
        PINNED,
        opener=_api({
            "tag_name": "9.9",
            "assets": [
                {
                    "name": "PhotoGIMP-linux.zip",
                    "browser_download_url": "https://example.invalid/PhotoGIMP-linux.zip",
                }
            ],
        }),
    )

    assert source == PINNED
    assert source.startswith(PHOTOGIMP_RELEASE_PREFIX)
    assert evidence["resolution"] == "pinned"


# ----------------------------------------------- «extrae el ZIP sobre el HOME»
def test_payload_with_icons_only_is_recognized(tmp_path: Path):
    (tmp_path / "PhotoGIMP" / ".icons").mkdir(parents=True)

    assert _find_photogimp_payload(tmp_path).name == "PhotoGIMP"


def test_legacy_flatpak_layout_is_still_a_valid_template_root(tmp_path: Path):
    legacy = tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/2.10"
    legacy.mkdir(parents=True)

    assert _photogimp_template_root(tmp_path).name == "GIMP"


def test_modern_layout_wins_over_legacy(tmp_path: Path):
    (tmp_path / ".config/GIMP/3.0").mkdir(parents=True)
    (tmp_path / ".var/app/org.gimp.GIMP/config/GIMP/2.10").mkdir(parents=True)

    assert _photogimp_template_root(tmp_path) == tmp_path / ".config" / "GIMP"


# ------------------------------------------------------------ «abre GIMP»
def test_plan_ends_by_opening_gimp_after_verification(tmp_path: Path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    plan = service.build_plan("photogimp", "flatpak")

    step_ids = [phase.step_id for phase in plan.phases]
    assert step_ids[-1] == "app.photogimp.launch"
    assert step_ids.index("app.photogimp.verify") < step_ids.index("app.photogimp.launch")

    launch = next(step for step in plan.workflow.steps if step.id == "app.photogimp.launch")
    assert launch.step_type == "initialize_flatpak_app"
    assert launch.required is True
    assert "app.photogimp.verify" in launch.needs


# --------------------------------------------- detección agnóstica de versión
def test_existing_photogimp_is_detected_on_a_future_gimp_version(tmp_path: Path):
    home = tmp_path / "home"
    version_dir = home / ".var/app/org.gimp.GIMP/config/GIMP/4.0"
    version_dir.mkdir(parents=True)
    (version_dir / ".photogimp-marker").write_text("source=test\n", encoding="utf-8")

    service = ChangeService(tmp_path / "library", home)
    detected = service._detect_photogimp()

    assert detected is not None
    provider, marker = detected
    assert provider == "flatpak"
    assert marker.parent.name == "4.0"
