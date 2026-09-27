"""Per-series defaults for newly discovered issues.

Existing issue flags are changed only when the user explicitly changes the
series mode or its broad monitoring switch. Routine metadata refreshes retain
individual issue choices.
"""

from collections.abc import Iterable

from .models import Issue, Series


def issue_wanted(series: Series, number: float) -> bool:
    if not series.monitored:
        return False
    if series.monitor_mode == "future":
        return series.monitor_from is not None and number > series.monitor_from
    if series.monitor_mode == "from_issue":
        return series.monitor_from is not None and number >= series.monitor_from
    return True


def apply_mode(series: Series, issues: Iterable[Issue]) -> None:
    issues = list(issues)
    if series.monitor_mode == "future":
        series.monitor_from = max((i.number for i in issues), default=None)
    elif series.monitor_mode == "all":
        series.monitor_from = None
    for issue in issues:
        issue.monitored = issue_wanted(series, issue.number)


def resolve_initial_future(series: Series, issues: Iterable[Issue]) -> None:
    if series.monitor_mode == "future" and series.monitor_from is None:
        apply_mode(series, issues)
