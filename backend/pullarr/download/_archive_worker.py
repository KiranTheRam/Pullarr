"""Resource-limited decoder subprocess; never run untrusted codecs in the server.

Invoked by archive.py rather than using preexec_fn in a multithreaded process.
No decoded content or decoder diagnostics are written to application logs.
"""
import os
import resource
import sys
import zipfile


def main() -> None:
    # Linux enforces address-space limits across exec, including libarchive's
    # dictionaries/solid blocks. Darwin lacks this facility; the parent monitors
    # decoder RSS there in addition to output and wall-clock limits.
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    action, path, *args = sys.argv[1:]
    if action == "zip":
        with zipfile.ZipFile(path) as archive:
            info = archive.infolist()[int(args[0])]
            with archive.open(info) as source:
                while block := source.read(1024 * 1024):
                    sys.stdout.buffer.write(block)
        return
    decoder = args[0]
    os.execv(decoder, [decoder, "-tf" if action == "list" else "-xOf", path])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Parent maps every decoder failure to a non-sensitive import error.
        sys.exit(1)
