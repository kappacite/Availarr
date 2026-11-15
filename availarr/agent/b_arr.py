import httpx

from availarr.common import Item


class BArr:
    """Looks up the quality profile name B's Radarr/Sonarr use for an item."""

    def __init__(self, radarr_url, radarr_key, sonarr_url, sonarr_key, client=None):
        self.radarr = (radarr_url.rstrip("/"), radarr_key)
        self.sonarr = (sonarr_url.rstrip("/"), sonarr_key)
        self.client = client or httpx.Client(timeout=15)

    def _get(self, conn, path, **params):
        r = self.client.get(f"{conn[0]}/api/v3/{path}", params=params, headers={"X-Api-Key": conn[1]})
        r.raise_for_status()
        return r.json()

    def _entry(self, item: Item) -> tuple[tuple, dict | None]:
        if item.kind == "movie":
            conn, field, found = self.radarr, "tmdbId", self._get(self.radarr, "movie", tmdbId=item.ext_id)
        else:
            conn, field, found = self.sonarr, "tvdbId", self._get(self.sonarr, "series", tvdbId=item.ext_id)
        # some versions ignore the filter and return the whole library: never trust found[0]
        return conn, next((e for e in found if e.get(field) == item.ext_id), None)

    def b_profile(self, item: Item) -> str:
        conn, e = self._entry(item)
        if e is None:
            return "default"
        return self._get(conn, f"qualityprofile/{e['qualityProfileId']}")["name"]

    def episodes(self, item: Item) -> int:
        """Episodes in B's season: Sonarr checks a season pack's size per episode, so the placeholder must scale."""
        if item.kind != "season":
            return 0
        _, e = self._entry(item)
        if e is None:
            return 0
        return len(self._get(self.sonarr, "episode", seriesId=e["id"], seasonNumber=item.season))
