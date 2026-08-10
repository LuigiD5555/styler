# Styler Reinvented 0.9.1

En 0.9.1, `bash ./install.sh` publica el comando `styler` de forma inmediata
cuando existe una ruta visible segura para el puente. La instalación real
continúa en `${XDG_BIN_HOME:-$HOME/.local/bin}`; si esa ruta aún no era visible,
el instalador inspecciona el `PATH` heredado y reutiliza cualquier directorio
`bin` seguro, escribible y perteneciente al usuario. En una instalación normal
con Python del sistema, puede usar `/usr/local/bin` como respaldo administrado
cuando esa ruta ya está en el `PATH`. El puente solo delega al ejecutable real y
`uninstall.sh` lo elimina únicamente si reconoce la marca de Styler.

En 0.9.1, Styler conserva **AppImageLauncher** y **Affinity para Linux** como cambios declarativos YAML. Los YAML concretan qué release descargar y cómo componer el cambio; Python solo implementa primitivas genéricas (`release.fetch`, `package.install_artifact`, `appimage.integrate`, `appimage.verify`) y PipeCraft ejecuta el DAG por el mismo flujo de **Cambios** que PhotoGIMP.

Antes de descargar AppImageLauncher, el DAG comprueba la capacidad declarada `ail-cli`. Si ya existe, los pasos de descarga e instalación se satisfacen sin red ni reinstalación; Affinity continúa usando ese proveedor existente.

Affinity declara a AppImageLauncher como requisito. Al integrar Affinity, Styler compone ambos YAML en un solo DAG: instala/reutiliza AppImageLauncher, descarga el AppImage oficial de Affinity, lo integra mediante `ail-cli` y verifica la entrada de escritorio. Los assets están fijados por tag, nombre y SHA-256. Esta definición inicial se ofrece solo en familias APT (Ubuntu/Debian, incluido Linux Mint) y x86_64 porque el proveedor incorporado usa el `.deb` amd64 de AppImageLauncher y el AppImage x86_64 de Affinity.


En 0.8.3, los cambios que necesitan permisos administrativos piden autorización antes de iniciar el DAG. Si un comando falla, Styler conserva y muestra la causa real, el comando, el código técnico y el log durable en vez de reducir todo a «código 1». El DAG no se modifica para conseguirlo.

En 0.8.3, terminar un paquete cierra el ciclo del Constructor: conserva la línea base, limpia selección/plan/campos y vuelve a **Detección**. Los estados ya empaquetados dejan de ofrecerse mientras sigan idénticos; si una aplicación se actualiza, un archivo cambia o se elimina el paquete local que los representaba, vuelven a aparecer como pendientes.

En 0.8.1, las líneas base oficiales precargadas son **defaults por identidad de sistema**, no un default global de Styler. La baseline incluida pertenece exclusivamente a **Linux Mint 22.3 · XFCE · X11 · stable · x86_64**. Solo se recomienda y adopta automáticamente cuando esa identidad coincide; otra distro, versión, escritorio, sesión, modelo de release o arquitectura necesita su propia baseline oficial.


En 0.7.6, un DAG importado desde `.stylerpkg` se integra desde **Cambios** exactamente por el mismo flujo de revisión y PipeCraft que los demás cambios. La administración ya no ofrece "ocultar" paquetes: un cambio local se elimina de Styler cuando la persona decide borrarlo.

Styler convierte cambios hechos en Linux en paquetes revisables, reproducibles y retirables.

La aplicación conserva tres secciones superiores:

- **Cambios:** integra y retira todos los cambios disponibles, tanto incorporados como PhotoGIMP como importados o creados mediante `.stylerpkg`.
- **Actividad:** muestra operaciones aplicadas y permite deshacer las reversibles.
- **Herramientas:** abre el **Constructor de cambios**.


## Un solo flujo para aplicar cambios

Importar o crear un `.stylerpkg` de tipo `change` **no lo ejecuta**. Styler registra sus DAG en el catálogo y los muestra en **Cambios**, junto a PhotoGIMP. Desde allí se revisan, se pasan a la selección y se integran con el flujo existente de PipeCraft.

`Paquetes guardados` queda limitado a administración del artefacto: importar, inspeccionar, exportar o eliminar. No existe un segundo botón de aplicación para paquetes.

## Constructor de cambios

El constructor es un asistente de cuatro pasos y solo muestra el paso que corresponde:

1. **Punto de partida:** elegir, importar o capturar la línea base.
2. **Detección:** escanear aplicaciones, AppImages y recursos visuales.
3. **Selección:** mover a la lista del paquete solo los elementos deseados.
4. **Paquete:** generar el plan, desglosarlo cuando se necesite y crear el `.stylerpkg`.

Las acciones poco frecuentes viven en **Más**. El informe del plan siempre distingue lo incluido de lo omitido y explica el motivo.

En el paso **Punto de partida**, **Exportar seleccionada** conserva el tipo actual de la línea base. **Preparar para catálogo oficial** crea una copia oficial sin modificar la personalizada local y exige confirmar que la captura procede de una instalación limpia. El `.stylerpkg` resultante puede colocarse en `styler/baselines/catalog/` para distribuirlo como línea base recomendada en una versión posterior.

## Un único formato

El único formato portable de Styler es:

```text
.stylerpkg
```

Un `.stylerpkg` puede representar una línea base o un cambio. Las recetas YAML, los grafos, las acciones y los recursos son contenido interno del paquete; no son formatos públicos separados.

## PATH de instalación

El instalador añade automáticamente `${XDG_BIN_HOME:-$HOME/.local/bin}` al `PATH`
del propio proceso de instalación y lo deja persistido en `~/.profile` y en el
archivo del shell interactivo compatible (`~/.bashrc`, `~/.zshrc`, etc.). No
duplica la configuración si Styler se reinstala.

Si `install-styler.sh` se ejecuta con `source`, también actualiza el `PATH` del
shell actual.

## Inicio

```bash
./run-styler.sh
```

O, después de instalar:

```bash
styler
styler --version
styler doctor
```

CLI principal:

```bash
styler change --help
styler constructor --help
styler baseline --help
styler package --help
```

## Desarrollo

```bash
python -m pytest
python -m build --wheel --no-isolation
```

El catálogo de líneas base oficiales acepta únicamente paquetes `.stylerpkg` de tipo `baseline` en `styler/baselines/catalog/`.
