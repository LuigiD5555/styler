# Styler 0.13.1

## Cambio 0.13.1 — corrección de runtime PipeCraft

0.13.1 corrige el empaquetado y la verificación del binario PipeCraft incluido, y consolida la frontera por spec.

### Frontera PipeCraft por spec

- El compilador `styler.pipecraft.compiler` ya no escribe YAML: produce una spec `pipecraft/v1` en memoria.
- El cliente IPC negocia capacidades y soporta `submit_spec`, `validate_spec` y `plan_spec` para PipeCraft 1.6+.
- El PipeCraft 1.5.0-alpha.1 incluido permanece sin rebautizar y se usa mediante un adaptador YAML aislado hasta disponer del source/release 1.6.
- Los steps declarados explícitamente como `command` bajan al executor Rust nativo; los steps semánticos siguen usando el host de plugins de Styler.
- La recuperación DPKG salió de `ProcessRunner` y vive en el dominio de gestores de paquetes; existe un step semántico `apt_reconcile`.
- La TUI fue separada por pantallas; `styler/tui/app.py` queda como router ligero.
- `ChangeService` conserva la API pública pero su implementación se dividió en descubrimiento, planificación, ejecución y retiro; `service.py` queda concentrado en estado/persistencia.
- Restauración pasó de `restore.py` + `advanced_restore.py` a un solo subsistema `styler/restore/` con modelos, fuentes, planner, executor, verificación, política y candidatos. La ruta histórica `styler.advanced_restore` es sólo compatibilidad.
- Se añadió `packaging/pipecraft.lock`, un benchmark de hashing reproducible y guardas de regresión para impedir que el compilador vuelva a escribir pipelines temporales o que la capa PipeCraft absorba supervisión de procesos Python.

## Simplificación arquitectónica incluida en 0.13.1

Styler 0.13.1 elimina capas que habían quedado vivas durante la migración a PipeCraft. La ruta productiva tiene una sola autoridad de ejecución: `styler.workflow` planifica y delega; PipeCraft 1.5 ejecuta el DAG por IPC.

- Se eliminó por completo el paquete Python `styler.runtime`. Los contratos de DAG y planificación viven ahora en `styler.planning`; el único runtime productivo es PipeCraft. El arnés local equivalente existe únicamente bajo `tests/support`.
- Se eliminó el segundo motor `rust/styler-engine` y sus bridges `engine_client.py`/`engine_cli.py`; hashing conserva sólo la extensión PyO3 opcional y fallback Python.
- Se eliminaron las fachadas `orchestrator.py` y `pipelines.py`; restauración y stages convergen en `styler.restore`.
- Se eliminaron adaptadores sin ruta productiva (`environment_restore.py`, `component_catalog.bridge` y `restore_bridge`); no se conservan wrappers sólo para sostener tests históricos.
- Se eliminaron prototipos históricos sin consumidores (`diff.py`, `interpreter.py`, `review.py`, `demo.py`, `restoration_plan.py`, `session_profile.py`); el modelo productivo actual entra por componentes/catálogo y planificación explícita.
- `styler/planning/` conserva sólo contratos, grafo, selección, validación y compilación de plan. La ejecución concreta vive en `styler/execution/`; el scheduler no.
- `PipeCraftRunner` pasó a llamarse `ProcessRunner` para no confundir una utilidad de subprocess de los plugins con el runtime PipeCraft real.
- La persistencia de Cambios se aisló en `styler.changes.storage`; el archivo gigante de ejecutores se separó en `gimp_runtime.py`, `photogimp_overlay.py` y ejecutores generales.
- Los contratos de `StepExecutor`/`ExecutorRegistry` y la composición del registro se separaron, eliminando ciclos entre automation/undo/execution. También se rompieron los ciclos applications/resolvers, portable y provenance.
- El YAML transitorio usado para `submit` se elimina en cuanto PipeCraft acepta el job; el snapshot durable de PipeCraft queda como fuente de verdad del plan ejecutado.
- `scripts/verify-runtime-boundary.py` impide que reaparezcan motores históricos, dependencias productivas de `tests/support`, source vendorizado de PipeCraft o ciclos de imports en `styler/`.

PipeCraft sigue sin conocer APT, Flatpak, PhotoGIMP, receipts ni `.stylerpkg`; esas decisiones permanecen en el dominio Styler.

## Cambio

Styler valida que su registro de estado sea escribible antes de ejecutar un DAG y diferencia un error de almacenamiento del fallo del cambio. Si el registro se vuelve de solo lectura después de producir efectos, el lote se detiene, los recibos se conservan y se genera un diagnóstico de emergencia en `/tmp`.

### Ajuste de selección en Cambios

- La fila completa de **Cambios disponibles** funciona como control de selección; ya no hay que acertar en una casilla pequeña.
- Una fila elegida muestra `✓ SELECCIONADO`, borde de éxito y fondo diferenciado.
- La pantalla usa un único botón inferior de integración. Con un cambio seleccionado ejecuta el flujo individual; con dos o más cambia a `Integrar lote (N)` y abre la revisión conjunta.
- El resumen de selección indica explícitamente si el contexto actual es una integración individual o por lote.

## Cambio

- Cada cambio marcado para un lote recibe una clase visual `batch-selected` además del estado interno de selección.
- La fila seleccionada usa una insignia y un borde de éxito para que el estado del lote sea visible incluso en terminales con representación limitada de controles.
- `_render_batch_selection()` usa `batch_selected_ids` como única fuente para filas, insignias y resumen, evitando desincronización entre modelo y UI.


## Cambio

- Los comandos largos de instalación pueden usar un presupuesto de **inactividad** en vez de un límite total rígido. Cualquier byte de salida, incluidas barras con `\r`, renueva la espera.
- `PackageInstallExecutor` usa 300 s de inactividad por defecto; si apt/flatpak siguen reportando progreso, la instalación puede superar el antiguo límite total sin ser terminada.
- La inicialización de GIMP previa a PhotoGIMP ya no tiene el `timeout=150` exterior que podía convertir en fallo un ciclo que había terminado correctamente.
- La creación y estabilización de la configuración de GIMP distinguen actividad real del árbol de archivos. Las escrituras renuevan el presupuesto; la ausencia de actividad sí produce un timeout diagnóstico.
- Se mantiene un techo de seguridad de 600 s en las esperas de configuración para evitar bloqueos infinitos cuando una señal observable cambia de forma patológica.

## Cambio

- La lista **Cambios disponibles** incorpora selección múltiple.
- La revisión por lote presenta el orden completo antes de modificar el equipo.
- Los cambios se ejecutan secuencialmente; nunca se fusionan los IDs internos de DAG distintos.
- Antes de cada cambio, `ChangeService.execute()` reconstruye su plan con el estado que dejó el cambio anterior, permitiendo reconciliar paquetes, ejecutables y otras capacidades ya disponibles.
- Si dos cambios YAML seleccionados tienen una relación explícita de dependencia, la dependencia se ordena primero y no se repite visualmente en el preview del consumidor.
- El lote se detiene en el primer fallo. Los cambios completados conservan sus recibos; los restantes quedan sin iniciar y se indican claramente en el resultado.


## Cambio

- El comando inmediato ya no depende de detectar Conda.
- El instalador inspecciona el `PATH` original y puede reutilizar cualquier directorio `bin` seguro, escribible y perteneciente al usuario.
- Esto cubre Python del sistema, `~/bin`, pyenv, venv y Conda con una sola regla genérica.
- Si no hay un `bin` de usuario visible, `/usr/local/bin` funciona como respaldo administrado cuando ya pertenece al `PATH` de la terminal.
- La instalación real sigue en `~/.local/bin`; los puentes no contienen otra copia de Styler.


## Cambio

- El instalador ya no construye el wheel dentro de la carpeta extraída por el usuario.
- Antes de invocar pip crea una copia temporal limpia y excluye `build/`, `dist/`, `*.egg-info`, `__pycache__` y cachés de pruebas.
- La copia usa contenido, no permisos/ACL/xattrs del archivo de origen, para evitar errores `Operation not permitted` heredados del ZIP.
- Las baselines empacadas se distribuyen con permisos `0644`.
- La instalación anterior sigue siendo transaccional: un fallo del build no activa la versión nueva.

## Cambio

- El YAML de AppImageLauncher declara `satisfied_by: executable: ail-cli` en descarga e instalación.
- Si `ail-cli` ya existe, Styler no consulta GitHub ni vuelve a instalar AppImageLauncher.
- La dependencia de Affinity y el DAG de PipeCraft se conservan; solo se reconcilia infraestructura ya disponible.

## Cambio

Los cambios incorporados ya no necesitan código específico por aplicación. Styler carga recetas YAML desde `styler/catalog/changes/`, las compila a DAG y las expone en **Cambios** por el mismo `ChangeService` que usa PhotoGIMP.

Styler incluye dos YAML separados: `appimagelauncher.yaml` proporciona la capacidad `appimage.integration.ready`; `affinity-linux.yaml` la requiere. Al seleccionar Affinity, el resolutor compone primero AppImageLauncher y después la descarga, integración y verificación del AppImage de Affinity. La descarga se resuelve desde GitHub Releases por repositorio, tag y nombre exacto de asset y comprueba el SHA-256 declarado.

El soporte incorporado se limita a familias Ubuntu/Debian y x86_64: AppImageLauncher se instala desde su `.deb` amd64 y Affinity usa `Affinity-3-x86_64.AppImage`. Añadir otra familia o arquitectura requiere otro proveedor declarativo, no condicionales específicos de Affinity en Python.


## Cambio

El Constructor cierra explícitamente cada ciclo después de crear un `.stylerpkg`: mantiene la línea base activa, vuelve al paso **Detección**, limpia la selección y el plan, y exige un nuevo escaneo. Cada paquete creado guarda fingerprints de los estados que lo originaron; mientras esos estados sigan idénticos y el paquete permanezca registrado, no se ofrecen otra vez como cambios pendientes.

## Cambio

No existe una «baseline por default de Styler». El catálogo puede contener muchas baselines oficiales y cada una es predeterminada únicamente para la identidad de sistema que declara. La baseline actualmente incluida es `linuxmint-22.3-xfce-x11-stable-x86_64` y exige Linux Mint 22.3, XFCE, X11, modelo stable y arquitectura x86_64.

Si la identidad no coincide o está incompleta, Styler no adivina ni usa esta baseline como fallback. Al añadir en el futuro Debian, Arch, Fedora, openSUSE u otra combinación, cada una aportará su propio `.stylerpkg` de tipo `baseline` al catálogo.

Al actualizar, una baseline oficial que pertenecía a un catálogo empacado anterior pero ya no existe en el catálogo actual se retira del registro local; no se conservan defaults oficiales obsoletos.

## Cambio

Styler distribuye ahora una línea base precargada para **Linux Mint 22.3 · XFCE · X11 · stable · x86_64**. El archivo viaja dentro del catálogo de baselines como `.stylerpkg`; `BaselineService` lo registra al iniciar y, si el sistema coincide y no existe otra línea base activa, lo adopta como recomendación predeterminada.

La captura original aportada al proyecto se conserva en inventario; para el catálogo integrado se empaqueta como línea base oficial `linuxmint-22.3-xfce-x11-stable-x86_64`, que es la forma que el sistema de recomendación ya entiende.

## Cambio

Los DAG portables son integrables desde Cambios aunque no tengan opciones de proveedor. Los cambios cuya fuente pertenece al usuario —paquetes importados o creados por el Constructor— pueden eliminarse físicamente de Styler desde la lista de disponibles. Eliminar la fuente no equivale a retirar un cambio ya integrado.

## Cambio

Los DAG contenidos en `.stylerpkg` de tipo `change` pasan a formar parte del catálogo de **Cambios**. Importar o crear un paquete no ejecuta nada: el cambio aparece junto a PhotoGIMP y se revisa, integra y retira mediante el mismo flujo de `ChangeService` y PipeCraft. La administración de paquetes queda limitada a importar, inspeccionar, exportar y eliminar artefactos.

La ruta pública separada `styler package plan/run` se eliminó; desde CLI también se usa `styler change plan/apply`. El workflow contenido en el paquete no se recompila ni se reordena al entrar en Cambios.

## Corrección

Al crear un paquete, Styler acepta texto humano para el identificador y lo normaliza automáticamente. Por ejemplo, `Local` pasa a `local` y `Mi paquete José` a `mi-paquete-jose`. El nombre visible se conserva tal como lo escribió el usuario. Los paquetes externos siguen validándose de forma estricta y nunca cambian de identidad al importarse.

## Corrección

Las filas del Constructor deshabilitan la selección arbitraria de texto. Esto evita el fallo de Textual `None.region` al hacer clic sobre una línea base, un cambio detectado o un paquete guardado.

Una línea base personalizada puede exportarse sin cambiar su tipo o prepararse explícitamente como candidata oficial. Esta segunda operación no muta la copia local y solo debe confirmarse cuando el inventario fue capturado en una instalación limpia.

**Manual único de Styler.**

## Propósito

Styler registra cambios realizados en Linux, permite elegir cuáles pertenecen juntos, genera automáticamente una receta semántica y la compila como un DAG de PipeCraft. El resultado se guarda en un único formato: `.stylerpkg`.

## Secciones que permanecen

### Cambios

Catálogo único de cambios listos para integrar o retirar. Reúne PhotoGIMP y cada DAG de los `.stylerpkg` de cambio registrados.

### Actividad

Historial de operaciones aplicadas y puntos reversibles.

### Herramientas: Constructor de cambios

Es una pantalla larga con dos pestañas internas:

- **Nuevo cambio**
- **Paquetes guardados**

El flujo de Nuevo cambio es:

```text
línea base
→ escaneo
→ cambios detectados
→ contenido del paquete
→ receta automática
→ DAG
→ validación
→ exportación
```

## Línea base

La línea base es el estado de referencia. Puede ser oficial o personalizada. Se administra al principio del constructor porque determina qué se considera nuevo o modificado.

Las líneas base también viajan como `.stylerpkg`; el manifiesto las distingue mediante `package_type: baseline`.

Una línea base personalizada se puede eliminar. Eliminarla no desinstala aplicaciones ni modifica el sistema. Las oficiales no se eliminan: se comprueban y reparan desde el catálogo incluido.

## Detección

El inventario reúne aplicaciones de gestores disponibles y recursos observables del usuario. La comparación identifica:

- aplicaciones añadidas o cambiadas;
- AppImages localizadas;
- temas;
- temas de iconos y cursores;
- fondos;
- fuentes;
- CSS visual;
- otros recursos declarados por los detectores.

Sin línea base el constructor funciona en modo inventario: muestra lo presente, pero no afirma que sea nuevo.

## Receta y grafo automáticos

Styler no genera el DAG directamente desde archivos sueltos. Primero sintetiza una receta interna:

```text
evidencia
→ efectos normalizados
→ receta YAML interna
→ compilador determinista
→ DAG PipeCraft
```

La receta no es un archivo público. Viaja dentro del `.stylerpkg` junto con el grafo y los assets.

El compilador añade automáticamente:

- checkpoint inicial;
- instalación o copia segura;
- dependencias;
- respaldos mediante los ejecutores;
- verificación final;
- datos necesarios para recibos y retiro.

El modo normal muestra un resumen. **Desglosar plan** enseña nodos, tipos y dependencias.

## Paquetes guardados

La pestaña administra artefactos creados o importados. Permite importar, volver a exportar y eliminar paquetes. Un paquete de tipo `change` registrado aparece automáticamente en **Cambios**; no se aplica desde aquí.

## Formato `.stylerpkg`

Un paquete contiene:

```text
manifest.json
checksums.json
baseline/...        # cuando package_type=baseline
recipe/...          # cuando package_type=change
graph/...
assets/...
components/...      # opcional
actions/...         # opcional
```

No se admiten otras extensiones portables.

## Seguridad

- importar nunca ejecuta;
- las rutas internas se confinan;
- se rechazan enlaces simbólicos peligrosos;
- existen límites de archivos y tamaño;
- todos los artefactos se verifican por SHA-256;
- el paquete normal no admite shell o Python arbitrario;
- los assets se aplican mediante acciones registradas.

## CLI

```bash
styler baseline list
styler baseline capture --name "Estado inicial"
styler baseline export ID base.stylerpkg
styler baseline import base.stylerpkg --activate
styler baseline delete ID

styler constructor scan
styler constructor plan --package-id mi-cambio --select ID
styler constructor export salida.stylerpkg --package-id mi-cambio --select ID

styler package inspect archivo.stylerpkg
styler package import archivo.stylerpkg
styler package list
styler change list
styler change plan CHANGE_ID
styler change apply CHANGE_ID --execute --approve
```

## Pruebas de aceptación

Antes de publicar una versión del constructor deben pasar casos de:

1. aplicación APT detectada después de una línea base;
2. AppImage añadida a una ruta vigilada;
3. tema, cursor y CSS detectados;
4. selección mixta en un solo paquete;
5. receta y DAG generados;
6. importación y vista previa;
7. eliminación de línea base personalizada;
8. apertura y uso real de la TUI con Textual.

## Pipeline actual de PhotoGIMP

PhotoGIMP continúa siendo el cambio de referencia del catálogo. Styler resuelve GIMP, crea un checkpoint, inicializa y verifica la aplicación, respalda la configuración, aplica el overlay, verifica el resultado y registra efectos para construir el retiro. Su construcción del DAG permanece intacta.

## PipeCraft dentro de Styler

PipeCraft es el backend de ejecución preferente de Styler. `ChangeService` y el puente de restauración producen el plan semántico en Styler; un adaptador lo convierte en un pipeline transitorio y lo envía al servicio Rust por `pipecraft.ipc/v1`. PipeCraft posee el ciclo operativo del DAG —scheduling, recursos, supervisión de procesos, eventos, cancelación y estado durable— mientras Styler conserva receipts, reconciliación del estado real, backups, undo y significado de cada operación.

La ruta local Python permanece durante esta alpha como compatibilidad deliberada cuando no hay un binario PipeCraft disponible. La migración no crea dos autoridades simultáneas: si PipeCraft ya aceptó un `run_id`, Styler nunca repite automáticamente el mismo trabajo con el backend local.

## Auditoría de los archivos del ZIP

El paquete de release debe contener únicamente el código, pruebas, documentación vigente y archivos de distribución necesarios. La auditoría rechaza extensiones portables distintas de `.stylerpkg`, reportes históricos, logs, cachés, artefactos de compilación y formatos públicos retirados.

### Cambio de arquitectura

- PipeCraft dejó de estar incluido como vendor dentro del repositorio de Styler. El runtime Rust se versiona y distribuye como proyecto independiente.
- Los flujos productivos de integración/restauración fallan cerrado si PipeCraft no está disponible; el scheduler Python ya no forma parte del paquete instalado.
- El backend Python histórico permanece temporalmente sólo para pruebas unitarias y compatibilidad explícita mientras se retiran sus últimas dependencias de modelos/planificación.
- `styler doctor` informa por separado binario, daemon, protocolo y compatibilidad de versión.
- Esta separación hace que la barra de lenguajes de GitHub describa a **Styler** (UI y dominio, principalmente Python) y la del repositorio **PipeCraft** describa al runtime (Rust), en vez de mezclar dos proyectos en una sola estadística.

### Sobre los porcentajes de lenguajes en GitHub

Styler seguirá apareciendo mayoritariamente como Python porque su interfaz Textual,
catálogo, dominio, receipts y reconciliación viven en este repositorio. El motor
Rust de scheduling/multi-pipeline pertenece a PipeCraft y, desde alpha.2, ya no se
copia dentro del repositorio de Styler. Por tanto la barra de lenguajes de Styler
debe describir Styler, no la suma artificial Styler + PipeCraft.

El objetivo arquitectónico no es maximizar el porcentaje Rust de este repositorio,
sino evitar motores duplicados: un solo runtime Rust privado incluido en la distribución (PipeCraft) y un
cliente/adaptador Python pequeño dentro de Styler.
