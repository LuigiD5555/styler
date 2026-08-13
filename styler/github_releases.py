"""Consulta segura y explícita de instaladores publicados en GitHub Releases.

Esta capa no intenta adivinar repositorios por semejanza de nombres. Solo usa:

* el upstream GitHub declarado en el inventario; o
* una asociación curada y revisable para paquetes concretos.
"""
from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from styler.provenance.models import ApplicationRecord

GITHUB_API = "https://api.github.com"

# Excepción curada para paquetes cuyo .deb histórico no conserva Homepage/Source.
# No es inferencia por nombre: la asociación está declarada explícitamente.
KNOWN_GITHUB_UPSTREAMS: dict[tuple[str, str], str] = {
    ("apt", "appimagelauncher"): "TheAssassin/AppImageLauncher",
}


@dataclass(frozen=True)
class GitHubReleaseAsset:
    repository: str
    tag: str
    version: str
    name: str
    download_url: str
    architecture: str


ReleaseFetcher = Callable[[str], list[dict]]


def default_release_fetcher(repository: str) -> list[dict]:
    """Devuelve releases recientes usando la API pública de GitHub."""
    owner, repo = repository.split("/", 1)
    url = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/releases?per_page=30"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Styler-Reinvented/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310 - host fijo y HTTPS
        payload = json.load(response)
    return payload if isinstance(payload, list) else []


def repository_for_record(record: ApplicationRecord) -> str:
    upstream = record.upstream
    if upstream.provider.lower() == "github" and _valid_repository(upstream.repository):
        return upstream.repository
    return KNOWN_GITHUB_UPSTREAMS.get((record.manager.lower(), record.name.lower()), "")


def github_deb_assets(
    record: ApplicationRecord,
    *,
    fetcher: ReleaseFetcher = default_release_fetcher,
) -> tuple[list[GitHubReleaseAsset], list[str]]:
    """Busca .deb compatibles publicados por el upstream oficial/conocido."""
    repository = repository_for_record(record)
    if not repository:
        return [], []
    if record.manager.lower() != "apt":
        return [], []

    try:
        releases = fetcher(repository)
    except Exception as exc:  # red, API, límite de tasa o JSON inválido
        return [], [f"No se pudo consultar GitHub Releases para {repository}: {exc}"]

    wanted_arch = _normalize_arch(record.architecture or platform.machine())
    assets: list[GitHubReleaseAsset] = []
    for release in releases:
        if release.get("draft"):
            continue
        tag = str(release.get("tag_name") or release.get("name") or "").strip()
        for raw_asset in release.get("assets") or []:
            name = str(raw_asset.get("name") or "")
            url = str(raw_asset.get("browser_download_url") or "")
            if not name.lower().endswith(".deb"):
                continue
            if any(marker in name.lower() for marker in ("-dbgsym", "debug", "devel", "source")):
                continue
            arch = _architecture_from_asset(name)
            if arch and wanted_arch and arch not in {wanted_arch, "all"}:
                continue
            if not _trusted_release_url(repository, url):
                continue
            version = _version_from_deb_name(record.name, name) or tag.lstrip("v")
            assets.append(
                GitHubReleaseAsset(
                    repository=repository,
                    tag=tag,
                    version=version,
                    name=name,
                    download_url=url,
                    architecture=arch or wanted_arch,
                )
            )
    return assets, []


def _valid_repository(repository: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""))


def _trusted_release_url(repository: str, url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return False
    expected = "/" + repository.lower() + "/releases/download/"
    return parsed.path.lower().startswith(expected)


def _normalize_arch(architecture: str) -> str:
    value = (architecture or "").strip().lower()
    return {
        "x86_64": "amd64",
        "x64": "amd64",
        "aarch64": "arm64",
        "armv8": "arm64",
        "i386": "i386",
        "i686": "i386",
    }.get(value, value)


def _architecture_from_asset(filename: str) -> str:
    stem = filename[:-4] if filename.lower().endswith(".deb") else filename
    match = re.search(r"(?:_|-)(amd64|x86_64|arm64|aarch64|i386|i686|all)$", stem, re.I)
    return _normalize_arch(match.group(1)) if match else ""


def _version_from_deb_name(package_name: str, filename: str) -> str:
    stem = filename[:-4] if filename.lower().endswith(".deb") else filename
    match = re.match(re.escape(package_name) + r"[_-](.+?)[_-](?:amd64|x86_64|arm64|aarch64|i386|i686|all)$", stem, re.I)
    return match.group(1) if match else ""


__all__ = [
    "GitHubReleaseAsset",
    "KNOWN_GITHUB_UPSTREAMS",
    "ReleaseFetcher",
    "default_release_fetcher",
    "github_deb_assets",
    "repository_for_record",
]
