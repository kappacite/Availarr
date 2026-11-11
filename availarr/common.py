import base64
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

NS = "http://www.newzbin.com/DTD/2003/nzb"


@dataclass(frozen=True)
class Item:
    kind: Literal["movie", "season", "episode"]
    ext_id: int  # TMDB id for a movie, TVDB id for a series
    season: int | None = None
    episode: int | None = None

    def key(self) -> str:
        parts = [self.kind, self.ext_id, self.season, self.episode]
        return ":".join(str(p) for p in parts if p is not None)

    @classmethod
    def from_key(cls, s: str) -> "Item":
        kind, *nums = s.split(":")
        if len(nums) != {"movie": 1, "season": 2, "episode": 3}.get(kind, -1):
            raise ValueError(f"bad item key: {s}")
        return cls(kind, *map(int, nums))


def build_nzb(payload: dict) -> bytes:
    meta = base64.b64encode(json.dumps(payload).encode()).decode()
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<nzb xmlns="{NS}">'
        f'<head><meta type="availarr">{meta}</meta></head>'
        '<file poster="availarr" date="0" subject="availarr">'
        '<groups><group>alt.binaries.availarr</group></groups>'
        '<segments><segment bytes="1" number="1">availarr@availarr</segment></segments>'
        "</file></nzb>"
    ).encode()


def parse_nzb(data: bytes) -> dict:
    try:
        meta = ET.fromstring(data).find(f".//{{{NS}}}meta[@type='availarr']")
        return json.loads(base64.b64decode(meta.text))
    except Exception as e:
        raise ValueError("not an Availarr NZB") from e


def release_title(name: str, year: int | None, item: Item, tags: str) -> str:
    parts = [re.sub(r"\.+", ".", "".join(c if c.isalnum() else "." for c in name)).strip(".")]
    if item.kind == "movie":
        if year:
            parts.append(str(year))
    elif item.kind == "season":
        parts.append(f"S{item.season:02d}")
    else:
        parts.append(f"S{item.season:02d}E{item.episode:02d}")
    parts.append(tags)
    return ".".join(parts) + "-Availarr"
