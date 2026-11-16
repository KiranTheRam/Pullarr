"""qBittorrent Web API v2 client (login, add magnet, poll status)."""

from dataclasses import dataclass

import httpx

from .. import USER_AGENT
from .torrent import TorrentClientError


class QbtError(TorrentClientError):
    pass


@dataclass
class QbtTorrent:
    hash: str  # qBittorrent's API torrent ID
    name: str
    progress: float  # 0..1
    state: str
    content_path: str
    category: str
    save_path: str = ""
    infohash_v1: str = ""

    @classmethod
    def from_api(cls, data: dict) -> "QbtTorrent":
        strings = {key: data.get(key, "") for key in
                   ("hash", "name", "state", "content_path", "category", "save_path")}
        v1_hash = data.get("infohash_v1")
        try:
            progress = float(data.get("progress", 0))
        except (TypeError, ValueError, OverflowError):
            raise QbtError("qBittorrent returned invalid torrent status") from None
        if (not all(isinstance(value, str) for value in strings.values()) or not strings["hash"]
                or (v1_hash is not None and not isinstance(v1_hash, str))
                or not 0 <= progress <= 1):
            raise QbtError("qBittorrent returned invalid torrent status")
        return cls(
            **strings, progress=progress, infohash_v1=(v1_hash or "").lower(),
        )

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.state in (
            "uploading",
            "stalledUP",
            "pausedUP",
            "stoppedUP",
            "queuedUP",
            "forcedUP",
        )


class QbtClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Referer": self.base_url},
            timeout=30,
        )
        self._logged_in = False
        self._v1_aliases: dict[str, QbtTorrent] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL):
            # Transport exceptions can include credential-bearing URLs. This
            # client is also used by background jobs that log error messages.
            raise QbtError("Cannot reach qBittorrent") from None

    async def _login(self) -> None:
        resp = await self._send(
            "POST", f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        # qBittorrent 4.x answers 200 "Ok."/"Fails."; 5.x answers 204 on
        # success and 401 on bad credentials.
        if resp.status_code >= 300 or resp.text.strip() == "Fails.":
            raise QbtError(f"qBittorrent login failed: HTTP {resp.status_code}")
        self._logged_in = True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        **kwargs,
    ) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        resp = await self._send(method, f"{self.base_url}/api/v2{path}", **kwargs)
        if resp.status_code == 403:
            await self._login()
            resp = await self._send(method, f"{self.base_url}/api/v2{path}", **kwargs)
        if allowed_statuses and resp.status_code in allowed_statuses:
            return resp
        if not resp.is_success:
            raise QbtError(f"qBittorrent request failed: HTTP {resp.status_code}")
        return resp

    async def version(self) -> str:
        resp = await self._request("GET", "/app/version")
        return resp.text.strip()

    async def default_save_path(self) -> str:
        """qBittorrent's configured default download directory."""
        try:
            resp = await self._request("GET", "/app/preferences")
            data = resp.json()
            if isinstance(data, dict) and isinstance(path := data.get("save_path"), str):
                return path
        except (QbtError, ValueError):
            pass
        return ""

    async def ensure_category(self, name: str, save_path: str | None = None) -> None:
        """Create the category (idempotent) so torrents land in its subfolder."""
        data = {"category": name}
        if save_path:
            data["savePath"] = save_path
        resp = await self._request(
            "POST", "/torrents/createCategory", data=data, allowed_statuses={409}
        )
        # 409/Conflict means it already exists — set its path instead
        if resp.status_code == 409 and save_path:
            await self._request("POST", "/torrents/editCategory", data=data)

    async def add_magnet(self, magnet: str, category: str, save_path: str | None = None) -> None:
        data = {"urls": magnet, "category": category}
        if save_path:
            data["savepath"] = save_path
            data["autoTMM"] = "false"
        resp = await self._request("POST", "/torrents/add", data=data)
        if resp.text.strip() == "Fails.":
            raise QbtError("qBittorrent rejected the torrent")

    async def add_torrent(self, content: bytes, category: str, save_path: str | None = None) -> None:
        data = {"category": category}
        if save_path:
            data.update(savepath=save_path, autoTMM="false")
        resp = await self._request(
            "POST", "/torrents/add", data=data,
            files={"torrents": ("release.torrent", content, "application/x-bittorrent")},
        )
        if resp.text.strip() == "Fails.":
            raise QbtError("qBittorrent rejected the torrent")

    async def _entries(self, path: str, **kwargs) -> list[dict]:
        resp = await self._request("GET", path, **kwargs)
        try:
            entries = resp.json()
        except ValueError:
            raise QbtError("qBittorrent returned invalid JSON") from None
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise QbtError("qBittorrent returned an invalid list")
        return entries

    async def list_torrents(self, category: str) -> list[QbtTorrent]:
        entries = await self._entries("/torrents/info", params={"category": category})
        return [QbtTorrent.from_api(data) for data in entries]

    async def _torrent_v1_aliases(self) -> dict[str, QbtTorrent]:
        # A client is scoped to one operation or sync pass. Share the fallback
        # snapshot across lookups so multiple missing IDs only need one list.
        if self._v1_aliases is None:
            entries = await self._entries("/torrents/info")
            torrents = (QbtTorrent.from_api(data) for data in entries)
            self._v1_aliases = {
                torrent.infohash_v1: torrent for torrent in torrents
                if torrent.hash and torrent.infohash_v1 and torrent.hash != torrent.infohash_v1
            }
        return self._v1_aliases

    async def delete_torrents(self, hashes: list[str], delete_files: bool = True) -> None:
        """Remove torrents, including partial data by default."""
        if not hashes:
            return
        aliases = await self._torrent_v1_aliases()
        torrent_ids = [aliases[info_hash].hash if info_hash in aliases else info_hash for info_hash in hashes]
        await self._request(
            "POST",
            "/torrents/delete",
            data={
                "hashes": "|".join(torrent_ids),
                "deleteFiles": "true" if delete_files else "false",
            },
        )

    async def torrent_files(self, torrent_hash: str) -> list[str]:
        """Wanted files, with names relative to the torrent's save_path."""
        entries = await self._entries("/torrents/files", params={"hash": torrent_hash})
        if any(not isinstance(entry.get("name"), str) or not isinstance(entry.get("priority", 1), int)
               for entry in entries):
            raise QbtError("qBittorrent returned an invalid file list")
        return [entry["name"] for entry in entries if entry.get("priority", 1) != 0]

    async def get_torrent(self, torrent_hash: str) -> QbtTorrent | None:
        entries = await self._entries("/torrents/info", params={"hashes": torrent_hash})
        if entries:
            torrent = QbtTorrent.from_api(entries[0])
            if len(entries) != 1 or torrent_hash not in {torrent.hash.lower(), torrent.infohash_v1}:
                raise QbtError("qBittorrent returned an unexpected torrent")
            return torrent
        # libtorrent 2 can expose hybrids under a truncated v2 API ID.
        return (await self._torrent_v1_aliases()).get(torrent_hash)


async def test_connection(base_url: str, username: str, password: str) -> str:
    """Returns qBittorrent version or raises QbtError."""
    client = QbtClient(base_url, username, password)
    try:
        return await client.version()
    finally:
        await client.close()
