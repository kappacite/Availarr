import time
from datetime import datetime, timezone

import httpx

from availarr.common import Item
from availarr.server.library import Live, Unit, queue_progress
from availarr.server.quality import FileInfo
from availarr.server.radarr import _epoch


class Sonarr:
    def __init__(self, url: str, key: str, root: str, client: httpx.Client | None = None,
                 sleep=time.sleep, wait_s: float = 30):
        self.root = root
        self.sleep = sleep
        self.wait_s = wait_s
        self.c = client or httpx.Client(base_url=url)
        self.c.headers["X-Api-Key"] = key

    def _req(self, method: str, path: str, **kw):
        r = self.c.request(method, "/api/v3" + path, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def _series(self, item: Item) -> dict | None:
        s = self._req("GET", "/series", params={"tvdbId": item.ext_id})
        return s[0] if s else None

    def _lookup(self, item: Item) -> dict:
        found = self._req("GET", "/series/lookup", params={"term": f"tvdb:{item.ext_id}"})
        if not found:
            raise ValueError(f"tvdb {item.ext_id} not found")
        return found[0]

    def _eps(self, sid: int, item: Item) -> list[dict]:
        eps = self._req("GET", "/episode", params={"seriesId": sid, "seasonNumber": item.season})
        return [e for e in eps if item.kind != "episode" or e["episodeNumber"] == item.episode]

    def _aired(self, sid: int, item: Item) -> list[dict]:
        now = datetime.now(timezone.utc).timestamp()
        return [e for e in self._eps(sid, item) if e.get("airDateUtc") and _epoch(e["airDateUtc"]) <= now]

    def _key(self, item: Item, e: dict) -> str:
        return f"episode:{item.ext_id}:{e['seasonNumber']}:{e['episodeNumber']}"

    def _files(self, sid: int) -> list[dict]:
        return self._req("GET", "/episodefile", params={"seriesId": sid})

    def units(self, item: Item) -> list[Unit]:
        s = self._series(item)
        if s is None:
            return [Unit(item.key(), False, False, None)]
        files = {f["id"]: f for f in self._files(s["id"])}
        out = []
        for e in self._aired(s["id"], item):
            f = files.get(e.get("episodeFileId"))
            info = None
            if f:
                q = f["quality"]["quality"]
                info = FileInfo(q["resolution"], q["name"], [l["name"] for l in f.get("languages", [])], f["path"], f["size"])
            out.append(Unit(self._key(item, e), True, e["monitored"], info))
        return out

    def title(self, item: Item) -> tuple[str, int | None]:
        s = self._series(item) or self._lookup(item)
        return s["title"], s.get("year")

    def profile_name(self, item: Item) -> str | None:
        s = self._series(item)
        if s is None:
            return None
        return next((p["name"] for p in self._req("GET", "/qualityprofile") if p["id"] == s["qualityProfileId"]), None)

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
        s = self._series(item)
        created = s is None
        if created:
            body = self._lookup(item)
            if not (body.get("languageProfileId") or 0) > 0:
                body["languageProfileId"] = 1  # Sonarr v3 requires it; v4 ignores it
            body.update(qualityProfileId=self._profile_id(profile), rootFolderPath=self.root, monitored=True,
                        seasonFolder=True, tags=[self._tag_id()],
                        addOptions={"monitor": "none", "searchForMissingEpisodes": False})
            s = self._req("POST", "/series", json=body)
            self._req("POST", "/command", json={"name": "RefreshSeries", "seriesId": s["id"]})
            # ponytail: synchronous wait for Sonarr refresh, move to refresh() loop if it times out in practice
            waited = 0.0
            while not self._eps(s["id"], item) and waited < self.wait_s:
                self.sleep(2)
                waited += 2
        elif change_profile:
            s["qualityProfileId"] = self._profile_id(profile)
            self._req("PUT", f"/series/{s['id']}", json=s)
        ids = [e["id"] for e in self._aired(s["id"], item)]
        if ids:
            self._req("PUT", "/episode/monitor", json={"episodeIds": ids, "monitored": True})
            cmd = ({"name": "EpisodeSearch", "episodeIds": ids} if item.kind == "episode"
                   else {"name": "SeasonSearch", "seriesId": s["id"], "seasonNumber": item.season})
            self._req("POST", "/command", json=cmd)
        return created

    def progress(self, item: Item, live: Live | None = None) -> float:
        s = self._series(item)
        if s is None:
            return 0.0
        ids = {e["id"] for e in self._eps(s["id"], item)}
        q = [e for e in self._req("GET", "/queue/details", params={"seriesId": s["id"]}) if e.get("episodeId") in ids]
        return queue_progress(q, live)

    def hashes(self, item: Item, since: float, exclude_keys: list[str]) -> list[str]:
        s = self._series(item)
        if s is None:
            return []
        season = self._req("GET", "/episode", params={"seriesId": s["id"], "seasonNumber": item.season})
        ids = {e["id"] for e in season if item.kind != "episode" or e["episodeNumber"] == item.episode}
        excluded = {e["id"] for e in season if self._key(item, e) in exclude_keys}
        h = self._req("GET", "/history/series", params={"seriesId": s["id"], "seasonNumber": item.season})
        imports = [e for e in h if e["eventType"] == "downloadFolderImported" and e.get("downloadId")]
        # a torrent that also imported a protected episode is A's own: never attribute it to B
        owned = {e["downloadId"].lower() for e in imports if e.get("episodeId") in excluded}
        out = (e["downloadId"].lower() for e in imports
               if e.get("episodeId") in ids and _epoch(e["date"]) >= since and e["downloadId"].lower() not in owned)
        return list(dict.fromkeys(out))

    def remove(self, item: Item, unit_keys: list[str], drop_entry: bool) -> None:
        if not unit_keys:
            return
        s = self._series(item)
        if s is None:
            return
        season = self._req("GET", "/episode", params={"seriesId": s["id"], "seasonNumber": item.season})
        targets = [e for e in season if self._key(item, e) in unit_keys]
        kept = {e.get("episodeFileId") for e in season if self._key(item, e) not in unit_keys}  # multi-episode files
        for fid in dict.fromkeys(e["episodeFileId"] for e in targets if e.get("episodeFileId") and e["episodeFileId"] not in kept):
            self._req("DELETE", f"/episodefile/{fid}")
        if targets:
            self._req("PUT", "/episode/monitor", json={"episodeIds": [e["id"] for e in targets], "monitored": False})
        if drop_entry and not self._files(s["id"]):
            self._req("DELETE", f"/series/{s['id']}", params={"deleteFiles": "true"})
