from styler.applications import apt_install_argv, apt_update_argv


def test_apt_install_waits_for_dpkg_lock():
    argv = apt_install_argv(["sudo", "-n"], "kde-plasma-desktop")
    assert "DPkg::Lock::Timeout=300" in argv


def test_apt_update_waits_for_dpkg_lock():
    argv = apt_update_argv(["sudo", "-n"])
    assert "DPkg::Lock::Timeout=300" in argv
