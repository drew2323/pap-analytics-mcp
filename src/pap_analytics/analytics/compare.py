"""Compare two periods by the means of key nightly metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pap_analytics.analytics.results import Comparison, NightRow
from pap_analytics.model import Metric


def _best_ahi(r: NightRow) -> Metric:
    return r.ahi_device if r.ahi_device is not None and r.ahi_device.value is not None else r.ahi_derived


def _vals(rows: Sequence[NightRow]) -> dict[str, tuple[list[float], str]]:
    def pick(getter, unit):  # noqa: ANN001, ANN202
        out = []
        for r in rows:
            m = getter(r)
            if m is not None and m.value is not None:
                out.append(m.value)
        return out, unit

    return {
        "usage_min": pick(lambda r: r.usage_min, "min"),
        "ahi": pick(_best_ahi, "1/h"),
        "ahi_derived": pick(lambda r: r.ahi_derived, "1/h"),
        "leak_p95": pick(lambda r: r.leak_p95, "L/s"),
        "pressure_p95": pick(lambda r: r.pressure_p95, "cmH2O"),
    }  # fmt: skip


def _extra(rows: Sequence[Mapping[str, Any]], units: Mapping[str, str]) -> dict[str, tuple[list[float], str]]:
    return {k: ([r[k] for r in rows if r.get(k) is not None], u) for k, u in units.items()}

def compare_periods(
    a: Sequence[NightRow], b: Sequence[NightRow],
    extra_a: Sequence[Mapping[str, Any]] = (), extra_b: Sequence[Mapping[str, Any]] = (),
    extra_units: Mapping[str, str] | None = None,
) -> Comparison:  # fmt: skip
    """Means per period, ``delta`` = b − a, ``delta_pct`` relative to a (None when a is 0/missing).

    ``extra_a``/``extra_b`` are further nightly rows (e.g. ``disturbance_trend`` nights) whose keys
    listed in ``extra_units`` (key -> unit) are compared the same way."""
    va, vb = _vals(a), _vals(b)
    if extra_units:
        va |= _extra(extra_a, extra_units)
        vb |= _extra(extra_b, extra_units)
    metrics: dict[str, dict[str, object]] = {}
    for key, (xs, unit) in va.items():
        ys = vb[key][0]
        ma = sum(xs) / len(xs) if xs else None
        mb = sum(ys) / len(ys) if ys else None
        delta = mb - ma if ma is not None and mb is not None else None
        pct = delta / ma * 100 if delta is not None and ma else None
        metrics[key] = {"unit": unit, "a_mean": ma, "b_mean": mb, "a_n": len(xs), "b_n": len(ys),
                        "delta": delta, "delta_pct": pct}  # fmt: skip
    notes = []
    if not a or not b:
        notes.append("one of the periods has no nights")
    return Comparison(len(a), len(b), metrics, notes)
