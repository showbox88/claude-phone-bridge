"""Nearby POI search — Foursquare + 高德 + OSM Overpass fallback."""
from __future__ import annotations

import asyncio
import logging
import math

from fastapi import APIRouter, HTTPException

from app.settings import settings

log = logging.getLogger("bridge")
router = APIRouter()


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Approximate distance in metres between two lat/lng points."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def _overpass_query(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Query OpenStreetMap Overpass for nearby named POIs."""
    import aiohttp
    q = (
        f"[out:json][timeout:8];"
        f"(nwr[\"amenity\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"shop\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"tourism\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"leisure\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"office\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"craft\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"healthcare\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"building\"=\"retail\"][\"name\"](around:{radius_m},{lat},{lng});"
        f" nwr[\"building\"=\"commercial\"][\"name\"](around:{radius_m},{lat},{lng}););"
        f"out center 40;"
    )
    out: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": q},
                headers={"User-Agent": "PhoneBridge/0.1 (checkin POI lookup)"},
            ) as r:
                if r.status != 200:
                    log.warning("Overpass HTTP %d", r.status)
                    return []
                data = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Overpass query failed: %s", e)
        return []

    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:en") or tags.get("name:zh") or tags.get("brand")
        if not name:
            continue
        p_lat = el.get("lat")
        p_lng = el.get("lon")
        if p_lat is None or p_lng is None:
            c = el.get("center") or {}
            p_lat = c.get("lat")
            p_lng = c.get("lon")
        if p_lat is None or p_lng is None:
            continue
        kind = (tags.get("amenity") or tags.get("shop")
                or tags.get("tourism") or tags.get("leisure")
                or tags.get("office") or tags.get("craft")
                or tags.get("healthcare") or tags.get("building") or "")
        el_type = el.get("type", "node")
        out.append({
            "name": str(name)[:80],
            "lat": float(p_lat),
            "lng": float(p_lng),
            "distance_m": int(round(_haversine_m(lat, lng, float(p_lat), float(p_lng)))),
            "type": kind,
            "address": tags.get("addr:street", "") or tags.get("addr:full", ""),
            "city": tags.get("addr:city", ""),
            "osm_id": f"{el_type}/{el.get('id')}" if el.get("id") else "",
            "amap_poi_id": "",
            "fsq_id": "",
            "source": "osm",
        })
    return out


async def _foursquare_query(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Query Foursquare Places API for nearby POIs. Requires FOURSQUARE_KEY env."""
    key = settings.foursquare_key.strip()
    if not key:
        return []
    import aiohttp
    out: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        params = {
            "ll": f"{lat},{lng}",
            "radius": str(radius_m),
            "limit": "25",
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "X-Places-Api-Version": "2025-06-17",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(
                "https://places-api.foursquare.com/places/search",
                params=params, headers=headers,
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("Foursquare HTTP %d: %s", r.status, body[:200])
                    return []
                data = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Foursquare query failed: %s: %s", type(e).__name__, e)
        return []

    for p in data.get("results") or []:
        name = p.get("name")
        if not name:
            continue
        p_lat = p.get("latitude")
        p_lng = p.get("longitude")
        if p_lat is None or p_lng is None:
            geo = (p.get("geocodes") or {}).get("main") or {}
            p_lat = geo.get("latitude"); p_lng = geo.get("longitude")
        if p_lat is None or p_lng is None:
            continue
        cats = p.get("categories") or []
        kind = cats[0]["name"] if cats and cats[0].get("name") else ""
        loc = p.get("location") or {}
        out.append({
            "name": str(name)[:80],
            "lat": float(p_lat),
            "lng": float(p_lng),
            "distance_m": int(p.get("distance") or
                              round(_haversine_m(lat, lng, float(p_lat), float(p_lng)))),
            "type": kind,
            "address": loc.get("address", "") or loc.get("formatted_address", ""),
            "city": loc.get("locality", "") or loc.get("region", ""),
            "osm_id": "",
            "amap_poi_id": "",
            "fsq_id": p.get("fsq_place_id") or p.get("fsq_id") or "",
            "source": "fsq",
        })
    return out


async def _amap_query(lat: float, lng: float, radius_m: int) -> list[dict]:
    """Query 高德 Web API /place/around. Requires AMAP_KEY env."""
    key = settings.amap_key.strip()
    if not key:
        return []
    import aiohttp
    out: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        params = {
            "location": f"{lng},{lat}",  # NB: 高德 uses lng,lat order
            "radius": str(radius_m),
            "extensions": "base",
            "offset": "25",
            "page": "1",
            "key": key,
        }
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(
                "https://restapi.amap.com/v3/place/around",
                params=params,
            ) as r:
                if r.status != 200:
                    log.warning("Amap HTTP %d", r.status)
                    return []
                data = await r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Amap query failed: %s", e)
        return []

    if str(data.get("status")) != "1":
        log.warning("Amap error: %s", data.get("info"))
        return []
    for p in data.get("pois") or []:
        loc = (p.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            p_lng = float(loc[0]); p_lat = float(loc[1])
        except ValueError:
            continue
        out.append({
            "name": str(p.get("name") or "")[:80],
            "lat": p_lat,
            "lng": p_lng,
            "distance_m": int(p.get("distance") or
                              round(_haversine_m(lat, lng, p_lat, p_lng))),
            "type": p.get("type", "").split(";")[0] if p.get("type") else "",
            "address": p.get("address") or "",
            "city": p.get("cityname") or "",
            "osm_id": "",
            "amap_poi_id": p.get("id") or "",
            "fsq_id": "",
            "source": "amap",
        })
    return out


def _merge_pois(lists: list[list[dict]]) -> list[dict]:
    """Combine multiple POI lists, dedup by (lowercased name, ~30m radius)."""
    merged: list[dict] = []
    for src in lists:
        for p in src:
            collapsed = False
            for m in merged:
                if (m["name"].lower() == p["name"].lower()
                        and _haversine_m(m["lat"], m["lng"], p["lat"], p["lng"]) < 30):
                    for k in ("osm_id", "amap_poi_id", "fsq_id"):
                        if not m.get(k) and p.get(k):
                            m[k] = p[k]
                    if not m.get("address") and p.get("address"):
                        m["address"] = p["address"]
                    if not m.get("city") and p.get("city"):
                        m["city"] = p["city"]
                    if p["distance_m"] < m["distance_m"]:
                        m["distance_m"] = p["distance_m"]
                    collapsed = True
                    break
            if not collapsed:
                merged.append(dict(p))
    merged.sort(key=lambda x: x["distance_m"])
    return merged


@router.get("/api/poi/around")
async def api_poi_around(lat: float, lng: float, radius: int = 200):
    """Return nearby POIs from Foursquare (US/global commercial), 高德 (CN),
    and OSM Overpass (global fallback). Empty list on total failure — UI
    should let the user type a name manually in that case."""
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(400, "invalid lat/lng")
    radius = max(50, min(int(radius), 1000))
    fsq_task  = asyncio.create_task(_foursquare_query(lat, lng, radius))
    amap_task = asyncio.create_task(_amap_query(lat, lng, radius))
    osm_task  = asyncio.create_task(_overpass_query(lat, lng, radius))
    fsq_pois, amap_pois, osm_pois = await asyncio.gather(fsq_task, amap_task, osm_task)
    merged = _merge_pois([fsq_pois, amap_pois, osm_pois])
    return {"pois": merged[:15], "lat": lat, "lng": lng, "radius_m": radius}
