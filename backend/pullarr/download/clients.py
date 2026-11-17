"""Choose a torrent client for new downloads and reconnect tracked transfers."""

from .qbittorrent import QbtClient
from .transmission import TransmissionClient

CLIENT_NAMES = {"qbittorrent": "qBittorrent", "transmission": "Transmission"}


def client_enabled(values: dict[str, str], name: str) -> bool:
    return name in CLIENT_NAMES and values.get(f"{name}_enabled") == "true"


def selected_client(values: dict[str, str]) -> str | None:
    preference = values.get("torrent_download_client", "qbittorrent")
    if client_enabled(values, preference):
        return preference
    return next((name for name in CLIENT_NAMES if client_enabled(values, name)), None)


def create_client(values: dict[str, str], name: str) -> QbtClient | TransmissionClient:
    args = (values[f"{name}_url"], values[f"{name}_username"], values[f"{name}_password"])
    if name == "transmission":
        return TransmissionClient(*args)
    if name == "qbittorrent":
        return QbtClient(*args)
    raise ValueError("Unknown torrent download client")
