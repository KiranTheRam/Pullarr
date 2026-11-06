#!/usr/bin/env python3
"""Optional container identity setup; exec the supplied command as PID 1."""
import os
from pathlib import Path
import re
import stat
import sys


class ConfigurationError(ValueError):
    pass


def options(env):
    uid, gid, mask = (env.get(name, "") for name in ("PUID", "PGID", "UMASK"))
    if bool(uid) != bool(gid):
        raise ConfigurationError("set both PUID and PGID, or neither")
    ids = None
    if uid:
        # Linux reserves (uid_t)-1; reject signs, whitespace, Unicode and overflow.
        if any(not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > 4294967294
               for value in (uid, gid)):
            raise ConfigurationError("PUID and PGID must be decimal IDs from 0 to 4294967294")
        ids = int(uid), int(gid)
    if mask and not re.fullmatch(r"(?:[0-7]{1,3}|0[0-7]{3})", mask):
        raise ConfigurationError("UMASK must be octal from 000 to 777 (for example 022 or 002)")
    return ids, int(mask, 8) if mask else None


def nested_mounts():
    # st_dev alone misses same-filesystem bind mounts. Do not traverse either.
    mounts = set()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        value = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
        mounts.add(Path(value))
    return mounts


def prepare_data_dir(value, uid, gid):
    path = Path(os.path.abspath(value))
    # Require dedicated appdata, never a filesystem root or image/media tree.
    protected = {"app", "bin", "boot", "comics", "dev", "downloads", "etc", "home",
                 "lib", "lib64", "media", "mnt", "opt", "proc", "root", "run", "sbin",
                 "srv", "sys", "usr", "var"}
    if path == Path("/") or path.parts[1] in protected:
        raise ConfigurationError("PULLARR_DATA_DIR must be a dedicated appdata directory outside system/media paths")
    # Refuse aliases before mkdir/chown. Child symlinks are skipped below.
    if path.resolve() != path:
        raise ConfigurationError("PULLARR_DATA_DIR must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True)
    mounts = nested_mounts()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def visit(fd, current):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            return  # A hardlink may refer to a library/download outside appdata.
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.fchown(fd, uid, gid)
        if not stat.S_ISDIR(info.st_mode):
            return
        with os.scandir(fd) as entries:
            for entry in entries:
                child = current / entry.name
                info = entry.stat(follow_symlinks=False)
                if child in mounts or info.st_dev != os.fstat(fd).st_dev:
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                child_fd = os.open(entry.name, flags | (os.O_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0), dir_fd=fd)
                try:
                    visit(child_fd, child)
                finally:
                    os.close(child_fd)

    fd = os.open(path, flags | os.O_DIRECTORY)
    try:
        visit(fd, path)
    finally:
        os.close(fd)


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    try:
        ids, mask = options(env)
        if not argv:
            raise ConfigurationError("a command is required")
        if ids and os.geteuid() != 0:
            raise ConfigurationError("PUID/PGID require root startup; remove --user / user: or omit PUID/PGID")
        if mask is not None:
            os.umask(mask)
        if ids:
            uid, gid = ids
            prepare_data_dir(env.get("PULLARR_DATA_DIR") or "/config", uid, gid)
            # Numeric IDs need no passwd/group edits. Only the startup process is
            # privileged; the app cannot regain privileges through child binaries.
            argv = ["/usr/bin/setpriv", f"--reuid={uid}", f"--regid={gid}",
                    "--clear-groups", "--bounding-set=-all", "--inh-caps=-all",
                    "--ambient-caps=-all", "--no-new-privs", "--", *argv]
        os.execvp(argv[0], argv)
    except (ConfigurationError, OSError):
        # Never interpolate environment values, paths or exception messages.
        message = "invalid configuration or appdata permissions; check PUID/PGID/UMASK and PULLARR_DATA_DIR"
        error = sys.exc_info()[1]
        if isinstance(error, ConfigurationError):
            message = str(error)
        print(f"pullarr-entrypoint: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
