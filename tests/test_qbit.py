from urllib.parse import parse_qs

import httpx
import pytest

from availarr.server.qbit import QBit


H = "a" * 40


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
    q, calls = make([{"hash": H, "seeding_time": 3600}])
    assert q.seeding_seconds(H) == 3600
    assert calls[0] == ("POST", "/api/v2/auth/login", {"username": ["u"], "password": ["p"]})
    assert make([])[0].seeding_seconds(H) is None


def test_delete_form():
    q, calls = make([])
    q.delete(H)
    assert calls[-1] == ("POST", "/api/v2/torrents/delete", {"hashes": [H], "deleteFiles": ["true"]})


def test_relogin_on_403_once():
    q, calls = make([{"hash": H, "seeding_time": 5}], fail_first=True)
    assert q.seeding_seconds(H) == 5
    assert [c[1] for c in calls].count("/api/v2/auth/login") == 2


def test_errors_raise():
    with pytest.raises(httpx.HTTPStatusError):
        make(httpx.Response(500))[0].seeding_seconds(H)
    with pytest.raises(httpx.HTTPStatusError):  # persistent 403 after the one retry
        make(httpx.Response(403))[0].seeding_seconds(H)


def test_bad_hash_rejected_without_request():
    q, calls = make([])
    for bad in ("all", "", "  ", "abc"):
        with pytest.raises(ValueError):
            q.delete(bad)
        with pytest.raises(ValueError):
            q.seeding_seconds(bad)
    assert calls == []


def test_hash_lowercased_and_mismatch_is_absent():
    q, calls = make([{"hash": "b" * 40, "seeding_time": 9}])
    assert q.seeding_seconds(H.upper()) is None
    assert calls[-1][2] == {}
    q, calls = make([])
    q.delete(H.upper())
    assert calls[-1][2]["hashes"] == [H]


def test_live_batches_valid_hashes():
    q, calls = make([{"hash": H.upper(), "progress": 0.5, "eta": 600}])
    assert q.live([H, "", "all", H]) == {H: (0.5, 600)}
    assert [c for c in calls if c[1] == "/api/v2/torrents/info"]
    assert make([])[0].live(["bad"]) == {}
    assert make([{"hash": H, "progress": 0.1, "eta": 8640000}])[0].live([H]) == {H: (0.1, None)}
    assert make([{"hash": H, "progress": 1, "eta": 8640000}])[0].live([H]) == {H: (1.0, 0)}
