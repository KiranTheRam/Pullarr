"""Shared issue/collected-volume coverage from filenames.

Only complete labels inside bracket groups carry coverage. Spans retain their
bounds so tracked decimal issues are included without inventing issue rows.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

BRACKET_GROUPS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NUMBER = r"\d+(?:\.\d+)?"
_ISSUE = r"(?:#\s*|(?<![a-z])(?:no|issue)[ ._]{0,2})"
_VOLUME = r"(?<![a-z0-9])v(?:ol(?:ume)?)?[ ._]{0,2}"
_ISSUE_TOKEN = re.compile(
    rf"{_ISSUE}({_NUMBER})(?:\s*[-–—]\s*(?:{_ISSUE})?({_NUMBER}))?(?![a-z0-9]|\.\d)", re.I
)
_VOLUME_TOKEN = re.compile(
    rf"{_VOLUME}(\d+)(?:\s*[-–—]\s*(?:{_VOLUME})?(\d+))?(?![a-z0-9]|\.\d)", re.I
)
_BARE = re.compile(rf"(?<![\w.])({_NUMBER})(?:\s*[-–—]\s*({_NUMBER}))?\s*$")


@dataclass(frozen=True)
class FilenameCoverage:
    issue: tuple[float, float] | None = None
    volume: tuple[int, int] | None = None
    explicit_issue: bool = False
    ambiguous: bool = False

    @property
    def is_range(self) -> bool:
        return any(span is not None and span[0] != span[1]
                   for span in (self.issue, self.volume))

    @property
    def issue_number(self) -> float | None:
        if not self.ambiguous and self.issue and self.issue[0] == self.issue[1]:
            return self.issue[0]
        return None

    @property
    def volume_number(self) -> int | None:
        if not self.ambiguous and self.volume and self.volume[0] == self.volume[1]:
            return self.volume[0]
        return None


@lru_cache(maxsize=4096)
def parse_filename(text: str) -> FilenameCoverage:
    untagged = BRACKET_GROUPS.sub(" ", text)
    # Underscores delimit tokens in scene filenames, including trailing numbers.
    untagged = re.sub(r"[_\s]+", " ", untagged).strip()
    tags = [m.group()[1:-1].strip() for m in BRACKET_GROUPS.finditer(text)]
    outside_issues = list(_ISSUE_TOKEN.finditer(untagged))
    outside_volumes = list(_VOLUME_TOKEN.finditer(untagged))
    issue_matches = outside_issues + [m for tag in tags if (m := _ISSUE_TOKEN.fullmatch(tag))]
    volume_matches = outside_volumes + [m for tag in tags if (m := _VOLUME_TOKEN.fullmatch(tag))]
    issues = {(float(m[1]), float(m[2] or m[1])) for m in issue_matches}
    volumes = {(int(m[1]), int(m[2] or m[1])) for m in volume_matches}
    explicit = bool(issues)
    bare = _BARE.search(untagged)
    if not issues and not volumes and bare:
        issues.add((float(bare[1]), float(bare[2] or bare[1])))
    issue = next(iter(issues), None)
    volume = next(iter(volumes), None)
    ambiguous = len(issues) > 1 or len(volumes) > 1
    ambiguous |= any(span is not None and span[1] < span[0] for span in (issue, volume))
    if explicit and bare and not any(m.end() == len(untagged) for m in outside_issues + outside_volumes):
        ambiguous |= (float(bare[1]), float(bare[2] or bare[1])) != issue
    # Do not let an incomplete marked span fall back to its first endpoint.
    for match in outside_issues + outside_volumes:
        if re.match(r"\s*[-–—]\s*(?:#|(?:issue|no|v(?:ol(?:ume)?)?)[ ._]*)?\d", untagged[match.end():], re.I):
            ambiguous = True
    return FilenameCoverage(issue, volume, explicit, ambiguous)
