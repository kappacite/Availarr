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

    def b_profile(self, item: Item) -> str:
        if item.kind == "movie":
            conn, found = self.radarr, self._get(self.radarr, "movie", tmdbId=item.ext_id)
        else:
            conn, found = self.sonarr, self._get(self.sonarr, "series", tvdbId=item.ext_id)
        if not found:
            return "default"
        return self._get(conn, f"qualityprofile/{found[0]['qualityProfileId']}")["name"]
