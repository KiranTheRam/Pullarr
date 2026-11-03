"""Shared Torznab query templates for interactive and scheduled searches."""
import re
from string import Formatter

from ..library.naming import _format_issue


def validate_templates(value: str, required: bool = False) -> None:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if (required and not lines) or len(lines) > 10 or len(value) > 2000:
        raise ValueError("Provide between 1 and 10 query templates (up to 2000 characters)")
    for line in lines:
        try:
            for _, field, spec, conversion in Formatter().parse(line):
                if field is None:
                    continue
                if field not in {"series", "issue", "year"} or conversion:
                    raise ValueError("Use only {series}, {issue}, and {year} query tokens")
                if spec and (field != "issue" or not re.fullmatch(r'0[1-9][0-9]?d?', spec)):
                    raise ValueError("Issue padding must use a format such as {issue:03d}")
            _format_issue(line, "1").format(series="Series", issue="1", year=2026)
        except (ValueError, KeyError, IndexError) as exc:
            raise ValueError(f"Invalid query template: {exc}") from exc


def search_queries(series, issue, values: dict[str, str]) -> list[str]:
    title = getattr(series, "search_title", "") or series.title
    if issue is None:
        return [title]
    templates = getattr(series, "search_templates", "") or values.get("torrent_query_templates", "{series} {issue}")
    number = issue.display_number or f"{issue.number:g}"
    year = issue.released_at.year if issue.released_at else (series.year or "")
    return list(dict.fromkeys(
        _format_issue(line.strip(), number).format(series=title, issue=number, year=year).strip()
        for line in templates.splitlines() if line.strip()
    ))


async def search_variants(indexer, queries):
    for query in queries:
        results = await indexer.search(query)
        if results:
            return results
    return []
