import json

import httpx
import pytest

from availarr.common import Item
from availarr.server.library import Unit
from availarr.server.quality import FileInfo
from availarr.server.radarr import Radarr

ITEM = Item("movie", 603)
MOVIE = {"id": 1, "title": "Dune", "year": 2021, "monitored": True, "hasFile": True, "qualityProfileId": 2,
         "movieFile": {"id": 10, "path": "/movies/Dune/Dune.mkv", "size": 9000,
                       "quality": {"quality": {"resolution": 1080, "name": "Bluray-1080p"}},
                       "languages": [{"name": "French"}, {"name": "English"}]}}
PROFILES = [{"id": 2, "name": "Any"}, {"id": 4, "name": "HD-1080p FR"}]


def make(routes):
    calls = []

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.url.params), body))
        assert req.headers["X-Api-Key"] == "k"
        out = routes.get((req.method, req.url.path), [])
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://radarr")
    return Radarr("http://radarr", "k", "/movies", client), calls


def test_units_existing_with_file():
    r, _ = make({("GET", "/api/v3/movie"): [MOVIE]})
    assert r.units(ITEM) == [Unit("movie:603", True, True, FileInfo(1080, "Bluray-1080p", ["French", "English"], "/movies/Dune/Dune.mkv", 9000))]


def test_units_no_file():
    r, _ = make({("GET", "/api/v3/movie"): [{**MOVIE, "hasFile": False}]})
    assert r.units(ITEM)[0].file is None


def test_units_missing():
    r, _ = make({})
    assert r.units(ITEM) == [Unit("movie:603", False, False, None)]


def test_error_raises():
    r, _ = make({("GET", "/api/v3/movie"): httpx.Response(500)})
    with pytest.raises(httpx.HTTPStatusError):
        r.units(ITEM)


def test_title_and_profile_name():
    r, _ = make({("GET", "/api/v3/movie"): [MOVIE], ("GET", "/api/v3/qualityprofile"): PROFILES})
    assert r.title(ITEM) == ("Dune", 2021) and r.profile_name(ITEM) == "Any"
    r, _ = make({("GET", "/api/v3/movie/lookup/tmdb"): {"title": "X", "year": 1999}})
    assert r.title(ITEM) == ("X", 1999)


def test_ensure_adds_missing():
    r, calls = make({("GET", "/api/v3/movie/lookup/tmdb"): {"title": "Dune", "tmdbId": 603},
                     ("GET", "/api/v3/qualityprofile"): PROFILES, ("GET", "/api/v3/tag"): [],
                     ("POST", "/api/v3/tag"): {"id": 1, "label": "availarr"}})
    assert r.ensure(ITEM, "HD-1080p FR", True) is True
    assert ("POST", "/api/v3/tag", {}, {"label": "availarr"}) in calls
    body = next(c[3] for c in calls if c[:2] == ("POST", "/api/v3/movie"))
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/movies" and body["tags"] == [1]
    assert body["monitored"] is True and body["addOptions"] == {"searchForMovie": True}


def test_ensure_existing_changes_profile_and_searches():
    r, calls = make({("GET", "/api/v3/movie"): [MOVIE], ("GET", "/api/v3/qualityprofile"): PROFILES})
    assert r.ensure(ITEM, "HD-1080p FR", True) is False
    put = next(c[3] for c in calls if c[0] == "PUT")
    assert put["qualityProfileId"] == 4 and put["title"] == "Dune" and put["id"] == 1
    assert ("POST", "/api/v3/command", {}, {"name": "MoviesSearch", "movieIds": [1]}) in calls


def test_progress():
    r, _ = make({("GET", "/api/v3/movie"): [MOVIE], ("GET", "/api/v3/queue/details"): [{"size": 100, "sizeleft": 25}]})
    assert r.progress(ITEM) == 0.75
    r, _ = make({("GET", "/api/v3/movie"): [MOVIE]})
    assert r.progress(ITEM) == 0.0


def test_hashes_filters_event_and_date():
    h = [{"eventType": "grabbed", "date": "2026-01-02T00:00:00Z", "downloadId": "G"},
         {"eventType": "downloadFolderImported", "date": "2025-01-01T00:00:00Z", "downloadId": "OLD"},
         {"eventType": "downloadFolderImported", "date": "2026-01-02T00:00:00Z", "downloadId": "ABC"},
         {"eventType": "downloadFolderImported", "date": "2026-01-03T00:00:00Z", "downloadId": "ABC"}]
    r, _ = make({("GET", "/api/v3/movie"): [MOVIE], ("GET", "/api/v3/history/movie"): h})
    assert r.hashes(ITEM, 1767225600, []) == ["abc"]  # 2026-01-01 UTC
    assert r.hashes(ITEM, 1767225600, ["movie:603"]) == []  # protected: A's own torrent


def test_remove_drop_entry_and_keep_entry():
    r, calls = make({("GET", "/api/v3/movie"): [MOVIE]})
    r.remove(ITEM, ["movie:603"], True)
    assert ("DELETE", "/api/v3/movie/1", {"deleteFiles": "true"}, None) in calls
    r, calls = make({("GET", "/api/v3/movie"): [MOVIE]})
    r.remove(ITEM, ["movie:603"], False)
    assert ("DELETE", "/api/v3/moviefile/10", {}, None) in calls
    put = next(c[3] for c in calls if c[0] == "PUT")
    assert put["monitored"] is False and put["title"] == "Dune" and put["id"] == 1
    assert "movieFile" not in put and put["hasFile"] is False
    assert not any(c[0] == "DELETE" and c[1] == "/api/v3/movie/1" for c in calls)
    r, calls = make({})
    r.remove(ITEM, [], True)
    assert calls == []


def test_unknown_profile_and_empty_lookup_raise():
    r, _ = make({("GET", "/api/v3/qualityprofile"): PROFILES})
    with pytest.raises(ValueError, match="unknown quality profile"):
        r._profile_id("nope")
    r, _ = make({})
    with pytest.raises(ValueError):
        r.title(ITEM)
