import re

import httpx


def _norm(h: str) -> str:
    h = h.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", h):  # "" / "all" would hit every torrent
        raise ValueError(f"bad torrent hash: {h!r}")
    return h


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

    def delete(self, h: str) -> None:
        h = _norm(h)
        self._req("POST", "/torrents/delete", data={"hashes": h, "deleteFiles": "true"})
