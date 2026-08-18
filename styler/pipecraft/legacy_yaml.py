"""Adaptador temporal para PipeCraft 1.5.

PipeCraft 1.6 recibe specs directamente por IPC. Este módulo existe únicamente
para que los paquetes 0.13 que aún incluyen el runtime 1.5 puedan funcionar sin
convertir el YAML transitorio en la ruta principal ni en una segunda fuente de
verdad.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def write_legacy_pipeline(spec: dict[str, Any], pipeline_dir: Path) -> Path:
    name = str(spec.get("name") or "styler")
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    pipeline_path = pipeline_dir / f"{name}.yaml"
    tmp = pipeline_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(pipeline_path)
    return pipeline_path
