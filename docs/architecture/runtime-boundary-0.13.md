# Styler 0.13.1 — frontera de runtime

Styler decide **qué significa** una operación. PipeCraft decide **cómo se ejecuta**.

## Ruta preferida (PipeCraft 1.6+)

`Workflow/ExecutionPlan -> compile_spec() -> IPC submit_spec -> PipeCraft`

`compile_spec()` es puro: no escribe YAML ni crea temporales. Cuando el runtime
anuncia `validate_spec` y `plan_spec`, Styler puede pedir validación/planificación
al runtime sin reserializar el pipeline a disco.

## Compatibilidad empaquetada

El ZIP 0.13.1 conserva temporalmente el binario verificado PipeCraft
`1.5.0-alpha.1` porque esta fuente de Styler no contiene el workspace Rust de
PipeCraft y no existe una copia accesible del repositorio conectada. Esa ruta usa
`styler/pipecraft/legacy_yaml.py`, aislada deliberadamente del compilador puro.
No se cambia ni se rebautiza el ELF: su versión y checksum reales siguen siendo
los declarados en `packaging/pipecraft.lock`.

## Procesos

- `styler/pipecraft` contiene contrato, cliente, servicio y traducción de spec.
- El host semántico de plugins vive en `styler/execution/plugin_host.py`.
- `ProcessRunner` conserva la frontera de procesos Python que todavía necesitan
  los plugins semánticos; la política DPKG ya no vive dentro del runner.
- Nodos declarados explícitamente como `command` se compilan a `type: command`
  para que PipeCraft los ejecute directamente.

## Trabajo que exige PipeCraft source

`submit_spec`, `validate_spec` y `plan_spec` deben implementarse también en el
servidor Rust. Styler ya negocia esas capacidades, pero no pretende que el
binario 1.5 incluido las tenga.

## Estado de esta entrega

Completado en Styler 0.13.1:

- compilador puro `compile_spec()`;
- negociación de capabilities y cliente `submit_spec` / `validate_spec` / `plan_spec`;
- compatibilidad PipeCraft 1.5 aislada en `legacy_yaml.py`;
- soporte de nodos `command` nativos;
- política DPKG fuera de `ProcessRunner` y `apt_reconcile` semántico;
- interfaz mínima `CommandExecutor`;
- host de plugins fuera de `styler.pipecraft`;
- `ChangeService` separado por descubrimiento, planificación, ejecución y retiro;
- restauración unificada como paquete y `advanced_restore.py` reducido a compatibilidad;
- TUI separada por pantallas;
- lock de PipeCraft, benchmark de hashing y guardas de frontera.

Pendiente por dependencia externa, no simulado en esta release:

- PipeCraft `1.5.0-alpha.2` y `1.6.0-alpha.1` no pueden compilarse aquí porque el
  workspace Rust fuente no forma parte del ZIP ni de los repositorios/archivos
  accesibles y este entorno tampoco dispone de Cargo;
- mientras el runtime incluido siga siendo 1.5, Styler conserva su selección y
  validación DAG necesarias para la ruta de compatibilidad;
- la migración masiva de ejecutores semánticos a `command` y el adelgazamiento
  final de `ProcessRunner` deben hacerse después de tener `submit_spec` real;
- el benchmark Rust quedó preparado pero no se toma todavía la decisión de
  conservar/eliminar PyO3 porque `styler_rust` no está compilado en este entorno.
