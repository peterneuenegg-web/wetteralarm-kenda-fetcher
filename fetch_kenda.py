#!/usr/bin/env python3
"""
KENDA-CH1 PoC fetcher.

Pollt die STAC-Collection ch.meteoschweiz.ogd-analysis-kenda-ch1, lädt die
GRIB2-Files für die schaden-relevanten Parameter herunter und schreibt pro
Stunde × Layer eine kompakte JSON-Datei.

KENDA-CH1 benutzt ein triangulares ICON-Mesh (`unstructured_grid`), kein
reguläres Lat/Lon-Gitter. Die Zell-Koordinaten (CLAT, CLON) stehen im
Collection-Asset `horizontal_constants_kenda-ch1.grib2` (~11 MB, statisch).
Wir laden das einmal pro Worker-Run, maskieren auf die Schweiz-Bbox und
interpolieren die Werte via scipy.griddata auf ein reguläres 0.01°-Gitter
(≈ 1.1 km). Das Output-JSON bleibt kompatibel mit der statischen Karte.

Env-Variablen (via GitHub Secrets):
  SFTP_HOST           — z.B. widget.wetteralarm.ch
  SFTP_USER           — SFTP-Login
  SFTP_PASSWORD       — SFTP-Passwort
  SFTP_REMOTE_DIR     — z.B. /web-scripts/kenda-poc/data
  LOOKBACK_HOURS      — optional, default 24

Exit-Code:
  0  alle Stunden erfolgreich oder nichts Neues
  1  mindestens eine Stunde fehlgeschlagen
  2  Konfiguration fehlt
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr
from scipy.interpolate import griddata

# KENDA-Daten-GRIBs verwenden gridType='unstructured_grid' und bringen keine
# eingebetteten Lat/Lon mit — Koordinaten holen wir uns separat aus
# horizontal_constants_kenda-ch1.grib2 (siehe load_mesh()). Die ecCodes-Warnung
# ist deshalb erwartbar und harmlos; wir unterdrücken sie, damit das Log
# sauber bleibt und echte Probleme klarer sichtbar werden.
warnings.filterwarnings(
    "ignore",
    message=r".*provides no latitudes/longitudes for gridType='unstructured_grid'.*",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kenda-fetcher")

STAC_COLLECTION = "ch.meteoschweiz.ogd-analysis-kenda-ch1"
STAC_ITEMS_URL = (
    f"https://data.geo.admin.ch/api/stac/v1/collections/{STAC_COLLECTION}/items"
)
COLLECTION_ASSETS_URL = (
    f"https://data.geo.admin.ch/api/stac/v1/collections/{STAC_COLLECTION}/assets"
)
HORIZONTAL_CONSTANTS_ASSET = "horizontal_constants_kenda-ch1.grib2"

RELEVANT_PARAMS = {
    # Wind
    "vmax_10m": {"label": "Wind-Böenspitze 10 m",     "unit": "m/s",  "scale": [0, 15, 35]},
    "u_10m":    {"label": "Wind 10 m (Ost-Komp.)",    "unit": "m/s",  "scale": [-20, 0, 20]},
    "v_10m":    {"label": "Wind 10 m (Nord-Komp.)",   "unit": "m/s",  "scale": [-20, 0, 20]},
    # Niederschlag
    "tot_prec": {"label": "Total Niederschlag (1 h)", "unit": "mm",   "scale": [0, 5, 30]},
    "rain_gsp": {"label": "Regen (1 h)",              "unit": "mm",   "scale": [0, 5, 30]},
    "snow_gsp": {"label": "Schnee-Niederschlag (1 h)","unit": "mm",   "scale": [0, 5, 30]},
    "grau_gsp": {"label": "Graupel (1 h)",            "unit": "mm",   "scale": [0, 2, 10]},
    # Konvektion / Radar
    "cape_ml":  {"label": "CAPE (Konvektion)",        "unit": "J/kg", "scale": [0, 1000, 2500]},
    "dbz_cmax": {"label": "Max Radar-Reflektivität",  "unit": "dBZ",  "scale": [0, 35, 55]},
    # Temperatur
    "t_2m":     {"label": "Temperatur 2 m",           "unit": "°C",   "scale": [-10, 10, 35]},
    "tmax_2m":  {"label": "Tagesmax 2 m (1 h)",       "unit": "°C",   "scale": [-10, 15, 38]},
    "tmin_2m":  {"label": "Tagesmin 2 m (1 h)",       "unit": "°C",   "scale": [-15, 5, 30]},
    # Schnee
    "h_snow":   {"label": "Schneehöhe",               "unit": "m",    "scale": [0, 0.5, 2]},
    "snowlmt":  {"label": "Schneefallgrenze",         "unit": "m",    "scale": [0, 1500, 4000]},
}

# KENDA publiziert unterschiedlich, je nach Aggregationstyp:
#  - Instant- und Constant-Variablen → lead=0 (Analyse)
#  - Stündliche Aggregate (Min/Max/Sum über vorige Stunde) → lead=1 (First Guess)
# Pro Parameter exakt den richtigen Lead akzeptieren — sonst kommen 404 im Frontend.
PARAM_LEAD = {
    # Aggregate über die Vorstunde
    "vmax_10m": 1,
    "tot_prec": 1,
    "rain_gsp": 1,
    "snow_gsp": 1,
    "grau_gsp": 1,
    "tmax_2m":  1,
    "tmin_2m":  1,
    # Constant / Instant
    "u_10m":    0,
    "v_10m":    0,
    "cape_ml":  0,
    "dbz_cmax": 0,
    "t_2m":     0,
    "h_snow":   0,
    "snowlmt":  0,
}

# Konvertierungs-Helper: KENDA t_2m kommt in Kelvin, wir wollen °C im JSON.
# Andere Parameter sind in den Einheiten, die wir oben deklariert haben.
def post_process(param: str, arr: np.ndarray) -> np.ndarray:
    if param in ("t_2m", "tmax_2m", "tmin_2m"):
        return arr - 273.15
    return arr

CH_BBOX = (5.8, 45.7, 10.6, 47.9)  # lng_min, lat_min, lng_max, lat_max
GRID_RES_DEG = 0.01                # ≈ 1.1 km bei 47°N

ITEM_ID_RE = re.compile(
    r"^(?P<date>\d{8})-(?P<hhmm>\d{4})-(?P<lead>\d+)-(?P<param>[a-z0-9_]+)-(?P<member>[a-z]+)",
)


@dataclass
class StacItem:
    item_id: str
    timestamp: datetime
    param: str
    grib_url: str


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        log.error("Missing required env var: %s", name)
        sys.exit(2)
    return v


# ────────────────────────────────────────────────────────────────────────────
# Mesh laden — einmalig pro Worker-Run
# ────────────────────────────────────────────────────────────────────────────

def fetch_horizontal_constants_url() -> str:
    r = requests.get(COLLECTION_ASSETS_URL, timeout=30)
    r.raise_for_status()
    for a in r.json().get("assets", []):
        if a.get("id") == HORIZONTAL_CONSTANTS_ASSET:
            return a["href"]
    raise RuntimeError("horizontal_constants asset not found in STAC")


def load_mesh(tmp_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    Lädt das KENDA-Mesh und liefert (clat, clon) als 1D-Arrays in Grad.

    Geht über die eccodes-Python-API direkt durch alle GRIB-Messages, weil
    cfgrib's Filter-by-shortName empfindlich auf die exakte Schreibweise und
    die Konsistenz der Grid-Definitionen reagiert. Hier iterieren wir
    schlicht und nehmen das, was als latitude/longitude erkennbar ist.
    """
    import eccodes  # bringt cfgrib mit

    url = fetch_horizontal_constants_url()
    path = tmp_dir / HORIZONTAL_CONSTANTS_ASSET
    log.info("Downloading horizontal_constants (~11 MB) …")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)

    # Erkennungs-Heuristik: case-insensitive Match auf bekannte ICON-Namen.
    # KENDA-CH1 verwendet `tlat`/`tlon` (Latitude/Longitude on T grid).
    lat_candidates = {"tlat", "clat", "rlat", "latitude", "lat"}
    lon_candidates = {"tlon", "clon", "rlon", "longitude", "lon"}

    clat: np.ndarray | None = None
    clon: np.ndarray | None = None
    seen: list[str] = []

    with path.open("rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                short = eccodes.codes_get(gid, "shortName")
                name = eccodes.codes_get(gid, "name") if eccodes.codes_is_defined(gid, "name") else ""
                seen.append(f"{short} ({name})")
                short_lc = short.lower()
                if clat is None and short_lc in lat_candidates:
                    clat = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
                    log.info("  Found CLAT via shortName=%r (%d cells)", short, clat.size)
                elif clon is None and short_lc in lon_candidates:
                    clon = np.asarray(eccodes.codes_get_array(gid, "values"), dtype=np.float64)
                    log.info("  Found CLON via shortName=%r (%d cells)", short, clon.size)
            finally:
                eccodes.codes_release(gid)

    log.info("GRIB messages in horizontal_constants: %s", "; ".join(seen) if seen else "(none)")

    if clat is None or clon is None:
        raise RuntimeError(
            f"CLAT/CLON not found via shortName heuristic. "
            f"Seen shortNames: {[s.split(' ')[0] for s in seen]}"
        )

    # Manche ICON-Outputs liefern Radians statt Grad. Plausibilitäts-Check:
    # Werte im Bereich [-π, π] → Radians → in Grad konvertieren.
    if np.nanmax(np.abs(clat)) <= np.pi + 0.01:
        log.info("  CLAT values look like radians (max=%.3f), converting to degrees", np.nanmax(np.abs(clat)))
        clat = np.degrees(clat)
    if np.nanmax(np.abs(clon)) <= np.pi + 0.01:
        log.info("  CLON values look like radians (max=%.3f), converting to degrees", np.nanmax(np.abs(clon)))
        clon = np.degrees(clon)

    if clat.size != clon.size:
        raise RuntimeError(f"CLAT/CLON size mismatch: {clat.size} vs {clon.size}")

    log.info("  CLAT range: %.3f .. %.3f", float(np.nanmin(clat)), float(np.nanmax(clat)))
    log.info("  CLON range: %.3f .. %.3f", float(np.nanmin(clon)), float(np.nanmax(clon)))

    path.unlink(missing_ok=True)
    return clat, clon


# ────────────────────────────────────────────────────────────────────────────
# Daten verarbeiten
# ────────────────────────────────────────────────────────────────────────────

def fetch_stac_items(lookback_hours: int) -> list[StacItem]:
    items: list[StacItem] = []
    next_url = STAC_ITEMS_URL + "?limit=500"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    while next_url:
        log.info("STAC: GET %s", next_url)
        r = requests.get(next_url, timeout=30)
        r.raise_for_status()
        body = r.json()

        for feat in body.get("features", []):
            m = ITEM_ID_RE.match(feat["id"])
            if not m:
                continue
            param = m.group("param")
            if param not in RELEVANT_PARAMS:
                continue
            if m.group("member") != "ctrl":
                continue
            expected_lead = PARAM_LEAD.get(param, 0)
            if int(m.group("lead")) != expected_lead:
                continue

            iso = feat.get("properties", {}).get("datetime")
            if not iso:
                continue
            ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if ts < cutoff:
                continue

            assets = feat.get("assets", {})
            asset = next(iter(assets.values()), None)
            if not asset or "href" not in asset:
                continue

            items.append(
                StacItem(
                    item_id=feat["id"],
                    timestamp=ts,
                    param=param,
                    grib_url=asset["href"],
                )
            )

        next_url = None
        for link in body.get("links", []):
            if link.get("rel") == "next":
                next_url = link.get("href")
                break

    log.info("Found %d relevant items (%d hours lookback)", len(items), lookback_hours)
    return items


def download_grib(url: str, dest: Path) -> bool:
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        log.error("Download failed (%s): %s", url[:80], e)
        return False


def read_values_1d(grib_path: Path) -> np.ndarray | None:
    """Liest das 1D-Wertearray (eine Zahl pro Mesh-Zelle) aus dem Daten-GRIB."""
    try:
        ds = xr.open_dataset(
            grib_path,
            engine="cfgrib",
            backend_kwargs={"indexpath": ""},
        )
    except Exception as e:
        log.error("Open GRIB failed: %s", e)
        return None
    data_vars = list(ds.data_vars)
    if not data_vars:
        log.error("No data variables in %s", grib_path.name)
        return None
    return ds[data_vars[0]].values.astype(np.float32).ravel()


def build_regular_grid() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Baut das Ziel-Gitter (Lat absteigend von Nord→Süd, Lng aufsteigend West→Ost)
    und liefert (lats_1d, lngs_1d, grid_lng_2d, grid_lat_2d).
    """
    lng_min, lat_min, lng_max, lat_max = CH_BBOX
    lngs = np.arange(lng_min, lng_max + GRID_RES_DEG / 2, GRID_RES_DEG)
    lats = np.arange(lat_max, lat_min - GRID_RES_DEG / 2, -GRID_RES_DEG)
    gx, gy = np.meshgrid(lngs, lats)
    return lats, lngs, gx, gy


def regrid_to_array(
    ch_lats: np.ndarray,
    ch_lons: np.ndarray,
    ch_vals: np.ndarray,
    gx: np.ndarray,
    gy: np.ndarray,
) -> np.ndarray:
    """Linear-Interpolation der Punkte auf das reguläre Gitter; Lücken = NaN."""
    grid = griddata(
        points=(ch_lons, ch_lats),
        values=ch_vals,
        xi=(gx, gy),
        method="linear",
        fill_value=np.nan,
    )
    return grid.astype(np.float32)


def write_layer_json(
    out_dir: Path,
    ts: datetime,
    param: str,
    grid: np.ndarray,
    lats_1d: np.ndarray,
    lngs_1d: np.ndarray,
) -> Path:
    payload = {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "param": param,
        "label": RELEVANT_PARAMS[param]["label"],
        "unit": RELEVANT_PARAMS[param]["unit"],
        "scale": RELEVANT_PARAMS[param]["scale"],
        "shape": [int(grid.shape[0]), int(grid.shape[1])],
        "lat_max": float(lats_1d[0]),
        "lat_min": float(lats_1d[-1]),
        "lng_min": float(lngs_1d[0]),
        "lng_max": float(lngs_1d[-1]),
        "values": [
            [None if np.isnan(v) else round(float(v), 2) for v in row]
            for row in grid
        ],
    }
    fname = f"{ts.strftime('%Y%m%dT%H%M')}_{param}.json"
    path = out_dir / fname
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    return path


def write_index(out_dir: Path, hours: list[datetime], params_written: set[str]) -> Path:
    """Nur Parameter ins Index aufnehmen, für die wir tatsächlich JSON erzeugt haben."""
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bbox": list(CH_BBOX),
        "params": {p: RELEVANT_PARAMS[p] for p in RELEVANT_PARAMS if p in params_written},
        "hours": [h.strftime("%Y-%m-%dT%H:%M:%SZ") for h in sorted(hours)],
    }
    path = out_dir / "index.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def _post_with_retry(url: str, body: bytes, headers: dict, max_attempts: int = 3) -> tuple[int | None, str]:
    """
    POST mit kurzem Exponential-Backoff (2/4/8 s) gegen transiente Netzwerk-
    Aussetzer. Retry nur bei Netzwerk-Errors und 5xx-Antworten — 4xx (Token,
    Format etc.) wird sofort als fatal markiert.

    Returns: (status_code | None, error_or_body_snippet)
    """
    import time

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(url, data=body, headers=headers, timeout=60)
            if r.status_code in (200, 201):
                return r.status_code, ""
            if 400 <= r.status_code < 500:
                # 4xx ist immer permanent — kein Retry, sofort raus
                return r.status_code, r.text[:200]
            # 5xx → Retry
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = str(e)[:200]
        except Exception as e:
            return None, f"unexpected: {e!s}"

        if attempt < max_attempts:
            time.sleep(2 ** attempt)  # 2s, 4s

    return None, last_err


def post_to_schaden(local_files: list[Path]) -> bool:
    """
    POSTet jedes Layer-JSON ans Schaden-Ingest-Endpoint. Server speichert es
    gzipped auf Disk und schreibt einen Eintrag in `kenda_frames`.
    Idempotent — bereits ingestiertete (timestamp, parameter)-Paare werden
    server-seitig überschrieben.

    Bei transienten Fehlern (Connection/Timeout/5xx) wird bis zu 3× retried.
    Permanente 4xx (Auth, Bad Request) brechen sofort ohne Retry ab.

    Env (optional, sonst skip):
      INGEST_URL    — z.B. https://schaden.wetteralarm.ch/api/kenda-ingest.php
      INGEST_TOKEN  — muss matchen mit KENDA_INGEST_TOKEN in .env
      HEARTBEAT_URL — optional, wird nach erfolgreichem Lauf gepingt
    """
    url = os.environ.get("INGEST_URL", "").strip()
    token = os.environ.get("INGEST_TOKEN", "").strip()
    if not url or not token:
        log.info("INGEST_URL/INGEST_TOKEN nicht gesetzt — Schaden-POST übersprungen")
        return True

    sent = 0
    failed = 0
    for f in local_files:
        # index.json überspringen — der Schaden-Server baut seinen eigenen Index
        if f.name == "index.json":
            continue
        with f.open("rb") as fh:
            body = fh.read()
        headers = {
            "Content-Type": "application/json",
            "X-Ingest-Token": token,
            "X-Filename": f.name,
        }
        status, err = _post_with_retry(url, body, headers)
        if status in (200, 201):
            sent += 1
        else:
            log.warning("POST %s failed (final): status=%s err=%s", f.name, status, err)
            failed += 1

    log.info("Schaden-Ingest: %d sent, %d failed", sent, failed)

    # Heartbeat ans Schaden-Monitoring (best-effort, auch mit ein paar Failures)
    hb_url = os.environ.get("HEARTBEAT_URL", "").strip()
    if hb_url and sent > 0:
        try:
            requests.get(hb_url, headers={"X-Ingest-Token": token}, timeout=15)
        except Exception:
            pass

    return failed == 0


def upload_sftp(local_files: list[Path], remote_dir: str) -> bool:
    import paramiko

    host = require_env("SFTP_HOST")
    user = require_env("SFTP_USER")
    pwd = require_env("SFTP_PASSWORD")

    log.info("SFTP: connecting to %s as %s", host, user)
    transport = paramiko.Transport((host, 22))
    transport.connect(username=user, password=pwd)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        # Diagnose: was sieht der User nach Login? Hilft, den richtigen
        # SFTP_REMOTE_DIR zu finden (chroot vs. absoluter Pfad).
        try:
            cwd = sftp.getcwd() or sftp.normalize(".")
            log.info("SFTP login cwd: %s", cwd)
        except Exception as e:
            log.info("SFTP cwd unknown (%s)", e)
        try:
            entries = sorted(sftp.listdir("."))
            log.info("SFTP top-level entries (%d): %s", len(entries), ", ".join(entries[:30]))
        except Exception as e:
            log.warning("SFTP listdir failed: %s", e)

        # Pfad-Navigation: erst absoluten Pfad probieren, sonst relativ vom
        # Login-Cwd aus. Detaillierte Fehlermeldung bei Permission-Issues.
        is_absolute = remote_dir.startswith("/")
        parts = remote_dir.strip("/").split("/")
        current = "" if is_absolute else "."
        for p in parts:
            next_path = (current + "/" + p) if is_absolute else (current + "/" + p if current != "." else p)
            try:
                sftp.stat(next_path)
                log.info("  exists: %s", next_path)
            except FileNotFoundError:
                try:
                    sftp.mkdir(next_path)
                    log.info("  mkdir : %s", next_path)
                except OSError as e:
                    log.error(
                        "  Cannot mkdir %s: %s. "
                        "Hinweis: SFTP_REMOTE_DIR ist ggf. ausserhalb des Chroot-Bereichs. "
                        "Vergleiche den Wert mit der Top-Level-Listing oben.",
                        next_path, e,
                    )
                    raise
            except OSError as e:
                log.error(
                    "  Cannot stat %s: %s. "
                    "Hinweis: vermutlich Chroot-Grenze überschritten — relative Pfade probieren.",
                    next_path, e,
                )
                raise
            current = next_path

        target_dir = current
        for f in local_files:
            remote = f"{target_dir.rstrip('/')}/{f.name}"
            sftp.put(str(f), remote)
        log.info("Uploaded %d files to %s", len(local_files), target_dir)
        return True
    finally:
        sftp.close()
        transport.close()


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    lookback = int(os.environ.get("LOOKBACK_HOURS", "24"))
    remote_dir = os.environ.get("SFTP_REMOTE_DIR", "/web-scripts/kenda-poc/data")

    items = fetch_stac_items(lookback)
    if not items:
        log.warning("No items found")
        return 0

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        out_dir = tmp / "out"
        out_dir.mkdir()

        # 1) Mesh einmalig laden
        log.info("Loading KENDA mesh (CLAT/CLON) …")
        try:
            clat, clon = load_mesh(tmp)
        except Exception as e:
            log.error("Mesh load failed: %s", e)
            return 1
        log.info("Mesh: %d cells total", clat.size)

        # 2) Bbox-Mask auf Schweiz
        lng_min, lat_min, lng_max, lat_max = CH_BBOX
        mask = (clat >= lat_min) & (clat <= lat_max) & (clon >= lng_min) & (clon <= lng_max)
        ch_lats = clat[mask]
        ch_lons = clon[mask]
        ch_indices = np.where(mask)[0]
        log.info("CH-Bbox-Filter: %d / %d cells", ch_indices.size, clat.size)

        if ch_indices.size < 100:
            log.error("Too few cells inside bbox — check CLAT/CLON parsing")
            return 1

        # 3) Reguläres Ziel-Gitter
        lats_1d, lngs_1d, gx, gy = build_regular_grid()
        log.info("Target grid: %d × %d (lat × lng) at %g°", lats_1d.size, lngs_1d.size, GRID_RES_DEG)

        # 4) Pro Item: download, mask, regrid, json
        success_files: list[Path] = []
        hours_seen: set[datetime] = set()
        params_written: set[str] = set()

        for item in sorted(items, key=lambda x: (x.timestamp, x.param)):
            grib_path = tmp / f"{item.item_id}.grib2"
            log.info("Processing %s (%s, %s)", item.item_id, item.param, item.timestamp.isoformat())

            if not download_grib(item.grib_url, grib_path):
                continue
            values_1d = read_values_1d(grib_path)
            grib_path.unlink(missing_ok=True)
            if values_1d is None:
                continue
            if values_1d.size != clat.size:
                log.error(
                    "Cell count mismatch for %s: data=%d, mesh=%d",
                    item.item_id, values_1d.size, clat.size,
                )
                continue

            ch_vals = values_1d[ch_indices]
            grid = regrid_to_array(ch_lats, ch_lons, ch_vals, gx, gy)
            grid = post_process(item.param, grid)
            json_path = write_layer_json(out_dir, item.timestamp, item.param, grid, lats_1d, lngs_1d)
            success_files.append(json_path)
            hours_seen.add(item.timestamp)
            params_written.add(item.param)

        if not success_files:
            log.error("No successful conversions")
            return 1

        log.info("Layers written: %s", sorted(params_written))
        index_path = write_index(out_dir, list(hours_seen), params_written)
        success_files.append(index_path)

        # SFTP-Upload zur PoC-Karte ist optional und wird nur ausgeführt, wenn
        # SFTP_HOST gesetzt ist. Standard ab Phase 2 (Schaden-Karten-Integration):
        # KEIN SFTP mehr — die Daten gehen direkt in die Schaden-Plattform.
        sftp_ok = True
        if os.environ.get("SFTP_HOST", "").strip():
            log.info("Uploading %d files via SFTP", len(success_files))
            try:
                upload_sftp(success_files, remote_dir)
            except Exception as e:
                log.error("SFTP upload failed: %s", e)
                sftp_ok = False
        else:
            log.info("SFTP_HOST nicht gesetzt — PoC-Upload übersprungen")

        # POST an Schaden-Ingest
        ingest_ok = post_to_schaden(success_files)

        if not sftp_ok and not ingest_ok:
            return 1

    log.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
