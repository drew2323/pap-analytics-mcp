"""Pure analytics over the store. Never imports adapters or vendor code."""

from pap_analytics.analytics.anomalies import Anomaly, find_anomalies
from pap_analytics.analytics.compare import compare_periods
from pap_analytics.analytics.events import event_clusters, get_events
from pap_analytics.analytics.nights import list_nights, night_summary
from pap_analytics.analytics.results import AnalyticsError, to_dict
from pap_analytics.analytics.segments import downsample, get_signal, segment_stats
from pap_analytics.analytics.settings import settings_history
from pap_analytics.analytics.timeparse import Context, parse_day, parse_time

__all__ = [
    "AnalyticsError", "Anomaly", "Context", "compare_periods", "downsample", "event_clusters",
    "find_anomalies", "get_events",
    "get_signal", "list_nights", "night_summary", "parse_day", "parse_time", "segment_stats",
    "settings_history", "to_dict",
]  # fmt: skip
