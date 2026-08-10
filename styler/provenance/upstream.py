"""
styler.provenance.upstream
==========================
Inferencia de repositorio upstream a partir de metadatos declarados por el
gestor de paquetes (Homepage, URL, Source, update-information de AppImage).

Regla dura, tomada del diseño de la 0.8:

    Styler NO adivina el repositorio de GitHub por parecido de nombre.

Solo se acepta un upstream cuando una URL real, entregada por el propio
paquete o por su metadato de actualización, apunta a una forja conocida y
contiene owner/repo. Todo lo demás queda como UNKNOWN, guardando la homepage
como evidencia para que una persona decida.
"""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlparse

from styler.provenance.models import Confidence, Upstream

# Forjas donde la ruta /owner/repo identifica un repositorio de código.
FORGES: dict[str, str] = {
    "github.com": "github",
    "www.github.com": "github",
    "gitlab.com": "gitlab",
    "www.gitlab.com": "gitlab",
    "codeberg.org": "codeberg",
    "invent.kde.org": "gitlab",
    "salsa.debian.org": "gitlab",
    "gitlab.gnome.org": "gitlab",
    "gitlab.freedesktop.org": "gitlab",
    "git.sr.ht": "sourcehut",
    "bitbucket.org": "bitbucket",
}

# Rutas que nunca son un repositorio aunque estén en una forja.
_RESERVED = {
    "about", "explore", "features", "pricing", "topics", "orgs", "users",
    "sponsors", "marketplace", "login", "join", "-", "help", "settings",
}


def _clean_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def parse_repository_url(url: str) -> tuple[str, str, str]:
    """Devuelve (provider, "owner/repo", url_normalizada) o ("", "", "")."""
    if not url:
        return "", "", ""
    candidate = url.strip()
    if candidate.startswith("git@") and ":" in candidate:
        host, _, path = candidate.partition(":")
        candidate = "https://" + host.removeprefix("git@") + "/" + path
    if candidate.startswith("git+"):
        candidate = candidate[4:]
    if "://" not in candidate:
        candidate = "https://" + candidate

    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    provider = FORGES.get(host)
    if not provider:
        return "", "", ""

    segments = _clean_segments(parsed.path)
    if provider == "sourcehut" and segments and segments[0].startswith("~"):
        segments = segments  # sr.ht usa ~owner/repo, se conserva tal cual
    if len(segments) < 2:
        return "", "", ""
    owner, repo = segments[0], segments[1]
    if owner.lower() in _RESERVED:
        return "", "", ""
    repo = repo.removesuffix(".git")
    if not repo:
        return "", "", ""

    normalized = f"https://{parsed.netloc}/{owner}/{repo}"
    return provider, f"{owner}/{repo}", normalized


def upstream_from_metadata(
    homepage: str = "",
    source_url: str = "",
    evidence: str = "",
) -> Upstream:
    """Construye un Upstream a partir de metadatos declarados por el paquete.

    * Si la URL apunta a una forja con owner/repo -> INFERRED.
    * Si hay homepage pero no es una forja -> UNKNOWN, con la homepage guardada.
    * Si no hay nada -> UNKNOWN vacío.
    """
    for url in (source_url, homepage):
        provider, repository, normalized = parse_repository_url(url)
        if repository:
            return Upstream(
                provider=provider,
                repository=repository,
                url=normalized,
                homepage=homepage,
                confidence=Confidence.INFERRED,
                evidence=evidence,
            )
    return Upstream(homepage=homepage, confidence=Confidence.UNKNOWN, evidence=evidence)


def upstream_from_update_information(update_info: str, evidence: str = "") -> Upstream:
    """Lee la cadena `update-information` de un AppImage.

    Formatos declarados por el propio AppImage (no es una suposición):

        gh-releases-zsync|owner|repo|tag|archivo.zsync
        zsync|https://servidor/archivo.zsync

    Cuando declara GitHub Releases, la procedencia es CONFIRMADA: el binario
    mismo dice de dónde se actualiza.
    """
    info = (update_info or "").strip()
    if not info:
        return Upstream(confidence=Confidence.UNKNOWN, evidence=evidence)

    parts = info.split("|")
    kind = parts[0].strip().lower()

    if kind == "gh-releases-zsync" and len(parts) >= 3:
        owner, repo = parts[1].strip(), parts[2].strip()
        if owner and repo:
            return Upstream(
                provider="github",
                repository=f"{owner}/{repo}",
                url=f"https://github.com/{owner}/{repo}",
                releases_url=f"https://github.com/{owner}/{repo}/releases",
                confidence=Confidence.CONFIRMED,
                evidence=evidence or "AppImage update-information",
            )

    if kind == "zsync" and len(parts) >= 2:
        url = parts[1].strip()
        provider, repository, normalized = parse_repository_url(url)
        if repository:
            return Upstream(
                provider=provider,
                repository=repository,
                url=normalized,
                confidence=Confidence.CONFIRMED,
                evidence=evidence or "AppImage update-information",
            )
        return Upstream(
            homepage=url,
            confidence=Confidence.SUGGESTED,
            evidence=evidence or "AppImage update-information (zsync)",
        )

    return Upstream(confidence=Confidence.UNKNOWN, evidence=evidence)


def with_packaging_repository(upstream: Upstream, repository: str, evidence: str) -> Upstream:
    """Anota el repositorio de EMPAQUETADO (no el de desarrollo).

    Ejemplo: una app de Flathub siempre se empaqueta en github.com/flathub/<id>.
    Eso es una regla del remote, no un parecido de nombre, pero tampoco es el
    repositorio del desarrollador: se guarda en un campo aparte.
    """
    return replace(
        upstream,
        packaging_repository=repository,
        evidence=upstream.evidence or evidence,
    )
