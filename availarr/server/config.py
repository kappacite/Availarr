import tomllib
from dataclasses import dataclass, field

from availarr.server.quality import Requirement


@dataclass
class ServerConfig:
    token: str
    admin_password: str
    radarr_url: str
    radarr_key: str
    radarr_root: str
    sonarr_url: str
    sonarr_key: str
    sonarr_root: str
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    api_port: int = 8787
    panel_port: int = 8788
    db_path: str = "/data/availarr.db"
    bandwidth_mbps: float = 0.0  # 0 = unlimited
    keep_days: float = 7.0
    seed_days: float = 3.0
    fail_days: float = 7.0
    movie_gb: float = 8.0
    episode_gb: float = 2.0
    profiles: dict[str, Requirement] = field(default_factory=dict)


def load_server_config(path: str) -> ServerConfig:
    with open(path, "rb") as fh:
        d = tomllib.load(fh)

    def need(sec, key=None):
        try:
            return d[sec] if key is None else d[sec][key]
        except KeyError:
            raise ValueError(f"missing config key: {sec if key is None else f'[{sec}] {key}'}") from None

    opt = {k: d[k] for k in ("api_port", "panel_port", "db_path", "bandwidth_mbps") if k in d}
    ret, sz = d.get("retention", {}), d.get("sizes", {})
    for k in ("keep_days", "seed_days", "fail_days"):
        if k in ret:
            opt[k] = ret[k]
    for k in ("movie_gb", "episode_gb"):
        if k in sz:
            opt[k] = sz[k]
    return ServerConfig(
        token=need("token"), admin_password=need("admin_password"),
        radarr_url=need("radarr", "url"), radarr_key=need("radarr", "key"), radarr_root=need("radarr", "root_folder"),
        sonarr_url=need("sonarr", "url"), sonarr_key=need("sonarr", "key"), sonarr_root=need("sonarr", "root_folder"),
        qbit_url=need("qbittorrent", "url"), qbit_user=need("qbittorrent", "user"), qbit_pass=need("qbittorrent", "password"),
        profiles={n: Requirement(**p) for n, p in d.get("profiles", {}).items()},
        **opt,
    )
