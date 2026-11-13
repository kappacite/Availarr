from fastapi.testclient import TestClient

from availarr import setup
from availarr.agent.config import load_agent_config
from availarr.setup import create_setup, netns_lost, tailscale_view

LOGIN = {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/abc"}


def test_view_states():
    assert "n'est pas utilisé" in tailscale_view(None, "server", True)
    assert "abc" not in tailscale_view(LOGIN, "agent", False)  # link hidden until the setup code is typed
    agent = tailscale_view(LOGIN, "agent", True)
    assert "Envoie ce lien à l'envoyeur" in agent and "https://login.tailscale.com/a/abc" in agent
    assert "Connecter à Tailscale" in tailscale_view(LOGIN, "server", True)
    run = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.1.2", "fd7a::1"]},
           "CurrentTailnet": {"Name": "me@example.com"}}
    assert "http://100.64.1.2:8787" in tailscale_view(run, "server", False)
    assert "approbation" in tailscale_view({"BackendState": "NeedsMachineAuth"}, "agent", True)
    assert "ne répond pas" in tailscale_view({"BackendState": "Unreachable"}, "agent", True)


def test_endpoint_needs_code(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "tailscale_status", lambda: LOGIN)
    c = TestClient(create_setup("agent", str(tmp_path / "a.toml"), load_agent_config, code="ABC123"))
    assert "abc" not in c.get("/tailscale", params={"code": "nope"}).text
    assert "login.tailscale.com/a/abc" in c.get("/tailscale", params={"code": "abc123"}).text
    assert 'id="ts"' in c.get("/").text


def test_netns_lost():
    assert netns_lost({"BackendState": "Running"}, None)
    assert not netns_lost({"BackendState": "Running"}, "100.64.1.2")
    assert not netns_lost({"BackendState": "NeedsLogin"}, None)
    assert not netns_lost(None, None)  # no sidecar at all
