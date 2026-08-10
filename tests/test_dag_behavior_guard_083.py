"""0.8.3 mejora autorización/diagnóstico sin cambiar el DAG que se ejecuta."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from styler.changes import ChangeService
from styler.portable.workflow import workflow_to_portable_dict
from styler.target import Target


EXPECTED_PHOTOGIMP_DAG_SHA256 = "95c94e46bf521935921e4354df04d796d1f7af657eb0be06c511d570e0a3e8b4"


def test_photogimp_dag_is_byte_equivalent_to_082(tmp_path):
    service = ChangeService(tmp_path / "library", tmp_path / "home")
    service._target = Target(family="ubuntu", distro_id="ubuntu", root=str(tmp_path))
    plan = service.build_plan("photogimp", "flatpak")
    canonical = json.dumps(
        workflow_to_portable_dict(plan.workflow),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == EXPECTED_PHOTOGIMP_DAG_SHA256


def test_change_progress_execution_block_remains_unchanged():
    """La pantalla que entrega el plan a ChangeService sigue siendo la de 0.8.2."""
    source = Path("styler/tui/app.py").read_text(encoding="utf-8")
    start = source.index("class ChangeProgressScreen(Screen):")
    end = source.index("class ChangeResultScreen(Screen):")
    block = source[start:end]
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    assert digest == "981950ba11de315b9bf5d23922271a71ba957d543c9de543fd121d10900c94a8"
