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
import os, json, hmac, hashlib, time, secrets, datetime as dt, subprocess, ssl, threading, math
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
# v1.7.16 (2026-07-03): switched E_MOIST and E_TEMP off the ghost 'sensor.veg_soil_*' mirror
# helpers (which no longer exist in HA — probably deleted with an old templates.yaml)
# and onto the real Ecowitt channel-1 sensors that the rest of the app has been using.
# Prior behavior: temp.get('state') returned None -> float(None or 0) = 0.0 -> freeze_alert
# fired at +32C outside. Now: hardened None/unknown/unavailable handling below at line ~339.
E_MOIST  = "sensor.hp2564bu_pro_v2_1_1_soil_moisture_1"
E_TEMP   = "sensor.hp2564bu_pro_v2_1_1_soil_temperature_1"
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

# ===== Ecowitt HP2564 (PRIMARY sensors — v1.3.0) =====
# Verified entity names from _live_entities.json 2026-05-25

# Ecowitt soil (PRIMARY)
# CH1 = Tomato (WN34S with temp sensor), CH2 = Cucumber (plain WH51, no temp)
E_SOIL_T_TOMATO   = "sensor.hp2564bu_pro_v2_1_1_soil_temperature_1"
E_SOIL_M_TOMATO   = "sensor.hp2564bu_pro_v2_1_1_soil_moisture_1"
E_SOIL_B_TOMATO   = "sensor.hp2564bu_pro_v2_1_1_soil_battery_1"
E_SOIL_M_CUCUMBER = "sensor.hp2564bu_pro_v2_1_1_soil_moisture_2"
E_SOIL_B_CUCUMBER = "sensor.hp2564bu_pro_v2_1_1_soil_battery_2"
# Note: CH2 has no temperature sensor (plain WH51, not WN34S)

# Ecowitt weather (PRIMARY)
# wind_speed / wind_gust are in m/s from HA integration — multiply by 3.6 for km/h
E_OUTDOOR_T  = "sensor.hp2564bu_pro_v2_1_1_outdoor_temperature"
E_OUTDOOR_H  = "sensor.hp2564bu_pro_v2_1_1_humidity"
E_WIND_SPEED = "sensor.hp2564bu_pro_v2_1_1_wind_speed"
E_WIND_GUST  = "sensor.hp2564bu_pro_v2_1_1_wind_gust"
E_WIND_DIR   = "sensor.hp2564bu_pro_v2_1_1_wind_direction"
E_UV         = "sensor.hp2564bu_pro_v2_1_1_uv_index"
E_LUX        = "sensor.hp2564bu_pro_v2_1_1_solar_lux"
E_RADIATION  = "sensor.hp2564bu_pro_v2_1_1_solar_radiation"
# Rain: WH40 tipping-bucket ONLY (piezo is mis-calibrated — ignore *_piezo entities)
E_RAIN_DAILY   = "sensor.hp2564bu_pro_v2_1_1_daily_rain"
E_RAIN_WEEKLY  = "sensor.hp2564bu_pro_v2_1_1_weekly_rain"
E_RAIN_MONTHLY = "sensor.hp2564bu_pro_v2_1_1_monthly_rain"
E_RAIN_RATE    = "sensor.hp2564bu_pro_v2_1_1_rain_rate"
E_RAIN_24H     = "sensor.hp2564bu_pro_v2_1_1_24h_rain"

# HP10 camera
E_GARDEN_CAM = "camera.192_168_50_52"

# Old Zigbee (deprecated, kept for back-compat in API only — sensor was pulled)
E_MOIST_OLD = "sensor.veg_soil_moisture"
E_TEMP_OLD  = "sensor.veg_soil_temperature"

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
    manual_sw = get_state(E_MANUAL) or {}
    remain = get_state(E_REMAIN) or {}
    rain   = get_state(E_RAIN) or {}
    weather= get_state(E_WEATHER) or {}
    sun    = get_state(E_SUN) or {}
    panel  = get_state(E_SOLAR_PANEL) or {}

    # --- v1.3.0: Ecowitt primary sensors ---
    def _f(eid):
        s = get_state(eid) or {}
        try: return float(s.get("state"))
        except (TypeError, ValueError): return None

    e_soil_t1 = _f(E_SOIL_T_TOMATO)
    e_soil_m1 = _f(E_SOIL_M_TOMATO)
    e_soil_b1 = _f(E_SOIL_B_TOMATO)
    e_soil_m2 = _f(E_SOIL_M_CUCUMBER)
    e_soil_b2 = _f(E_SOIL_B_CUCUMBER)
    e_out_t   = _f(E_OUTDOOR_T)
    e_out_h   = _f(E_OUTDOOR_H)
    e_wind_ms = _f(E_WIND_SPEED)
    e_gust_ms = _f(E_WIND_GUST)
    e_wind_dir= _f(E_WIND_DIR)
    e_lux     = _f(E_LUX)
    e_uv      = _f(E_UV)
    e_rad     = _f(E_RADIATION)
    e_rain_d  = _f(E_RAIN_DAILY)
    e_rain_w  = _f(E_RAIN_WEEKLY)
    e_rain_m  = _f(E_RAIN_MONTHLY)
    e_rain_r  = _f(E_RAIN_RATE)
    e_rain_24 = _f(E_RAIN_24H)

    e_wind_kmh = round(e_wind_ms * 3.6, 1) if e_wind_ms is not None else None
    e_gust_kmh = round(e_gust_ms * 3.6, 1) if e_gust_ms is not None else None
    e_wind_compass = _deg_to_compass(e_wind_dir) if e_wind_dir is not None else None

    greenhouse_on = (get_state("input_boolean.greenhouse_plastic_on") or {}).get("state") == "on"
    wind_alert = bool(e_gust_kmh and e_gust_kmh > 25 and greenhouse_on)

    crop_tom = (get_state("input_boolean.crop_tomatoes_planted") or {}).get("state") == "on"
    crop_cuc = (get_state("input_boolean.crop_cucumbers_planted") or {}).get("state") == "on"

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

    # v1.7.16: harden None/unknown/unavailable handling.
    # Previously `float(state or 0)` coerced missing sensors to 0.0, which triggered
    # freeze_alert (temp <= 1.0) year-round when the mirror sensor was missing.
    def _safe_float(x):
        try:
            if x is None: return None
            s = str(x).strip().lower()
            if s in ("", "unknown", "unavailable", "none", "null"): return None
            return float(s)
        except (TypeError, ValueError):
            return None
    moist_raw = _safe_float(moist.get("state"))
    temp_raw  = _safe_float(temp.get("state"))
    moist_v = moist_raw if moist_raw is not None else 0.0
    temp_v  = temp_raw  if temp_raw  is not None else None
    # v1.7.17 fix: Hydrawise cloud sometimes lags 2h+ syncing manual-switch
    # flips back to the valve/binary_sensor entities. Trust the switch as
    # the authoritative signal for UX (Start disabled / Stop enabled).
    manual_on = manual_sw.get("state") == "on"
    is_watering = manual_on or watering.get("state") == "on"
    if manual_on and valve.get("state") != "open":
        valve_state = "open"
    else:
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

    # Freeze warning — only fire when we have a real soil temperature reading.
    # A missing/unknown sensor MUST NOT trigger a freeze warning (v1.7.16 fix).
    freeze_alert = (temp_v is not None) and (temp_v <= FREEZE_AT)

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

    # v1.3.5: Moon phase — simple synodic-cycle math (no integration needed).
    # Reference new moon: 2000-01-06 18:14 UTC. Cycle = 29.530588853 days.
    # Returns 0.0 (new) → 0.25 (first quarter) → 0.5 (full) → 0.75 (last quarter) → 1.0 (→ new).
    _ref_new_moon = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)
    _now_utc = dt.datetime.now(dt.timezone.utc)
    _days_since = (_now_utc - _ref_new_moon).total_seconds() / 86400.0
    moon_phase = (_days_since % 29.530588853) / 29.530588853  # 0–1

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

    # v1.3.1: single soil temp probe sits between tomato + cucumber rows (~30cm from each),
    # so we use CH1 temp for BOTH zones. Per-crop moisture bands research-backed:
    # - Turkish heirloom tomatoes: drought-tolerant, prefer 45-65%, water <40%, too wet >75%
    # - Ukrainian пупырчатые (Nezhinsky pickling cucumbers): thirsty, prefer 65-85%, water <60%
    def _tomato_status(m):
        if m is None: return (None, None, None)
        if m < 40:  return ("water",     "Water now — too dry",      "Полить — слишком сухо")
        if m < 45:  return ("low",       "Getting dry",              "Подсыхает")
        if m <= 65: return ("ok",        "Ideal",                    "Идеально")
        if m <= 75: return ("high",      "Damp — let it breathe",    "Влажновато — пусть подсохнет")
        return ("wet",                   "Too wet — risk of rot",    "Слишком мокро — риск гнили")
    def _cucumber_status(m):
        if m is None: return (None, None, None)
        if m < 60:  return ("water",     "Water now — cucumbers thirsty", "Полить — огурцам жажда")
        if m < 65:  return ("low",       "Getting dry for cucumbers",     "Подсыхает для огурцов")
        if m <= 85: return ("ok",        "Ideal for пупырчатые",          "Идеально для пупырчатых")
        if m <= 92: return ("high",      "Very damp",                     "Очень влажно")
        return ("wet",                   "Waterlogged",                   "Заболочено")
    _tom_st = _tomato_status(e_soil_m1)
    _cuc_st = _cucumber_status(e_soil_m2)

    return {
        "version": "1.3.2",
        "soil_zones": [
            {"key":"tomato",   "name":"Tomato",  "name_ru":"Помидоры", "moisture": e_soil_m1, "temp_c": e_soil_t1, "battery_v": e_soil_b1, "planted": crop_tom,
             "status": _tom_st[0], "status_msg": _tom_st[1], "status_msg_ru": _tom_st[2],
             "ideal_min": 45, "ideal_max": 65, "variety": "Turkish heirloom"},
            {"key":"cucumber", "name":"Cucumber","name_ru":"Огурцы",   "moisture": e_soil_m2, "temp_c": e_soil_t1, "battery_v": e_soil_b2, "planted": crop_cuc,
             "status": _cuc_st[0], "status_msg": _cuc_st[1], "status_msg_ru": _cuc_st[2],
             "ideal_min": 65, "ideal_max": 85, "variety": "Ukrainian пупырчатые"},
        ],
        "wind": {
            "speed_kmh": e_wind_kmh,
            "gust_kmh":  e_gust_kmh,
            "direction": e_wind_compass,
            "alert":     wind_alert,
            "alert_msg":    "Gusty winds — check greenhouse plastic" if wind_alert else None,
            "alert_msg_ru": "Сильный ветер — проверьте плёнку теплицы" if wind_alert else None,
        },
        "rain": {
            "today_mm":   round(e_rain_d, 2) if e_rain_d is not None else None,
            "week_mm":    round(e_rain_w, 2) if e_rain_w is not None else None,
            "month_mm":   round(e_rain_m, 2) if e_rain_m is not None else None,
            "rate_mm_h":  round(e_rain_r, 2) if e_rain_r is not None else None,
            "last_24h_mm":round(e_rain_24,2) if e_rain_24 is not None else None,
        },
        "lux": int(e_lux) if e_lux is not None else None,
        "uv_index": e_uv,
        "solar_radiation_w_m2": e_rad,
        "outdoor_temp_c": e_out_t,
        "outdoor_humidity": e_out_h,
        "moisture": round(moist_v, 1),
        "temperature": round(temp_v, 1) if temp_v is not None else None,
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
        # rain_outdoor_now: derived from Ecowitt rain_rate when present (computed in
        # renderWindRainSoil's sun_summary path); here we use the Hydrawise binary
        # as the simple signal. UI also has the live Ecowitt rate via sun_summary.
        "rain_outdoor_now": (rain.get("state") == "on") or ((e_rain_r or 0) > 0.0),
        "rain_sensor_hydrawise": rain.get("state") == "on",
        "solar_panel_w": round(panel_w_now, 0),
        "solar_pct": round(panel_pct, 0),
        "solar_kwh_today": round(panel_kwh_today, 2),
        "solar_band": solar_band,
        "solar_panel_peak_w": SOLAR_PANEL_PEAK_W,
        "weather_now": {
            # legacy field — still wired so existing JS doesn't break.
            "condition": weather.get("state"),
            "temperature": w_attrs.get("temperature"),
            "humidity": w_attrs.get("humidity"),
            "wind_speed": w_attrs.get("wind_speed"),
        },
        # v1.3.2: explicit dual weather panes
        "weather_real": {
            "source": "Ecowitt HP2564BU",
            "temp_c":   e_out_t,
            "humidity": e_out_h,
            "uv_index": e_uv,
            "solar_w_m2": e_rad,
            "lux": int(e_lux) if e_lux is not None else None,
            "wind_kmh": e_wind_kmh,
            "wind_gust_kmh": e_gust_kmh,
            "wind_dir": e_wind_compass,
            "rain_now": (e_rain_r is not None and e_rain_r > 0) or (rain.get("state") == "on"),
            "rain_rate_mm_h": round(e_rain_r, 2) if e_rain_r is not None else None,
            "rain_today_mm": round(e_rain_d, 2) if e_rain_d is not None else None,
        },
        "weather_forecast": {
            "source": "weather.forecast_home",
            "condition": weather.get("state"),
            "temp_c":    w_attrs.get("temperature"),
            "humidity":  w_attrs.get("humidity"),
            "wind_kmh":  w_attrs.get("wind_speed"),
        },
        "forecast": forecast,
        "zones": zones,
        "today_local": today_local,
        "sun_above": sun_above,
        "moon_phase": round(moon_phase, 3),  # v1.3.5
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
    """Return latest motion-event JPEG from HA's image entity.

    Why not direct cam RTSP / HA HLS / camera_proxy?
      - Direct RTSP rtsp://<cam_ip>/live3: cam refuses TCP. Verified 2026-05-08.
      - HA /api/camera_proxy: returns 500 even while streaming. Eufy plugin bug.
      - HA HLS via supervisor: ffmpeg times out. HLS infra unreliable for grabs.
      - eufy_security.start_*_livestream: doesn't make snapshot endpoints work.

    Final answer (2026-05-08): use image.backyard_event_image. Updates whenever
    motion is detected (Pavel walking by, the cat passing, garden activity).
    Stale by minutes-to-hours, but the right tradeoff vs. a perpetually-broken
    "live" snapshot. The card's tap action launches the Eufy iOS app for
    actual live viewing.
    """
    status, body, ctype = _ha_get_bytes(
        f"/api/image_proxy/{E_IMAGE}", timeout=int(timeout))
    if status == 200 and body and ("image" in ctype or body[:3] == b"\xff\xd8\xff"):
        return body
    print(f"[cam] image_proxy failed status={status} ctype={ctype}", flush=True)
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
    ok = _ensure_streaming()
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

# ===== Wind direction helper =====
def _deg_to_compass(deg):
    """Convert wind direction degrees to 16-point compass string."""
    try:
        deg = float(deg)
    except (TypeError, ValueError):
        return "N/A"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    idx = round(deg / 22.5) % 16
    return dirs[idx]

def _uv_level(index):
    """Return (level_en, level_ru) for UV index."""
    try:
        v = float(index)
    except (TypeError, ValueError):
        return ("Unknown", "Неизвестно")
    if v < 3:   return ("Low",       "Низкий")
    if v < 6:   return ("Moderate",  "Умеренный")
    if v < 8:   return ("High",      "Высокий")
    if v < 11:  return ("Very High", "Очень высокий")
    return         ("Extreme",    "Экстремальный")

# ===== Solar Literacy — /api/sun_summary =====
# Returns 7-day daily kWh stats, crop/greenhouse states, soil readings, and
# tomorrow's solar forecast. Frontend uses this to drive the Sun modal
# (section 1-4) and to update the solar tile stars + one-liner every 60 s.
@app.get("/api/sun_summary")
def api_sun_summary(request: Request):
    if not is_authed(request):
        raise HTTPException(401)

    # --- 1. Clear-day baseline ---
    def _float_state(eid):
        s = get_state(eid) or {}
        try:
            return float(s.get("state") or 0)
        except (TypeError, ValueError):
            return None

    baseline = None
    for bid in ("input_number.solar_clear_day_baseline_kwh",
                "sensor.solar_clear_day_baseline_kwh"):
        v = _float_state(bid)
        if v and v > 0:
            baseline = v
            break
    if not baseline:
        baseline = 92.0

    # --- 2. Instantaneous solar % (from existing panel sensor) ---
    panel = get_state(E_SOLAR_PANEL) or {}
    try:
        panel_w_now = float(panel.get("state") or 0)
    except (TypeError, ValueError):
        panel_w_now = 0.0
    now_solar_pct = round(max(0.0, min(100.0, panel_w_now / SOLAR_PANEL_PEAK_W * 100.0)))

    # --- 3. Sun above horizon ---
    sun_st = get_state(E_SUN) or {}
    sun_above = sun_st.get("state") == "above_horizon"

    # Helper (must be defined before use)
    def _bool_state(eid):
        s = get_state(eid) or {}
        return s.get("state") == "on"

    # --- 4. Soil sensors (Ecowitt PRIMARY) ---
    # CH1 = Tomato (WN34S has temp), CH2 = Cucumber (plain WH51, no temp)
    soil_t_tomato  = _float_state(E_SOIL_T_TOMATO)
    soil_m_tomato  = _float_state(E_SOIL_M_TOMATO)
    soil_b_tomato  = _float_state(E_SOIL_B_TOMATO)
    soil_m_cuc     = _float_state(E_SOIL_M_CUCUMBER)
    soil_b_cuc     = _float_state(E_SOIL_B_CUCUMBER)
    # Check planted flags
    tomatoPlanted  = _bool_state("input_boolean.crop_tomatoes_planted")
    cucumberPlanted= _bool_state("input_boolean.crop_cucumbers_planted")
    # v1.3.1: shared temp probe between zones (30cm each side); per-crop moisture bands
    def _zone_status(m, lo, hi, crop):
        if m is None: return (None, None, None)
        water_thr = lo - 5
        if m < water_thr: return ("water", f"Water now — too dry",
                                  "Полить — слишком сухо")
        if m < lo:        return ("low",   "Getting dry",      "Подсыхает")
        if m <= hi:       return ("ok",    "Ideal",            "Идеально")
        if m <= hi + 10:  return ("high",  "Damp",             "Влажновато")
        return ("wet", "Too wet — risk of rot", "Слишком мокро")
    _tom_lo, _tom_hi = 45, 65   # Turkish heirloom tomatoes
    _cuc_lo, _cuc_hi = 65, 85   # Ukrainian пупырчатые pickling cucumbers
    _tst = _zone_status(soil_m_tomato, _tom_lo, _tom_hi, "tomato")
    _cst = _zone_status(soil_m_cuc,    _cuc_lo, _cuc_hi, "cucumber")
    soil_zones = [
        {
            "name": "Tomato", "name_ru": "Помидоры",
            "moisture": round(soil_m_tomato, 1) if soil_m_tomato is not None else None,
            "temp_c":   round(soil_t_tomato, 1) if soil_t_tomato is not None else None,
            "battery":  round(soil_b_tomato, 2) if soil_b_tomato is not None else None,
            "planted":  tomatoPlanted,
            "status": _tst[0], "status_msg": _tst[1], "status_msg_ru": _tst[2],
            "ideal_min": _tom_lo, "ideal_max": _tom_hi, "variety": "Turkish heirloom",
        },
        {
            "name": "Cucumber", "name_ru": "Огурцы",
            "moisture": round(soil_m_cuc, 1) if soil_m_cuc is not None else None,
            "temp_c":   round(soil_t_tomato, 1) if soil_t_tomato is not None else None,  # shared probe (30cm from cucumber row)
            "battery":  round(soil_b_cuc, 2) if soil_b_cuc is not None else None,
            "planted":  cucumberPlanted,
            "status": _cst[0], "status_msg": _cst[1], "status_msg_ru": _cst[2],
            "ideal_min": _cuc_lo, "ideal_max": _cuc_hi, "variety": "Ukrainian пупырчатые",
        },
    ]
    # Back-compat: legacy single soil reading (use CH1/Tomato as primary)
    soil_temp_c    = soil_t_tomato
    soil_moist_pct = soil_m_tomato

    # --- 4b. Ecowitt weather + UV + wind + rain ---
    outdoor_temp_c = _float_state(E_OUTDOOR_T)
    outdoor_hum    = _float_state(E_OUTDOOR_H)
    # Wind: HA Ecowitt integration reports m/s; convert to km/h
    wind_ms   = _float_state(E_WIND_SPEED)
    gust_ms   = _float_state(E_WIND_GUST)
    wind_kmh  = round(wind_ms * 3.6, 1) if wind_ms is not None else None
    gust_kmh  = round(gust_ms * 3.6, 1) if gust_ms is not None else None
    wind_dir_deg = _float_state(E_WIND_DIR)
    wind_dir_str = _deg_to_compass(wind_dir_deg) if wind_dir_deg is not None else "N/A"
    # Wind alert: gust > 25 km/h AND greenhouse plastic is on
    greenhouse_on  = _bool_state("input_boolean.greenhouse_plastic_on")
    wind_alert = bool(gust_kmh and gust_kmh > 25 and greenhouse_on)
    wind_alert_msg    = "Gusty winds — check greenhouse plastic" if wind_alert else None
    wind_alert_msg_ru = "Сильный ветер — проверьте плёнку теплицы" if wind_alert else None
    uv_raw     = _float_state(E_UV)
    uv_lv_en, uv_lv_ru = _uv_level(uv_raw)
    lux_val    = _float_state(E_LUX)
    rad_val    = _float_state(E_RADIATION)
    rain_today  = _float_state(E_RAIN_DAILY)
    rain_week   = _float_state(E_RAIN_WEEKLY)
    rain_month  = _float_state(E_RAIN_MONTHLY)
    rain_rate   = _float_state(E_RAIN_RATE)
    rain_24h    = _float_state(E_RAIN_24H)

    # --- 5. Tomorrow's solar forecast ---
    tomorrow_kwh = _float_state("sensor.solar_forecast_tomorrow")
    if tomorrow_kwh is None:
        tomorrow_kwh = 0.0
    tomorrow_pct = round(tomorrow_kwh / baseline * 100) if baseline else 0

    # --- 6. Today's accumulated kWh ---
    # Use the whole-system Riemann sensor (covers all roof panels), NOT the single
    # IQ7A panel proxy used for instantaneous %. Falls back to single-panel
    # integration if Riemann sensor is missing/unavailable.
    today_kwh = _float_state("sensor.solar_riemann_daily")
    if today_kwh is None or today_kwh <= 0:
        today_kwh = _panel_kwh_today(E_SOLAR_PANEL)
    today_pct = round(today_kwh / baseline * 100) if baseline else 0

    # --- 7. Crop / greenhouse booleans ---
    crops = {
        "tomatoes": _bool_state("input_boolean.crop_tomatoes_planted"),
        "cucumbers": _bool_state("input_boolean.crop_cucumbers_planted"),
        "peppers":   _bool_state("input_boolean.crop_peppers_planted"),
        "lettuce":   _bool_state("input_boolean.crop_lettuce_planted"),
    }

    # --- 8. 7-day daily kWh via HA WebSocket recorder ---
    week_entries = []
    week_avg_kwh = None
    week_avg_pct = None
    ws_error = None
    try:
        if websocket is None:
            raise RuntimeError("websocket-client not installed")
        import json as _json, ssl as _ssl
        ws_url = HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        ws = websocket.create_connection(ws_url, timeout=8.0,
                                         sslopt={"cert_reqs": _ssl.CERT_NONE})
        ws.recv()  # auth_required
        ws.send(_json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth_resp = _json.loads(ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"ws auth failed: {auth_resp}")
        start_dt = (dt.datetime.now(LOCAL_TZ) - dt.timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        end_dt = dt.datetime.now(LOCAL_TZ)
        ws.send(_json.dumps({
            "id": 1,
            "type": "recorder/statistics_during_period",
            "start_time": start_dt.astimezone(dt.timezone.utc).isoformat(),
            "end_time":   end_dt.astimezone(dt.timezone.utc).isoformat(),
            "statistic_ids": ["sensor.solar_riemann_daily"],
            "period": "day",
            "types": ["change"],
            "units": {"energy": "kWh"},
        }))
        stat_resp = _json.loads(ws.recv())
        ws.close()
        entries_raw = []
        if stat_resp.get("success"):
            res = stat_resp.get("result") or {}
            entries_raw = res.get("sensor.solar_riemann_daily") or []
        for entry in entries_raw:
            kwh = entry.get("change") or 0.0
            try:
                kwh = float(kwh)
            except (TypeError, ValueError):
                kwh = 0.0
            # 'start' may be epoch ms (int/float) or UTC ISO string depending on HA version
            start_raw = entry.get("start")
            tdt = None
            try:
                if isinstance(start_raw, (int, float)):
                    tdt = dt.datetime.fromtimestamp(float(start_raw) / 1000.0, tz=dt.timezone.utc).astimezone(LOCAL_TZ)
                elif isinstance(start_raw, str) and start_raw:
                    tdt = dt.datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            except Exception:
                tdt = None
            if tdt is not None:
                date_str = tdt.strftime("%Y-%m-%d")
                weekday_str = tdt.strftime("%a")
            else:
                date_str = str(start_raw)[:10] if start_raw else ""
                weekday_str = ""
            pct = round(kwh / baseline * 100) if baseline else 0
            week_entries.append({
                "date": date_str,
                "weekday": weekday_str,
                "kwh": round(kwh, 2),
                "pct": pct,
            })
        # Sort oldest→newest, keep last 7
        week_entries.sort(key=lambda x: x["date"])
        week_entries = week_entries[-7:]
        if week_entries:
            avg_kwh = sum(e["kwh"] for e in week_entries) / len(week_entries)
            week_avg_kwh = round(avg_kwh, 2)
            week_avg_pct = round(avg_kwh / baseline * 100) if baseline else 0
    except Exception as ex:
        ws_error = str(ex)
        print(f"[sun_summary] ws error: {ex}", flush=True)

    return {
        "week": week_entries,
        "week_avg_kwh": week_avg_kwh,
        "week_avg_pct": week_avg_pct,
        "today_kwh": round(today_kwh, 2),
        "today_pct": today_pct,
        "tomorrow_kwh": round(tomorrow_kwh, 2),
        "tomorrow_pct": tomorrow_pct,
        "clear_day_baseline_kwh": round(baseline, 2),
        "soil_temp_c": round(soil_temp_c, 1) if soil_temp_c is not None else None,
        "soil_moisture_pct": round(soil_moist_pct, 1) if soil_moist_pct is not None else None,
        "greenhouse_plastic_on": greenhouse_on,
        "crops": crops,
        "now_solar_pct": now_solar_pct,
        "sun_above": sun_above,
        # v1.3.0 Ecowitt extensions
        "soil_zones": soil_zones,
        "wind": {
            "speed_kmh": wind_kmh,
            "gust_kmh":  gust_kmh,
            "direction": wind_dir_str,
            "alert":     wind_alert,
            "alert_msg":    wind_alert_msg,
            "alert_msg_ru": wind_alert_msg_ru,
        },
        "rain": {
            "today_mm":   round(rain_today,  2) if rain_today  is not None else None,
            "week_mm":    round(rain_week,   2) if rain_week   is not None else None,
            "month_mm":   round(rain_month,  2) if rain_month  is not None else None,
            "rate_mm_h":  round(rain_rate,   2) if rain_rate   is not None else None,
            "last_24h_mm":round(rain_24h,    2) if rain_24h    is not None else None,
        },
        "uv": {
            "index":    round(uv_raw, 1) if uv_raw is not None else None,
            "level":    uv_lv_en,
            "level_ru": uv_lv_ru,
        },
        "lux":                  round(lux_val, 1) if lux_val is not None else None,
        "solar_radiation_w_m2": round(rad_val, 2) if rad_val is not None else None,
        "outdoor_temp_c":       round(outdoor_temp_c, 1) if outdoor_temp_c is not None else None,
        "outdoor_humidity":     round(outdoor_hum, 1) if outdoor_hum is not None else None,
        **(({"_ws_error": ws_error}) if ws_error else {}),
    }


# ===== v1.3.0 new endpoints =====

@app.get("/api/cam/garden.jpg")
def api_cam_garden(request: Request):
    """Proxy HP10 snapshot from HA camera_proxy. ?t=<timestamp> for cache-bust."""
    if not is_authed(request):
        raise HTTPException(401)
    sc, body, ctype = _ha_get_bytes(f"/api/camera_proxy/{E_GARDEN_CAM}", timeout=10)
    if sc == 200 and body:
        return Response(content=body, media_type="image/jpeg", headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        })
    return Response(status_code=sc or 502, content=b"", media_type="image/jpeg")


# v1.4.0: timelapse history sourced from Ecowitt cloud (HP10 daily timelapse).
# Public share URL is the auth path — no login required.
# Endpoints used:
#   POST https://www.ecowitt.net/index/get_video_info
#     body: authorize=<code>&device_id=<base64>&date=YYYYMMDD
#     -> errcode 0 + data.video_url (.mp4) when rendered
#     -> errcode 9001003 "Making" when still rendering / no video yet
#     -> other errcodes when day has no data
_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
ECOWITT_AUTHORIZE  = os.environ.get("ECOWITT_AUTHORIZE", "").strip()
ECOWITT_DEVICE_ID  = os.environ.get("ECOWITT_DEVICE_ID", "").strip()
ECOWITT_BASE       = "https://www.ecowitt.net"
ECOWITT_PROBE_DAYS = 30  # how far back to scan

# Cache: { "YYYY-MM-DD": ("ok", "https://.../foo.mp4", expires_epoch)
#                       | ("making", None, expires_epoch)
#                       | ("none",   None, expires_epoch) }
_TL_CACHE = {}
_TL_LIST_CACHE = {"dates": [], "expires": 0.0}

def _tl_cache_ttl(status: str) -> float:
    # Found videos cached for 24h, "making"/none retried often
    if status == "ok":     return 86400.0
    if status == "making": return 300.0     # 5 min
    return 1800.0                            # 30 min for definitive no-video days

def _ecowitt_fetch_video_info(date_iso: str):
    """Returns (status, url_or_none). status ∈ {"ok","making","none","err"}.
    Caches results in memory."""
    if not (ECOWITT_AUTHORIZE and ECOWITT_DEVICE_ID):
        return ("err", None)
    now = time.time()
    cached = _TL_CACHE.get(date_iso)
    if cached and cached[2] > now:
        return (cached[0], cached[1])
    yyyymmdd = date_iso.replace("-", "")
    from urllib.parse import urlencode
    body = urlencode({
        "authorize":  ECOWITT_AUTHORIZE,
        "device_id":  ECOWITT_DEVICE_ID,
        "date":       yyyymmdd,
    }).encode("utf-8")
    req = ur.Request(
        f"{ECOWITT_BASE}/index/get_video_info",
        data=body,
        method="POST",
        headers={
            "User-Agent":   "garden-pwa/1.4.0",
            "Accept":       "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with ur.urlopen(req, timeout=8.0) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ("err", None)  # don't cache transient errors
    errcode = str(j.get("errcode", "")).strip()
    if errcode == "0":
        url = (j.get("data") or {}).get("video_url") or ""
        if url:
            _TL_CACHE[date_iso] = ("ok", url, now + _tl_cache_ttl("ok"))
            return ("ok", url)
        # No URL but success — treat as none
        _TL_CACHE[date_iso] = ("none", None, now + _tl_cache_ttl("none"))
        return ("none", None)
    if errcode == "9001003":  # "Making"
        _TL_CACHE[date_iso] = ("making", None, now + _tl_cache_ttl("making"))
        return ("making", None)
    # Any other error → no video for this day
    _TL_CACHE[date_iso] = ("none", None, now + _tl_cache_ttl("none"))
    return ("none", None)

def _list_timelapse_dates():
    """Return sorted list of YYYY-MM-DD strings that have a rendered Ecowitt video.
    Probes the last ECOWITT_PROBE_DAYS days. Caches the resolved list for 5 min."""
    now = time.time()
    if _TL_LIST_CACHE["expires"] > now and _TL_LIST_CACHE["dates"]:
        return list(_TL_LIST_CACHE["dates"])
    if not (ECOWITT_AUTHORIZE and ECOWITT_DEVICE_ID):
        return []
    today_local = dt.datetime.now(LOCAL_TZ).date()
    out = []
    # Walk back from yesterday (today rarely has a video before midnight)
    for offset in range(1, ECOWITT_PROBE_DAYS + 1):
        d = today_local - dt.timedelta(days=offset)
        iso = d.isoformat()
        status, _url = _ecowitt_fetch_video_info(iso)
        if status == "ok":
            out.append(iso)
    # Also try today (in case render finished early)
    s_today, _ = _ecowitt_fetch_video_info(today_local.isoformat())
    if s_today == "ok":
        out.append(today_local.isoformat())
    out.sort()
    _TL_LIST_CACHE["dates"]   = out
    _TL_LIST_CACHE["expires"] = now + 300.0  # 5 min
    return out


# v1.6.4: local timelapse cache on the HA VM (/data/ is persistent across addon
# restarts via supervisor). Each rendered Ecowitt mp4 is downloaded once and
# served as a static file thereafter, so re-opening the camera modal is instant
# and we don't re-fetch ~50-150MB per tap.
TL_CACHE_DIR = os.environ.get("TL_CACHE_DIR", "/data/timelapse_cache")
TL_CACHE_KEEP_DAYS = 30   # match ECOWITT_PROBE_DAYS
_TL_DL_LOCKS = {}         # date -> threading.Lock (prevents concurrent re-downloads of the same day)
_TL_DL_LOCKS_GUARD = threading.Lock()

def _tl_cache_path(date_iso: str) -> str:
    return os.path.join(TL_CACHE_DIR, f"{date_iso}.mp4")

def _tl_cache_has(date_iso: str) -> bool:
    p = _tl_cache_path(date_iso)
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 1024
    except OSError:
        return False

def _tl_cache_lock(date_iso: str):
    with _TL_DL_LOCKS_GUARD:
        lock = _TL_DL_LOCKS.get(date_iso)
        if lock is None:
            lock = threading.Lock()
            _TL_DL_LOCKS[date_iso] = lock
        return lock

def _tl_download_to_cache(date_iso: str, url: str) -> bool:
    """Download Ecowitt mp4 to /data/timelapse_cache/<date>.mp4 atomically.
    Returns True on success. Safe to call concurrently — uses a per-date lock."""
    try:
        os.makedirs(TL_CACHE_DIR, exist_ok=True)
    except OSError as e:
        print(f"[timelapse] cannot mkdir {TL_CACHE_DIR}: {e}", flush=True)
        return False
    lock = _tl_cache_lock(date_iso)
    with lock:
        # double-check inside lock — another thread may have completed it
        if _tl_cache_has(date_iso):
            return True
        dest = _tl_cache_path(date_iso)
        tmp  = dest + ".tmp"
        try:
            req = ur.Request(url, headers={"User-Agent": "garden-pwa/1.6.4"})
            with ur.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(tmp) < 1024:
                os.unlink(tmp)
                print(f"[timelapse] download for {date_iso} too small, discarded", flush=True)
                return False
            os.replace(tmp, dest)
            print(f"[timelapse] cached {date_iso} ({os.path.getsize(dest)} bytes)", flush=True)
            _tl_prune_cache()
            return True
        except Exception as e:
            print(f"[timelapse] download failed for {date_iso}: {e}", flush=True)
            try: os.unlink(tmp)
            except OSError: pass
            return False

def _tl_prune_cache():
    """Keep the newest TL_CACHE_KEEP_DAYS files; delete older."""
    try:
        files = []
        for name in os.listdir(TL_CACHE_DIR):
            if not name.endswith(".mp4"):
                continue
            stem = name[:-4]
            if not _DATE_RE.match(stem):
                continue
            files.append(stem)
        files.sort(reverse=True)  # newest first by ISO date
        for stale in files[TL_CACHE_KEEP_DAYS:]:
            try:
                os.unlink(_tl_cache_path(stale))
                print(f"[timelapse] pruned {stale}", flush=True)
            except OSError:
                pass
    except OSError:
        pass

def _tl_cached_dates() -> list:
    try:
        out = []
        for name in os.listdir(TL_CACHE_DIR):
            if name.endswith(".mp4"):
                stem = name[:-4]
                if _DATE_RE.match(stem):
                    out.append(stem)
        out.sort()
        return out
    except OSError:
        return []

def _tl_serve_cached(date_iso: str):
    """FileResponse for a cached mp4 with strong client-side caching headers.
    Safari will then keep the bytes in its disk cache so even re-opens after
    addon restart are instant."""
    path = _tl_cache_path(date_iso)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={
            # Daily videos never change once rendered — safe to cache forever.
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"tl-{date_iso}"',
            "X-Timelapse-Source": "local-cache",
        },
    )


@app.get("/api/timelapse/today.mp4")
def api_timelapse_today(request: Request, date: str = ""):
    """Serve the timelapse mp4 for the requested date. Local cache first; on miss,
    download from Ecowitt cloud once and cache to /data/timelapse_cache/.
    No date → today if available, else most recent.
    v1.6.4: local file cache (was: 302 redirect on every open)."""
    if not is_authed(request):
        raise HTTPException(401)
    if not (ECOWITT_AUTHORIZE and ECOWITT_DEVICE_ID):
        return JSONResponse(
            status_code=503,
            content={"error": "Ecowitt share not configured",
                     "hint": "Set ecowitt_authorize and ecowitt_device_id in addon config"},
        )

    def _serve_or_fetch(d_iso: str):
        # Cache hit
        if _tl_cache_has(d_iso):
            return _tl_serve_cached(d_iso)
        # Cache miss → look up Ecowitt URL, download to cache, then serve
        st, url = _ecowitt_fetch_video_info(d_iso)
        if st == "ok" and url and _tl_download_to_cache(d_iso, url):
            return _tl_serve_cached(d_iso)
        if st == "making":
            return JSONResponse(status_code=404,
                content={"error": f"Video for {d_iso} is still rendering on Ecowitt cloud", "making": True})
        # Last-resort fallback: 302 to Ecowitt so the user still sees something
        if st == "ok" and url:
            return RedirectResponse(url, status_code=302)
        return JSONResponse(status_code=404, content={"error": f"No timelapse for {d_iso}"})

    if date and _DATE_RE.match(date):
        return _serve_or_fetch(date)

    # No date provided: pick today if Ecowitt has it, else most recent (cached or remote)
    today_local = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    if _tl_cache_has(today_local):
        return _tl_serve_cached(today_local)
    s_today, _u_today = _ecowitt_fetch_video_info(today_local)
    if s_today == "ok":
        return _serve_or_fetch(today_local)
    # Fall back to most recent rendered date
    cached = _tl_cached_dates()
    if cached:
        return _tl_serve_cached(cached[-1])
    dates = _list_timelapse_dates()
    if not dates:
        return JSONResponse(status_code=404,
            content={"error": "No timelapse available yet", "hint": "Ecowitt renders daily — check back tomorrow"})
    return _serve_or_fetch(dates[-1])


@app.get("/api/timelapse/status")
def api_timelapse_status(request: Request, date: str = ""):
    """Status for the picker UI — reports whether the queried date has a video
    on Ecowitt, and provides the latest available date.
    v1.4.0: Ecowitt-backed."""
    if not is_authed(request):
        raise HTTPException(401)
    today_local = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    queried = today_local
    if date and _DATE_RE.match(date):
        queried = date
    if not (ECOWITT_AUTHORIZE and ECOWITT_DEVICE_ID):
        return {
            "today_date": today_local, "queried_date": queried,
            "configured": False,
            "mp4_exists": False, "mp4_today_exists": False,
            "latest_date": None, "available_dates": [],
            "hint": "Set ecowitt_authorize / ecowitt_device_id in addon config",
        }
    status, _url = _ecowitt_fetch_video_info(queried)
    dates = _list_timelapse_dates()
    cached_dates = _tl_cached_dates()
    return {
        "today_date": today_local,
        "queried_date": queried,
        "configured": True,
        "queried_status": status,                # "ok" | "making" | "none" | "err"
        "mp4_exists": (status == "ok") or (queried in cached_dates),
        "mp4_today_exists": (today_local in dates) or (today_local in cached_dates),
        "queried_cached": queried in cached_dates,   # v1.6.4: served from local disk?
        "cached_dates": cached_dates,                # v1.6.4: list of locally cached dates
        "cached_count": len(cached_dates),
        "latest_date": dates[-1] if dates else None,
        "available_dates": dates,
        "snapshot_count": 0,                     # Ecowitt path — N/A
        "render_schedule": "Ecowitt cloud (daily) → local cache",
        "hint": "Timelapse rendered nightly on Ecowitt, cached on HA VM after first view",
    }


@app.get("/api/timelapse/list")
def api_timelapse_list(request: Request):
    """v1.4.0: Return list of dates with a rendered Ecowitt video.
    Used by the modal's date picker."""
    if not is_authed(request):
        raise HTTPException(401)
    today_local = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    if not (ECOWITT_AUTHORIZE and ECOWITT_DEVICE_ID):
        return {
            "dates": [], "latest": None, "earliest": None,
            "today": today_local, "count": 0,
            "configured": False,
            "hint": "Set ecowitt_authorize / ecowitt_device_id in addon config",
        }
    dates = _list_timelapse_dates()
    return {
        "dates":     dates,
        "latest":    dates[-1] if dates else None,
        "earliest":  dates[0]  if dates else None,
        "today":     today_local,
        "count":     len(dates),
        "configured": True,
    }


# ===== v1.7.15: Long-range timelapses (weekly / 30-day / 90-day) =====
# Renders produced by the garden_timelapse HA package live in
# /media/garden_timelapse/renders/ with naming:
#   weekly_<start>_<end>.mp4 | 30d_<start>_<end>.mp4 | 90d_<start>_<end>.mp4
#
# The library endpoint lists what's ready; range.mp4 streams a chosen file.
RENDERS_DIR = "/media/garden_timelapse/renders"
_ANCHOR_DATE = "2026-05-27"   # first day of timelapse program (Tuesday)
_RENDER_RE = __import__("re").compile(r"^(weekly|30d|90d)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.mp4$")

def _scan_renders():
    """Scan RENDERS_DIR and return sorted list of {kind, start, end, filename, size_mb}."""
    out = []
    try:
        for fn in os.listdir(RENDERS_DIR):
            m = _RENDER_RE.match(fn)
            if not m:
                continue
            kind, start, end = m.group(1), m.group(2), m.group(3)
            try:
                size_mb = round(os.path.getsize(os.path.join(RENDERS_DIR, fn)) / (1024*1024), 1)
            except OSError:
                size_mb = 0
            out.append({"kind": kind, "start": start, "end": end, "filename": fn, "size_mb": size_mb})
    except FileNotFoundError:
        pass
    # Newest first by end date
    out.sort(key=lambda r: (r["end"], r["start"]), reverse=True)
    return out


@app.get("/api/timelapse/library")
def api_timelapse_library(request: Request):
    """v1.7.15: List long-range renders + placeholder dates for not-yet-ready ones.

    Response:
      {
        "renders": [{kind, start, end, filename, size_mb}, ...],
        "placeholders": [
          {kind: "30d", ready: false, next_render: "2026-06-25", first_period: "..."},
          {kind: "90d", ready: false, next_render: "2026-08-24", first_period: "..."}
        ],
        "anchor": "2026-05-27"
      }
    """
    if not is_authed(request):
        raise HTTPException(401)
    renders = _scan_renders()
    have_kinds = {r["kind"] for r in renders}
    today = dt.datetime.now(LOCAL_TZ).date()
    anchor = dt.date.fromisoformat(_ANCHOR_DATE)
    placeholders = []
    for kind, period in (("30d", 30), ("90d", 90)):
        if kind in have_kinds:
            continue
        # First render fires when (today - anchor) >= period at 23:00
        first_render = anchor + dt.timedelta(days=period)
        placeholders.append({
            "kind": kind,
            "ready": False,
            "next_render": first_render.isoformat(),
            "period_days": period,
            "days_until": max(0, (first_render - today).days),
        })
    return {
        "renders": renders,
        "placeholders": placeholders,
        "anchor": _ANCHOR_DATE,
        "today": today.isoformat(),
    }


@app.get("/api/timelapse/range.mp4")
def api_timelapse_range(request: Request, f: str = ""):
    """v1.7.15: Serve a long-range mp4 by filename.
    Filename is validated against the kind_start_end.mp4 pattern to prevent traversal."""
    if not is_authed(request):
        raise HTTPException(401)
    if not f or not _RENDER_RE.match(f):
        raise HTTPException(400, detail="bad filename")
    path = os.path.join(RENDERS_DIR, f)
    if not os.path.isfile(path):
        raise HTTPException(404, detail="not rendered yet")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400", "Accept-Ranges": "bytes"},
    )


@app.get("/api/history_7d")
def api_history_7d(request: Request):
    """v1.3.2: 7-day daily-aggregated history for the REAL weather tile tap-through.
    Sources:
      - Ecowitt outdoor temp + humidity (daily min/avg/max)
      - Enphase solar daily kWh (from sensor.solar_riemann_daily statistics)
      - Ecowitt rain daily totals
    """
    if not is_authed(request):
        raise HTTPException(401)

    def _utc_z(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_local = dt.datetime.now(LOCAL_TZ)
    days = []
    for offset in range(6, -1, -1):  # 6 days ago → today
        d_local = (now_local - dt.timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = d_local.astimezone(dt.timezone.utc)
        end_local = d_local + dt.timedelta(days=1)
        end_utc = end_local.astimezone(dt.timezone.utc)
        days.append({
            "date_local": d_local.strftime("%Y-%m-%d"),
            "weekday":    d_local.strftime("%a"),
            "start_utc":  _utc_z(start_utc),
            "end_utc":    _utc_z(end_utc),
        })

    def _daily_minmaxavg(eid, day):
        """Fetch /history/period for one day and return min/max/avg of numeric states."""
        try:
            hist = ha("GET", f"/history/period/{day['start_utc']}?filter_entity_id={eid}&end_time={day['end_utc']}&minimal_response")
        except Exception:
            return None, None, None
        if not hist or not isinstance(hist, list) or len(hist) == 0 or not isinstance(hist[0], list):
            return None, None, None
        vals = []
        for st in hist[0]:
            try:
                v = float(st.get("state"))
                if math.isfinite(v):
                    vals.append(v)
            except (TypeError, ValueError):
                continue
        if not vals:
            return None, None, None
        return round(min(vals), 1), round(max(vals), 1), round(sum(vals)/len(vals), 1)

    def _daily_last_minus_first(eid, day):
        """For monotonic counters (Enphase production_daily, rain_daily): last − first.
        Returns the day's accumulated total in same units as the sensor."""
        try:
            hist = ha("GET", f"/history/period/{day['start_utc']}?filter_entity_id={eid}&end_time={day['end_utc']}&minimal_response")
        except Exception:
            return None
        if not hist or not isinstance(hist, list) or len(hist) == 0 or not isinstance(hist[0], list) or len(hist[0]) < 2:
            return None
        first_v, last_v = None, None
        for st in hist[0]:
            try:
                v = float(st.get("state"))
                if not math.isfinite(v): continue
                if first_v is None: first_v = v
                last_v = v
            except (TypeError, ValueError):
                continue
        if first_v is None or last_v is None: return None
        return max(0.0, round(last_v - first_v, 2))

    series = []
    for day in days:
        t_min, t_max, t_avg = _daily_minmaxavg(E_OUTDOOR_T, day)
        h_min, h_max, h_avg = _daily_minmaxavg(E_OUTDOOR_H, day)
        # Enphase: solar_riemann_daily resets at midnight, take max of day = total kWh
        solar_kwh = _daily_minmaxavg("sensor.solar_riemann_daily", day)[1]  # max
        # Rain: ecowitt daily_rain accumulates over the day, take last value (or max)
        rain_mm = _daily_minmaxavg(E_RAIN_DAILY, day)[1]
        # Wind: Ecowitt wind_speed is in m/s — fetch min/max/avg and convert to km/h
        w_min, w_max, w_avg = _daily_minmaxavg(E_WIND_SPEED, day)
        g_min, g_max, g_avg = _daily_minmaxavg(E_WIND_GUST,  day)
        def _ms2kmh(v): return round(v * 3.6, 1) if v is not None else None
        series.append({
            "date":    day["date_local"],
            "weekday": day["weekday"],
            "temp_min_c": t_min, "temp_max_c": t_max, "temp_avg_c": t_avg,
            "hum_min":    h_min, "hum_max":    h_max, "hum_avg":    h_avg,
            "solar_kwh":  solar_kwh,
            "rain_mm":    rain_mm,
            "wind_avg_kmh": _ms2kmh(w_avg),
            "wind_max_kmh": _ms2kmh(w_max),
            "gust_max_kmh": _ms2kmh(g_max),
        })

    return {
        "days": series,
        "sources": {
            "temperature": "Ecowitt outdoor_temperature",
            "humidity":    "Ecowitt humidity",
            "solar":       "Enphase solar_riemann_daily",
            "rain":        "Ecowitt daily_rain",
        },
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
