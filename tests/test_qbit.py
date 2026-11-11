from urllib.parse import parse_qs

import httpx
import pytest

from availarr.server.qbit import QBit


def make(info, fail_first=False):
    calls = []
    state = {"first": fail_first}

    def handler(req):
        calls.append((req.method, req.url.path, parse_qs(req.content.decode())))
        if req.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if state["first"]:
            state["first"] = False
            return httpx.Response(403)
        if isinstance(info, httpx.Response):
            return info
        return httpx.Response(200, json=info)

    return QBit("http://q", "u", "p", httpx.Client(transport=httpx.MockTransport(handler), base_url="http://q")), calls


def test_seeding_and_absent():
    q, calls = make([{"seeding_time": 3600}])
    assert q.seeding_seconds("h") == 3600
    assert calls[0] == ("POST", "/api/v2/auth/login", {"username": ["u"], "password": ["p"]})
    assert make([])[0].seeding_seconds("h") is None


def test_delete_form():
    q, calls = make([])
    q.delete("h")
    assert calls[-1] == ("POST", "/api/v2/torrents/delete", {"hashes": ["h"], "deleteFiles": ["true"]})


def test_relogin_on_403_once():
    q, calls = make([{"seeding_time": 5}], fail_first=True)
    assert q.seeding_seconds("h") == 5
    assert [c[1] for c in calls].count("/api/v2/auth/login") == 2


def test_errors_raise():
    with pytest.raises(httpx.HTTPStatusError):
        make(httpx.Response(500))[0].seeding_seconds("h")
    with pytest.raises(httpx.HTTPStatusError):  # persistent 403 after the one retry
        make(httpx.Response(403))[0].seeding_seconds("h")
