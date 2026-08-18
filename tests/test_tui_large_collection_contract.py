from pathlib import Path


def test_constructor_limits_detected_changes_rendering() -> None:
    source = Path("styler/tui/screens/constructor.py").read_text(encoding="utf-8")
    assert "MAX_VISIBLE_CHANGES = 500" in source
    assert "pending[: self.MAX_VISIBLE_CHANGES]" in source
    assert "Refina el registro o usa el inventario técnico" in source


def test_constructor_limits_saved_packages_rendering() -> None:
    source = Path("styler/tui/screens/constructor.py").read_text(encoding="utf-8")
    assert "MAX_VISIBLE_PACKAGES = 200" in source
    assert "packages[: self.MAX_VISIBLE_PACKAGES]" in source
    assert "paquetes guardados" in source
