import httpx

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
