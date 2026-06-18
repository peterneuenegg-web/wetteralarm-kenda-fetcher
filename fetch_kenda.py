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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr
from scipy.interpolate import griddata

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
    lat_candidates = {"clat", "rlat", "latitude", "lat"}
    lon_candidates = {"clon", "rlon", "longitude", "lon"}

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
            if int(m.group("lead")) != 0:
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


def write_index(out_dir: Path, hours: list[datetime]) -> Path:
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
    import paramiko

    host = require_env("SFTP_HOST")
    user = require_env("SFTP_USER")
    pwd = require_env("SFTP_PASSWORD")

    log.info("SFTP: connecting to %s as %s", host, user)
    transport = paramiko.Transport((host, 22))
    transport.connect(username=user, password=pwd)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
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
            json_path = write_layer_json(out_dir, item.timestamp, item.param, grid, lats_1d, lngs_1d)
            success_files.append(json_path)
            hours_seen.add(item.timestamp)

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
