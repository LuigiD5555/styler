from __future__ import annotations

import json
from pathlib import Path

from styler import system_registry as registry


def _snapshot(snapshot_id: str, items: list[registry.RegistryItem], phase: str = "test") -> registry.RegistrySnapshot:
    return registry.RegistrySnapshot(
        snapshot_id=snapshot_id,
        captured_at=1.0 if snapshot_id == "before" else 2.0,
        phase=phase,
        system_only=False,
        system={"distro_id": "test"},
        managers_seen=["flatpak"],
        items=items,
    )


def test_user_scan_is_allowlisted_and_does_not_walk_personal_content(tmp_path: Path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents/secret.txt").write_text("private")
    (home / ".config").mkdir()
    (home / ".config/kdeglobals").write_text("[General]\nColorScheme=BreezeDark\n")
    gimp = home / ".var/app/org.gimp.GIMP/config/GIMP/3.2"
    gimp.mkdir(parents=True)
    (gimp / "gimprc").write_text("photogimp")
    (gimp / "sessionrc").write_text("/home/user/private-photo.xcf")
    launchers = home / ".local/share/applications"
    launchers.mkdir(parents=True)
    (launchers / "org.gimp.GIMP.desktop").write_text(
        "[Desktop Entry]\nName=PhotoGIMP\nIcon=photogimp\nExec=flatpak run org.gimp.GIMP\n"
    )

    items, problems = registry._user_configuration_items(home)
    paths = {item.path for item in items}
    ids = {item.item_id for item in items}

    assert "${HOME}/.config/kdeglobals" in paths
    assert "${HOME}/.var/app/org.gimp.GIMP/config/GIMP/3.2/gimprc" in paths
    assert not any("Documents" in path or "secret.txt" in path for path in paths)
    assert not any("sessionrc" in path for path in paths)
    assert "customization:photogimp" in ids
    assert problems == []


def test_snapshot_diff_keeps_each_change_as_an_individual_event():
    old = registry.RegistryItem(
        item_id="flatpak:org.gimp.GIMP",
        kind="installed_application",
        name="GIMP",
        source="package-manager",
        version="3.0",
        manager="flatpak",
        exportable=True,
    )
    new = registry.RegistryItem(
        item_id="flatpak:org.gimp.GIMP",
        kind="installed_application",
        name="GIMP",
        source="package-manager",
        version="3.2",
        manager="flatpak",
        exportable=True,
    )
    photogimp = registry.RegistryItem(
        item_id="customization:photogimp",
        kind="application_customization",
        name="PhotoGIMP",
        source="semantic-detection",
        exportable=True,
    )

    events = registry.compare_snapshots(
        _snapshot("before", [old]),
        _snapshot("after", [new, photogimp]),
        session="PhotoGIMP",
    )

    assert [(event.change, event.item_id) for event in events] == [
        ("added", "customization:photogimp"),
        ("changed", "flatpak:org.gimp.GIMP"),
    ]
    assert all(event.exportable for event in events)


def test_install_pre_and_post_create_baseline_and_event_log(monkeypatch, tmp_path: Path):
    gimp = registry.RegistryItem(
        item_id="flatpak:org.gimp.GIMP",
        kind="installed_application",
        name="GIMP",
        source="package-manager",
        version="3.2",
        manager="flatpak",
        exportable=True,
    )
    before = _snapshot("before", [], "install-pre")
    after = _snapshot("after", [gimp], "install-post")
    snapshots = iter([(before, None), (after, None)])
    monkeypatch.setattr(registry, "capture_snapshot", lambda **_kwargs: next(snapshots))

    registry.install_pre(root=tmp_path, home=tmp_path / "home")
    update = registry.install_post(root=tmp_path, home=tmp_path / "home")

    assert update.baseline_created is True
    assert [event.item_id for event in update.events] == ["flatpak:org.gimp.GIMP"]
    assert registry.load_pointer(tmp_path, registry.BASELINE_POINTER).snapshot_id == "after"
    assert registry.load_pointer(tmp_path, registry.CURRENT_POINTER).snapshot_id == "after"
    lines = (tmp_path / "registry/events.jsonl").read_text().splitlines()
    payloads = [json.loads(line) for line in lines]
    assert payloads[-1]["session"] == "styler-installation"
    assert payloads[-1]["change"] == "added"


def test_packaging_hooks_do_not_call_removed_registry_cli():
    root = Path(__file__).resolve().parents[1]
    source_installer = (root / "install.sh").read_text(encoding="utf-8")
    assert "styler.system_registry install-pre" in source_installer
    assert "registry-install-post" in source_installer

    lifecycle_hooks = [
        root / "debian/styler.postinst",
        root / "packaging/arch/styler.install",
        root / "packaging/release/arch/styler.install",
    ]
    rpm_specs = [
        root / "packaging/rpm/styler.spec",
        root / "packaging/release/rpm/styler-portable.spec",
    ]
    for hook in [*lifecycle_hooks, *rpm_specs]:
        assert "registry-bootstrap" not in hook.read_text(encoding="utf-8")
    for hook in lifecycle_hooks:
        assert "update-mime-database" in hook.read_text(encoding="utf-8")
    for spec in rpm_specs:
        assert "styler-package.xml" in spec.read_text(encoding="utf-8")
