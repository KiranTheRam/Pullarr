"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title (Year)}/{Series Title} #012.cbz
Templates use Python format-spec style with {series}, {year}, {issue}, {title}."""

import logging
import re
from collections.abc import Callable
from functools import lru_cache
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..util import NAME_MAX_BYTES, sanitize_filename, shorten_utf8, truncate_utf8

log = logging.getLogger(__name__)

# Metadata refresh uses the longest temporary suffix of all archive writers.
_ATOMIC_SUFFIX = ".metadata-partial"

DEFAULT_TEMPLATE = "{series} #{issue:03d}"

_ISSUE_FMT = re.compile(r"\{issue:0(\d+)d?\}")


def _format_issue(template: str, issue: float | str) -> str:
    """Renders {issue:03d} as zero-padded but keeping half-issues readable:
    12 → 012, 12.5 → 012.5"""

    def repl(m: re.Match) -> str:
        try:
            numeric = Decimal(str(issue).strip())
        except InvalidOperation:
            return str(issue)  # annuals and variant labels remain readable
        if not numeric.is_finite():
            return str(issue)
        width = int(m.group(1))
        if numeric == numeric.to_integral_value():
            return f"{int(numeric):0{width}d}"
        whole, fraction = format(numeric.normalize(), "f").split(".")
        return f"{whole.zfill(width)}.{fraction}"

    return _ISSUE_FMT.sub(repl, template)


def _finish(render: Callable[[str, str], str], series: str, title: str, ext: str) -> str | None:
    """Fit fields around an intact issue/volume label and archive extension.

    Titles give way before series names. Shortened series retain a digest,
    even when a custom template leaves room only for the digest itself.
    """
    budget = NAME_MAX_BYTES - len((ext + _ATOMIC_SUFFIX).encode("utf-8"))
    fields = {"series": sanitize_filename(series) if series else "", "title": title}

    def build() -> str:
        rendered = render(fields["series"], fields["title"])
        # Sanitize without cutting the finished name: that would lose numbers.
        return sanitize_filename(rendered, len(rendered.encode("utf-8")) + 20)

    def fits() -> bool:
        return len(build().encode("utf-8")) <= budget

    for key, cut in (("title", truncate_utf8), ("series", shorten_utf8)):
        whole = fields[key]
        if fits() or not whole:
            continue
        # Never silently discard the identity of a nonempty series to fit
        # template literals. Falling back to the default frees that space.
        minimum = 18 if key == "series" else 0
        fields[key] = cut(whole, minimum)
        if not fits():
            if key == "series":
                return None
            continue
        keep, drop = minimum, len(whole.encode("utf-8"))
        while drop - keep > 1:
            middle = (keep + drop) // 2
            fields[key] = cut(whole, middle)
            keep, drop = (middle, drop) if fits() else (keep, middle)
        fields[key] = cut(whole, keep)
    return build() + ext if fits() else None


@lru_cache(maxsize=128)
def _warn_long_template(template: str) -> None:
    log.warning("Naming template %r is too long for a filename; using %r", template, DEFAULT_TEMPLATE)


def _issue_render(template: str, issue: float | str, year: int | None) -> Callable[[str, str], str]:
    chosen = _format_issue(template, issue)
    return lambda series, title: chosen.format(
        series=series, issue=issue, title=title, year=year if year is not None else "",
    )


def _numbered_filename(render: Callable[[str, str], str], series: str, ext: str) -> str:
    name = _finish(render, series, "", ext)
    if name is not None:
        return name
    # Pathological issue labels/volume numbers can exceed an entire component.
    # Keep them distinct with a digest rather than failing filesystem writes.
    budget = NAME_MAX_BYTES - len((ext + _ATOMIC_SUFFIX).encode("utf-8"))
    return sanitize_filename(render(series, ""), budget) + ext


def issue_filename(
    template: str,
    series_title: str,
    issue: float | str,
    title: str = "",
    year: int | None = None,
    ext: str = ".cbz",
) -> str:
    render = _issue_render(template, issue, year)
    name = _finish(render, series_title, title, ext)
    if name is not None:
        return name
    _warn_long_template(template)
    return _numbered_filename(_issue_render(DEFAULT_TEMPLATE, issue, year), series_title, ext)


def volume_filename(series_title: str, volume: int, ext: str = ".cbz") -> str:
    """Name for a collected-volume (TPB) archive."""
    return _numbered_filename(lambda series, _: f"{series} Vol. {volume:02d}", series_title, ext)


def range_filename(series_title: str, lo: float, hi: float, year: int | None = None,
                   ext: str = ".cbz") -> str:
    """Name for a multi-issue bundle file, e.g. 'Series #001-003.cbz'."""
    label = f"#{_format_issue('{issue:03d}', lo)}-{_format_issue('{issue:03d}', hi)}"
    return _numbered_filename(lambda series, _: f"{series} {label}", series_title, ext)


def volume_range_filename(series_title: str, lo: int, hi: int, ext: str = ".cbz") -> str:
    return _numbered_filename(lambda series, _: f"{series} Vol. {lo:02d}-{hi:02d}", series_title, ext)


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
