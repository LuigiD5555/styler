"""Pruebas de la capa de procedencia (Styler 0.8).

Ninguna prueba toca la red ni un gestor de paquetes real: todos los comandos
externos pasan por FakeRunner.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from styler.provenance import inventory as inventory_mod
from styler.provenance import report as report_mod
from tests.support.fake_provenance import FakeRunner
from styler.provenance.detectors.appimage import (
    AppImageDetector,
    read_update_information,
    split_name_version,
)
from styler.provenance.detectors.apt import (
    AptDetector,
    parse_apt_policy,
    parse_deb822_sources,
    parse_one_line_sources,
)
from styler.provenance.detectors.flatpak import FlatpakDetector
from styler.provenance.detectors.pacman import PacmanDetector, parse_pacman_info
from styler.provenance.detectors.rpm import RpmDetector
from styler.provenance.detectors.snap import SnapDetector
from styler.provenance.models import ApplicationRecord, Confidence, Inventory, Origin, OriginKind
from styler.provenance.upstream import (
    parse_repository_url,
    upstream_from_metadata,
    upstream_from_update_information,
)
from styler.ui.provenance import ProvenanceService

# --------------------------------------------------------------------------
# upstream: nunca adivinar por parecido de nombre
# --------------------------------------------------------------------------


def test_parse_repository_url_reconoce_forjas():
    assert parse_repository_url("https://github.com/krita/krita")[:2] == ("github", "krita/krita")
    assert parse_repository_url("git@gitlab.com:owner/repo.git")[:2] == ("gitlab", "owner/repo")
    assert parse_repository_url("https://invent.kde.org/graphics/krita")[1] == "graphics/krita"


def test_parse_repository_url_rechaza_lo_que_no_es_repositorio():
    assert parse_repository_url("https://github.com/features") == ("", "", "")
    assert parse_repository_url("https://mozilla.org") == ("", "", "")
    assert parse_repository_url("") == ("", "", "")


def test_homepage_sin_forja_queda_desconocida_pero_guardada():
    upstream = upstream_from_metadata(homepage="https://www.mozilla.org/firefox/")
    assert upstream.confidence == Confidence.UNKNOWN
    assert upstream.repository == ""
    assert upstream.homepage == "https://www.mozilla.org/firefox/"


def test_homepage_en_forja_es_inferida_no_confirmada():
    upstream = upstream_from_metadata(homepage="https://github.com/obsproject/obs-studio")
    assert upstream.confidence == Confidence.INFERRED
    assert upstream.repository == "obsproject/obs-studio"


def test_update_information_de_appimage_es_confirmada():
    upstream = upstream_from_update_information(
        "gh-releases-zsync|nextcloud|desktop|latest|Nextcloud-*.AppImage.zsync"
    )
    assert upstream.confidence == Confidence.CONFIRMED
    assert upstream.repository == "nextcloud/desktop"
    assert upstream.releases_url.endswith("/releases")


# --------------------------------------------------------------------------
# APT
# --------------------------------------------------------------------------

POLICY = """firefox:
  Installed: 1:143.0
  Candidate: 1:144.0
  Version table:
 *** 1:143.0 500
        500 https://packages.mozilla.org/apt mozilla/main amd64 Packages
        100 /var/lib/dpkg/status
     1:144.0 500
        500 https://packages.mozilla.org/apt mozilla/main amd64 Packages
krita:
  Installed: 5.2.2
  Candidate: 5.2.2
  Version table:
 *** 5.2.2 100
        100 /var/lib/dpkg/status
"""


def test_parse_apt_policy_distingue_repositorio_de_instalacion_local():
    parsed = parse_apt_policy(POLICY)
    assert parsed["firefox"]["installed_from"]["url"] == "https://packages.mozilla.org/apt"
    assert parsed["firefox"]["installed_from"]["suite"] == "mozilla/main"
    assert parsed["krita"]["installed_from"] == {"local": True}


def test_parse_one_line_sources_lee_signed_by():
    index = parse_one_line_sources(
        "deb [signed-by=/usr/share/keyrings/mozilla.gpg] "
        "https://packages.mozilla.org/apt mozilla main\n"
        "# comentario\n"
    )
    entry = index["https://packages.mozilla.org/apt|mozilla"]
    assert entry["signed_by"].endswith("mozilla.gpg")


def test_parse_deb822_sources():
    index = parse_deb822_sources(
        "Types: deb\n"
        "URIs: http://archive.ubuntu.com/ubuntu\n"
        "Suites: jammy jammy-updates\n"
        "Components: main universe\n"
        "Signed-By: /usr/share/keyrings/ubuntu.gpg\n"
    )
    assert "http://archive.ubuntu.com/ubuntu|jammy-updates" in index
    assert index["http://archive.ubuntu.com/ubuntu|jammy"]["signed_by"]


def _apt_runner() -> FakeRunner:
    dpkg_out = (
        "firefox\t1:143.0\tamd64\tinstalled\thttps://www.mozilla.org\tfirefox\tMozilla\n"
        "krita\t5.2.2\tamd64\tinstalled\thttps://github.com/KDE/krita\tkrita\tKDE\n"
        "obsolete\t1.0\tamd64\tconfig-files\t\t\t\n"
    )
    from styler.provenance.detectors.apt import DPKG_FORMAT

    return FakeRunner(
        programs={"dpkg-query", "apt-cache", "dpkg"},
        outputs={
            ("dpkg-query", "-W", f"-f={DPKG_FORMAT}"): dpkg_out,
            ("apt-cache", "policy", "firefox", "krita"): POLICY,
        },
    )


def test_apt_detector_marca_paquete_sin_remote(tmp_path):
    sources = tmp_path / "apt"
    (sources / "sources.list.d").mkdir(parents=True)
    (sources / "sources.list").write_text(
        "deb [signed-by=/usr/share/keyrings/mozilla.gpg] "
        "https://packages.mozilla.org/apt mozilla main\n"
    )
    cache = tmp_path / "archives"
    cache.mkdir()
    (cache / "firefox_1%3a143.0_amd64.deb").write_bytes(b"deb")

    detector = AptDetector(
        runner=_apt_runner(),
        sources_dirs=[sources],
        applications_dirs=[tmp_path / "vacio"],
        cache_dir=cache,
    )
    records = {record.name: record for record in detector.detect(scope="all")}

    assert set(records) == {"firefox", "krita"}  # config-files no cuenta como instalado

    firefox = records["firefox"]
    assert firefox.origin.confidence == Confidence.CONFIRMED
    assert firefox.origin.remote_url == "https://packages.mozilla.org/apt"
    assert firefox.origin.signed is True
    assert firefox.integrity.artifact_available is True  # el .deb sigue en caché
    assert firefox.reproducible_today is True

    krita = records["krita"]
    assert krita.origin.confidence == Confidence.UNKNOWN
    assert krita.install_method == "manual"
    assert krita.reproducible_today is False
    assert krita.warnings
    # Homepage declarada por el paquete: inferida, nunca confirmada.
    assert krita.upstream.repository == "KDE/krita"
    assert krita.upstream.confidence == Confidence.INFERRED


# --------------------------------------------------------------------------
# Flatpak
# --------------------------------------------------------------------------


def test_flatpak_detector_captura_remote_rama_y_commit():
    runner = FakeRunner(
        programs={"flatpak"},
        outputs={
            (
                "flatpak",
                "list",
                "--app",
                "--columns=application,version,branch,arch,origin,installation",
            ): "org.mozilla.firefox\t143.0\tstable\tx86_64\tflathub\tsystem\n",
            ("flatpak", "remotes", "--columns=name,url,options"): (
                "flathub\thttps://dl.flathub.org/repo/\tsystem\n"
            ),
            ("flatpak", "info", "--show-commit", "org.mozilla.firefox"): "abc123\n",
            ("flatpak", "info", "--show-ref", "org.mozilla.firefox"): (
                "app/org.mozilla.firefox/x86_64/stable\n"
            ),
        },
    )
    record = FlatpakDetector(runner).detect()[0]

    assert record.app_id == "flatpak:org.mozilla.firefox"
    assert record.origin.commit == "abc123"
    assert record.origin.ref == "app/org.mozilla.firefox/x86_64/stable"
    assert record.origin.remote_url == "https://dl.flathub.org/repo/"
    assert record.origin.confidence == Confidence.CONFIRMED
    # El repositorio de empaquetado NO se presenta como el del desarrollador.
    assert record.upstream.packaging_repository == "flathub/org.mozilla.firefox"
    assert record.upstream.repository == ""
    assert record.upstream.confidence == Confidence.UNKNOWN


def test_flatpak_remote_sin_firma_genera_aviso():
    runner = FakeRunner(
        programs={"flatpak"},
        outputs={
            (
                "flatpak",
                "list",
                "--app",
                "--columns=application,version,branch,arch,origin,installation",
            ): "com.ejemplo.App\t1.0\tstable\tx86_64\tcasero\tuser\n",
            ("flatpak", "remotes", "--columns=name,url,options"): (
                "casero\thttps://ejemplo.local/repo/\tno-gpg-verify\n"
            ),
            ("flatpak", "info", "--show-commit", "com.ejemplo.App"): "deadbeef\n",
            ("flatpak", "info", "--show-ref", "com.ejemplo.App"): "",
        },
    )
    record = FlatpakDetector(runner).detect()[0]
    assert record.origin.signed is False
    assert any("firma" in warning.lower() for warning in record.warnings)


# --------------------------------------------------------------------------
# Snap, pacman, RPM
# --------------------------------------------------------------------------


def test_snap_detector_registra_revision_y_canal():
    runner = FakeRunner(
        programs={"snap"},
        outputs={
            ("snap", "list", "--color=never", "--unicode=never"): (
                "Name      Version  Rev   Tracking       Publisher   Notes\n"
                "spotify   1.2.26   78    latest/stable  spotify**   -\n"
                "code      1.90.0   160   latest/stable  vscode**    classic\n"
            )
        },
    )
    records = {record.name: record for record in SnapDetector(runner).detect()}
    assert records["spotify"].origin.commit == "78"
    assert records["spotify"].origin.channel == "stable"
    assert records["spotify"].origin.confidence == Confidence.CONFIRMED
    assert any("clásico" in warning for warning in records["code"].warnings)


def test_pacman_detector_marca_paquetes_foraneos(tmp_path):
    info = (
        "Name            : krita\n"
        "Version         : 5.2.2-1\n"
        "Architecture    : x86_64\n"
        "URL             : https://krita.org\n"
        "Packager        : Arch Linux\n"
        "Validated By    : Signature\n"
        "\n"
        "Name            : spotify\n"
        "Version         : 1.2.26-1\n"
        "Architecture    : x86_64\n"
        "URL             : https://spotify.com\n"
        "Packager        : Unknown Packager\n"
        "Validated By    : None\n"
    )
    runner = FakeRunner(
        programs={"pacman"},
        outputs={
            ("pacman", "-Qi"): info,
            ("pacman", "-Sl"): "extra krita 5.2.2-1 [installed]\ncore bash 5.2 [installed]\n",
            ("pacman", "-Qm"): "spotify 1.2.26-1\n",
        },
    )
    records = {record.name: record for record in PacmanDetector(runner, cache_dir=tmp_path).detect()}

    assert records["krita"].origin.remote_name == "extra"
    assert records["krita"].origin.confidence == Confidence.CONFIRMED
    assert records["krita"].origin.signed is True

    spotify = records["spotify"]
    assert spotify.origin.confidence == Confidence.UNKNOWN
    assert spotify.install_method == "manual"
    assert spotify.reproducible_today is False


def test_parse_pacman_info_ignora_lineas_continuadas():
    entries = parse_pacman_info(
        "Name            : bash\nDescription     : GNU shell\n                  segunda línea\n"
    )
    assert entries[0]["name"] == "bash"


def test_rpm_detector_usa_dnf_para_el_repositorio():
    from styler.provenance.detectors.rpm import RPM_FORMAT

    runner = FakeRunner(
        programs={"rpm", "dnf"},
        outputs={
            ("rpm", "-qa", "--qf", RPM_FORMAT): (
                "krita\t5.2.2-1.fc40\tx86_64\thttps://krita.org\tFedora\t"
                "krita-5.2.2-1.fc40.src.rpm\tFedora\tfirmado\n"
                "casero\t1.0-1\tx86_64\t\t\t\t\tsin-firma\n"
            ),
            (
                "dnf",
                "repoquery",
                "--installed",
                "--qf",
                "%{name}\t%{from_repo}",
            ): "krita\tfedora\ncasero\t@System\n",
        },
    )
    records = {record.name: record for record in RpmDetector(runner).detect()}
    assert records["krita"].origin.remote_name == "fedora"
    assert records["krita"].origin.confidence == Confidence.CONFIRMED
    assert records["casero"].origin.confidence == Confidence.UNKNOWN
    assert records["casero"].integrity.signature_verified is False


# --------------------------------------------------------------------------
# AppImage
# --------------------------------------------------------------------------


def _fake_elf(update_info: bytes) -> bytes:
    names = b"\x00.shstrtab\x00.upd_info\x00"
    shstr_off = 64 + 3 * 64
    upd_off = shstr_off + len(names)

    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # 64 bits
    header[5] = 1  # little endian
    struct.pack_into("<Q", header, 0x28, 64)      # e_shoff
    struct.pack_into("<H", header, 0x3A, 64)      # e_shentsize
    struct.pack_into("<H", header, 0x3C, 3)       # e_shnum
    struct.pack_into("<H", header, 0x3E, 1)       # e_shstrndx

    def section(name_off: int, offset: int, size: int) -> bytes:
        entry = bytearray(64)
        struct.pack_into("<I", entry, 0x00, name_off)
        struct.pack_into("<Q", entry, 0x18, offset)
        struct.pack_into("<Q", entry, 0x20, size)
        return bytes(entry)

    table = (
        section(0, 0, 0)
        + section(1, shstr_off, len(names))
        + section(11, upd_off, len(update_info))
    )
    return bytes(header) + table + names + update_info


def test_read_update_information_sin_ejecutar_el_binario(tmp_path):
    path = tmp_path / "Nextcloud-3.13.0-x86_64.AppImage"
    path.write_bytes(
        _fake_elf(b"gh-releases-zsync|nextcloud|desktop|latest|Nextcloud-*.AppImage.zsync\x00")
    )
    assert read_update_information(path).startswith("gh-releases-zsync|nextcloud|desktop")


def test_appimage_detector_registra_artefacto_y_upstream(tmp_path):
    path = tmp_path / "Nextcloud-3.13.0-x86_64.AppImage"
    path.write_bytes(
        _fake_elf(b"gh-releases-zsync|nextcloud|desktop|latest|Nextcloud-*.AppImage.zsync\x00")
    )
    record = AppImageDetector(FakeRunner(), search_dirs=[tmp_path]).detect()[0]

    assert record.app_id == "appimage:Nextcloud"
    assert record.version == "3.13.0"
    assert record.upstream.repository == "nextcloud/desktop"
    assert record.upstream.confidence == Confidence.CONFIRMED
    assert record.integrity.checksum.startswith("sha256:")
    assert record.integrity.artifact_available is True
    assert record.reproducible_today is True


def test_appimage_sin_update_information_avisa(tmp_path):
    path = tmp_path / "Misterio-1.0.AppImage"
    path.write_bytes(_fake_elf(b"\x00"))
    record = AppImageDetector(FakeRunner(), search_dirs=[tmp_path]).detect()[0]
    assert record.upstream.confidence == Confidence.UNKNOWN
    assert record.warnings
    # El archivo existe, así que sí se puede reinstalar tal cual.
    assert record.reproducible_today is True


def test_split_name_version():
    assert split_name_version("Krita-5.2.2-x86_64") == ("Krita", "5.2.2")
    assert split_name_version("herramienta") == ("herramienta", "")


# --------------------------------------------------------------------------
# Inventario, persistencia y reporte
# --------------------------------------------------------------------------


class _StubDetector:
    name = "stub"
    manager = "stub"
    problems: list[str] = []

    def __init__(self, records: list[ApplicationRecord]) -> None:
        self.records = records
        self.problems = []

    def applies(self) -> bool:
        return True

    def detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        return self.records


class _BrokenDetector(_StubDetector):
    name = "roto"
    manager = "roto"

    def detect(self, scope: str = "apps") -> list[ApplicationRecord]:
        self.problems.append("roto: el gestor no respondió")
        return []


def _record(app_id: str, confidence: Confidence, remote: str = "flathub") -> ApplicationRecord:
    return ApplicationRecord(
        app_id=app_id,
        name=app_id.split(":", 1)[1],
        manager=app_id.split(":", 1)[0],
        version="1.0",
        origin=Origin(
            kind=OriginKind.FLATPAK,
            remote_name=remote,
            confidence=confidence,
        ),
    )


def test_scan_aisla_un_gestor_roto_y_deduplica():
    bueno = _record("flatpak:a", Confidence.CONFIRMED)
    duplicado_debil = _record("flatpak:a", Confidence.UNKNOWN, remote="")
    inventory, problems = inventory_mod.scan(
        detectors=[_StubDetector([duplicado_debil, bueno]), _BrokenDetector([])]
    )
    assert len(inventory.applications) == 1
    assert inventory.applications[0].origin.confidence == Confidence.CONFIRMED
    assert problems == ["roto: el gestor no respondió"]


def test_guardar_y_recuperar_inventario(tmp_path):
    inventory, _ = inventory_mod.scan(
        detectors=[_StubDetector([_record("flatpak:a", Confidence.CONFIRMED)])]
    )
    inventory_mod.save_inventory(inventory, root=tmp_path)

    recuperado = inventory_mod.latest_inventory(root=tmp_path)
    assert recuperado is not None
    assert recuperado.inventory_id == inventory.inventory_id
    assert recuperado.applications[0].app_id == "flatpak:a"
    assert inventory_mod.list_inventories(root=tmp_path) == [inventory.inventory_id]


def test_inventario_rechaza_esquema_desconocido():
    with pytest.raises(ValueError):
        Inventory.from_dict({"schema": "otra-cosa/9", "inventory_id": "x"})


def test_needs_attention_solo_lista_lo_irrecuperable():
    inventory, _ = inventory_mod.scan(
        detectors=[
            _StubDetector(
                [
                    _record("flatpak:seguro", Confidence.CONFIRMED),
                    _record("flatpak:riesgo", Confidence.UNKNOWN, remote=""),
                ]
            )
        ]
    )
    riesgos = [record.app_id for record in inventory.needs_attention()]
    assert riesgos == ["flatpak:riesgo"]

    texto = report_mod.full_report(inventory)
    assert "flatpak:riesgo" in texto
    assert "Aplicaciones registradas: 2" in texto


def test_scan_rechaza_alcance_invalido():
    with pytest.raises(inventory_mod.ProvenanceError):
        inventory_mod.scan(scope="todo")


# --------------------------------------------------------------------------
# Servicio de interfaz
# --------------------------------------------------------------------------


def test_servicio_de_interfaz_sin_catalogo_pide_analizar(tmp_path):
    service = ProvenanceService(root=str(tmp_path))
    assert service.latest() is None
    with pytest.raises(Exception) as error:
        service.report()
    assert "análisis" in str(error.value)


def test_servicio_de_interfaz_traduce_registros(tmp_path, monkeypatch):
    inventory, _ = inventory_mod.scan(
        detectors=[
            _StubDetector(
                [
                    _record("flatpak:seguro", Confidence.CONFIRMED),
                    _record("flatpak:riesgo", Confidence.UNKNOWN, remote=""),
                ]
            )
        ]
    )
    inventory_mod.save_inventory(inventory, root=tmp_path)

    service = ProvenanceService(root=str(tmp_path))
    view = service.latest()
    assert view is not None
    assert view.total == 2
    assert len(view.at_risk) == 1
    assert view.at_risk[0].status_line.startswith("Sin forma")
    assert "Origen de la aplicación" in service.detail("flatpak:seguro")

    destination = Path(tmp_path) / "salida" / "catalogo.json"
    assert service.export(destination) == str(destination)
    assert destination.is_file()


def test_appimage_detector_finds_nested_files(tmp_path):
    nested = tmp_path / "downloads" / "graphics"
    nested.mkdir(parents=True)
    app = nested / "Krita-5.2.AppImage"
    app.write_bytes(b"not-an-elf-but-still-an-appimage")

    detector = AppImageDetector(search_dirs=[tmp_path], max_depth=4)
    records = detector.detect()
    assert [record.name for record in records] == ["Krita"]
    assert records[0].integrity.artifact_path == str(app)
