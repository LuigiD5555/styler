"""Políticas de resolución para restauración.

La política decide qué candidato es aceptable; no crea otro motor de ejecución.
"""
from enum import Enum

class RestorePolicy(str, Enum):
    STRICT = "strict"
    COMPATIBLE = "compatible"
    ADVANCED = "advanced"
