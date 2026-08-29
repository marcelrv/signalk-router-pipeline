# Spec: USACE Inland ENC (IENC) Integration

Status: Draft — analysis only, no code changes
Authors: pipeline analysis session 2026-08-27
Scope: data acquisition, `enc_preprocessor.py`, `nautical_routing_pipeline.py`, build orchestration, catalog

## 1. Deficiency

The pipeline advertises “US inland rivers → Inland ENC, USACE (`ienccloud.us`)” (`README:160`) but **no US inland build uses it**.

Current `build_region.sh` + `scripts/download_noaa.py` only fetches **NOAA Office of Coast Survey coastal ENC** (S-57 `.000` per-state ZIPs at `charts.noaa.gov/ENCs/{STATE}_ENCs.zip`, bands 3–6 after overview skip). USACE Inland ENC is a **separate distribution** at `https://ienccloud.us` (IENC, Inland ECDIS standard, harmonized with European IENC at `eurisportal.eu`). It carries the inland-specific object catalogue that makes European inland routing work:

| Object | NL RWS IENC (every cell) | US NOAA coastal (sample 30 cells) | USACE IENC (expected) |
|---|---|---|---|
| `wtwaxs` (Waterway Axis, line) | 9/cell | 0/30 | Present — primary inland centerline |
| `RECTRC` (Recommended Track) | present (`NAVLNE` 1/cell) | 6/30 sparse | Present |
| `NAVLNE` (Navigation Line) | present | 6/30 sparse | Present |
| `FAIRWY` | 12/cell | sparse | Present but secondary |
| `DRGARE` | absent | dominant channel | May be absent inland (river polygon is `wtwaxs`/`comare` instead) |
| `comare` / `berths` / `hrbare` etc | present | absent/rare | Present |

Because the east-coast, Gulf, Great Lakes builds all read only NOAA, the `inland_waterways_lines.geojson` layer (`RECTRC+NAVLNE+WTWAXS` merged in `enc_preprocessor.py:58-60`) is **near-empty for US**: PR 61 features (from 1–2 `RECTRC` lines), VA/FL similar. Inland US rivers (Mississippi, Ohio, Hudson–Champlain) have **no centerline network at all** in current DBs, while the Dutch inland network (which the architecture was validated against) is dense via `wtwaxs`.

Impact:
- `build_network()` inland branch (`_build_inland_network`) emits almost no `edge_type=inland` edges for US.
- `_inject_waterway_crossings` / `_connect_waterway_crossing` (navmesh ↔ inland cross-type connectors, `WATERWAY_CROSSING_*`) has nothing to connect — inland nodes never sit inside `coastal_water` polygons.
- `_has_regulatory_structure` falls back to `FAIRWY`/`DRGARE` only, never to `wtwaxs` — loss of “regulated channel” signal inland.
- Long-distance inland routes (e.g. Chicago→New Orleans via Mississippi) are unroutable; coastal-to-inland transitions (e.g. Cape Fear River) degrade to skeleton navmesh without lane guidance.

## 2. USACE IENC Source Characterization

- **Authority:** US Army Corps of Engineers, via `ienccloud.us`. Public domain, update cycle weekly/biweekly. Organized by river system (e.g. Upper Mississippi, Ohio, GIWW), not by NOAA state ZIPs. Distribution is S-57 `.000` cells with Inland ENC object catalogue (`wtwaxs`, `comare`, `berths`, `wtnare`, `bridges` with `verclr`, etc.), analogous to RWS IENC.
- **Coordinate/Catalogue:** Same S-57 `.000` container, readable by GDAL `S57` driver with same `OGR_S57_OPTIONS`. Inland-specific layers: `wtwaxs` (line, waterway axis, the inland equivalent of `DRGARE`/`FAIRWY` for topology), `comare` (communication area), `berths`, `hrbare`, `notmrk`, `wtnare` etc. `LNDARE`/`DEPARE` semantics differ slightly (river depth may be charted via `depare`/`SOUNDG` but often via `wtwaxs` attribution).
- **Coverage confirmation:** European inland (`eurisportal.eu`) and NL RWS are the reference; USACE mirrors the standard. No USACE `.000` is currently cached. Verified by a recursive inventory rather than a name glob (a `*ienc*` glob proves nothing, since USACE cells are not named for the scheme): all **2,874** `.000` files under `data/raw/` carry NOAA `US<band><state>` cell names — 553 `USFL`, 439 `USNY`, 295 `USNC`, and so on — with **zero** non-`US[1-6]*` cells.
- **License/attribution:** Federal public domain, same as NOAA — no share-alike encumbrance, compatible with `signalk-router-data` `LICENSE-DATA.md`. Adds a `data_sources` row (`source_type='ienc'`, `contributor='USACE'`).

## 3. Design

### 3.1 Acquisition

Add `scripts/download_usace_ienc.py` parallel to `download_noaa.py`:

- Source: `https://ienccloud.us` (API/portal). USACE publishes per-waterway ZIPs / chart catalog. Implementation to enumerate waterways (Upper Mississippi, Lower Mississippi, Missouri, Ohio, GIWW, etc.) as region keys, analogous to `REGIONS` in `download_noaa.py`.
- Output layout: `data/raw/usace-ienc/{waterway}/ENC_ROOT/.../*.000`, mirroring NOAA `data/raw/{region}/{STATE}/ENC_ROOT/...` so `enc_preprocessor.py` can ingest recursively via `**/*.000` without structural change.
- CLI: `--waterway usace-upper-mississippi`, `--region all`, `--list-waterways`, `--output-dir data/raw`, `--force`.
- Manifest: `manifest.json` per waterway, same shape as NOAA.

### 3.2 Preprocessing

- **No `enc_preprocessor.py` code change needed for layer mapping** — it already handles `WTWAXS` (`inland_waterways_lines.geojson`). The deficiency is upstream (no files to read), not in mapping. However, extend mapping for inland-specific completeness:
  ```python
  'WTWAXS': 'inland_waterways_lines.geojson',  # existing
  'RECTRC': 'inland_waterways_lines.geojson',  # existing
  'NAVLNE': 'inland_waterways_lines.geojson',  # existing
  # Optional additions for quality/topology:
  'COMARE': 'inland_comare_polygons.geojson',
  'berths': 'berths_polygons.geojson',
  ```
  Keep core `inland_waterways_lines.geojson` as the routable centerline layer.
- **Deduplication:** Same as coastal — deterministic node IDs (`_coord_to_id`) merge coincident `wtwaxs` vertices across NOAA/USACE overlap zones (e.g. estuary where both publish).
- **Tagging:** Retain `SORIND`/`SORDAT` from USACE cells to distinguish inland vs coastal provenance in `inland_waterways_lines.geojson` properties.

### 3.3 Build Orchestration (`build_region.sh`)

- New mode `--source usace` vs `--source noaa` vs `--source combined`.
- Combined coastal+inland build: compose `RAW_DIR` from both `data/raw/{noaa_region}` and `data/raw/usace-ienc/{waterway}` via symlinks (same pattern as existing `--states` sub-region mode). Example:
  ```bash
  ./build_region.sh us-mississippi --usace-waterways UMR,LMR,OHIO --coastal-states LA,MS --source combined
  ```
- Clip/overlap handling: reuse `clip_pilot_data.py --clip-bbox` + `--overlap-deg` to carve USACE river polygons to the desired catalog region bbox; seam stitching (`--stitch-registry`) already supports cross-DB seams between coastal and inland tiles.

### 3.4 Pipeline Consumption (`nautical_routing_pipeline.py`)

- **No code change for basic inland network** — `_build_inland_network` already reads `inland_waterways_lines.geojson` and emits inland edges. With USACE data present, it will naturally populate.
- **Cross-type connectors:** `_inject_waterway_crossings` and Pass 0d (`_stitch_component_pieces` inland↔coastal local adjacency) remain the mechanism by which inland `wtwaxs` lines stitched through estuarine `coastal_water` polygons become routable to the coastal navmesh. With data, these passes will activate (currently no-ops for US).
- **Data sources provenance:** Extend `_default_data_sources()` to include an `inland_waterways` row with `source_type='ienc'` already present — just ensure USACE builds set `contributor='USACE'` and `url='https://ienccloud.us'` instead of RWS.
- **Schema:** No migration. Inland edges already `edge_type_id=1`, `poi_type waterway=4`.

### 3.5 Catalog (`signalk-router-data`)

New region keys: `us-inland-mississippi`, `us-inland-giww`, `us-great-lakes-inland`, etc., with `tags ["usace","ienc","inland"]` distinct from `["noaa","enc","coastal"]`.

## 4. Phased Rollout

- **Phase A — Downloader + single waterway proof:** Implement `download_usace_ienc.py` for one waterway (Upper Mississippi), run `enc_preprocessor.py` → verify `inland_waterways_lines.geojson` feature count comparable to NL density (hundreds, not tens).
- **Phase B — Combined build:** Build `us-inland-test` (USACE only) then `us-gulf-combined` (USACE GIWW + NOAA LA/MS coastal) with `--stitch-registry`; verify inland↔coastal connector stats (`waterway_crossing_stats` >0, Pass 0d edges >0) and no `_sanity_check_no_land_crossings` regressions.
- **Phase C — Catalog publication:** Publish US inland regions to `signalk-router-data` with `index.json` entries and `coverage-map.png` regeneration.

## 5. Risks

- USACE distribution format may differ slightly (ZIP layout, cell naming not `US*` but `1R*`-like); downloader must handle both.
- River `wtwaxs` lines may be more schematically digitized than coastal `DEPARE` — width profile from medial axis may be less accurate; keep `ClassificationConfig` depth checks authoritative.
- Overlap duplication where NOAA and USACE both publish the same estuary — deterministic ID dedupe handles, but `coastal_water` vs `wtwaxs` containment must be validated (inland node inside coastal polygon test in `_ensure_coastal_connectivity`).
