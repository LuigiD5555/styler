"""Compatibilidad de import para la antigua ruta ``styler.advanced_restore``.

La implementación vive en ``styler.restore.candidates``; este archivo no contiene
un motor de restauración paralelo.
"""
from styler.restore import candidates as _impl

globals().update({name: value for name, value in vars(_impl).items() if name not in {"__name__", "__package__", "__loader__", "__spec__"}})
