"""
styler.restore
==============
**El** orquestador. Un solo camino, un solo plan, un solo reporte.

Antes había dos caminos separados (`environment_restore` para KDE Plasma y otro
para aplicaciones y archivos). Aquí se unifican, porque restaurar un escritorio
es una sola cosa con un orden que no es negociable:

    escritorio → gestores y remotos → aplicaciones → VERIFICAR → archivos

La regla central del módulo, y la razón por la que existe:

    **Styler no copia ningún archivo de configuración hasta que el entorno y
    las aplicaciones necesarias estén instalados y verificados.**

Un panel de Plasma sin Plasma, o un `konsolerc` sin Konsole, no son una
restauración: son basura en el HOME de alguien.
"""
from __future__ import annotations

import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from styler import applications as apps_mod
from styler import catalogs
from styler import privileges
from styler import resolution as resolution_mod
from styler import target as target_mod
from styler import transaction as transaction_mod
from styler import verification as verify_mod
from styler.applications import AppSpec, ProgressCallback
from styler.models import FileEntry
from styler.parts import classify
from styler.resolution import Requirement, Resolution
from styler.resolvers import Candidate
from styler.execution.processes import Runner, ProcessRunner

# --------------------------------------------------------------------------- #
# Estados (punto 10 del rediseño: distinguir resultados reales)
# --------------------------------------------------------------------------- #
