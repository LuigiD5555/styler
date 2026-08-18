"""Simulador de detectores de procedencia exclusivo de tests."""
from __future__ import annotations

from dataclasses import dataclass, field

from styler.provenance.detectors.base import CommandError


@dataclass
class FakeRunner:
    outputs: dict[tuple[str, ...], str] = field(default_factory=dict)
    programs: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)

    def available(self, program: str) -> bool:
        return program in self.programs

    def run(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        key = tuple(argv)
        if key in self.outputs:
            return self.outputs[key]
        raise CommandError(f"Comando no simulado: {' '.join(argv)}")
