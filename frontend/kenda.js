// KENDA-CH1 PoC — Frontend
//
// Lädt die JSON-Layer-Files aus ./data/ und visualisiert sie als Canvas-Heatmap
// über swisstopo-Karte. Layer-Wechsel und Time-Slider triggern Neuladen.

const DATA_BASE = 'data';

const state = {
    index: null,        // index.json content
    currentHour: null,  // ISO-Datum-String
    currentLayer: null, // Parameter-Code
    cachedGrids: {},    // key: hour+param → grid object
    canvasLayer: null,
};

// ─────────────────────────────────────────────────────────────────────────────
// Karte init
// ─────────────────────────────────────────────────────────────────────────────
const map = L.map('map', {
    center: [46.8, 8.2],
    zoom: 8,
    minZoom: 7,
    maxZoom: 12,
});

// swisstopo Pixelkarte als Hintergrund (analog Schaden-Tool)
L.tileLayer(
    'https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg',
    {
        attribution: '© swisstopo · KENDA-CH1 © MeteoSchweiz (CC-BY)',
        maxZoom: 18,
    }
).addTo(map);

// Canvas-Layer-Subclass für die Gitterdaten — performant für 100k+ Pixel
const GridCanvasLayer = L.Layer.extend({
    onAdd(map) {
        this._map = map;
        this._canvas = L.DomUtil.create('canvas', 'kenda-canvas');
        map.getPanes().overlayPane.appendChild(this._canvas);
        map.on('viewreset', this._reset, this);
        map.on('zoom', this._reset, this);
        map.on('move', this._reset, this);
        this._reset();
    },
    onRemove(map) {
        L.DomUtil.remove(this._canvas);
        map.off('viewreset', this._reset, this);
        map.off('zoom', this._reset, this);
        map.off('move', this._reset, this);
    },
    setData(grid) {
        this._grid = grid;
        this._reset();
    },
    _reset() {
        if (!this._grid) return;
        const size = this._map.getSize();
        const topLeft = this._map.containerPointToLayerPoint([0, 0]);
        L.DomUtil.setPosition(this._canvas, topLeft);
        this._canvas.width = size.x;
        this._canvas.height = size.y;
        this._render();
    },
    _render() {
        const g = this._grid;
        const ctx = this._canvas.getContext('2d');
        ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);

        const [rows, cols] = g.shape;
        const dLat = (g.lat_max - g.lat_min) / (rows - 1);
        const dLng = (g.lng_max - g.lng_min) / (cols - 1);

        // Pixel-Render Reihe für Reihe — für 220×460 absolut performant
        const imageData = ctx.createImageData(this._canvas.width, this._canvas.height);
        const data = imageData.data;
        const w = this._canvas.width;
        const h = this._canvas.height;

        // Inverse Lookup: für jeden Bildschirm-Pixel → lat/lng → Grid-Index
        for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
                const containerPt = L.point(x, y);
                const latlng = this._map.containerPointToLatLng(containerPt);
                // Achtung: lat ist invertiert (oben = max lat)
                const row = Math.round((g.lat_max - latlng.lat) / dLat);
                const col = Math.round((latlng.lng - g.lng_min) / dLng);
                if (row < 0 || row >= rows || col < 0 || col >= cols) continue;
                const v = g.values[row][col];
                if (v === null || v === undefined) continue;

                const rgba = this._colorFor(v, g.scale);
                const idx = (y * w + x) * 4;
                data[idx]     = rgba[0];
                data[idx + 1] = rgba[1];
                data[idx + 2] = rgba[2];
                data[idx + 3] = rgba[3];
            }
        }
        ctx.putImageData(imageData, 0, 0);
    },
    _colorFor(v, scale) {
        // scale = [low, mid, high] → green→yellow→red, mit Alpha-Mapping
        const [lo, mid, hi] = scale;
        if (v <= lo) return [0, 0, 0, 0];
        const t = Math.min(1, (v - lo) / (hi - lo));
        let r, g, b;
        if (v < mid) {
            const u = (v - lo) / (mid - lo);
            r = Math.round(0   + u * 255);
            g = Math.round(160 + u * 40);
            b = 0;
        } else {
            const u = Math.min(1, (v - mid) / (hi - mid));
            r = 255;
            g = Math.round(200 - u * 200);
            b = 0;
        }
        const alpha = Math.round(80 + t * 160);
        return [r, g, b, alpha];
    },
});

state.canvasLayer = new GridCanvasLayer();
state.canvasLayer.addTo(map);

// ─────────────────────────────────────────────────────────────────────────────
// Init: index.json laden und Controls aufbauen
// ─────────────────────────────────────────────────────────────────────────────
async function init() {
    try {
        const r = await fetch(`${DATA_BASE}/index.json?ts=${Date.now()}`);
        if (!r.ok) throw new Error('index.json fehlt');
        state.index = await r.json();
    } catch (e) {
        document.getElementById('status').textContent = 'Fehler: ' + e.message;
        return;
    }

    // Layer-Dropdown
    const layerSelect = document.getElementById('layer-select');
    Object.entries(state.index.params).forEach(([code, meta]) => {
        const opt = document.createElement('option');
        opt.value = code;
        opt.textContent = `${meta.label} (${meta.unit})`;
        layerSelect.appendChild(opt);
    });
    state.currentLayer = layerSelect.value;
    layerSelect.addEventListener('change', () => {
        state.currentLayer = layerSelect.value;
        loadAndRender();
        updateLegend();
    });

    // Hour-Slider
    const slider = document.getElementById('hour-slider');
    slider.max = String(state.index.hours.length - 1);
    slider.value = String(state.index.hours.length - 1); // neueste Stunde default
    state.currentHour = state.index.hours[state.index.hours.length - 1];
    slider.addEventListener('input', () => {
        state.currentHour = state.index.hours[parseInt(slider.value, 10)];
        document.getElementById('hour-label').textContent = formatHour(state.currentHour);
        loadAndRender();
    });
    document.getElementById('hour-label').textContent = formatHour(state.currentHour);

    // Karten-Klick → Popup mit allen 4 Werten
    map.on('click', onMapClick);

    updateLegend();
    loadAndRender();
    document.getElementById('status').textContent =
        `${state.index.hours.length} Stunden verfügbar · Letztes Update ${formatHour(state.index.generated_at)}`;
}

function formatHour(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleString('de-CH', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    }) + ' UTC';
}

function updateLegend() {
    const meta = state.index.params[state.currentLayer];
    document.getElementById('scale-min').textContent = `${meta.scale[0]} ${meta.unit}`;
    document.getElementById('scale-max').textContent = `${meta.scale[2]} ${meta.unit}`;
}

async function loadAndRender() {
    const grid = await loadGrid(state.currentHour, state.currentLayer);
    if (!grid) {
        document.getElementById('status').textContent =
            `Layer ${state.currentLayer} für ${formatHour(state.currentHour)} nicht verfügbar`;
        return;
    }
    state.canvasLayer.setData(grid);
}

async function loadGrid(hour, param) {
    const key = `${hour}_${param}`;
    if (state.cachedGrids[key]) return state.cachedGrids[key];

    const ts = hour.replace(/[-:]/g, '').replace('T', 'T').slice(0, 13); // YYYYMMDDTHHMM
    // index hour ist 'YYYY-MM-DDTHH:MM:SSZ' → 'YYYYMMDDTHHMM'
    const compact = hour.replace(/-/g, '').replace(/:/g, '').slice(0, 13);
    const url = `${DATA_BASE}/${compact}_${param}.json?ts=${Date.now()}`;

    try {
        const r = await fetch(url);
        if (!r.ok) return null;
        const grid = await r.json();
        state.cachedGrids[key] = grid;
        return grid;
    } catch (e) {
        return null;
    }
}

async function onMapClick(ev) {
    const { lat, lng } = ev.latlng;
    const params = Object.keys(state.index.params);
    const values = {};
    for (const p of params) {
        const grid = await loadGrid(state.currentHour, p);
        if (!grid) { values[p] = null; continue; }
        const [rows, cols] = grid.shape;
        const dLat = (grid.lat_max - grid.lat_min) / (rows - 1);
        const dLng = (grid.lng_max - grid.lng_min) / (cols - 1);
        const row = Math.round((grid.lat_max - lat) / dLat);
        const col = Math.round((lng - grid.lng_min) / dLng);
        if (row < 0 || row >= rows || col < 0 || col >= cols) {
            values[p] = null;
        } else {
            values[p] = grid.values[row][col];
        }
    }

    const html = renderPopup(lat, lng, state.currentHour, values);
    L.popup({ maxWidth: 320 })
        .setLatLng(ev.latlng)
        .setContent(html)
        .openOn(map);
}

function renderPopup(lat, lng, hour, values) {
    const rows = Object.entries(state.index.params).map(([code, meta]) => {
        const v = values[code];
        const display = v === null || v === undefined ? '—' : `${v} ${meta.unit}`;
        return `<tr><td>${meta.label}</td><td class="val">${display}</td></tr>`;
    }).join('');
    return `
        <div class="popup-box">
            <div class="coord">${lat.toFixed(4)} / ${lng.toFixed(4)} · ${formatHour(hour)}</div>
            <table>${rows}</table>
        </div>
    `;
}

init();
