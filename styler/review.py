"""
styler.review
================
Bandeja de revisión mínima (interfaz de texto). Aplica las mismas
decisiones descritas en el resumen: include / pending / personal /
ignored. Una UI gráfica podría reemplazar esto sin tocar el resto del
pipeline, porque solo opera sobre Changeset/Component.
"""

from __future__ import annotations

from styler.models import Changeset, Decision

_KEYS = {
    "i": Decision.INCLUDE,
    "p": Decision.PENDING,
    "s": Decision.PERSONAL,   # "solo esta computadora"
    "x": Decision.IGNORED,
}


def auto_decide(changeset: Changeset, default: Decision = Decision.PENDING) -> None:
    """Marca todos los componentes con una decisión por defecto —
    útil para modo no interactivo (scripts, demos, CI)."""
    for comp in changeset.components:
        comp.decision = default


def interactive_review(changeset: Changeset) -> None:
    print(f"\nRevisión de {len(changeset.components)} componente(s) detectado(s).")
    print("Decisiones: [i]ncluir  [p]endiente  [s]olo-esta-máquina  [x]ignorar  (Enter = pendiente)\n")
    for comp in changeset.components:
        print(f"- {comp.title}  ({comp.category})")
        if comp.human_summary:
            print(f"  {comp.human_summary}")
        if comp.depends_on:
            print(f"  depende de: {', '.join(comp.depends_on)}")
        choice = input("  decisión [i/p/s/x]: ").strip().lower()
        comp.decision = _KEYS.get(choice, Decision.PENDING)
        print()


def summarize(changeset: Changeset) -> str:
    counts: dict[str, int] = {}
    for comp in changeset.components:
        counts[comp.decision.value] = counts.get(comp.decision.value, 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in counts.items())
