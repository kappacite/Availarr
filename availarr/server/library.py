from dataclasses import dataclass
from typing import Callable, Protocol

from availarr.common import Item
from availarr.server.quality import FileInfo


@dataclass
class Unit:
    key: str  # "movie:<tmdb>" or "episode:<tvdb>:<s>:<e>"; item.key() if the entry is absent
    exists: bool
    monitored: bool
    file: FileInfo | None


Live = Callable[[list[str]], dict[str, float]]  # torrent hashes -> live progress 0..1 (qBittorrent)


def queue_progress(entries: list[dict], live: Live | None = None) -> float:
    """Progress of Radarr/Sonarr queue entries; live qBittorrent values win over the arr's (refreshed ~1/min)."""
    total = sum(e["size"] for e in entries)
    if not total:
        return 0.0
    try:
        now = live([e.get("downloadId") or "" for e in entries]) if live else {}
    except Exception:  # qBittorrent down: the arr's numbers are still good enough
        now = {}
    done = sum(e["size"] * now[h] if (h := (e.get("downloadId") or "").lower()) in now else e["size"] - e["sizeleft"]
               for e in entries)
    return done / total


class Library(Protocol):
    def units(self, item: Item) -> list[Unit]: ...
    def title(self, item: Item) -> tuple[str, int | None]: ...
    def profile_name(self, item: Item) -> str | None: ...
    def ensure(self, item: Item, profile: str, change_profile: bool) -> bool: ...
    def progress(self, item: Item, live: Live | None = None) -> float: ...
    def hashes(self, item: Item, since: float, exclude_keys: list[str]) -> list[str]: ...
    def remove(self, item: Item, unit_keys: list[str], drop_entry: bool) -> None: ...
