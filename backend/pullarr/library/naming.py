"""File naming for library output. Komga/Kavita-friendly:
  {root}/{Series Title (Year)}/{Series Title} #012.cbz
Templates use Python format-spec style with {series}, {year}, {issue}, {title}."""

import logging
import re
from string import Formatter
from collections.abc import Callable
from functools import lru_cache
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..filename import UNMATCHED_FILENAME_PREFIX, parse_filename
from ..models import Issue, Series
from ..util import NAME_MAX_BYTES, filename_edition_markers, sanitize_filename, shorten_utf8, truncate_utf8

log = logging.getLogger(__name__)

# Metadata refresh uses the longest temporary suffix of all archive writers.
_ATOMIC_SUFFIX = ".metadata-partial"

DEFAULT_TEMPLATE = "{series} #{issue:03d}"

_ISSUE_FMT = re.compile(r"0(\d+)d?")
_FORMATTER = Formatter()


def _pad_issue(issue: float | str, width: int) -> str:
    """Zero-pad the integer part while preserving fractions and edition labels."""
    try:
        numeric = Decimal(str(issue).strip())
    except InvalidOperation:
        return str(issue)
    if not numeric.is_finite():
        return str(issue)
    if numeric == numeric.to_integral_value():
        return f"{int(numeric):0{width}d}"
    whole, fraction = format(numeric.normalize(), "f").split(".")
    return f"{whole.zfill(width)}.{fraction}"


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


def format_with_issue(template: str, *, series: str, issue: float | str,
                      year: int | str | None, title: str = "") -> str:
    # Parse escapes before formatting so literal braces and characters in
    # display numbers never turn into a second round of substitutions.
    values = {"series": series, "issue": issue, "title": title, "year": year if year is not None else ""}
    output = []
    for literal, field, spec, conversion in _FORMATTER.parse(template):
        output.append(literal)
        if field is None:
            continue
        if field == "issue" and conversion is None and (padding := _ISSUE_FMT.fullmatch(spec)):
            output.append(_pad_issue(issue, int(padding[1])))
        else:
            value, _ = _FORMATTER.get_field(field, (), values)
            if "{" in spec or "}" in spec:
                spec = _FORMATTER.vformat(spec, (), values)
            output.append(format(_FORMATTER.convert_field(value, conversion), spec))
    return "".join(output)


def _issue_render(template: str, issue: float | str, year: int | None) -> Callable[[str, str], str]:
    return lambda series, title: format_with_issue(template, series=series, issue=issue, year=year, title=title)


def validate_template(template: str) -> None:
    fields = set()
    for _, field, spec, conversion in _FORMATTER.parse(template):
        if field is None:
            continue
        if field not in {"series", "issue", "title", "year"}:
            raise ValueError("Use only {series}, {issue}, {title} and {year} placeholders")
        fields.add(field)
        if "{" in spec or "}" in spec or any(int(n) > NAME_MAX_BYTES for n in re.findall(r"\d+", spec)):
            raise ValueError("Invalid or oversized format specifier")
        if field == "issue" and (conversion or (spec and not _ISSUE_FMT.fullmatch(spec))):
            raise ValueError("Use {issue} or zero-padding such as {issue:03d} for issue labels")
    if "issue" not in fields:
        raise ValueError("Include {issue} so each issue has its own filename")

    labels = ("1", "12", "12.5", "12.21", "12.25", "78.BEY", "78.BAT", "Annual 1", "½")
    for year in (2024, None):
        for title in ("Issue Title", ""):
            names = [_finish(_issue_render(template, label, year), "Series", title, ".cbz")
                     for label in labels]
            if None in names or any(_finish(_issue_render(template, n, year), "Series", title, ".cbz") is None
                                    for n in (12.0, 12.5)):
                raise ValueError("Rendered filenames must fit within the filename length limit")
            if len(set(names)) != len(labels):
                raise ValueError("Different issue labels must produce different filenames")


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


def filename_for_issue(template: str, series: Series, issue: Issue, ext: str = ".cbz") -> str:
    year = issue.released_at.year if issue.released_at is not None else series.year
    return issue_filename(
        template, series.title, issue.display_number or issue.number, issue.title, year, ext=ext,
    )


def volume_filename(series_title: str, volume: int, ext: str = ".cbz") -> str:
    """Name for a collected-volume (TPB) archive."""
    return _numbered_filename(lambda series, _: f"{series} Vol. {volume:02d}", series_title, ext)


def range_filename(series_title: str, lo: float, hi: float, year: int | None = None,
                   ext: str = ".cbz") -> str:
    """Name for a multi-issue bundle file, e.g. 'Series #001-003.cbz'."""
    label = f"#{_pad_issue(lo, 3)}-{_pad_issue(hi, 3)}"
    return _numbered_filename(lambda series, _: f"{series} {label}", series_title, ext)


def volume_range_filename(series_title: str, lo: int, hi: int, ext: str = ".cbz") -> str:
    return _numbered_filename(lambda series, _: f"{series} Vol. {lo:02d}-{hi:02d}", series_title, ext)


def series_folder(series_title: str, year: int | None = None) -> str:
    """Series directory name; the start year disambiguates the many comic
    series that share a title across reboots (Batman (1940) vs (2016))."""
    if year:
        return sanitize_filename(f"{series_title} ({year})")
    return sanitize_filename(series_title)


def unmatched_filename(series_title: str, source_stem: str, year: int | None = None,
                       ext: str = ".cbz", *, preserve_identity: bool = False) -> str:
    """Preserve an extra's identity within the same atomic-write byte budget."""
    budget = NAME_MAX_BYTES - len((ext + _ATOMIC_SUFFIX).encode("utf-8"))
    conventional = sanitize_filename(f"{series_folder(series_title, year)} - {source_stem}", budget)
    if not preserve_identity:
        return conventional + ext
    coverage, editions = parse_filename(source_stem), filename_edition_markers(source_stem)
    for stem in (conventional, sanitize_filename(source_stem, budget)):
        if parse_filename(stem) == coverage and filename_edition_markers(stem) == editions:
            return stem + ext
    # When neither name preserves the evidence, prevent metadata or a
    # surviving partial label from claiming issues on the next scan.
    return sanitize_filename(f"{UNMATCHED_FILENAME_PREFIX} {source_stem}", budget) + ext


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
