"""Compatibilidad para el antiguo import ``styler.pipecraft.plugin_host``.

El host ejecuta semántica de Styler y por ello pertenece a ``styler.execution``;
la capa ``styler.pipecraft`` queda limitada a contrato, transporte y compilación.
"""
from styler.execution.plugin_host import PREFIX, PIPECRAFT_STATUSES, _runtime_status, main

__all__ = ["PREFIX", "PIPECRAFT_STATUSES", "_runtime_status", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
