from dataclasses import dataclass

SOURCES = {"Bluray": "BluRay", "WEBDL": "WEB-DL", "WEBRip": "WEBRip", "HDTV": "HDTV", "Remux": "REMUX"}


@dataclass
class FileInfo:
    resolution: int
    quality: str
    languages: list[str]
    path: str
    size: int


@dataclass
class Requirement:
    a_profiles: list[str]  # [0] used for adds/upgrades; any listed one counts as sufficient
    min_resolution: int
    languages: list[str]
    announce: str


def satisfies(f: FileInfo | None, req: Requirement) -> bool:
    return f is not None and f.resolution >= req.min_resolution and all(l in f.languages for l in req.languages)


def file_tags(f: FileInfo) -> str:
    if len(f.languages) > 1:
        lang = "MULTi"
    else:
        lang = f.languages[0].upper() if f.languages and f.languages[0] != "English" else ""
    src = f.quality.split("-")[0]
    return ".".join(p for p in (lang, f"{f.resolution}p", SOURCES.get(src, src)) if p)
