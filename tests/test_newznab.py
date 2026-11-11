import base64
import json
import xml.etree.ElementTree as ET

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from availarr.agent.config import AgentConfig
from availarr.agent.newznab import router
from availarr.common import Item, parse_nzb

CFG = AgentConfig(server_url="u", token="t", api_key="K", public_url="http://agent:8789", complete_dir="/c",
                  radarr_url="", radarr_key="", sonarr_url="", sonarr_key="")


class FakeServer:
    def __init__(self, res=None, exc=None):
        self.res, self.exc, self.calls = res, exc, []

    def lookup(self, item, b_profile):
        self.calls.append(item)
        if self.exc:
            raise self.exc
        return self.res


class FakeB:
    def b_profile(self, item):
        return "HD-1080p FR"


REL = {"title": "Dune.2024.MULTi.1080p.BluRay-Availarr", "size": 9000}


def client(server):
    app = FastAPI()
    app.include_router(router(CFG, server, FakeB()))
    return TestClient(app)


def items(r):
    return ET.fromstring(r.text).findall(".//item")


def test_caps():
    r = client(FakeServer()).get("/newznab/api", params={"t": "caps"})
    assert 'supportedParams="q,tmdbid"' in r.text and 'supportedParams="q,tvdbid,season,ep"' in r.text


def test_bad_apikey():
    r = client(FakeServer(REL)).get("/newznab/api", params={"t": "movie", "tmdbid": 603, "apikey": "no"})
    assert 'code="100"' in r.text and "<item>" not in r.text


def test_movie_search_returns_release():
    r = client(FakeServer(REL)).get("/newznab/api", params={"t": "movie", "tmdbid": 603, "apikey": "K"})
    (it,) = items(r)
    assert it.find("title").text == REL["title"]
    assert it.find("enclosure").get("length") == "9000"
    assert it.find("enclosure").get("type") == "application/x-nzb"
    attrs = {a.get("name"): a.get("value") for a in it if a.tag.endswith("}attr")}
    assert attrs == {"category": "2000", "size": "9000"}
    p = it.find("link").text.split("&p=")[1]
    assert json.loads(base64.urlsafe_b64decode(p)) == {
        "item": "movie:603", "b_profile": "HD-1080p FR", "title": REL["title"], "size": 9000}


def test_lookup_none_gives_empty_rss():
    r = client(FakeServer(None)).get("/newznab/api", params={"t": "movie", "tmdbid": 603, "apikey": "K"})
    assert items(r) == []


def test_lookup_http_error_gives_empty_rss():
    s = FakeServer(exc=httpx.ConnectError("down"))
    r = client(s).get("/newznab/api", params={"t": "movie", "tmdbid": 603, "apikey": "K"})
    assert r.status_code == 200 and items(r) == []


def test_rss_without_id_is_empty():
    s = FakeServer(REL)
    c = client(s)
    assert items(c.get("/newznab/api", params={"t": "movie", "apikey": "K"})) == []
    assert items(c.get("/newznab/api", params={"t": "search", "q": "x", "apikey": "K"})) == []
    assert s.calls == []


def test_tvsearch_season_vs_episode():
    s = FakeServer(REL)
    c = client(s)
    c.get("/newznab/api", params={"t": "tvsearch", "tvdbid": 81189, "season": 2, "apikey": "K"})
    c.get("/newznab/api", params={"t": "tvsearch", "tvdbid": 81189, "season": 2, "ep": 5, "apikey": "K"})
    assert s.calls == [Item("season", 81189, 2), Item("episode", 81189, 2, 5)]


def test_nzb_endpoint():
    c = client(FakeServer(REL))
    payload = {"item": "movie:603", "b_profile": "x", "title": "T", "size": 1}
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    r = c.get("/newznab/nzb", params={"apikey": "K", "p": p})
    assert r.headers["content-type"].startswith("application/x-nzb")
    assert parse_nzb(r.content) == payload
    assert c.get("/newznab/nzb", params={"apikey": "K", "p": "!!"}).status_code == 400
    assert 'code="100"' in c.get("/newznab/nzb", params={"apikey": "bad", "p": p}).text


def test_non_int_params_give_empty_rss_not_422():
    s = FakeServer(REL)
    c = client(s)
    for params in ({"t": "tvsearch", "tvdbid": 1, "season": "", "ep": "11/11"},
                   {"t": "tvsearch", "tvdbid": "x", "season": 1},
                   {"t": "movie", "tmdbid": "abc"}):
        r = c.get("/newznab/api", params={**params, "apikey": "K"})
        assert r.status_code == 200 and items(r) == []
    assert s.calls == []
    c.get("/newznab/api", params={"t": "tvsearch", "tvdbid": 1, "season": 2, "ep": "11/11", "apikey": "K"})
    assert s.calls == [Item("season", 1, 2)]


def test_nzb_bad_payload_shapes_400():
    c = client(FakeServer(REL))
    for obj in ([1], {"item": "x"}, 5):
        p = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        assert c.get("/newznab/nzb", params={"apikey": "K", "p": p}).status_code == 400


def test_lookup_value_error_empty_and_link_quoting():
    r = client(FakeServer(exc=ValueError("x"))).get("/newznab/api", params={"t": "movie", "tmdbid": 1, "apikey": "K"})
    assert items(r) == []
    cfg = AgentConfig(server_url="u", token="t", api_key="a&b", public_url="http://h/", complete_dir="/c",
                      radarr_url="", radarr_key="", sonarr_url="", sonarr_key="")
    app = FastAPI()
    app.include_router(router(cfg, FakeServer(REL), FakeB()))
    r = TestClient(app).get("/newznab/api", params={"t": "movie", "tmdbid": 1, "apikey": "a&b"})
    assert items(r)[0].find("link").text.startswith("http://h/newznab/nzb?apikey=a%26b&p=")
