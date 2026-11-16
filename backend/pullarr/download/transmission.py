"""Transmission RPC operations used by the torrent queue."""

import base64
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .. import USER_AGENT
from .torrent import TorrentAlreadyExists, TorrentClientError, normalize_btih

SESSION_HEADER = "X-Transmission-Session-Id"


def validate_rpc_url(value: str) -> str:
    try:
        url = urlsplit(value.strip())
        port = url.port
        if (url.scheme in {"http", "https"} and url.hostname and url.username is None
                and not url.query and not url.fragment and port != 0
                and not any(c.isspace() or c in "\\?#" for c in value.strip())):
            return url.geturl()
    except ValueError:
        pass
    raise ValueError("Enter a Transmission HTTP(S) RPC URL without credentials, query, or fragment")


@dataclass(frozen=True)
class TransmissionTorrent:
    hash: str
    progress: float
    save_path: str
    is_complete: bool

    @property
    def content_path(self) -> str:
        # Transmission returns paths relative to downloadDir for both a single
        # file and a nested torrent. The importer uses only the requested files.
        return self.save_path


class TransmissionClient:
    def __init__(self, url: str, username: str, password: str) -> None:
        try:
            self.url = validate_rpc_url(url)
        except ValueError as exc:
            raise TorrentClientError(str(exc)) from exc
        self._http = httpx.AsyncClient(
            auth=(username, password) if username or password else None,
            headers={"User-Agent": USER_AGENT}, timeout=30,
        )
        self._session_id = ""

    async def close(self) -> None:
        await self._http.aclose()

    async def _rpc(self, method: str, arguments: dict) -> dict:
        # The legacy RPC envelope is supported across Transmission 3.x and 4.x.
        for _ in range(3):
            headers = {SESSION_HEADER: self._session_id} if self._session_id else {}
            try:
                response = await self._http.post(
                    self.url, headers=headers, json={"method": method, "arguments": arguments},
                )
            except httpx.HTTPError:
                raise TorrentClientError("Cannot reach Transmission") from None
            if response.status_code == 409 and (session_id := response.headers.get(SESSION_HEADER)):
                self._session_id = session_id
                continue
            if not response.is_success:
                raise TorrentClientError(f"Transmission RPC returned HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise TorrentClientError("Transmission returned invalid RPC data") from None
            if not isinstance(data, dict) or data.get("result") != "success":
                # RPC error strings may echo an authenticated URL.
                raise TorrentClientError(f"Transmission rejected {method}")
            result = data.get("arguments", {})
            if not isinstance(result, dict):
                raise TorrentClientError("Transmission returned invalid RPC arguments")
            return result
        raise TorrentClientError("Transmission RPC session could not be established")

    async def version(self) -> str:
        result = await self._rpc("session-get", {"fields": ["version"]})
        version = result.get("version")
        if not isinstance(version, str) or not version:
            raise TorrentClientError("Transmission returned no version")
        return version

    async def add_release(self, magnet: str, torrent_data: bytes | None, info_hash: str,
                          download_dir: str, *, keep_existing: bool) -> None:
        arguments = ({"metainfo": base64.b64encode(torrent_data).decode("ascii")}
                     if torrent_data is not None else {"filename": magnet})
        if download_dir:
            arguments["download-dir"] = download_dir
        result = await self._rpc("torrent-add", arguments)
        added, duplicate = "torrent-added" in result, "torrent-duplicate" in result
        if added == duplicate:
            raise TorrentClientError("Transmission returned an invalid torrent-add result")
        transfer = result["torrent-added" if added else "torrent-duplicate"]
        returned_hash = transfer.get("hashString") if isinstance(transfer, dict) else None
        if not isinstance(returned_hash, str) or returned_hash.lower() != info_hash:
            raise TorrentClientError("Transmission returned an unexpected torrent hash")
        if duplicate and not keep_existing:
            raise TorrentAlreadyExists("Transmission already has this torrent")

    async def _get(self, info_hash: str, fields: list[str]) -> dict | None:
        info_hash = normalize_btih(info_hash)
        if not info_hash:
            raise TorrentClientError("Transmission torrent hash is invalid")
        result = await self._rpc("torrent-get", {"ids": [info_hash], "fields": ["hashString", *fields]})
        entries = result.get("torrents")
        if not isinstance(entries, list):
            raise TorrentClientError("Transmission returned an invalid torrent list")
        return next((item for item in entries if isinstance(item, dict)
                     and str(item.get("hashString", "")).lower() == info_hash), None)

    async def get_torrent(self, info_hash: str) -> TransmissionTorrent | None:
        transfer = await self._get(info_hash, ["downloadDir", "percentDone", "metadataPercentComplete", "status"])
        if transfer is None:
            return None
        try:
            progress = float(transfer["percentDone"])
            save_path = transfer["downloadDir"]
            metadata_ready = float(transfer["metadataPercentComplete"]) >= 1
            status = int(transfer["status"])
        except (KeyError, OverflowError, TypeError, ValueError):
            raise TorrentClientError("Transmission returned invalid torrent status") from None
        if not isinstance(save_path, str) or not 0 <= progress <= 1:
            raise TorrentClientError("Transmission returned invalid torrent status")
        return TransmissionTorrent(normalize_btih(info_hash), progress, save_path,
                                   progress == 1 and metadata_ready and status not in {1, 2})

    async def torrent_files(self, info_hash: str) -> list[str]:
        transfer = await self._get(info_hash, ["files", "wanted"])
        if transfer is None:
            raise TorrentClientError("Transmission torrent vanished before its files could be read")
        files, wanted = transfer.get("files"), transfer.get("wanted")
        if not isinstance(files, list) or not isinstance(wanted, list) or len(files) != len(wanted):
            raise TorrentClientError("Transmission returned an invalid file list")
        names = []
        for file, selected in zip(files, wanted):
            if not isinstance(file, dict) or not isinstance(file.get("name"), str) or selected not in (0, 1):
                raise TorrentClientError("Transmission returned an invalid file list")
            if selected:
                names.append(file["name"])
        return names

    async def delete_torrents(self, hashes: list[str], delete_files: bool = True) -> None:
        # Omit the RPC entirely for an empty list. A missing ID filter can
        # apply an RPC to every torrent on the daemon.
        ids = [info_hash for value in hashes if (info_hash := normalize_btih(value))]
        if ids:
            await self._rpc("torrent-remove", {"ids": ids, "delete-local-data": delete_files})
