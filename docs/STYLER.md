# Styler 0.9.1


## Cambio 0.9.1

- El YAML de AppImageLauncher declara `satisfied_by: executable: ail-cli` en descarga e instalación.
- Si `ail-cli` ya existe, Styler no consulta GitHub ni vuelve a instalar AppImageLauncher.
- La dependencia de Affinity y el DAG de PipeCraft se conservan; solo se reconcilia infraestructura ya disponible.

## Cambio 0.9.0

Los cambios incorporados ya no necesitan código específico por aplicación. Styler carga recetas YAML desde `styler/catalog/changes/`, las compila a DAG y las expone en **Cambios** por el mismo `ChangeService` que usa PhotoGIMP.

Esta versión incluye dos YAML separados: `appimagelauncher.yaml` proporciona la capacidad `appimage.integration.ready`; `affinity-linux.yaml` la requiere. Al seleccionar Affinity, el resolutor compone primero AppImageLauncher y después la descarga, integración y verificación del AppImage de Affinity. La descarga se resuelve desde GitHub Releases por repositorio, tag y nombre exacto de asset y comprueba el SHA-256 declarado.

El soporte incorporado de 0.9.0 se limita a familias Ubuntu/Debian y x86_64: AppImageLauncher se instala desde su `.deb` amd64 y Affinity usa `Affinity-3-x86_64.AppImage`. Añadir otra familia o arquitectura requiere otro proveedor declarativo, no condicionales específicos de Affinity en Python.


## Cambio 0.8.3

El Constructor cierra explícitamente cada ciclo después de crear un `.stylerpkg`: mantiene la línea base activa, vuelve al paso **Detección**, limpia la selección y el plan, y exige un nuevo escaneo. Cada paquete creado guarda fingerprints de los estados que lo originaron; mientras esos estados sigan idénticos y el paquete permanezca registrado, no se ofrecen otra vez como cambios pendientes.

## Cambio 0.8.1

No existe una «baseline por default de Styler». El catálogo puede contener muchas baselines oficiales y cada una es predeterminada únicamente para la identidad de sistema que declara. La baseline actualmente incluida es `linuxmint-22.3-xfce-x11-stable-x86_64` y exige Linux Mint 22.3, XFCE, X11, modelo stable y arquitectura x86_64.

Si la identidad no coincide o está incompleta, Styler no adivina ni usa esta baseline como fallback. Al añadir en el futuro Debian, Arch, Fedora, openSUSE u otra combinación, cada una aportará su propio `.stylerpkg` de tipo `baseline` al catálogo.

Al actualizar, una baseline oficial que pertenecía a un catálogo empacado anterior pero ya no existe en el catálogo actual se retira del registro local; no se conservan defaults oficiales obsoletos.

## Cambio 0.8.0

Styler distribuye ahora una línea base precargada para **Linux Mint 22.3 · XFCE · X11 · stable · x86_64**. El archivo viaja dentro del catálogo de baselines como `.stylerpkg`; `BaselineService` lo registra al iniciar y, si el sistema coincide y no existe otra línea base activa, lo adopta como recomendación predeterminada.

La captura original aportada al proyecto se conserva en inventario; para el catálogo integrado se empaqueta como línea base oficial `linuxmint-22.3-xfce-x11-stable-x86_64`, que es la forma que el sistema de recomendación ya entiende.

## Cambio 0.7.6

Los DAG portables son integrables desde Cambios aunque no tengan opciones de proveedor. Los cambios cuya fuente pertenece al usuario —paquetes importados o creados por el Constructor— pueden eliminarse físicamente de Styler desde la lista de disponibles. Eliminar la fuente no equivale a retirar un cambio ya integrado.

## Cambio 0.7.4

Los DAG contenidos en `.stylerpkg` de tipo `change` pasan a formar parte del catálogo de **Cambios**. Importar o crear un paquete no ejecuta nada: el cambio aparece junto a PhotoGIMP y se revisa, integra y retira mediante el mismo flujo de `ChangeService` y PipeCraft. La administración de paquetes queda limitada a importar, inspeccionar, exportar y eliminar artefactos.

La ruta pública separada `styler package plan/run` se eliminó; desde CLI también se usa `styler change plan/apply`. El workflow contenido en el paquete no se recompila ni se reordena al entrar en Cambios.

## Corrección 0.7.3

Al crear un paquete, Styler acepta texto humano para el identificador y lo normaliza automáticamente. Por ejemplo, `Local` pasa a `local` y `Mi paquete José` a `mi-paquete-jose`. El nombre visible se conserva tal como lo escribió el usuario. Los paquetes externos siguen validándose de forma estricta y nunca cambian de identidad al importarse.

## Corrección 0.7.2

Las filas del Constructor deshabilitan la selección arbitraria de texto. Esto evita el fallo de Textual `None.region` al hacer clic sobre una línea base, un cambio detectado o un paquete guardado.

Una línea base personalizada puede exportarse sin cambiar su tipo o prepararse explícitamente como candidata oficial. Esta segunda operación no muta la copia local y solo debe confirmarse cuando el inventario fue capturado en una instalación limpia.

**Manual único de Styler Reinvented.**

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

PhotoGIMP continúa siendo el cambio de referencia del catálogo. Styler resuelve GIMP, crea un checkpoint, inicializa y verifica la aplicación, respalda la configuración, aplica el overlay, verifica el resultado y registra efectos para construir el retiro. Su construcción del DAG permanece intacta. La 0.7.4 hizo que los DAG portables convergieran en la misma interfaz y servicio de aplicación; la 0.7.6 no cambia ese DAG ni su ejecución.

## PipeCraft dentro de Styler

PipeCraft sigue siendo el runtime único para planificar DAG, ejecutar operaciones, emitir eventos, guardar logs y registrar resultados. El Constructor genera una receta semántica y la compila a un workflow de PipeCraft; no crea una segunda ruta de ejecución.

## Auditoría de los archivos del ZIP

El paquete de release debe contener únicamente el código, pruebas, documentación vigente y archivos de distribución necesarios. La auditoría rechaza extensiones portables distintas de `.stylerpkg`, reportes históricos, logs, cachés, artefactos de compilación y formatos públicos retirados.
