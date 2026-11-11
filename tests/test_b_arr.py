import httpx
import pytest

from availarr.agent.b_arr import BArr
from availarr.common import Item


def _b(movies):
    def h(req):
        assert req.headers["X-Api-Key"] == "rk"
        if req.url.path == "/api/v3/movie":
            return httpx.Response(200, json=movies)
        assert req.url.path == "/api/v3/qualityprofile/7"
        return httpx.Response(200, json={"name": "HD-1080p FR"})
    return BArr("http://r", "rk", "http://s", "sk", client=httpx.Client(transport=httpx.MockTransport(h)))


def test_b_profile_from_library_and_default():
    assert _b([{"qualityProfileId": 7}]).b_profile(Item("movie", 603)) == "HD-1080p FR"
    assert _b([]).b_profile(Item("movie", 603)) == "default"


def test_series_path_and_non_2xx_raises():
    def h(req):
        assert req.headers["X-Api-Key"] == "sk" and req.url.host == "s"
        if req.url.path == "/api/v3/series":
            assert req.url.params["tvdbId"] == "81189"
            return httpx.Response(200, json=[{"qualityProfileId": 3}])
        return httpx.Response(200, json={"name": "SD"})
    b = BArr("http://r", "rk", "http://s", "sk", client=httpx.Client(transport=httpx.MockTransport(h)))
    assert b.b_profile(Item("season", 81189, 2)) == "SD"
    bad = BArr("http://r", "rk", "http://s", "sk",
               client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(httpx.HTTPStatusError):
        bad.b_profile(Item("movie", 1))
