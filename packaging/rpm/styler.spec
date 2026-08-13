Name:           styler
Version:        0.9.10
Release:        1%{?dist}
Summary:        Integrate semantic, reproducible changes on Linux
License:        Apache-2.0
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  python3-build
BuildRequires:  python3-installer
BuildRequires:  python3-pytest
BuildRequires:  python3-pytest-asyncio
BuildRequires:  python3dist(textual) >= 0.89.1
BuildRequires:  python3dist(pyyaml) >= 6
Requires:       python3 >= 3.10
Requires:       python3dist(textual) >= 0.89.1
Requires:       python3dist(pyyaml) >= 6
Recommends:     kdialog
Recommends:     zenity
Recommends:     python3-pyatspi

%description
Styler saves desktop configuration as reusable profiles, previews the files
that will change, imports and exports .stylerpkg packages, applies
selected configuration and keeps a journal so the operation can be undone.

%prep
%autosetup

%build
python3 -m build --wheel --no-isolation

%install
python3 -m installer --destdir=%{buildroot} dist/*.whl
install -Dpm 0644 packaging/linux/styler.desktop \
  %{buildroot}%{_datadir}/applications/styler.desktop
install -Dpm 0644 packaging/linux/styler-package.xml \
  %{buildroot}%{_datadir}/mime/packages/styler-package.xml
install -Dpm 0644 docs/styler.1 \
  %{buildroot}%{_mandir}/man1/styler.1

%check
PYTHONPATH=. python3 -m pytest -q

%post

%files
%license LICENSE NOTICE
%doc README.md docs/STYLER.md
%{_bindir}/styler
%{python3_sitelib}/styler/
%{python3_sitelib}/styler_linux-*.dist-info/
%{_datadir}/applications/styler.desktop
%{_datadir}/mime/packages/styler-package.xml
%{_mandir}/man1/styler.1*

%changelog
* Mon Aug 10 2026 Styler contributors <noreply@example.invalid> - 0.9.10-1
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
