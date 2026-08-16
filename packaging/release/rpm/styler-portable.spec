Name:           styler
Version:        0.10.0-alpha.1
Release:        1%{?dist}
Summary:        Integrate semantic, reproducible changes on Linux
License:        Apache-2.0
BuildArch:      noarch
Source0:        styler.pyz
Source1:        styler.desktop
Source2:        styler-package.xml
Source3:        styler.1
Source4:        LICENSE
Source5:        NOTICE
Source6:        README.md
Source7:        STYLER.md
Requires:       python3 >= 3.10
Recommends:     kdialog
Recommends:     zenity
Recommends:     python3-pyatspi

%description
Styler stores reusable desktop profiles and portable automation packages,
previews changes, imports .stylerpkg files, applies approved
configuration and records rollback data.
This release package includes the Python application dependencies required by
its terminal interface.

%prep

%build

%install
install -Dpm 0755 %{SOURCE0} %{buildroot}%{_prefix}/lib/styler/styler.pyz
mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/styler <<'SH'
#!/bin/sh
exec python3 /usr/lib/styler/styler.pyz "$@"
SH
chmod 0755 %{buildroot}%{_bindir}/styler
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_datadir}/applications/styler.desktop
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_datadir}/mime/packages/styler-package.xml
install -Dpm 0644 %{SOURCE3} %{buildroot}%{_mandir}/man1/styler.1

%post
command -v update-mime-database >/dev/null 2>&1 && update-mime-database %{_datadir}/mime || :
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database %{_datadir}/applications || :

%postun
command -v update-mime-database >/dev/null 2>&1 && update-mime-database %{_datadir}/mime || :
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database %{_datadir}/applications || :

%files
%license %{SOURCE4} %{SOURCE5}
%doc %{SOURCE6} %{SOURCE7}
%{_bindir}/styler
%{_prefix}/lib/styler/styler.pyz
%{_datadir}/applications/styler.desktop
%{_datadir}/mime/packages/styler-package.xml
%{_mandir}/man1/styler.1*

%changelog
* Mon Aug 10 2026 Styler contributors <noreply@example.invalid> - 0.9.11-1
- Refleja visualmente la selección múltiple de Cambios.

* Mon Aug 10 2026 Styler contributors <noreply@example.invalid> - 0.9.7-1
- Build from a sanitized temporary source tree and ship readable baseline package data.

* Mon Aug 10 2026 Styler contributors <noreply@example.invalid> - 0.9.0-1
- YAML declarative AppImageLauncher/Affinity changes and generic AppImage primitives.
* Sun Aug 09 2026 Styler contributors <noreply@example.invalid> - 0.8.3-1
- Baselines oficiales seleccionadas por identidad exacta de distro/plataforma; sin fallback global.
- Linux Mint 22.3 XFCE X11 stable x86_64 usa su baseline propia.

* Sun Aug 09 2026 Styler contributors <noreply@example.invalid> - 0.8.0-1
- Bundle Linux Mint 22.3 XFCE x86_64 baseline as the default compatible baseline.
* Sat Aug 08 2026 Styler contributors <noreply@example.invalid> - 0.7.6-1
- Surface imported .stylerpkg DAGs in the unified Changes catalog.
- Keep PhotoGIMP DAG execution behavior unchanged.
- Remove the separate package plan/run application path.
* Sat Aug 08 2026 Styler contributors <noreply@example.invalid> - 0.7.3-1
- Normalize human package names to safe internal identifiers during authoring.
- Keep imported .stylerpkg identifier validation strict.
* Thu Aug 06 2026 Styler contributors <noreply@example.invalid> - 0.7.2-1
- Fix mouse selection crash in Constructor rows.
- Add explicit official baseline catalog candidate export.
* Thu Aug 06 2026 Styler contributors <noreply@example.invalid> - 0.7.1-1
- Guided four-step Change Constructor and honest omission reporting.

* Thu Aug 06 2026 Styler contributors <noreply@example.invalid> - 0.7.0-1
- Unify Herramientas in the Change Constructor.
- Use .stylerpkg as the only portable format.
- Generate a semantic recipe and deterministic DAG from detected changes.
