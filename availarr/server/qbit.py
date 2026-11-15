import re
import time
from dataclasses import dataclass

import httpx


def _norm(h: str) -> str:
    h = h.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", h):  # "" / "all" would hit every torrent
        raise ValueError(f"bad torrent hash: {h!r}")
    return h


def _eta(t: dict) -> int | None:
    if float(t.get("progress", 0)) >= 1:
        return 0
    e = t.get("eta")
    return e if isinstance(e, int) and 0 <= e < 8640000 else None  # 8640000 = qBittorrent's "infinity"


@dataclass
class Torrent:
    hash: str
    name: str
    state: str
    progress: float
    seeding: int | None
    upspeed: int
    dlspeed: int
    ratio: float
    peers: int


def _torrent(x: dict) -> Torrent:
    seeding = x.get("seeding_time")
    if seeding is None and x.get("completion_on", 0) > 0:
        seeding = int(time.time()) - x["completion_on"]
    return Torrent(x["hash"].lower(), x.get("name", ""), x.get("state", ""), float(x.get("progress", 0)), seeding,
                   x.get("upspeed", 0), x.get("dlspeed", 0), float(x.get("ratio", 0)),
                   x.get("num_seeds", 0) + x.get("num_leechs", 0))


class QBit:
    def __init__(self, url: str, user: str, password: str, client: httpx.Client | None = None):
        self.c = client or httpx.Client(base_url=url)
        self.creds = {"username": user, "password": password}
        self.logged = False

    def _login(self) -> None:
        r = self.c.post("/api/v2/auth/login", data=self.creds)
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise httpx.HTTPError("qBittorrent login failed")
        self.logged = True

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        if not self.logged:
            self._login()
        r = self.c.request(method, "/api/v2" + path, **kw)
        if r.status_code == 403:  # session expired: one re-login
            self._login()
            r = self.c.request(method, "/api/v2" + path, **kw)
        r.raise_for_status()
        return r

    def seeding_seconds(self, h: str) -> int | None:
        h = _norm(h)
        t = self._req("GET", "/torrents/info", params={"hashes": h}).json()
        return next((x["seeding_time"] for x in t if x.get("hash", "").lower() == h), None)

    def live(self, hashes: list[str]) -> dict[str, tuple[float, int | None]]:
        """hash -> (progress 0..1, ETA in seconds or None when qBittorrent can't tell)."""
        ok = []
        for h in hashes:
            try:
                ok.append(_norm(h))
            except ValueError:
                pass
        if not ok:
            return {}
        t = self._req("GET", "/torrents/info", params={"hashes": "|".join(dict.fromkeys(ok))}).json()
        return {x["hash"].lower(): (float(x["progress"]), _eta(x)) for x in t if x.get("hash")}

    def info(self, hashes: list[str]) -> dict[str, Torrent]:
        """hash -> live Torrent, one /torrents/info call; invalid hashes are ignored."""
        ok = []
        for h in hashes:
            try:
                ok.append(_norm(h))
            except ValueError:
                pass
        if not ok:
            return {}
        t = self._req("GET", "/torrents/info", params={"hashes": "|".join(dict.fromkeys(ok))}).json()
        return {x["hash"].lower(): _torrent(x) for x in t if x.get("hash")}

    def delete(self, h: str) -> None:
        h = _norm(h)
        self._req("POST", "/torrents/delete", data={"hashes": h, "deleteFiles": "true"})
