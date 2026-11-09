from pathlib import Path

from pap_analytics import cli


def test_cli_runs() -> None:
    assert cli.main([]) == 0


def test_real_fixture_present(resmed_air11: Path, resmed_air11_str_history: Path) -> None:
    assert (resmed_air11 / "STR.edf").is_file()
    assert (resmed_air11 / "Identification.json").is_file()
    assert len(list((resmed_air11 / "DATALOG").iterdir())) == 21
    assert (resmed_air11_str_history / "STR.edf").is_file()
