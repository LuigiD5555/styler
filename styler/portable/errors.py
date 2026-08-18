"""Errores de formato/seguridad de paquetes portables."""
from styler.automation.specs import SpecError


class PortablePackageError(SpecError):
    """El paquete no puede importarse de forma segura o consistente."""
