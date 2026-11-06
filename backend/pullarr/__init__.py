from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pullarr")
except PackageNotFoundError:
    # Source-only use is explicitly local, never an advertised release.
    __version__ = "0.3.0+local"

USER_AGENT = f"Pullarr/{__version__} (comic library automation)"
