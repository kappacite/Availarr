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
