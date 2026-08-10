"""PipeCraft runtime package.

La API se importa desde sus módulos concretos. El paquete no reexporta símbolos
porque esas fachadas ocultaban dependencias circulares y mantenían dos formas de
usar el motor.
"""
