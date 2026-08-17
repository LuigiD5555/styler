"""Aísla la suite del daemon externo.

El runtime Python histórico ya no es fallback productivo, pero sigue siendo un
arnés determinista para tests unitarios de semántica Styler mientras se migra la
última lógica de dominio. Las pruebas específicas de PipeCraft borran esta
variable cuando necesitan verificar el comportamiento fail-closed.
"""
import pytest


@pytest.fixture(autouse=True)
def _explicit_local_test_runtime(monkeypatch):
    monkeypatch.setenv("STYLER_RUNTIME", "local-test")
