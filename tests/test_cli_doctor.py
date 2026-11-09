import json
from pathlib import Path

import pytest

from pap_analytics import cli


def test_doctor_json(resmed_air11: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["doctor", str(resmed_air11), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["device"]["model"] == "AirSense11AutoSet" and rep["device"]["model_code"] == "39444"
    assert rep["days"] == 21 and rep["sessions"] == 43
    assert rep["oximetry_days"] == [
        "2026-10-16", "2026-10-17", "2026-10-19", "2026-10-20", "2026-10-21", "2026-11-03", "2026-11-08",
    ]  # fmt: skip
    assert rep["str_range"] == ["2026-11-08", "2026-11-08"]
    assert rep["channels_by_file_type"]["SA2"] == ["pulse", "spo2"]


def test_doctor_text_and_unknown(
    resmed_air11: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor", str(resmed_air11)]) == 0
    assert "AirSense11AutoSet" in capsys.readouterr().out
    assert cli.main(["doctor", str(tmp_path)]) == 2
