from datetime import datetime, timezone

import httpx

from availarr.common import Item
from availarr.server.library import Live, Unit, queue_progress
from availarr.server.quality import FileInfo


def _epoch(iso: str) -> float:
    d = datetime.fromisoformat(iso)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


class Radarr:
    def __init__(self, url: str, key: str, root: str, client: httpx.Client | None = None):
        self.root = root
        self.c = client or httpx.Client(base_url=url)
        self.c.headers["X-Api-Key"] = key

    def _req(self, method: str, path: str, **kw):
        r = self.c.request(method, "/api/v3" + path, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def _movie(self, item: Item) -> dict | None:
        movies = self._req("GET", "/movie", params={"tmdbId": item.ext_id})
        return movies[0] if movies else None

    def _lookup(self, item: Item) -> dict:
        found = self._req("GET", "/movie/lookup/tmdb", params={"tmdbId": item.ext_id})
        if isinstance(found, list):
            if not found:
                raise ValueError(f"tmdb {item.ext_id} not found")
            return found[0]
        return found

    def units(self, item: Item) -> list[Unit]:
        key = f"movie:{item.ext_id}"
        m = self._movie(item)
        if m is None:
            return [Unit(key, False, False, None)]
        f = m.get("movieFile") if m.get("hasFile") else None
        info = None
        if f:
            q = f["quality"]["quality"]
            info = FileInfo(q["resolution"], q["name"], [l["name"] for l in f.get("languages", [])], f["path"], f["size"])
        return [Unit(key, True, m["monitored"], info)]

    def title(self, item: Item) -> tuple[str, int | None]:
        m = self._movie(item) or self._lookup(item)
        return m["title"], m.get("year")

    def profile_name(self, item: Item) -> str | None:
        m = self._movie(item)
        if m is None:
            return None
        return next((p["name"] for p in self._req("GET", "/qualityprofile") if p["id"] == m["qualityProfileId"]), None)

    def _profile_id(self, name: str) -> int:
        for p in self._req("GET", "/qualityprofile"):
            if p["name"] == name:
                return p["id"]
        raise ValueError(f"unknown quality profile {name!r}")

    def _tag_id(self) -> int:
        for t in self._req("GET", "/tag"):
            if t["label"] == "availarr":
                return t["id"]
        return self._req("POST", "/tag", json={"label": "availarr"})["id"]

    def ensure(self, item: Item, profile: str, change_profile: bool) -> bool:
        m = self._movie(item)
        if m is None:
            body = self._lookup(item)
            body.update(qualityProfileId=self._profile_id(profile), rootFolderPath=self.root, monitored=True,
                        tags=[self._tag_id()], addOptions={"searchForMovie": True})
            self._req("POST", "/movie", json=body)
            return True
        if change_profile:
            m["qualityProfileId"] = self._profile_id(profile)
        m["monitored"] = True
        self._req("PUT", f"/movie/{m['id']}", json=m)
        self._req("POST", "/command", json={"name": "MoviesSearch", "movieIds": [m["id"]]})
        return False

    def progress(self, item: Item, live: Live | None = None) -> float:
        m = self._movie(item)
        if m is None:
            return 0.0
        return queue_progress(self._req("GET", "/queue/details", params={"movieId": m["id"]}), live)

    def hashes(self, item: Item, since: float, exclude_keys: list[str]) -> list[str]:
        m = self._movie(item)
        if m is None or f"movie:{item.ext_id}" in exclude_keys:  # A's own torrent of a protected movie
            return []
        h = self._req("GET", "/history/movie", params={"movieId": m["id"]})
        out = (e["downloadId"].lower() for e in h
               if e["eventType"] == "downloadFolderImported" and e.get("downloadId") and _epoch(e["date"]) >= since)
        return list(dict.fromkeys(out))

    def remove(self, item: Item, unit_keys: list[str], drop_entry: bool) -> None:
        if not unit_keys:
            return
        m = self._movie(item)
        if m is None:
            return
        if drop_entry:
            self._req("DELETE", f"/movie/{m['id']}", params={"deleteFiles": "true"})
            return
        if m.get("movieFile"):
            self._req("DELETE", f"/moviefile/{m['movieFile']['id']}")
        m["monitored"] = False
        m.pop("movieFile", None)
        m["hasFile"] = False
        self._req("PUT", f"/movie/{m['id']}", json=m)
