"""Configured Torznab feeds, including Prowlarr's aggregate endpoint."""
import json
import re
from urllib.parse import urlsplit

import httpx
from lxml import etree

from .. import USER_AGENT
from .base import TorrentIndexer, TorrentRelease

MASK = "••••••••"


def validate_indexers(value: str) -> str:
    try:
        entries = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Torznab indexers must be a JSON list") from exc
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("Configure at most 20 Torznab indexers")
    ids, names = set(), set()
    for item in entries:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("id", "name", "url", "api_key")):
            raise ValueError("Each Torznab indexer needs an id, name, URL, and API key")
        if not item["id"] or item["id"] in ids or not item["name"].strip() or item["name"] in names:
            raise ValueError("Torznab indexer ids and names must be unique and nonempty")
        ids.add(item["id"])
        names.add(item["name"])
        url = urlsplit(item["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("Use a Torznab HTTP(S) API URL without credentials or query parameters")
        if not isinstance(item.get("categories", "7030"), str) or not re.fullmatch(r"\d+(,\d+)*", item.get("categories", "7030")):
            raise ValueError("Torznab categories must be comma-separated numbers")
        if item.get("search_type", "search") not in {"search", "book"}:
            raise ValueError("Torznab search type must be search or book")
        if not isinstance(item.get("enabled", True), bool):
            raise ValueError("Torznab enabled must be a boolean")
    return json.dumps(entries)


def mask_indexers(value: str) -> str:
    entries = json.loads(value)
    for item in entries:
        if item.get("api_key"):
            item["api_key"] = MASK
    return json.dumps(entries)


def restore_keys(value: str, previous: str) -> str:
    entries = json.loads(validate_indexers(value))
    old = {item["id"]: item["api_key"] for item in json.loads(previous)}
    for item in entries:
        if item["api_key"] == MASK:
            if item["id"] not in old:
                raise ValueError("Enter an API key for the new Torznab indexer")
            item["api_key"] = old[item["id"]]
    return json.dumps(entries)


def configured_indexers(values: str) -> list[TorrentIndexer]:
    return [TorznabIndexer(item) for item in json.loads(values) if item.get("enabled", True)]


class TorznabIndexer(TorrentIndexer):
    def __init__(self, settings: dict):
        self.name = settings["name"]
        self.settings = settings

    async def search(self, query: str) -> list[TorrentRelease]:
        params = {
            "t": self.settings.get("search_type", "search"), "q": query,
            "apikey": self.settings["api_key"], "cat": self.settings.get("categories", "7030"),
            "extended": "1", "limit": "100",
        }
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            response = await client.get(self.settings["url"], params=params)
            response.raise_for_status()
        return self.parse(response.content)

    def parse(self, content: bytes) -> list[TorrentRelease]:
        if len(content) > 10_000_000:
            raise ValueError("Torznab response is too large")
        root = etree.fromstring(content, etree.XMLParser(resolve_entities=False, no_network=True))
        if root.tag == "error" or root.find("error") is not None:
            raise ValueError("Torznab rejected the search; check its API key and settings")
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("Expected a Torznab RSS response")
        releases = []
        for item in root.findall("./channel/item"):
            attrs = {a.get("name"): a.get("value", "") for a in item if isinstance(a.tag, str) and etree.QName(a).localname == "attr"}
            enclosure = item.find("enclosure")
            link = attrs.get("magneturl") or (enclosure.get("url", "") if enclosure is not None else "") or item.findtext("link", "")
            if urlsplit(link).scheme not in {"magnet", "http", "https"}:
                continue
            def number(value):
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
            seeders = number(attrs.get("seeders"))
            size = number(attrs.get("size") or item.findtext("size") or (enclosure.get("length") if enclosure is not None else 0))
            indexer = item.findtext("prowlarrindexer") or item.findtext("jackettindexer")
            releases.append(TorrentRelease(
                source_name=f"{self.name} / {indexer}" if indexer else self.name,
                title=item.findtext("title", ""), magnet=link,
                url=item.findtext("comments", ""), size_bytes=size,
                seeders=seeders, leechers=number(attrs.get("leechers")) if "leechers" in attrs else max(0, number(attrs.get("peers")) - seeders),
            ))
        return releases
