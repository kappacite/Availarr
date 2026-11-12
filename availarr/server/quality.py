import os
import re
from dataclasses import dataclass

SOURCES = {"Bluray": "BluRay", "WEBDL": "WEB-DL", "WEBRip": "WEBRip", "HDTV": "HDTV", "Remux": "REMUX"}


@dataclass
class FileInfo:
    resolution: int
    quality: str
    languages: list[str]
    path: str
    size: int
    scene_name: str | None = None  # original release name recorded by the arr, if any


@dataclass
class Requirement:
    a_profiles: list[str]  # [0] used for adds/upgrades; any listed one counts as sufficient
    min_resolution: int
    languages: list[str]
    announce: str


def satisfies(f: FileInfo | None, req: Requirement) -> bool:
    return f is not None and f.resolution >= req.min_resolution and all(l in f.languages for l in req.languages)


_FRENCH = {"FRENCH", "TRUEFRENCH", "VFF", "VFQ", "VFI", "VF", "VF2"}


def original_title(f: FileInfo, req: Requirement) -> str | None:
    """A's own release name for the file (scene name, else file name), if B's Radarr/Sonarr will read the same
    resolution and languages from it; None means: use a generated title that carries them explicitly."""
    name = (f.scene_name or os.path.splitext(os.path.basename(f.path))[0]).strip()
    if not name or name in (".", "..") or any(c in name for c in '/\\<>:"|?*'):
        return None
    tokens = {t.upper() for t in re.split(r"[.\s_\-\[\]()]+", name) if t}
    res = {f"{f.resolution}P"} | ({"4K", "UHD"} if f.resolution == 2160 else set())
    if not tokens & res:
        return None
    for lang in req.languages:
        ok = {"MULTI", lang.upper()} | (_FRENCH if lang == "French" else set())
        if not tokens & ok:
            return None
    return name


def file_tags(f: FileInfo) -> str:
    if len(f.languages) > 1:
        lang = "MULTi"
    else:
        lang = f.languages[0].upper() if f.languages and f.languages[0] != "English" else ""
    src = f.quality.split("-")[0]
    return ".".join(p for p in (lang, f"{f.resolution}p", SOURCES.get(src, src)) if p)
