#!/usr/bin/env python3
"""
KENDA-CH1 PoC fetcher.

Pollt die STAC-Collection ch.meteoschweiz.ogd-analysis-kenda-ch1, lädt die
GRIB2-Files für die schaden-relevanten Parameter herunter, schneidet auf die
Schweiz-Bbox und schreibt pro Stunde × Layer eine kompakte JSON-Datei.

Die JSON-Files werden anschliessend per SFTP nach widget.wetteralarm.ch
hochgeladen, von wo sie die statische PoC-Karte ausliest.

Env-Variablen (via GitHub Secrets):
  SFTP_HOST           — z.B. widget.wetteralarm.ch
  SFTP_USER           — SFTP-Login
  SFTP_PASSWORD       — SFTP-Passwort
  SFTP_REMOTE_DIR     — z.B. /web-scripts/kenda-poc/data
  LOOKBACK_HOURS      — optional, default 24 (wie weit zurück in STAC geschaut wird)

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr

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

# Schaden-relevante Parameter für den PoC.
# Mapping: STAC-Parameter-Code → (Anzeige-Label, Einheit, GRIB-Variable, Default-Skala)
# Skala-Format: (min, mid, max) für die Heatmap-Farbskala im Frontend
RELEVANT_PARAMS = {
    "vmax_10m": {
        "label": "Wind-Böenspitze 10 m",
        "unit": "m/s",
        "scale": [0, 15, 35],
    },
    "tot_prec": {
        "label": "Total Niederschlag (1 h)",
        "unit": "mm",
        "scale": [0, 5, 30],
    },
    "cape_ml": {
        "label": "CAPE (Konvektion)",
        "unit": "J/kg",
        "scale": [0, 1000, 2500],
    },
    "dbz_cmax": {
        "label": "Max Radar-Reflektivität",
        "unit": "dBZ",
        "scale": [0, 35, 55],
    },
}

# Schweiz-Bbox (lng_min, lat_min, lng_max, lat_max)
# Etwas grosszügig, damit alle Anrainer-Gebiete drin sind.
CH_BBOX = (5.8, 45.7, 10.6, 47.9)

# Item-ID-Pattern: 06172026-0900-0-vmax_10m-ctrl-XXXXXXX
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


def fetch_stac_items(lookback_hours: int) -> list[StacItem]:
    """Holt alle STAC-Items der letzten N Stunden für die 4 Ziel-Parameter."""
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
            # Nur Control-Run (ctrl), keine Ensemble-Mitglieder
            if m.group("member") != "ctrl":
                continue
            # Nur Analyse (lead=0), keine First-Guess (lead>0)
            if int(m.group("lead")) != 0:
                continue

            iso = feat.get("properties", {}).get("datetime")
            if not iso:
                continue
            ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if ts < cutoff:
                continue

            # Asset-URL holen (erstes Asset, da nur eins pro Item)
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

        # Pagination
        next_url = None
        for link in body.get("links", []):
            if link.get("rel") == "next":
                next_url = link.get("href")
                break

    log.info("Found %d relevant items (%d hours lookback)", len(items), lookback_hours)
    return items


def download_grib(url: str, dest: Path) -> bool:
    """Lädt eine GRIB2-Datei nach `dest`. True bei Erfolg."""
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


def grib_to_grid(grib_path: Path, bbox: tuple) -> dict | None:
    """
    Liest die GRIB2-Datei via cfgrib, schneidet auf die Bbox und liefert
    ein kompaktes Dict mit den Werten als 2D-Float32-Array.
    """
    try:
        ds = xr.open_dataset(
            grib_path,
            engine="cfgrib",
            backend_kwargs={"indexpath": ""},  # keine .idx-Files schreiben
        )
    except Exception as e:
        log.error("Open GRIB failed: %s", e)
        return None

    # Erste Daten-Variable rausziehen
    data_vars = [v for v in ds.data_vars if v not in ("step", "time")]
    if not data_vars:
        log.error("No data variables in %s", grib_path.name)
        return None
    var = ds[data_vars[0]]

    # Koordinaten finden (KENDA-CH1 nutzt lat/lon, manchmal x/y)
    lat_name = next((c for c in var.dims if c.lower() in ("latitude", "lat", "y")), None)
    lon_name = next((c for c in var.dims if c.lower() in ("longitude", "lon", "x")), None)
    if not (lat_name and lon_name):
        log.error("Unknown coord names: %s", list(var.dims))
        return None

    lats = ds[lat_name].values
    lons = ds[lon_name].values

    # Bbox-Slice
    lng_min, lat_min, lng_max, lat_max = bbox
    lat_idx = np.where((lats >= lat_min) & (lats <= lat_max))[0]
    lon_idx = np.where((lons >= lng_min) & (lons <= lng_max))[0]
    if not len(lat_idx) or not len(lon_idx):
        log.error("Empty bbox slice for %s", grib_path.name)
        return None

    arr = var.values[..., lat_idx[0] : lat_idx[-1] + 1, lon_idx[0] : lon_idx[-1] + 1]
    if arr.ndim > 2:
        arr = arr.squeeze()
    arr = np.asarray(arr, dtype=np.float32)

    return {
        "shape": list(arr.shape),
        "lat_min": float(lats[lat_idx[0]]),
        "lat_max": float(lats[lat_idx[-1]]),
        "lng_min": float(lons[lon_idx[0]]),
        "lng_max": float(lons[lon_idx[-1]]),
        # Werte als nested list; NaN → null für JSON-Konformität
        "values": [
            [None if np.isnan(v) else round(float(v), 2) for v in row]
            for row in arr
        ],
    }


def write_layer_json(out_dir: Path, ts: datetime, param: str, grid: dict) -> Path:
    """Schreibt eine Layer-JSON-Datei und liefert den Pfad zurück."""
    out = {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "param": param,
        "label": RELEVANT_PARAMS[param]["label"],
        "unit": RELEVANT_PARAMS[param]["unit"],
        "scale": RELEVANT_PARAMS[param]["scale"],
        **grid,
    }
    fname = f"{ts.strftime('%Y%m%dT%H%M')}_{param}.json"
    path = out_dir / fname
    with path.open("w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    return path


def write_index(out_dir: Path, hours: list[datetime]) -> Path:
    """Schreibt eine index.json mit allen verfügbaren Stunden + Parametern."""
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bbox": list(CH_BBOX),
        "params": {p: RELEVANT_PARAMS[p] for p in RELEVANT_PARAMS},
        "hours": [h.strftime("%Y-%m-%dT%H:%M:%SZ") for h in sorted(hours)],
    }
    path = out_dir / "index.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def upload_sftp(local_files: list[Path], remote_dir: str) -> bool:
    """Lädt eine Liste lokaler Dateien per SFTP hoch."""
    import paramiko

    host = require_env("SFTP_HOST")
    user = require_env("SFTP_USER")
    pwd = require_env("SFTP_PASSWORD")

    log.info("SFTP: connecting to %s as %s", host, user)
    transport = paramiko.Transport((host, 22))
    transport.connect(username=user, password=pwd)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        # mkdir -p
        parts = remote_dir.strip("/").split("/")
        current = ""
        for p in parts:
            current = current + "/" + p
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)

        for f in local_files:
            remote = f"{remote_dir.rstrip('/')}/{f.name}"
            log.info("  PUT %s → %s", f.name, remote)
            sftp.put(str(f), remote)
        return True
    finally:
        sftp.close()
        transport.close()


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

        success_files: list[Path] = []
        hours_seen = set()

        for item in sorted(items, key=lambda x: (x.timestamp, x.param)):
            grib_path = tmp / f"{item.item_id}.grib2"
            log.info("Processing %s (%s, %s)", item.item_id, item.param, item.timestamp.isoformat())

            if not download_grib(item.grib_url, grib_path):
                continue
            grid = grib_to_grid(grib_path, CH_BBOX)
            if grid is None:
                continue
            json_path = write_layer_json(out_dir, item.timestamp, item.param, grid)
            success_files.append(json_path)
            hours_seen.add(item.timestamp)
            grib_path.unlink(missing_ok=True)

        if not success_files:
            log.error("No successful conversions")
            return 1

        index_path = write_index(out_dir, list(hours_seen))
        success_files.append(index_path)

        log.info("Uploading %d files via SFTP", len(success_files))
        try:
            upload_sftp(success_files, remote_dir)
        except Exception as e:
            log.error("SFTP upload failed: %s", e)
            return 1

    log.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
