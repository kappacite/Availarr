import json

import httpx

from availarr.agent.transfer import ServerClient
from availarr.common import Item


def test_client_sends_item_keys_as_json():
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"release": None, "job_id": 1})

    c = ServerClient("http://s", "t", httpx.Client(base_url="http://s", transport=httpx.MockTransport(handler)))
    assert c.lookup(Item("movie", 118340), "HD-1080p") is None
    assert c.request(Item("season", 1, 2), "HD-1080p", "x") == 1
    assert [b["item"] for b in sent] == ["movie:118340", "season:1:2"]


def test_client_sends_identity():
    seen = []

    def handler(req):
        seen.append((req.headers["X-Availarr-Agent"], req.headers["X-Availarr-Agent-Name"]))
        return httpx.Response(200)

    c = ServerClient("http://s", "t", httpx.Client(base_url="http://s", transport=httpx.MockTransport(handler)),
                     agent_id="u1", agent_name="Lyon")
    c.ping()
    assert seen == [("u1", "Lyon")]


def test_client_encodes_non_ascii_name():
    seen = []
    c = ServerClient("http://s", "t", httpx.Client(base_url="http://s", transport=httpx.MockTransport(
        lambda r: seen.append(r.headers["X-Availarr-Agent-Name"]) or httpx.Response(200))),
        agent_id="u1", agent_name="Salon été")
    c.ping()
    assert seen == ["Salon%20%C3%A9t%C3%A9"]
