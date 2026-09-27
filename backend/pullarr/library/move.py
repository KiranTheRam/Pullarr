"""Move a series folder without overwriting destination files."""

import shutil
import uuid
from pathlib import Path

from ..models import Series
from .scanner import series_dir


class MoveError(Exception):
    pass


def prepare_move(source: Path, destination: Path) -> bool:
    """Stage a move. Returns True when copied across filesystems."""
    if source.is_symlink() or not source.is_dir():
        raise MoveError("Source folder is missing or is a symlink")
    if destination.exists():
        raise MoveError(f"Destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise MoveError("Destination parent folder does not exist")
    if source.stat().st_dev == destination.parent.stat().st_dev:
        source.rename(destination)
        return False
    temporary = destination.with_name(f".pullarr-moving-{uuid.uuid4().hex}")
    try:
        shutil.copytree(source, temporary, symlinks=True)
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return True


def undo_move(source: Path, destination: Path, copied: bool) -> None:
    if copied:
        shutil.rmtree(destination)
    else:
        destination.rename(source)


def finish_move(source: Path, copied: bool) -> None:
    if copied:
        shutil.rmtree(source)


def moved_paths(series: Series, new_root: Path) -> tuple[Path, Path, str]:
    if series.root_folder is None:
        raise MoveError("Series has no current root folder")
    old_root = Path(series.root_folder.path)
    source = series_dir(old_root, series)
    try:
        relative = source.relative_to(old_root)
    except ValueError as exc:
        raise MoveError("Primary folder is outside the old root") from exc
    if ".." in relative.parts or relative == Path("."):
        raise MoveError("Primary folder is not a safe relative path")
    if source.exists() and not source.resolve().is_relative_to(old_root.resolve()):
        raise MoveError("Primary folder points outside the old root")
    destination = new_root / relative
    if not destination.parent.is_dir() or not new_root.is_dir():
        raise MoveError("New root or destination parent is unavailable")
    if not destination.parent.resolve().is_relative_to(new_root.resolve()):
        raise MoveError("Destination parent points outside the new root")
    return source, destination, str(relative)


def rebase_file_paths(series: Series, source: Path, destination: Path,
                      old_root: Path, new_root: Path) -> None:
    for issue in series.issues:
        if not issue.file_path:
            continue
        try:
            issue.file_path = str(destination / Path(issue.file_path).relative_to(source))
        except ValueError:
            pass
    for extra in series.extra_folders:
        old = old_root / extra.path
        try:
            extra.path = str((destination / old.relative_to(source)).relative_to(new_root))
        except ValueError:
            # A relative extra folder outside the moved primary folder must
            # continue to point at its old location after the root changes.
            if not Path(extra.path).is_absolute():
                extra.path = str(old)
