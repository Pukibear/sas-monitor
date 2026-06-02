"""
SAS EuroBonus Monitor – Backend + Frontend server
==================================================
Kjører på Railway. Serverer både API og PWA-appen.

Lokalt:   uvicorn scraper:app --port 8000
Railway:  Startes automatisk via Procfile
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="SAS EuroBonus Monitor", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Cache ──────────────────────────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 3600

def cache_get(key):
    e = _cache.get(key)
    if e and (datetime.utcnow() - e["ts"]).seconds < CACHE_TTL:
        return e["data"]
    return None

def cache_set(key, data):
    _cache[key] = {"ts": datetime.utcnow(), "data": data}


# ── Parser ─────────────────────────────────────────────────────────────────
def parse_sas_response(data: dict, origin: str, destination: str, date: str) -> list[dict]:
    flights = []
    offers = (data.get("outboundFlights") or {}).get("flightOffers") or []
    for offer in offers:
        business = None
        for cabin in offer.get("cabinOffers") or []:
            if cabin.get("cabin", "").lower() in ("business", "plus", "sas business"):
                business = cabin
                break
        if not business:
            continue
        availability = business.get("availability") or business.get("availableSeats") or 0
        points_raw = (
            business.get("totalPoints") or business.get("points")
            or (business.get("price") or {}).get("points") or 0
        )
        if not points_raw or int(points_raw) < 1:
            continue
        segments = offer.get("segments") or []
        if not segments:
            continue
        first  = segments[0]
        last   = segments[-1]
        dep_dt = (first.get("departure") or {}).get("dateTime", "")
        arr_dt = (last.get("arrival")    or {}).get("dateTime", "")
        flights.append({
            "id":                 f"{origin}-{destination}-{date}-{first.get('flightNumber','')}",
            "origin":             origin,
            "destination":        destination,
            "date":               date,
            "flight_number":      first.get("flightNumber", ""),
            "departure":          dep_dt[11:16] if len(dep_dt) > 10 else dep_dt,
            "arrival":            arr_dt[11:16] if len(arr_dt) > 10 else arr_dt,
            "stops":              len(segments) - 1,
            "business_available": True,
            "business_points":    int(points_raw),
            "business_seats":     int(availability) if str(availability).isdigit() else 0,
            "business_direct":    len(segments) == 1,
            "business_airlines":  (first.get("operatingCarrier") or {}).get("code", "SK"),
            "aircraft":           (first.get("equipment") or {}).get("code", ""),
            "source":             "playwright",
            "updated_at":         datetime.utcnow().isoformat(),
        })
    return flights


# ── Playwright fetch ───────────────────────────────────────────────────────
async def fetch_with_playwright(origin: str, destination: str, date: str, passengers: int = 2) -> list[dict]:
    date_compact = date.replace("-", "")
    url = (
        f"https://www.sas.no/book/flights/"
        f"?search=OW_{origin}-{destination}-{date_compact}_a{passengers}c0i0y0"
        f"&view=upsell&bookingFlow=points&sortBy=rec&filterBy=all"
    )
    log.info(f"Playwright: {origin}→{destination} {date}")
    captured: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="nb-NO", timezone_id="Europe/Oslo",
            viewport={"width": 1280, "height": 800},
        )
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        async def on_response(response):
            if "/api/offers/flights" in response.url and not future.done():
                try:
                    body = await response.json()
                    future.set_result(body)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                data = await asyncio.wait_for(asyncio.shield(future), timeout=25)
                captured = parse_sas_response(data, origin, destination, date)
                log.info(f"  → {len(captured)} business class-avganger")
            except asyncio.TimeoutError:
                log.warning(f"  → Timeout: {origin}→{destination} {date}")
        except PWTimeout:
            log.warning(f"  → Sideinnlasting timeout")
        finally:
            await browser.close()

    return captured


# ── API-endepunkter ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/search")
async def search(
    origin:      str  = Query(...),
    destination: str  = Query(...),
    start_date:  str  = Query(None),
    end_date:    str  = Query(None),
    passengers:  int  = Query(2),
    direct_only: bool = Query(False),
):
    if not start_date:
        start_date = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = (datetime.utcnow() + timedelta(days=330)).strftime("%Y-%m-%d")

    cache_key = f"pw-{origin}-{destination}-{start_date}-{end_date}-{passengers}"
    cached = cache_get(cache_key)
    if cached:
        return {"source": "cache", "count": len(cached), "data": cached}

    dates, cur, end_dt = [], datetime.strptime(start_date, "%Y-%m-%d"), datetime.strptime(end_date, "%Y-%m-%d")
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=7)

    all_flights = []
    for date in dates:
        found = await fetch_with_playwright(origin, destination, date, passengers)
        if direct_only:
            found = [f for f in found if f["business_direct"]]
        all_flights.extend(found)
        await asyncio.sleep(3)

    all_flights.sort(key=lambda x: x["date"])
    cache_set(cache_key, all_flights)
    return {"source": "playwright", "count": len(all_flights), "data": all_flights}


@app.get("/routes")
async def search_multiple_routes(
    origins:      str  = Query(...),
    destinations: str  = Query(...),
    start_date:   str  = Query(None),
    end_date:     str  = Query(None),
    passengers:   int  = Query(2),
    direct_only:  bool = Query(False),
):
    pairs = [(o.strip(), d.strip()) for o in origins.split(",") for d in destinations.split(",")]
    all_flights, errors = [], []
    for origin, dest in pairs:
        try:
            result = await search(origin=origin, destination=dest,
                                  start_date=start_date, end_date=end_date,
                                  passengers=passengers, direct_only=direct_only)
            all_flights.extend(result.get("data", []))
            await asyncio.sleep(4)
        except Exception as e:
            errors.append(f"{origin}→{dest}: {str(e)}")
    all_flights.sort(key=lambda x: x.get("date", ""))
    return {"count": len(all_flights), "errors": errors, "data": all_flights}


# ── PWA-filer ──────────────────────────────────────────────────────────────
@app.get("/manifest.json")
async def manifest():
    return {
        "name": "SAS Bonus Monitor",
        "short_name": "Bonus Monitor",
        "description": "Overvåk EuroBonus bonusbilletter i business class",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#060810",
        "theme_color": "#4a8fde",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
            {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml"},
        ],
    }


@app.get("/icon-192.svg")
@app.get("/icon-512.svg")
async def icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <rect width="192" height="192" rx="40" fill="#060810"/>
  <rect width="192" height="192" rx="40" fill="#1a3a70" opacity=".6"/>
  <text x="96" y="130" font-size="110" text-anchor="middle" font-family="serif">✈️</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/sw.js")
async def service_worker():
    sw = """
const CACHE = 'sas-monitor-v1';
const ASSETS = ['/'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/health') || e.request.url.includes('/routes') || e.request.url.includes('/search')) return;
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
"""
    return Response(content=sw, media_type="application/javascript")


# ── Serve frontend ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_app():
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>Finner ikke index.html – sjekk at static/index.html finnes.</h1>"
