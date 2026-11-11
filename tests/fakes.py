from availarr.server.config import ServerConfig
from availarr.server.quality import Requirement


def make_cfg(**overrides) -> ServerConfig:
    base = dict(
        token="change-me", admin_password="change-me",
        radarr_url="http://radarr:7878", radarr_key="...", radarr_root="/movies",
        sonarr_url="http://sonarr:8989", sonarr_key="...", sonarr_root="/tv",
        qbit_url="http://qbittorrent:8080", qbit_user="admin", qbit_pass="...",
        bandwidth_mbps=200, keep_days=7, seed_days=3, fail_days=7, movie_gb=8, episode_gb=2,
        profiles={"HD-1080p FR": Requirement(["1080p MULTi"], 1080, ["French"], "MULTi.1080p.WEB-DL")},
    )
    return ServerConfig(**{**base, **overrides})


class _Attr:
    """Plain settable attribute (lib.progress = 0.5) that is read back as the Library method (lib.progress(item))."""

    def __init__(self, conv=lambda v: v):
        self.conv = conv

    def __set_name__(self, owner, name):
        self.name = "_" + name

    def __get__(self, obj, owner=None):
        return lambda *a: getattr(obj, self.name)

    def __set__(self, obj, value):
        setattr(obj, self.name, self.conv(value))


class FakeLibrary:
    units, title, progress, hashes = _Attr(), _Attr(), _Attr(), _Attr(list)

    def __init__(self, units, title=("Dune", 2024), profile=None, progress=0.0, hashes=()):
        self.profile = profile
        self.units = units
        self.title, self.progress, self.hashes = title, progress, hashes
        self.ensure_calls, self.removed = [], []

    def profile_name(self, item):
        return self.profile

    def ensure(self, item, profile, change_profile):
        self.ensure_calls.append((item, profile, change_profile))
        return not any(u.exists for u in self._units)

    def remove(self, item, unit_keys, drop_entry):
        self.removed.append((item, unit_keys, drop_entry))


class FakeQbit:
    def __init__(self, seed):
        self.seed, self.deleted = seed, []

    def seeding_seconds(self, h):
        return self.seed.get(h)
