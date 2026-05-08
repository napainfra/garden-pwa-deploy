"""
Veg Garden PWA — public no-login dashboard with PIN gate.

Routes:
  GET /            -> if no auth cookie -> /pin ; else dashboard
  GET /pin         -> PIN entry form
  POST /pin        -> validate + set cookie
  GET /api/state   -> JSON state (auth cookie required)
  POST /api/start  -> trigger 5-min drip
  POST /api/stop   -> stop drip
  GET /manifest.json, /icons/*  -> PWA assets
"""
import os, json, hmac, hashlib, time, secrets, datetime as dt, subprocess, ssl, threading
from urllib import request as ur
from urllib.error import HTTPError, URLError
try:
    import websocket  # websocket-client
except ImportError:
    websocket = None
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # py<3.9

LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "America/Toronto"))
from fastapi import FastAPI, Request, Response, Form, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

# ===== CONFIG =====
# Inside HAOS addon: HA_URL=http://supervisor/core, HA_TOKEN=${SUPERVISOR_TOKEN}
# Standalone: set both manually
HA_URL    = os.environ.get("HA_URL", "http://supervisor/core")
HA_TOKEN  = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN") or ""
if not HA_TOKEN:
    raise SystemExit("HA_TOKEN or SUPERVISOR_TOKEN must be set")
APP_PIN   = os.environ.get("APP_PIN", "0000")
COOKIE_SECRET = os.environ.get("COOKIE_SECRET", "change-me-please-32bytes-random")
COOKIE_NAME = "veg_auth"
COOKIE_TTL  = 365 * 24 * 3600  # 1 year

# Freeze threshold (°C)
FREEZE_AT = float(os.environ.get("FREEZE_AT", "1.0"))

# Entity IDs (must match HA)
E_MOIST  = "sensor.veg_soil_moisture"
E_TEMP   = "sensor.veg_soil_temperature"
E_BAT    = "sensor.veg_soil_battery"
E_VALVE  = "valve.veg_garden"
E_WATER  = "binary_sensor.veg_garden_watering"
E_REMAIN = "sensor.veg_garden_remaining_watering_time"
E_NEXT   = "sensor.veg_garden_next_cycle"
E_DAILY  = "sensor.veg_garden_daily_active_watering_time"
E_MANUAL = "switch.veg_garden_manual_watering"
E_RAIN   = "binary_sensor.allview_sprinklers_rain_sensor"
E_WEATHER= "weather.forecast_home"
E_SUN    = "sun.sun"
# Garden solar exposure proxy: single Enphase IQ7A microinverter slightly
# east-facing, mounted on roof. Closely mimics garden's E/W sun pattern
# with afternoon shadow from the house. IQ7A peak ~366W.
E_SOLAR_PANEL = "sensor.inverter_202228046476"
SOLAR_PANEL_PEAK_W = 366.0  # IQ7A nameplate, used as denominator for % full sun

# Camera (Eufy via eufy-security-ws + go2rtc bridge)
E_CAMERA   = "camera.veg_garden"
# The veg-garden physical camera (T8114-Z) was added before the bridge
# migration, so its eufy_security image entity ended up with the slug
# image.backyard_event_image but is actually pointed at the veg garden.
# Verified visually 2026-05-08: that JPEG returns the tarp/garden frame.
E_IMAGE    = "image.backyard_event_image"
E_DEBUG    = "binary_sensor.backyard_debug_device"  # carries rtspStreamUrl
# Default fallback if rtspStreamUrl can't be read (cam IP can change on DHCP):
RTSP_URL_DEFAULT = os.environ.get("VEG_RTSP_URL", "rtsp://192.168.50.99/live3")
GO2RTC_URL = os.environ.get("GO2RTC_URL", "http://homeassistant.local.hass.io:1984")
GO2RTC_SRC = os.environ.get("GO2RTC_SRC", "veg_garden")

# ===== HA CLIENT =====
def ha(method: str, path: str, body=None):
    req = ur.Request(
        f"{HA_URL}/api{path}",
        method=method,
        headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        return json.loads(ur.urlopen(req, timeout=10).read())
    except HTTPError as e:
        return {"error": f"http {e.code}", "body": e.read().decode()[:300]}
    except URLError as e:
        return {"error": f"url {e.reason}"}

def get_state(eid: str):
    r = ha("GET", f"/states/{eid}")
    return r if isinstance(r, dict) else None

def call_service(domain: str, service: str, payload: dict):
    return ha("POST", f"/services/{domain}/{service}", payload)

# ===== AUTH (PIN cookie) =====
def make_cookie(value: str) -> str:
    sig = hmac.new(COOKIE_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"

def verify_cookie(cookie: str) -> bool:
    if not cookie or "." not in cookie:
        return False
    value, sig = cookie.rsplit(".", 1)
    expected = hmac.new(COOKIE_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        ts = int(value.split("|")[0])
    except Exception:
        return False
    return (time.time() - ts) < COOKIE_TTL

def is_authed(request: Request) -> bool:
    return verify_cookie(request.cookies.get(COOKIE_NAME, ""))

# ===== APP =====
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Masha's Garden",
        "short_name": "Masha's Garden",
        "start_url": "/garden/",
        "scope": "/garden/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0b1117",
        "theme_color": "#0b1117",
        "icons": [
            {"src": "/garden/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/garden/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })

@app.get("/pin", response_class=HTMLResponse)
def pin_page(request: Request, error: str = ""):
    return templates.TemplateResponse("pin.html", {"request": request, "error": error})

@app.post("/pin")
def pin_submit(response: Response, pin: str = Form(...)):
    if pin.strip() != APP_PIN:
        return RedirectResponse(url="/garden/pin?error=1", status_code=303)
    value = f"{int(time.time())}|{secrets.token_hex(8)}"
    resp = RedirectResponse(url="/garden/", status_code=303)
    resp.set_cookie(COOKIE_NAME, make_cookie(value),
        max_age=COOKIE_TTL, httponly=True, samesite="lax", secure=True, path="/garden")
    return resp

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if not is_authed(request):
        return RedirectResponse(url="/garden/pin", status_code=303)
    return templates.TemplateResponse("dashboard.html", {"request": request})

# ===== state assembly =====
def humanize_age(iso: str) -> str:
    if not iso: return "never"
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z","+00:00"))
    except Exception:
        return iso
    delta = dt.datetime.now(dt.timezone.utc) - t
    s = int(delta.total_seconds())
    if s < 60:   return f"{s}s ago"
    if s < 3600: return f"{s//60}m ago"
    if s < 86400:return f"{s//3600}h ago"
    return f"{s//86400}d ago"

def fmt_local(iso: str) -> str:
    """Format an ISO timestamp in America/Toronto."""
    if not iso: return ""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z","+00:00"))
        return t.astimezone(LOCAL_TZ).strftime("%a %-I:%M %p")
    except Exception:
        return iso

# Hydrawise zones (controller-level + all per-zone valves)
HYDRAWISE_ZONES = [
    ("valve.front_lawn",            "sensor.front_lawn_next_cycle",            "binary_sensor.front_lawn_watering"),
    ("valve.front_lawn_2",          "sensor.front_lawn_2_next_cycle",          "binary_sensor.front_lawn_2_watering"),
    ("valve.front_flowerbed",       "sensor.front_flowerbed_next_cycle",       "binary_sensor.front_flowerbed_watering"),
    ("valve.4",                     "sensor.4_next_cycle",                     "binary_sensor.4_watering"),
    ("valve.left_side_floweres",    "sensor.left_side_floweres_next_cycle",    "binary_sensor.left_side_floweres_watering"),
    ("valve.backyard",              "sensor.backyard_next_cycle",              "binary_sensor.backyard_watering"),
    ("valve.backyard_2",            "sensor.backyard_2_next_cycle",            "binary_sensor.backyard_2_watering"),
    ("valve.veg_garden",            "sensor.veg_garden_next_cycle",            "binary_sensor.veg_garden_watering"),
]

def pretty_zone_name(eid: str) -> str:
    n = eid.split(".",1)[1].replace("_"," ").strip().title()
    return {"4": "Zone 4"}.get(n, n)

@app.get("/api/state")
def api_state(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    moist = get_state(E_MOIST) or {}
    temp  = get_state(E_TEMP) or {}
    bat   = get_state(E_BAT) or {}
    valve = get_state(E_VALVE) or {}
    watering = get_state(E_WATER) or {}
    remain = get_state(E_REMAIN) or {}
    rain   = get_state(E_RAIN) or {}
    weather= get_state(E_WEATHER) or {}
    sun    = get_state(E_SUN) or {}
    panel  = get_state(E_SOLAR_PANEL) or {}

    # Last watered: use the LOGBOOK endpoint (more reliable than /history/period
    # which requires end_time and silently returns [] without it). Combine two signals:
    #   1) binary_sensor.veg_garden_watering on->off (scheduled Hydrawise cycles)
    #   2) valve.veg_garden open->closed (catches manual valve opens)
    # HA history/logbook endpoints choke on '+00:00' offsets in the URL
    # (the '+' is parsed as a space). Use 'Z' suffix instead.
    def _utc_z(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_utc = dt.datetime.now(dt.timezone.utc)
    since = _utc_z(now_utc - dt.timedelta(days=14))
    end   = _utc_z(now_utc + dt.timedelta(minutes=1))

    def _logbook(eid):
        return ha("GET", f"/logbook/{since}?entity={eid}&end_time={end}")

    # Source 1: scheduled cycles (binary_sensor)
    last_off = None
    last_on = None
    lb_w = _logbook(E_WATER)
    if isinstance(lb_w, list):
        for ev in lb_w:
            st = ev.get("state")
            when = ev.get("when")
            if st == "on":
                last_on = when
            elif st == "off" and last_on:
                last_off = when

    # Source 2: valve open->closed transitions
    last_valve_open = None
    last_valve_close = None
    lb_v = _logbook(E_VALVE)
    if isinstance(lb_v, list):
        for ev in lb_v:
            st = ev.get("state")
            when = ev.get("when")
            if st == "open":
                last_valve_open = when
            elif st == "closed" and last_valve_open:
                last_valve_close = when

    # Pick whichever is most recent
    candidates = [x for x in [last_off, last_valve_close] if x]
    last_watered_iso = max(candidates) if candidates else None
    last_watered_source = None
    if last_watered_iso == last_valve_close and last_watered_iso != last_off:
        last_watered_source = "manual valve"
    elif last_watered_iso:
        last_watered_source = "scheduled cycle"

    moist_v = float(moist.get("state") or 0)
    temp_v  = float(temp.get("state") or 0)
    is_watering = watering.get("state") == "on"
    valve_state = valve.get("state","unknown")

    # Status pill
    if is_watering:
        rem = remain.get("state","?")
        status_text = f"💧 Watering — {rem} min left"
        status_kind = "watering"
    elif moist_v >= 80:
        status_text = "Soil moist — happy"
        status_kind = "ok"
    elif moist_v >= 60:
        status_text = "Soil OK"
        status_kind = "ok"
    else:
        status_text = "Thirsty 🥺"
        status_kind = "thirsty"

    # Freeze warning
    freeze_alert = temp_v <= FREEZE_AT

    # Weather
    w_attrs = weather.get("attributes", {}) or {}
    forecast = []
    # Try to fetch a forecast via service call (modern HA)
    fc = ha("POST","/services/weather/get_forecasts?return_response=true",
            {"entity_id": E_WEATHER, "type": "daily"})
    try:
        days = fc["service_response"][E_WEATHER]["forecast"][:5]
        for d in days:
            iso = d.get("datetime","") or ""
            # Parse to America/Toronto so weekday + date are unambiguous
            try:
                tdt = dt.datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(LOCAL_TZ)
                local_date = tdt.strftime("%Y-%m-%d")
                weekday = tdt.strftime("%a")
            except Exception:
                local_date = iso[:10]
                weekday = ""
            forecast.append({
                "date": local_date,
                "weekday": weekday,
                "low":  d.get("templow"),
                "high": d.get("temperature"),
                "precip": d.get("precipitation"),
                "precip_prob": d.get("precipitation_probability"),
                "cond": d.get("condition"),
            })
    except Exception:
        forecast = []

    # Today's date in local TZ for the client to filter forecast against
    today_local = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    # Sun position (drives day/night background)
    sun_above = sun.get("state") == "above_horizon"

    # Solar exposure proxy from a single roof microinverter
    # (slightly east-leaning IQ7A — closely mimics garden's sun pattern)
    try:
        panel_w_now = float(panel.get("state") or 0)
    except (TypeError, ValueError):
        panel_w_now = 0.0
    panel_pct = max(0.0, min(100.0, (panel_w_now / SOLAR_PANEL_PEAK_W) * 100.0)) if SOLAR_PANEL_PEAK_W else 0.0
    # Today's accumulated kWh from this panel (sum sampled W * dt over today)
    panel_kwh_today = _panel_kwh_today(E_SOLAR_PANEL)
    # Smart label band
    if not sun_above:
        solar_band = "night"
    elif panel_pct >= 70:
        solar_band = "full"
    elif panel_pct >= 40:
        solar_band = "partial"
    elif panel_pct >= 15:
        solar_band = "dim"
    else:
        solar_band = "shaded"

    # All 8 Hydrawise zones — current status snapshot
    zones = []
    for valve_eid, next_eid, watering_eid in HYDRAWISE_ZONES:
        v = get_state(valve_eid) or {}
        n = get_state(next_eid) or {}
        w = get_state(watering_eid) or {}
        nstate = n.get("state","")
        # Suspended = next_cycle is unknown/none; otherwise schedule pending
        if nstate in ("unknown","unavailable","none","",None):
            sched = "suspended"
            sched_when = None
        else:
            sched = "scheduled"
            sched_when = fmt_local(nstate) if "T" in str(nstate) else nstate
        zones.append({
            "name": pretty_zone_name(valve_eid),
            "valve": v.get("state","unknown"),
            "watering": w.get("state") == "on",
            "schedule": sched,
            "schedule_when": sched_when,
            "is_veg": valve_eid == E_VALVE,
        })

    return {
        "moisture": round(moist_v, 1),
        "temperature": round(temp_v, 1),
        "battery": int(float(bat.get("state") or 0)),
        "valve": valve_state,
        "is_watering": is_watering,
        "remaining_min": remain.get("state","0"),
        "status_text": status_text,
        "status_kind": status_kind,
        "freeze_alert": freeze_alert,
        "freeze_threshold": FREEZE_AT,
        "last_watered_iso": last_watered_iso,
        "last_watered_age": humanize_age(last_watered_iso),
        "last_watered_local": fmt_local(last_watered_iso),
        "last_watered_source": last_watered_source,
        "rain_outdoor_now": rain.get("state") == "on",
        "solar_panel_w": round(panel_w_now, 0),
        "solar_pct": round(panel_pct, 0),
        "solar_kwh_today": round(panel_kwh_today, 2),
        "solar_band": solar_band,
        "solar_panel_peak_w": SOLAR_PANEL_PEAK_W,
        "weather_now": {
            "condition": weather.get("state"),
            "temperature": w_attrs.get("temperature"),
            "humidity": w_attrs.get("humidity"),
            "wind_speed": w_attrs.get("wind_speed"),
        },
        "forecast": forecast,
        "zones": zones,
        "today_local": today_local,
        "sun_above": sun_above,
        "server_tz": str(LOCAL_TZ),
    }

# ===== solar today helper =====
def _panel_kwh_today(eid: str) -> float:
    """Estimate kWh produced by a single panel since local-midnight today.
    Uses Riemann sum over /history/period samples (W * dt seconds / 3.6e6)."""
    def _utc_z(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_local = dt.datetime.now(LOCAL_TZ)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = midnight_local.astimezone(dt.timezone.utc)
    end_utc   = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
    hist = ha("GET", f"/history/period/{_utc_z(since_utc)}?filter_entity_id={eid}&end_time={_utc_z(end_utc)}&minimal_response")
    pts = []
    if isinstance(hist, list) and hist and isinstance(hist[0], list):
        for ev in hist[0]:
            v = ev.get("state")
            if v in (None, "unknown", "unavailable", ""): continue
            try: v = float(v)
            except (TypeError, ValueError): continue
            t = _parse_dt(ev.get("last_changed") or ev.get("last_updated"))
            if t is None: continue
            pts.append((t, v))
    if len(pts) < 2:
        return 0.0
    # Trapezoidal integration
    total_wh = 0.0
    for i in range(1, len(pts)):
        dt_s = (pts[i][0] - pts[i-1][0]).total_seconds()
        if dt_s <= 0 or dt_s > 3600: continue  # skip gaps > 1h
        avg_w = (pts[i][1] + pts[i-1][1]) / 2.0
        total_wh += avg_w * dt_s / 3600.0
    return total_wh / 1000.0  # Wh -> kWh

# ===== 24h history =====
def _parse_dt(iso: str):
    if not iso: return None
    try: return dt.datetime.fromisoformat(iso.replace("Z","+00:00"))
    except Exception: return None

def _series_window(eid: str, hours: int, sample_n: int = 60):
    """Fetch last `hours` of states for entity. Returns rich stats + dense points
    [(epoch_ms, value)] suitable for an interactive chart."""
    def _utc_z(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_utc = dt.datetime.now(dt.timezone.utc)
    since = _utc_z(now_utc - dt.timedelta(hours=hours))
    end   = _utc_z(now_utc + dt.timedelta(minutes=1))
    hist = ha("GET", f"/history/period/{since}?filter_entity_id={eid}&end_time={end}&minimal_response")
    pts = []
    if isinstance(hist, list) and hist and isinstance(hist[0], list):
        for ev in hist[0]:
            v = ev.get("state")
            if v in (None, "unknown", "unavailable", ""): continue
            try: v = float(v)
            except (TypeError, ValueError): continue
            t = _parse_dt(ev.get("last_changed") or ev.get("last_updated"))
            if t is None: continue
            pts.append((t, v))
    if not pts:
        return {"min": None, "max": None, "first": None, "last": None,
                "points": [], "series": [], "count": 0}
    vals = [v for _, v in pts]
    if len(pts) > sample_n:
        step = len(pts) / sample_n
        sampled = [pts[int(i*step)] for i in range(sample_n)]
        if sampled[-1] != pts[-1]: sampled.append(pts[-1])
        spark_pts = sampled
    else:
        spark_pts = pts
    return {
        "min": round(min(vals), 1),
        "max": round(max(vals), 1),
        "first": round(pts[0][1], 1),
        "last": round(pts[-1][1], 1),
        "points": [round(v, 1) for _, v in spark_pts],
        "series": [[int(t.timestamp()*1000), round(v, 1)] for t, v in spark_pts],
        "count": len(pts),
    }

# Back-compat: keep _series_24h name
def _series_24h(eid, sample_n=24):
    return _series_window(eid, 24, sample_n)

@app.get("/api/history")
def api_history(request: Request, hours: int = 24):
    if not is_authed(request):
        raise HTTPException(401)
    # Allow only sane window sizes
    if hours not in (24, 48, 168):
        hours = 24
    sample_n = 24 if hours == 24 else (48 if hours == 48 else 84)
    moist24 = _series_window(E_MOIST, hours, sample_n)
    temp24  = _series_window(E_TEMP,  hours, sample_n)
    solar24 = _series_window(E_SOLAR_PANEL, hours, sample_n)

    # Watering events in window
    def _utc_z(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_utc = dt.datetime.now(dt.timezone.utc)
    since = _utc_z(now_utc - dt.timedelta(hours=hours))
    end   = _utc_z(now_utc + dt.timedelta(minutes=1))
    # Combine binary_sensor + valve open/close so manual cycles count too
    hist_w = ha("GET", f"/logbook/{since}?entity={E_WATER}&end_time={end}")
    hist_v = ha("GET", f"/logbook/{since}?entity={E_VALVE}&end_time={end}")
    total_min = 0.0
    events = 0

    def _accumulate(lb, on_state, off_state):
        nonlocal total_min, events
        last = None
        if not isinstance(lb, list): return
        for ev in lb:
            st = ev.get("state")
            t = _parse_dt(ev.get("when"))
            if t is None: continue
            if st == on_state:
                last = t
            elif st == off_state and last:
                total_min += (t - last).total_seconds() / 60.0
                events += 1
                last = None
        if last:
            total_min += (now_utc - last).total_seconds() / 60.0
            events += 1

    _accumulate(hist_w, "on", "off")
    _accumulate(hist_v, "open", "closed")

    # Rain in last 24h: report CURRENT state + when last detected (if any)
    rain_now = (get_state(E_RAIN) or {}).get("state") == "on"
    rain_lb = ha("GET", f"/logbook/{since}?entity={E_RAIN}&end_time={end}")
    last_rain_iso = None
    if isinstance(rain_lb, list):
        for ev in rain_lb:
            if ev.get("state") == "on":
                last_rain_iso = ev.get("when", last_rain_iso)

    # Trend label
    def trend(s):
        if s["first"] is None or s["last"] is None: return "flat"
        diff = s["last"] - s["first"]
        if abs(diff) < 1.0: return "flat"
        return "up" if diff > 0 else "down"

    return {
        "hours": hours,
        "moisture": {**moist24, "trend": trend(moist24)},
        "temperature": {**temp24, "trend": trend(temp24)},
        "solar": {**solar24, "trend": trend(solar24)},
        "watering": {
            "total_minutes": round(total_min, 1),
            "events": events,
        },
        "rain_now": rain_now,
        "rain_last_24h": bool(last_rain_iso),
        "rain_last_iso": last_rain_iso,
        "rain_last_age": humanize_age(last_rain_iso) if last_rain_iso else None,
    }

# ===== camera (RTSP livestream + HA-native HLS) =====
# Path overview:
#   1. start_rtsp_livestream -> camera enters state=streaming (1–3 s)
#      (P2P consistently fails on T8114-Z; RTSP is reliable.)
#   2. /api/camera_proxy/<entity> returns a fresh JPEG (snapshot path)
#   3. WS "camera/stream" returns /api/hls/<hash>/master_playlist.m3u8 (live modal)
#   4. modal close -> stop_*_livestream so battery cam can sleep
#
# State is per-process. Multi-worker uvicorn would need a shared cache.
_CAM_STATE = {
    "hls_path": None,        # /api/hls/<hash>/master_playlist.m3u8
    "hls_path_at": 0.0,      # epoch seconds when fetched
    "snapshot_jpeg": None,   # cached JPEG bytes
    "snapshot_at": 0.0,      # epoch seconds when grabbed
    "streaming": False,
    "lock": threading.Lock(),
}
HLS_TTL = 25.0          # HA refreshes the URL’s hash; refetch every 25 s
SNAPSHOT_TTL = 30.0     # serve cached snapshot for up to 30 s

def _ws_camera_stream_url(entity_id: str, timeout: float = 8.0):
    """Ask HA for an HLS URL for entity_id. Returns path or None."""
    if websocket is None:
        return None
    # HA WebSocket lives at /api/websocket — NOT /websocket
    ws_url = HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout,
                                         sslopt={"cert_reqs": ssl.CERT_NONE})
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            ws.close()
            print(f"[cam] ws auth failed: {auth}", flush=True)
            return None
        ws.send(json.dumps({"id": 1, "type": "camera/stream",
                            "entity_id": entity_id, "format": "hls"}))
        r = json.loads(ws.recv())
        ws.close()
        if r.get("success") and r.get("result", {}).get("url"):
            return r["result"]["url"]
        print(f"[cam] ws camera/stream resp: {r}", flush=True)
    except Exception as e:
        print(f"[cam] ws error ({ws_url}): {e}", flush=True)
    return None

def _ha_get_bytes(path: str, timeout: int = 10):
    """GET an HA endpoint and return (status, bytes, content_type)."""
    req = ur.Request(
        f"{HA_URL}{path}" if path.startswith("/api") else f"{HA_URL}/api{path}",
        method="GET",
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
    )
    try:
        resp = ur.urlopen(req, timeout=timeout)
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except HTTPError as e:
        return e.code, b"", "text/plain"
    except URLError:
        return 502, b"", "text/plain"

def _ensure_streaming(force: bool = False) -> bool:
    """Make sure RTSP livestream is running and we have a current HLS path.

    On a Eufy T8114-Z (the veg garden camera), start_p2p_livestream
    consistently fails (HTTP 500 or asyncio.Event timeout) but
    start_rtsp_livestream succeeds in <2s and lights up:
      - state -> streaming
      - /api/camera_proxy/camera.veg_garden -> live JPEG
      - WS camera/stream -> /api/hls/<hash>/master_playlist.m3u8 (works)

    Verified live 2026-05-08. Reverify if Eufy firmware changes.
    """
    now = time.time()
    with _CAM_STATE["lock"]:
        st = get_state(E_CAMERA) or {}
        is_streaming = (st.get("state") == "streaming")
        if not is_streaming or force:
            try:
                call_service("eufy_security", "start_rtsp_livestream",
                             {"entity_id": E_CAMERA})
                print("[cam] start_rtsp_livestream issued", flush=True)
            except Exception as e:
                print(f"[cam] start_rtsp_livestream failed: {e}", flush=True)
            # RTSP comes up in 1–3 s on this camera; allow up to 8 s.
            for _ in range(8):
                time.sleep(1.0)
                st = get_state(E_CAMERA) or {}
                if st.get("state") == "streaming":
                    is_streaming = True
                    break
        _CAM_STATE["streaming"] = is_streaming
        if not is_streaming:
            print(f"[cam] could not get camera into streaming state, last={st.get('state')}", flush=True)
            return False
        # Reuse cached HLS path if fresh
        if (_CAM_STATE["hls_path"] and
                now - _CAM_STATE["hls_path_at"] < HLS_TTL):
            return True
        # Get a new HLS path from HA WebSocket (used by live modal)
        path = _ws_camera_stream_url(E_CAMERA)
        if path:
            _CAM_STATE["hls_path"] = path
            _CAM_STATE["hls_path_at"] = now
        return True  # snapshot path doesn't need HLS

def _veg_rtsp_url():
    """Pull the live LAN RTSP URL from the eufy_security debug sensor.

    The cam exposes an unauthenticated RTSP stream on its LAN IP whenever
    'rtsp_stream' is on. The URL is in binary_sensor.backyard_debug_device
    attributes -> properties.rtspStreamUrl (e.g. rtsp://192.168.50.99/live3).
    Falling back to the env-overridable default keeps things working if
    the integration ever moves that field.
    """
    try:
        st = get_state(E_DEBUG) or {}
        url = (st.get("attributes", {}) or {}).get("properties", {}).get("rtspStreamUrl")
        if url and url.startswith("rtsp"):
            return url
    except Exception as e:
        print(f"[cam] rtspStreamUrl lookup failed: {e}", flush=True)
    return RTSP_URL_DEFAULT

def _grab_snapshot_jpeg(timeout: float = 8.0):
    """Pull a fresh JPEG frame straight off the camera's LAN RTSP feed.

    Why not HA?
      - /api/camera_proxy returns 500 for this Eufy battery cam even when
        state=streaming (verified live 2026-05-08).
      - camera.snapshot service writes 0 bytes (same root cause).
      - HLS playlist is fine for the live modal but slow for one-frame grabs.

    The Eufy cam itself runs an RTSP server on its LAN IP whenever
    switch.backyard_rtsp_stream is on (it currently is). ffmpeg pulls
    a single frame off that stream in well under a second from inside
    the addon's network. No HA intermediary, no bridge wake-up dance.
    """
    rtsp = _veg_rtsp_url()
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",   # more reliable than UDP through bridges
        "-stimeout", "4000000",     # 4s socket timeout
        "-i", rtsp,
        "-frames:v", "1",
        "-q:v", "4",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout and proc.stdout[:3] == b"\xff\xd8\xff":
            print(f"[cam] rtsp grab ok ({len(proc.stdout)} bytes from {rtsp})", flush=True)
            return proc.stdout
        print(f"[cam] rtsp grab failed rc={proc.returncode} stderr={proc.stderr[-200:]!r}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[cam] rtsp grab timed out from {rtsp}", flush=True)
    except Exception as e:
        print(f"[cam] rtsp grab error: {e}", flush=True)

    # Last-resort fallback: the stale motion-event image. Always works,
    # but might be hours old (and might literally show you walking past
    # the camera). Better than a blank tile.
    status, body, ctype = _ha_get_bytes(
        f"/api/image_proxy/{E_IMAGE}", timeout=int(timeout))
    if status == 200 and body and ("image" in ctype or body[:3] == b"\xff\xd8\xff"):
        print("[cam] using stale image_proxy fallback", flush=True)
        return body
    return None

@app.get("/api/cam/snapshot")
def api_cam_snapshot(request: Request, refresh: int = 0):
    if not is_authed(request):
        raise HTTPException(401)
    now = time.time()
    cached = _CAM_STATE["snapshot_jpeg"]
    age = now - _CAM_STATE["snapshot_at"]
    if refresh or not cached or age > SNAPSHOT_TTL:
        fresh = _grab_snapshot_jpeg()
        if fresh:
            _CAM_STATE["snapshot_jpeg"] = fresh
            _CAM_STATE["snapshot_at"] = now
            cached = fresh
    if not cached:
        return Response(status_code=502, content=b"", media_type="image/jpeg")
    return Response(content=cached, media_type="image/jpeg", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.post("/api/cam/stream/start")
def api_cam_stream_start(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    ok = _ensure_streaming(force_p2p=False)
    if ok:
        return {"type": "hls", "url": "/garden/api/cam/hls.m3u8"}
    return {"type": "snapshot", "hint": "camera waking, retry shortly"}

@app.post("/api/cam/stream/stop")
def api_cam_stream_stop(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    try:
        call_service("eufy_security", "stop_p2p_livestream", {"entity_id": E_CAMERA})
    except Exception:
        pass
    with _CAM_STATE["lock"]:
        _CAM_STATE["streaming"] = False
        _CAM_STATE["hls_path"] = None
    return {"ok": True}

@app.get("/api/cam/hls.m3u8")
def api_cam_hls_playlist(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    if not _ensure_streaming():
        return Response(status_code=503, content=b"camera not streaming")
    hls_path = _CAM_STATE["hls_path"]
    full = f"{HA_URL}{hls_path}"
    sc, body, _ = _ha_get_bytes(hls_path, timeout=8)
    if sc != 200 or not body:
        return Response(status_code=502, content=b"playlist fetch failed")
    text = body.decode("utf-8", errors="replace")
    base = "/".join(hls_path.split("/")[:-1])  # /api/hls/<hash>
    rewritten = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            rewritten.append(line)
            continue
        # Resolve relative path
        if s.startswith("http://") or s.startswith("https://"):
            # Strip host, keep /api/...
            try:
                from urllib.parse import urlparse
                rel = urlparse(s).path
            except Exception:
                rel = s
        elif s.startswith("/"):
            rel = s
        else:
            rel = f"{base}/{s}"
        rewritten.append(f"/garden/api/cam/hls/seg?path={rel}")
    return Response(
        content="\n".join(rewritten),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )

@app.get("/api/cam/hls/seg")
def api_cam_hls_segment(request: Request, path: str = ""):
    if not is_authed(request):
        raise HTTPException(401)
    if not path or ".." in path or not path.startswith("/api/hls/"):
        raise HTTPException(400)
    sc, body, ctype = _ha_get_bytes(path, timeout=10)
    if sc != 200 or not body:
        return Response(status_code=502, content=b"")
    # HLS sub-playlist (m3u8) -> rewrite again; segment (.ts/.m4s) -> pass through
    if path.endswith(".m3u8") or "mpegurl" in (ctype or "").lower():
        text = body.decode("utf-8", errors="replace")
        base = "/".join(path.split("/")[:-1])
        rewritten = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                rewritten.append(line)
                continue
            if s.startswith("http://") or s.startswith("https://"):
                try:
                    from urllib.parse import urlparse
                    rel = urlparse(s).path
                except Exception:
                    rel = s
            elif s.startswith("/"):
                rel = s
            else:
                rel = f"{base}/{s}"
            rewritten.append(f"/garden/api/cam/hls/seg?path={rel}")
        return Response(content="\n".join(rewritten),
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})
    # binary segment
    media_type = ctype or "video/mp2t"
    return Response(content=body, media_type=media_type,
                    headers={"Cache-Control": "no-store"})

# ===== weather full forecast =====
@app.get("/api/weather/full")
def api_weather_full(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    weather = get_state(E_WEATHER) or {}
    sun     = get_state(E_SUN) or {}
    w_attrs = weather.get("attributes", {}) or {}
    s_attrs = sun.get("attributes", {}) or {}

    # 7-day daily forecast
    daily = []
    fc = ha("POST", "/services/weather/get_forecasts?return_response=true",
            {"entity_id": E_WEATHER, "type": "daily"})
    try:
        days = fc["service_response"][E_WEATHER]["forecast"][:7]
        for d in days:
            iso = d.get("datetime", "") or ""
            try:
                tdt = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
                weekday = tdt.strftime("%a, %b %-d")
            except Exception:
                weekday = iso[:10]
            daily.append({
                "weekday": weekday,
                "condition": d.get("condition"),
                "high": d.get("temperature"),
                "low":  d.get("templow"),
                "precipitation": d.get("precipitation") or 0,
                "precip_prob": d.get("precipitation_probability"),
            })
    except Exception:
        pass

    def _fmt_sun(iso):
        if not iso: return None
        try:
            t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            return t.strftime("%-I:%M %p")
        except Exception:
            return None

    return {
        "now": {
            "condition":   weather.get("state"),
            "temperature": w_attrs.get("temperature"),
            "humidity":    w_attrs.get("humidity"),
            "wind_speed":  w_attrs.get("wind_speed"),
            "pressure":    w_attrs.get("pressure"),
        },
        "daily":   daily,
        "sunrise": _fmt_sun(s_attrs.get("next_rising")),
        "sunset":  _fmt_sun(s_attrs.get("next_setting")),
    }

@app.post("/api/start")
def api_start(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    r = call_service("switch","turn_on",{"entity_id": E_MANUAL})
    return {"ok": "error" not in str(r), "result": r}

@app.post("/api/stop")
def api_stop(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    r = call_service("switch","turn_off",{"entity_id": E_MANUAL})
    return {"ok": "error" not in str(r), "result": r}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), root_path="/garden")
