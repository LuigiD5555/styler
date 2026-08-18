Name:           styler
Version:        0.13.3
Release:        1%{?dist}
Summary:        Integrate semantic, reproducible changes on Linux
License:        Apache-2.0
Source0:        %{name}-%{version}.tar.gz
BuildArch:      x86_64

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
install -Dpm 0755 runtime/pipecraft/linux-x86_64/pipecraft \
  %{buildroot}%{_prefix}/libexec/styler/pipecraft

%check
PYTHONPATH=. python3 -m pytest -q

%post

%files
%license LICENSE NOTICE
%doc README.md docs/STYLER.md
%{_bindir}/styler
%{_prefix}/libexec/styler/pipecraft
%{python3_sitelib}/styler/
%{python3_sitelib}/styler_linux-*.dist-info/
%{_datadir}/applications/styler.desktop
%{_datadir}/mime/packages/styler-package.xml
%{_mandir}/man1/styler.1*

%changelog
* Tue Aug 18 2026 Styler contributors <noreply@example.invalid> - 0.13.3-1
- Track verified PipeCraft runtime in source distributions.

* Mon Aug 10 2026 Styler contributors <noreply@example.invalid> - 0.13.1-1
- Actualiza Styler y separa el runtime PipeCraft.
