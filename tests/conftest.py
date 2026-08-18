"""Fixtures compartidas de la suite.

Producción sólo ejecuta workflows por PipeCraft. Las pruebas unitarias que
necesitan monkeypatches o runners falsos dentro del mismo proceso deben pedir de
forma explícita ``local_execution_backend``; ya no existe un override autouse
que haga que toda la suite pruebe una arquitectura distinta a producción.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def local_execution_backend(monkeypatch):
    """Usa el scheduler de pruebas, nunca distribuido con Styler."""
    from styler import workflow
    from tests.support.local_engine import execute_backend

    monkeypatch.setattr(workflow, "_execution_backend", execute_backend)
    yield
