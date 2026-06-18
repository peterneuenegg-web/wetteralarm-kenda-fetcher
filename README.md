# wetteralarm-kenda-fetcher

PoC-Worker, der die MeteoSchweiz-Analyse **KENDA-CH1** stündlich aus der STAC-API
(`ch.meteoschweiz.ogd-analysis-kenda-ch1`) zieht, die schadenrelevanten Parameter
auf die Schweiz-Bbox schneidet und als JSON-Files auf `widget.wetteralarm.ch`
hochlädt. Eine statische Karte (`frontend/`) visualisiert die Daten.

## Warum ein eigenes Repo

KENDA-CH1 wird als **GRIB2** publiziert. Infomaniak Shared Hosting hat weder
`eccodes` noch `cfgrib`. Wie bei `wetteralarm-hail-fetcher` (HDF5/Hagel) machen
wir die Konversion in GitHub Actions und liefern dem Schaden-Tool fertige JSON.

## Wie es läuft

1. GitHub Actions triggert stündlich (`15 * * * *`)
2. Python-Skript queryt STAC, filtert auf 4 Parameter × `ctrl`-Member × `lead=0`
3. Lädt GRIB2-Files, parst via `cfgrib` + `xarray`
4. Schneidet auf Schweiz-Bbox `[5.8, 45.7, 10.6, 47.9]`
5. Schreibt pro Stunde × Layer eine JSON-Datei + eine `index.json`
6. SFTP-Upload nach `widget.wetteralarm.ch:/web-scripts/kenda-poc/data/`

## Parameter

| Code | Anzeige | Einheit | Heatmap-Skala |
|---|---|---|---|
| `vmax_10m` | Wind-Böenspitze 10 m | m/s | 0 → 15 → 35 |
| `tot_prec` | Total Niederschlag (1 h) | mm | 0 → 5 → 30 |
| `cape_ml` | CAPE (Konvektion) | J/kg | 0 → 1000 → 2500 |
| `dbz_cmax` | Max Radar-Reflektivität | dBZ | 0 → 35 → 55 |

Die 4 Layer decken Sturm-, Hagel- und Stark-Regen-Bewertung ab. Schneehöhe und
Temperatur sind im Skript schnell ergänzbar (siehe `RELEVANT_PARAMS`).

## Setup

### GitHub Secrets

Settings → Secrets and variables → Actions:

- `SFTP_HOST` — z.B. `widget.wetteralarm.ch`
- `SFTP_USER` — SFTP-Login auf Infomaniak
- `SFTP_PASSWORD` — SFTP-Passwort
- `SFTP_REMOTE_DIR` — z.B. `/web-scripts/kenda-poc/data`

### Frontend hochladen (einmalig)

Die statische Karte liegt unter `frontend/`. Einmalig per SFTP nach
`widget.wetteralarm.ch:/web-scripts/kenda-poc/` hochladen:

```
frontend/index.html   → /web-scripts/kenda-poc/index.html
frontend/kenda.css    → /web-scripts/kenda-poc/kenda.css
frontend/kenda.js     → /web-scripts/kenda-poc/kenda.js
```

Aufrufbar unter `https://widget.wetteralarm.ch/web-scripts/kenda-poc/`.

### Lokaler Testlauf

```powershell
pip install -r requirements.txt
$env:SFTP_HOST = "widget.wetteralarm.ch"
$env:SFTP_USER = "…"
$env:SFTP_PASSWORD = "…"
$env:SFTP_REMOTE_DIR = "/web-scripts/kenda-poc/data"
$env:LOOKBACK_HOURS = "6"
python fetch_kenda.py
```

## Speicher

- Pro Stunde × 4 Layer = ~4 JSON-Files à ~200 KB → **~1 MB/Stunde**
- Pro Tag (24 h) = **~24 MB**
- Bei rolling 24 h Archiv auf dem Server: **stets ~24 MB Footprint**, alte Files
  werden bei jedem Lauf überschrieben

Wenn das Archiv permanent wachsen soll (für später Schaden-Backfill), brauchen
wir eine Cleanup-Logik — fehlt aktuell bewusst (PoC-Scope).

## Caveat: KENDA Rolling-24h

Die STAC-Collection enthält **nur die letzten 24 Stunden**. Daten älter als 24 h
sind weg. Für eine Langzeit-Archivierung läuft der Cron deshalb stündlich und
schreibt neue Stunden in den Archiv-Ordner. Damit baut sich die Historie
langsam selbst auf.

## Lizenz

MIT für den Code dieses Repos. Die Daten selbst stehen unter CC-BY MeteoSchweiz
— die Attribution `© MeteoSchweiz (CC-BY)` ist im Frontend-Footer hardcoded.
