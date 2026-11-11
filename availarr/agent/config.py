import tomllib
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    server_url: str
    token: str
    api_key: str  # key B's Radarr/Sonarr use to talk to the agent
    public_url: str
    complete_dir: str
    radarr_url: str
    radarr_key: str
    sonarr_url: str
    sonarr_key: str
    port: int = 8789
    db_path: str = "/data/agent.db"
    categories: list[str] = field(default_factory=lambda: ["radarr", "sonarr", "tv", "movies"])
    poll_seconds: float = 30


def load_agent_config(path: str) -> AgentConfig:
    with open(path, "rb") as fh:
        d = tomllib.load(fh)

    def need(sec, key=None):
        try:
            return d[sec] if key is None else d[sec][key]
        except KeyError:
            raise ValueError(f"missing config key: {sec if key is None else f'[{sec}] {key}'}") from None

    opt = {k: d[k] for k in ("port", "db_path", "categories", "poll_seconds") if k in d}
    return AgentConfig(
        server_url=need("server_url"), token=need("token"), api_key=need("api_key"),
        public_url=need("public_url"), complete_dir=need("complete_dir"),
        radarr_url=need("radarr", "url"), radarr_key=need("radarr", "key"),
        sonarr_url=need("sonarr", "url"), sonarr_key=need("sonarr", "key"),
        **opt,
    )
