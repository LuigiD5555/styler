Name:           styler
Version:        0.13.3
Release:        1%{?dist}
Summary:        Integrate semantic, reproducible changes on Linux
License:        Apache-2.0
BuildArch:      x86_64
Source0:        styler.pyz
Source1:        styler.desktop
Source2:        styler-package.xml
Source3:        styler.1
Source4:        LICENSE
Source5:        NOTICE
Source6:        README.md
Source7:        STYLER.md
Source8:        pipecraft
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
install -Dpm 0755 %{SOURCE8} %{buildroot}%{_prefix}/libexec/styler/pipecraft
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
%{_prefix}/libexec/styler/pipecraft
%{_prefix}/lib/styler/styler.pyz
%{_datadir}/applications/styler.desktop
%{_datadir}/mime/packages/styler-package.xml
%{_mandir}/man1/styler.1*

%changelog
* Tue Aug 18 2026 Styler contributors <noreply@example.invalid> - 0.13.3-1
- Track verified PipeCraft runtime in source distributions.

* Mon Aug 10 2026 Styler contributors <noreply@example.invalid> - 0.13.1-1
- Actualiza Styler y separa el runtime PipeCraft.
