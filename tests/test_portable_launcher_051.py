from styler import launcher


def test_current_command_families_route_to_cli_before_tui_parser(monkeypatch):
    calls = []

    def fake_cli(argv):
        calls.append(list(argv))
        return 17

    monkeypatch.setattr("styler.cli.main", fake_cli)
    for command in ("change", "baseline", "constructor", "package"):
        assert launcher.main([command, "--help"]) == 17
    assert calls == [
        ["change", "--help"],
        ["baseline", "--help"],
        ["constructor", "--help"],
        ["package", "--help"],
    ]


def test_removed_public_command_families_are_not_routed(monkeypatch):
    calls = []

    def fake_cli(argv):
        calls.append(list(argv))
        return 17

    monkeypatch.setattr("styler.cli.main", fake_cli)
    for command in ("automation", "macro", "profile"):
        try:
            launcher.main([command, "--help"])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"{command} todavía fue aceptado como comando público")
    assert calls == []
