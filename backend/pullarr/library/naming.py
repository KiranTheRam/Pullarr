"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title (Year)}/{Series Title} #012.cbz
Templates use Python format-spec style with {series}, {year}, {issue}, {title}."""

import re
from pathlib import Path

from ..util import sanitize_filename

DEFAULT_TEMPLATE = "{series} #{issue:03d}"

_ISSUE_FMT = re.compile(r"\{issue:0(\d+)d?\}")


def _format_issue(template: str, issue: float) -> str:
    """Renders {issue:03d} as zero-padded but keeping half-issues readable:
    12 → 012, 12.5 → 012.5"""

    def repl(m: re.Match) -> str:
        width = int(m.group(1))
        if float(issue).is_integer():
            return f"{int(issue):0{width}d}"
        return f"{issue:0{width + 2}.1f}"

    return _ISSUE_FMT.sub(repl, template)


def issue_filename(
    template: str,
    series_title: str,
    issue: float,
    title: str = "",
    year: int | None = None,
    ext: str = ".cbz",
) -> str:
    chosen = _format_issue(template, issue)
    name = chosen.format(
        series=series_title,
        issue=issue,
        title=title,
        year=year if year is not None else "",
    )
    return sanitize_filename(name) + ext


def volume_filename(series_title: str, volume: int, ext: str = ".cbz") -> str:
    """Name for a collected-volume (TPB) archive."""
    return sanitize_filename(f"{series_title} Vol. {volume:02d}") + ext


def series_folder(series_title: str, year: int | None = None) -> str:
    """Series directory name; the start year disambiguates the many comic
    series that share a title across reboots (Batman (1940) vs (2016))."""
    if year:
        return sanitize_filename(f"{series_title} ({year})")
    return sanitize_filename(series_title)


def issue_path(
    root: Path,
    template: str,
    series_title: str,
    folder_name: str,
    issue: float,
    title: str = "",
    year: int | None = None,
) -> Path:
    folder = folder_name or series_folder(series_title, year)
    return root / folder / issue_filename(template, series_title, issue, title, year)
