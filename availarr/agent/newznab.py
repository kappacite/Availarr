import base64
import json
import logging
import secrets
from urllib.parse import quote
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
from fastapi import APIRouter, Response

from availarr.common import Item, build_nzb

log = logging.getLogger(__name__)
ATTR_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
PLACEHOLDER = "Availarr.Placeholder-Availarr"
_ERR = '<error code="100" description="Incorrect user credentials"/>'
_CAPS = (
    '<?xml version="1.0" encoding="UTF-8"?><caps><server title="Availarr"/>'
    '<searching><search available="yes" supportedParams="q"/>'
    '<tv-search available="yes" supportedParams="q,tvdbid,season,ep"/>'
    '<movie-search available="yes" supportedParams="q,tmdbid"/></searching>'
    '<categories><category id="2000" name="Movies"/><category id="5000" name="TV"/></categories></caps>'
)


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _xml(body: str | bytes, media="application/xml") -> Response:
    return Response(body, media_type=media)


def _rss(items: list[dict]) -> Response:
    ET.register_namespace("newznab", ATTR_NS)
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Availarr"
    for it in items:
        e = ET.SubElement(ch, "item")
        for tag in ("title", "guid", "link"):
            ET.SubElement(e, tag).text = it[tag]
        ET.SubElement(e, "pubDate").text = format_datetime(datetime.now(timezone.utc))
        ET.SubElement(e, "enclosure", url=it["link"], length=str(it["size"]), type="application/x-nzb")
        for name, val in (("category", it["cat"]), ("size", it["size"])):
            ET.SubElement(e, f"{{{ATTR_NS}}}attr", name=name, value=str(val))
    return _xml(ET.tostring(rss, encoding="unicode"), "application/rss+xml")


def router(cfg, server, b) -> APIRouter:
    r = APIRouter()

    def ok(apikey):
        return secrets.compare_digest(apikey.encode(), cfg.api_key.encode())

    def release(item: Item) -> list[dict]:
        try:
            prof = b.b_profile(item)
            found = server.lookup(item, prof)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("lookup failed for %s: %s", item.key(), e)
            return []
        if not found:
            return []
        payload = {"item": item.key(), "b_profile": prof, "title": found["title"], "size": found["size"]}
        return [{"title": found["title"], "guid": item.key() + " " + found["title"], "link": link(payload),
                 "size": found["size"], "cat": 2000 if item.kind == "movie" else 5000}]

    def link(payload: dict) -> str:
        p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return f"{cfg.public_url.rstrip('/')}/newznab/nzb?apikey={quote(cfg.api_key, safe='')}&p={p}"

    def placeholder(t: str) -> dict:
        # never grabbable: the SABnzbd side rejects it
        return {"title": PLACEHOLDER, "guid": "availarr-placeholder", "link": link({"placeholder": True}),
                "size": 1, "cat": 5000 if t == "tvsearch" else 2000}

    @r.get("/newznab/api")
    def api(t: str = "", apikey: str = "", tmdbid: str | None = None, tvdbid: str | None = None,
            season: str | None = None, ep: str | None = None):
        if t == "caps":
            return _xml(_CAPS)
        if not ok(apikey):
            return _xml(_ERR)
        if t in ("movie", "tvsearch", "search") and tmdbid is None and tvdbid is None:
            return _rss([placeholder(t)])  # RSS / connection test: Radarr/Sonarr refuse an indexer with no results
        item = None
        tmdb, tvdb, sn, epn = _int(tmdbid), _int(tvdbid), _int(season), _int(ep)
        if t == "movie" and tmdb:
            item = Item("movie", tmdb)
        elif t == "tvsearch" and tvdb and sn is not None:
            item = Item("season", tvdb, sn) if epn is None else Item("episode", tvdb, sn, epn)
        return _rss(release(item) if item else [])

    @r.get("/newznab/nzb")
    def nzb(apikey: str = "", p: str = ""):
        if not ok(apikey):
            return _xml(_ERR)
        try:
            payload = json.loads(base64.urlsafe_b64decode(p))
            if payload == {"placeholder": True}:
                return _xml(build_nzb(payload), "application/x-nzb")
            payload["item"], payload["b_profile"], payload["title"], payload["size"]
        except (ValueError, KeyError, TypeError):
            return Response("bad payload", status_code=400)
        return _xml(build_nzb(payload), "application/x-nzb")

    return r
