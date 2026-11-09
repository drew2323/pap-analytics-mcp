from pathlib import Path

import pytest

from pap_analytics import cli


def test_cli_runs() -> None:
    assert cli.main([]) == 0


def test_real_fixture_present(resmed_air11: Path, resmed_air11_str_history: Path) -> None:
    assert (resmed_air11 / "STR.edf").is_file()
    assert (resmed_air11 / "Identification.json").is_file()
    assert len(list((resmed_air11 / "DATALOG").iterdir())) == 21
    assert (resmed_air11_str_history / "STR.edf").is_file()


def test_help_lists_global_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--config" in out and "--log-level" in out


def test_registered_subcommand_runs() -> None:
    def register(subparsers: object) -> None:
        p = subparsers.add_parser("hello")  # type: ignore[attr-defined]
        p.set_defaults(func=lambda args, config: 7)

    cli.add_command(register)
    try:
        assert cli.main(["hello"]) == 7
    finally:
        cli._REGISTRARS.remove(register)
