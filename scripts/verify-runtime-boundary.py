#!/usr/bin/env python3
"""Comprueba las fronteras de runtime de Styler 0.13.

Styler conserva dominio y semántica; PipeCraft es la autoridad de planificación
y ejecución para la ruta productiva. La compatibilidad 1.5 queda aislada y no
debe volver a contaminar el compilador puro ni el transporte IPC.
"""
from __future__ import annotations

from pathlib import Path
from collections import defaultdict
import ast
import sys

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []

# PipeCraft es un proyecto independiente; el release puede incluir un binario,
# pero el repositorio Styler no debe volver a copiar su source.
for path in (ROOT / "vendor", ROOT / "third_party" / "pipecraft"):
    if path.exists():
        errors.append(f"source externo copiado dentro de Styler: {path.relative_to(ROOT)}")

manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
if "vendor/pipecraft" in manifest or "recursive-include pipecraft" in manifest:
    errors.append("MANIFEST.in vuelve a empaquetar source de PipeCraft")

# Estas implementaciones fueron sustituidas y no deben reaparecer en producto.
for relative in (
    "styler/runtime",
    "styler/engine_client.py",
    "styler/engine_cli.py",
    "styler/orchestrator.py",
    "styler/pipelines.py",
    "styler/environment_restore.py",
    "styler/review.py",
    "styler/diff.py",
    "styler/interpreter.py",
    "styler/demo.py",
    "styler/restoration_plan.py",
    "styler/session_profile.py",
    "styler/component_catalog/bridge.py",
    "styler/component_catalog/restore_bridge.py",
    "rust/styler-engine",
):
    if (ROOT / relative).exists():
        errors.append(f"implementación histórica reapareció: {relative}")

# La capa PipeCraft sólo puede contener contrato/transporte/compilación.
# Comprobamos dependencias concretas por AST, no palabras sueltas.
for path in (ROOT / "styler" / "pipecraft").glob("*.py"):
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        errors.append(f"Python inválido en {path.relative_to(ROOT)}: {exc}")
        continue
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    for forbidden in ("subprocess", "styler.execution.processes"):
        if forbidden in imports:
            errors.append(f"{path.relative_to(ROOT)} importa frontera de procesos prohibida: {forbidden}")
    if path.name != "legacy_yaml.py" and "yaml" in imports:
        errors.append(f"{path.relative_to(ROOT)} serializa YAML fuera del adaptador legado")
    if "topological_order" in text:
        errors.append(f"{path.relative_to(ROOT)} vuelve a implementar/consumir orden topológico")
    for forbidden in (
        "styler.planning.scheduler",
        "styler.planning.events",
        "ThreadPoolExecutor",
        "STYLER_RUNTIME",
        "PipeCraftRunner",
    ):
        if forbidden in text:
            errors.append(f"{path.relative_to(ROOT)} contiene dependencia prohibida: {forbidden}")

# El compilador 0.13 debe ser puro: produce una spec en memoria y no escribe archivos.
compiler_path = ROOT / "styler" / "pipecraft" / "compiler.py"
compiler_tree = ast.parse(compiler_path.read_text(encoding="utf-8"))
for node in ast.walk(compiler_tree):
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        if name in {"open", "write_text", "write_bytes", "mkstemp", "NamedTemporaryFile"}:
            errors.append(f"{compiler_path.relative_to(ROOT)} realiza I/O transitorio mediante {name}()")

# El host de plugins es semántica de Styler, no transporte PipeCraft.
plugin_host = ROOT / "styler" / "execution" / "plugin_host.py"
if not plugin_host.is_file():
    errors.append("falta styler/execution/plugin_host.py")

# ProcessRunner no debe volver a incorporar políticas de recuperación DPKG.
process_path = ROOT / "styler" / "execution" / "processes.py"
process_tree = ast.parse(process_path.read_text(encoding="utf-8"))
process_methods = {node.name for node in ast.walk(process_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
for obsolete in {"_repair_dpkg_after_cancel", "_is_dpkg_command"}:
    if obsolete in process_methods:
        errors.append(f"{process_path.relative_to(ROOT)} recuperó política de dominio {obsolete}")

# app.py debe seguir siendo router y no volver a absorber las pantallas.
tui_app = ROOT / "styler" / "tui" / "app.py"
if len(tui_app.read_text(encoding="utf-8").splitlines()) > 800:
    errors.append("styler/tui/app.py volvió a crecer por encima de 800 líneas")

# ChangeService es la API del subsistema, no el contenedor de todos los algoritmos.
change_service = ROOT / "styler" / "changes" / "service.py"
if len(change_service.read_text(encoding="utf-8").splitlines()) > 800:
    errors.append("styler/changes/service.py volvió a crecer por encima de 800 líneas")
for required in ("discovery.py", "planner.py", "execution.py", "removal.py"):
    if not (ROOT / "styler" / "changes" / required).is_file():
        errors.append(f"falta módulo de cambios separado: styler/changes/{required}")

# Restore es un subsistema único; advanced_restore sólo puede ser compatibilidad.
restore_dir = ROOT / "styler" / "restore"
for required in ("models.py", "planner.py", "candidates.py", "sources.py", "policy.py", "executor.py", "verification.py"):
    if not (restore_dir / required).is_file():
        errors.append(f"falta módulo de restauración unificada: styler/restore/{required}")
advanced_shim = ROOT / "styler" / "advanced_restore.py"
if advanced_shim.is_file() and len(advanced_shim.read_text(encoding="utf-8").splitlines()) > 50:
    errors.append("styler/advanced_restore.py volvió a convertirse en un segundo motor")

# El paquete completo no debe conservar nombres que confundan el process runner
# de Styler con el runtime PipeCraft real.
for path in (ROOT / "styler").rglob("*.py"):
    text = path.read_text(encoding="utf-8")
    if "PipeCraftRunner" in text:
        errors.append(f"nombre PipeCraftRunner legado en {path.relative_to(ROOT)}")


# La suite tampoco puede volver a ocultar la ruta productiva con un override
# autouse del backend: los tests que necesitan el arnés local deben pedirlo.
conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
if "autouse=True" in conftest and "_execution_backend" in conftest:
    errors.append("tests/conftest.py vuelve a sustituir el backend productivo de forma autouse")

# El código productivo tampoco debe volver a depender del arnés local de tests.
for path in (ROOT / "styler").rglob("*.py"):
    text = path.read_text(encoding="utf-8")
    if "tests.support" in text:
        errors.append(f"código productivo depende de tests/support: {path.relative_to(ROOT)}")

# Las capas ya fueron separadas para evitar imports circulares. Un ciclo suele
# indicar que dos módulos volvieron a absorber responsabilidades mutuas.
modules: dict[str, Path] = {}
for path in (ROOT / "styler").rglob("*.py"):
    relative = path.relative_to(ROOT / "styler").with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    name = "styler" + ("." + ".".join(parts) if parts else "")
    modules[name] = path

edges: dict[str, set[str]] = defaultdict(set)
for module, path in modules.items():
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        errors.append(f"Python inválido en {path.relative_to(ROOT)}: {exc}")
        continue
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")[:-node.level]
                if node.module:
                    base += node.module.split(".")
                targets = [".".join(base)]
            else:
                targets = [node.module or ""]
        elif isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        for target in targets:
            if not target.startswith("styler"):
                continue
            candidate = target
            while candidate and candidate not in modules:
                candidate = ".".join(candidate.split(".")[:-1])
            if candidate and candidate != module:
                edges[module].add(candidate)

index = 0
indices: dict[str, int] = {}
lowlink: dict[str, int] = {}
stack: list[str] = []
on_stack: set[str] = set()

def visit(module: str) -> None:
    global index
    indices[module] = lowlink[module] = index
    index += 1
    stack.append(module)
    on_stack.add(module)
    for target in edges[module]:
        if target not in indices:
            visit(target)
            lowlink[module] = min(lowlink[module], lowlink[target])
        elif target in on_stack:
            lowlink[module] = min(lowlink[module], indices[target])
    if lowlink[module] != indices[module]:
        return
    component: list[str] = []
    while True:
        current = stack.pop()
        on_stack.remove(current)
        component.append(current)
        if current == module:
            break
    if len(component) > 1:
        errors.append("ciclo de imports productivo: " + " -> ".join(sorted(component)))

for module in modules:
    if module not in indices:
        visit(module)

# Las rutas productivas de ejecución deben entrar por styler.workflow.execute.
product_files = [
    ROOT / "styler" / "changes" / "execution.py",
    ROOT / "styler" / "snapshot.py",
]
for path in product_files:
    text = path.read_text(encoding="utf-8")
    if "workflow_runtime.execute(" not in text:
        errors.append(f"{path.relative_to(ROOT)} no usa la frontera canónica styler.workflow.execute")

if errors:
    print("Runtime boundary FAILED:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    raise SystemExit(1)

print("Runtime boundary OK: spec pura, PipeCraft aislado, ProcessRunner sin política DPKG y TUI separada.")
