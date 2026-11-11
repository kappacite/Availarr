import json

import httpx
import pytest

from availarr.common import Item
from availarr.server.library import Unit
from availarr.server.quality import FileInfo
from availarr.server.sonarr import Sonarr

SEASON = Item("season", 81189, 2)
SERIES = {"id": 7, "title": "BB", "year": 2008, "qualityProfileId": 2}
PROFILES = [{"id": 2, "name": "Any"}, {"id": 4, "name": "HD-1080p FR"}]


def ep(i, n, file_id=0, air="2020-01-01T00:00:00Z"):
    return {"id": i, "seasonNumber": 2, "episodeNumber": n, "airDateUtc": air, "monitored": True, "episodeFileId": file_id}


EPS = [ep(101, 1, 51), ep(102, 2), ep(103, 3, air="2999-01-01T00:00:00Z")]
FILE = {"id": 51, "path": "/tv/BB/s2e1.mkv", "size": 5, "quality": {"quality": {"resolution": 720, "name": "HDTV-720p"}},
        "languages": [{"name": "French"}]}


def make(routes, wait_s=30):
    calls, sleeps = [], []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.url.params), body))
        assert req.headers["X-Api-Key"] == "k"
        out = routes.get((req.method, req.url.path), [])
        if callable(out):
            out = out()
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://sonarr")
    s = Sonarr("http://sonarr", "k", "/tv", client, sleep=sleeps.append, wait_s=wait_s)
    s.sleeps = sleeps
    return s, calls


def test_units_season_only_aired():
    s, _ = make({("GET", "/api/v3/series"): [SERIES], ("GET", "/api/v3/episode"): EPS, ("GET", "/api/v3/episodefile"): [FILE]})
    assert s.units(SEASON) == [
        Unit("episode:81189:2:1", True, True, FileInfo(720, "HDTV-720p", ["French"], "/tv/BB/s2e1.mkv", 5)),
        Unit("episode:81189:2:2", True, True, None)]


def test_units_episode_item_and_unaired_none():
    s, _ = make({("GET", "/api/v3/series"): [SERIES], ("GET", "/api/v3/episode"): EPS + [ep(104, 4, air=None)]})
    assert [u.key for u in s.units(Item("episode", 81189, 2, 2))] == ["episode:81189:2:2"]
    assert [u.key for u in s.units(SEASON)] == ["episode:81189:2:1", "episode:81189:2:2"]


def test_units_missing_series_and_error():
    s, _ = make({})
    assert s.units(SEASON) == [Unit("season:81189:2", False, False, None)]
    s, _ = make({("GET", "/api/v3/series"): httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        s.units(SEASON)


def test_title_and_profile_name():
    s, _ = make({("GET", "/api/v3/series"): [SERIES], ("GET", "/api/v3/qualityprofile"): PROFILES})
    assert s.title(SEASON) == ("BB", 2008) and s.profile_name(SEASON) == "Any"
    s, _ = make({("GET", "/api/v3/series/lookup"): [{"title": "X", "year": 1999}]})
    assert s.title(SEASON) == ("X", 1999) and s.profile_name(SEASON) is None


def test_ensure_adds_series_waits_and_searches():
    eps = iter([[], [], EPS, EPS])
    s, calls = make({("GET", "/api/v3/series/lookup"): [{"title": "BB", "tvdbId": 81189}],
                     ("GET", "/api/v3/qualityprofile"): PROFILES, ("GET", "/api/v3/tag"): [{"id": 1, "label": "availarr"}],
                     ("POST", "/api/v3/series"): {"id": 7}, ("GET", "/api/v3/episode"): lambda: next(eps)})
    assert s.ensure(SEASON, "HD-1080p FR", True) is True
    body = next(c[3] for c in calls if c[:2] == ("POST", "/api/v3/series"))
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/tv" and body["tags"] == [1]
    assert body["addOptions"] == {"monitor": "none", "searchForMissingEpisodes": False} and body["seasonFolder"] is True
    assert ("POST", "/api/v3/command", {}, {"name": "RefreshSeries", "seriesId": 7}) in calls
    assert s.sleeps == [2, 2]
    assert ("PUT", "/api/v3/episode/monitor", {}, {"episodeIds": [101, 102], "monitored": True}) in calls
    assert ("POST", "/api/v3/command", {}, {"name": "SeasonSearch", "seriesId": 7, "seasonNumber": 2}) in calls


def test_ensure_wait_times_out():
    s, calls = make({("GET", "/api/v3/series/lookup"): [{"title": "BB"}], ("GET", "/api/v3/qualityprofile"): PROFILES,
                     ("GET", "/api/v3/tag"): [{"id": 1, "label": "availarr"}], ("POST", "/api/v3/series"): {"id": 7}}, wait_s=4)
    assert s.ensure(SEASON, "Any", False) is True
    assert s.sleeps == [2, 2] and not any(c[1].endswith("/monitor") for c in calls)


def test_ensure_episode_search_and_profile_change():
    s, calls = make({("GET", "/api/v3/series"): [dict(SERIES)], ("GET", "/api/v3/episode"): EPS,
                     ("GET", "/api/v3/qualityprofile"): PROFILES})
    assert s.ensure(Item("episode", 81189, 2, 2), "HD-1080p FR", True) is False
    put = next(c[3] for c in calls if c[:2] == ("PUT", "/api/v3/series/7"))
    assert put["qualityProfileId"] == 4 and put["title"] == "BB"
    assert ("PUT", "/api/v3/episode/monitor", {}, {"episodeIds": [102], "monitored": True}) in calls
    assert ("POST", "/api/v3/command", {}, {"name": "EpisodeSearch", "episodeIds": [102]}) in calls


def test_progress():
    q = [{"episodeId": 101, "size": 100, "sizeleft": 25}, {"episodeId": 999, "size": 100, "sizeleft": 100}]
    s, _ = make({("GET", "/api/v3/series"): [SERIES], ("GET", "/api/v3/episode"): EPS, ("GET", "/api/v3/queue/details"): q})
    assert s.progress(SEASON) == 0.75
    s, _ = make({})
    assert s.progress(SEASON) == 0.0 and s.hashes(SEASON, 0) == []


def test_hashes_filtered_by_episode():
    imp = "downloadFolderImported"
    h = [{"eventType": imp, "episodeId": 101, "date": "2026-01-02T00:00:00Z", "downloadId": "AAA"},
         {"eventType": imp, "episodeId": 105, "date": "2026-01-02T00:00:00Z", "downloadId": "BBB"},
         {"eventType": imp, "episodeId": 102, "date": "2025-01-02T00:00:00Z", "downloadId": "OLD"},
         {"eventType": "grabbed", "episodeId": 102, "date": "2026-01-02T00:00:00Z", "downloadId": "G"}]
    s, calls = make({("GET", "/api/v3/series"): [SERIES], ("GET", "/api/v3/episode"): EPS, ("GET", "/api/v3/history/series"): h})
    assert s.hashes(SEASON, 1767225600) == ["aaa"]
    assert ("GET", "/api/v3/history/series", {"seriesId": "7", "seasonNumber": "2"}, None) in calls


def test_remove_drops_series_only_when_no_files_remain():
    base = {("GET", "/api/v3/series"): [SERIES], ("GET", "/api/v3/episode"): EPS}
    s, calls = make({**base, ("GET", "/api/v3/episodefile"): []})
    s.remove(SEASON, ["episode:81189:2:1", "episode:81189:2:2"], True)
    assert ("DELETE", "/api/v3/episodefile/51", {}, None) in calls
    assert ("PUT", "/api/v3/episode/monitor", {}, {"episodeIds": [101, 102], "monitored": False}) in calls
    assert ("DELETE", "/api/v3/series/7", {"deleteFiles": "true"}, None) in calls
    s, calls = make({**base, ("GET", "/api/v3/episodefile"): [FILE]})  # owner's file remains
    s.remove(SEASON, ["episode:81189:2:2"], True)
    assert ("PUT", "/api/v3/episode/monitor", {}, {"episodeIds": [102], "monitored": False}) in calls
    assert not any(c[0] == "DELETE" for c in calls)
    s, calls = make(base)
    s.remove(SEASON, ["episode:81189:2:1"], False)
    assert ("DELETE", "/api/v3/episodefile/51", {}, None) in calls
    assert not any(c[0] == "DELETE" and c[1] == "/api/v3/series/7" for c in calls)
    s, calls = make({})
    s.remove(SEASON, [], True)
    assert calls == []
