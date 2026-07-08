"""Preview and apply renames of owned files into pullarr's naming convention.

Format-preserving: a .cbr is renamed, never converted. Non-destructive preview;
apply moves files within the library, skips target collisions, never deletes."""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..models import Issue, Series
from ..util import has_issue_marker, parse_volume_number
from .naming import issue_filename, range_filename, volume_filename

log = logging.getLogger(__name__)


@dataclass
class RenameItem:
    issue_ids: list[int]  # issues that point at this file (>1 for volumes)
    current_path: str
    new_path: str
    conflict: bool = False  # a different file already occupies the target name

    @property
    def current_name(self) -> str:
        return Path(self.current_path).name

    @property
    def new_name(self) -> str:
        return Path(self.new_path).name


def plan_renames(
    series: Series,
    issues: list[Issue],
    template: str,
) -> list[RenameItem]:
    """Rename items for owned issues whose on-disk name differs from the
    naming convention. Each file is renamed in place (within its own
    directory), so a series that spans a TPB folder and an issues folder
    keeps that split. Files shared by several issues (whole-volume archives)
    produce one item, named with the volume convention; single issue files
    use the issue convention."""
    # group issues by the file they point at
    by_file: dict[str, list[Issue]] = {}
    for issue in issues:
        if issue.downloaded and issue.file_path:
            by_file.setdefault(issue.file_path, []).append(issue)

    # numeric reading order: volume archives first (by volume), then
    # issue files (by issue) — not lexicographic, where v10 < v2
    keyed: list[tuple[tuple, RenameItem]] = []
    for current, batch in by_file.items():
        current_path = Path(current)
        ext = current_path.suffix.lower()
        stem = current_path.stem
        # a whole-volume archive is decided by its NAME (a volume number and no
        # explicit issue marker) — not by how many issues happen to map to
        # it, so a "Vol. 3" archive that covers a single issue still gets a
        # volume name rather than an issue name
        vol = parse_volume_number(stem)
        if vol is not None and not has_issue_marker(stem):
            desired = volume_filename(series.title, vol, ext)
            key = (0, float(vol))
        elif len(batch) == 1:
            desired = issue_filename(
                template, series.title, batch[0].display_number or batch[0].number, batch[0].title,
                series.year, ext=ext,
            )
            key = (1, batch[0].number)
        else:
            numbers = sorted(i.number for i in batch)
            desired = range_filename(series.title, numbers[0], numbers[-1], series.year, ext=ext)
            key = (1, numbers[0], numbers[-1])
        # rename in place, in the file's own directory
        new_path = current_path.parent / desired
        if new_path.name != current_path.name:
            keyed.append((key, RenameItem(
                issue_ids=[i.id for i in batch],
                current_path=str(current_path),
                new_path=str(new_path),
                # a different file already occupies the target — rename would
                # be skipped (never overwrites); flag it so the preview is honest
                conflict=new_path.exists() and new_path != current_path,
            )))
    keyed.sort(key=lambda kv: kv[0])
    return [item for _, item in keyed]


@dataclass
class RenameOutcome:
    item: RenameItem
    status: str  # "renamed" | "skipped-missing" | "skipped-collision" | "error"
    detail: str = ""


def apply_renames(items: list[RenameItem], issue_by_id: dict[int, Issue]) -> list[RenameOutcome]:
    """Move each file to its new path and update the issues that reference it.
    Never overwrites an existing target and never deletes the source."""
    outcomes: list[RenameOutcome] = []
    for item in items:
        src = Path(item.current_path)
        dst = Path(item.new_path)
        if not src.exists():
            outcomes.append(RenameOutcome(item, "skipped-missing"))
            continue
        if dst.exists() and dst != src:
            outcomes.append(RenameOutcome(item, "skipped-collision", str(dst)))
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            outcomes.append(RenameOutcome(item, "error", str(exc)))
            continue
        for iid in item.issue_ids:
            issue = issue_by_id.get(iid)
            if issue is not None:
                issue.file_path = str(dst)
        log.info("Renamed %s -> %s", src.name, dst.name)
        outcomes.append(RenameOutcome(item, "renamed"))
    return outcomes
