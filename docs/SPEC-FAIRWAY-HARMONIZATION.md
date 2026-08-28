# Spec: Fairway Harmonization — FAIRWY + DRGARE as Unified Main-Waterway Signal

Status: Draft — analysis only, no code changes
Authors: pipeline analysis session 2026-08-27
Scope: `enc_preprocessor.py`, `nautical_routing_pipeline.py`, `add_pois_to_db.py`, DB format / data_sources

## 1. Problem

Dutch RWS IENC routes correctly prefer marked main waterways; US NOAA routes appear to lack that preference. Root-cause analysis (2026-08-27) shows the signal is **not missing from NOAA** — it is encoded under a different S-57 object that the pipeline never ingests.

| Aspect | NL RWS IENC (Dutch) | US NOAA ENC (coastal) |
|---|---|---|
| Fairway polygon layer | `FAIRWY` present in **every** cell (e.g. `2R70990A.000`: 12 `FAIRWY`, 9 `wtwaxs`) | `FAIRWY` sparse: Band 5 sampling 49 features in 24/60 cells (~40%), many harbour cells 0; Band 4 19 in 7 cells |
| Maintained channel polygon | `DRGARE` absent in NL sample | `DRGARE` dominant: Band 5 249 features in 22 cells — **5× FAIRWY** at harbour scale; `US5NYCUG` 36 `DRGARE` vs 0 `FAIRWY` |
| Waterway axis | `wtwaxs` every cell | `wtwaxs` 0/30 cells; `RECTRC/NAVLNE` 6/30 sparse |
| Pipeline ingestion (`enc_preprocessor.py:42`) | `FAIRWY→fairways_polygons.geojson`, `WTWAXS→inland_waterways_lines.geojson` | Same mapping → most US main-channel geometry (DRGARE) silently dropped; `fairways_polygons.geojson` contains only the minority `FAIRWY` subset |

Consequence: cost harmonization (`cost_factor 0.8` fairway / `1.2` open water, `nautical_routing_pipeline.py:234`), regulatory-structure classification (`_has_regulatory_structure` → laned/skeleton), and fairway-intersection bridge/lock crossing detection all run on an incomplete fairway layer for US. DRGARE-based channels are routed as undifferentiated open water.

Evidence:
- GDAL layer listing: `NL 2R70990A.000` layers include `FAIRWY`+`wtwaxs`, no `DRGARE`; `US5NY1FL` (harbour-scale NY) has no `FAIRWY` at all; `US4NY1BW` has 8 `FAIRWY` + 3 `DRGARE`; `US5NYCUG` has 36 `DRGARE` + 0 `FAIRWY`.
- Build logs: PR `Loaded 'fairways' with 46 features` vs 12k `DEPARE`/`coastal_water`; VA stitched `221 fairways` despite 249+ `DRGARE` features available upstream.
- Edge stats: Zeeland ~12% `cost_factor=0.8` (6880/57573), PR 1.7% (1636/93729), VA 2.9% — understated where `DRGARE` dominates.

## 2. S-57 Semantics

- **FAIRWY (Fairway, OBJL 51, polygon):** Designated fairway area, often an inbound/outbound traffic lane or recommended fairway. In US, used for regulated traffic fairways (e.g. East River Channel, Hudson River Channel — `OBJNAM` values in `US5NYCEG`); not the generic maintained channel footprint.
- **DRGARE (Dredged Area, OBJL 53, polygon):** Area dredged to a **controlled/maintained depth**. Attributes include `DRVAL1` (least depth, maintained), `QUASOU`, `SOUACC`, `TECSOU`. Observed in NOAA: `US5NYCUG` DRGARE `DRVAL1` 9.9–12.1 m (mean ~10.7 m), `US5NYCEG` 1.5–5.9 m; `US4NY1BW` 1.8–2.4 m with `QUASOU=[11]`. This is the primary US charting of “the channel” — the analogue of NL `FAIRWY`.
- **DEPARE (Depth Area, polygon):** Natural/charted depth band (`DRVAL1/DRVAL2`). Coastal water navmesh is derived from `DEPARE`+`LOKBSN`. `DRGARE` sits *inside* `DEPARE` but carries the authoritative maintained depth for that footprint.
- No overlap assumption: a centroid test on `US5NYCEG` shows most `DRGARE` centroids fall outside any single containing `DEPARE` polygon at the query point (digitization differences), but the logical containment is channel-inside-water.

## 3. Can DRGARE Be Considered Safe? Depth Association?

**Yes, with the maintained-depth caveat — and yes, it carries depth.**

- **Depth-bearing:** Every NOAA `DRGARE` inspected has `DRVAL1` populated (float, metres). It is *the* controlling depth for that dredged footprint — the depth the authority guarantees after dredging. Example: Hudson/East River DRGAREs chart 1.5–12 m maintained; these are intentional, surveyed values, not band floors.
- **Safe-to-navigate reading:** `DRGARE` is **safer than `FAIRWY` alone** for draft-constrained routing: it *is* the maintained channel. Correct usage is **depth-constrained preference**, not unconditional safe flag:
  - `min_depth` on an edge intersecting `DRGARE` should be `min(current DEPARE-derived min_depth, DRGARE.DRVAL1)` or `max`? Semantically DRGARE *overrides* DEPARE inside its polygon — the dredged depth is the governing least depth, often deeper than the surrounding `DEPARE` band but sometimes shallower where `DEPARE` is deep ocean. Conservative rule: `edge_min_depth = min(DEPARE_min_depth, DRGARE_DRVAL1)` is wrong (would shallow deep ocean). Correct: inside `DRGARE`, the navigable depth is `DRGARE.DRVAL1`; outside it, `DEPARE` applies. Intersection test should **clamp** to `DRGARE.DRVAL1` when the edge samples inside `DRGARE`.
  - Pipeline currently folds `DEPARE` via 5-point `max(DRVAL1)` among containing polygons. `DRGARE` should be a *second* depth source at higher priority where it exists — same sampling pattern, but `DRGARE` value wins inside its polygon.
- **Caveats:**
  - Siltation: maintained depth is survey-date dependent; treat as Tier 1 but mark provenance and date (`SORDAT/SORIND`).
  - `QUASOU=10` (maintained) / `11` (not maintained) and `TECSOU` qualify confidence — preserve for warning, not for discarding.
  - Where `DRVAL1` is null (rare), fall back to `DEPARE` logic.
  - Narrow `DRGARE` slivers can be noisy; same `NAVMESH_BOUNDARY_SIMPLIFY_M` / morphological handling as `DEPARE` depth splits may be needed if `DRGARE` is used for classification geometry.

## 4. Proposed Design

### 4.1 Ingestion (`enc_preprocessor.py`)

```python
layer_mapping = {
    'DRGARE': 'dredged_areas_polygons.geojson',  # NEW
    'FAIRWY': 'fairways_polygons.geojson',        # existing
    # plus merge DRGARE into a unified fairway layer for downstream simplicity:
}
# Post-merge: produce `fairways_polygons.geojson` as unary union of FAIRWY + DRGARE
# (or keep both and let downstream read either — see 4.2). Preserve source field
# `src_objl` in GeoJSON properties to distinguish origin for provenance.
```

Alternative considered and rejected: map `DRGARE` directly into `fairways_polygons.geojson` without retaining distinction — loses provenance for debugging. Preferred: emit `dredged_areas_polygons.geojson` *and* a merged `fairways_unified_polygons.geojson` (or merge at pipeline read time).

### 4.2 Pipeline consumption (`nautical_routing_pipeline.py`)

- **Data paths:** Add `dredged_areas` / `fairways_unified` to `data_paths` CLI (`--input-dir` contract). `parse_shapefiles()` loads both; if `fairways_unified` absent, synthesizes as `concat(fairways, dredged_areas)`.
- **Edge attributes (`_edge_attr_worker`):**
  - Depth: after `DEPARE` 5-point sampling, if edge intersects `dredged_areas`, sample `DRVAL1` from `dredged_areas` containing candidates (same `max(DRVAL1)` rule) and **override** `min_depth`/`drval1` *inside* the dredged footprint. Log split stats (DEPARE-only vs DRGARE-override).
  - Cost: `cost_factor = 0.8` if `intersects(fairways_unified)` (covers both FAIRWY and DRGARE). Keep `TRAFIC` one-way from `FAIRWY` where present; `DRGARE` contributes no traffic mode (remains `0`).
  - Keep `distance_to_land`, obstacle logic unchanged.
- **Classification (`classify_water_body` / `_has_regulatory_structure`):** Broaden to `fairways_unified` (any overlap → laned). This restores US channels to directional-treatment eligibility.
- **Bridge/lock crossing (`_add_opening_bridge_edges`, `_add_lock_crossing_edges`):** Already consumes fairway intersections — widen to `fairways_unified` so openings aligned to `DRGARE` channels are found.
- **Navmesh depth split (`_split_deep_shallow`):** No direct change — it keys on `DEPARE`; `DRGARE` depth override is applied later as edge attribute, not as region geometry. Consider optional future use for boundary refinement.
- **Provenance (`_default_data_sources`, `data_sources` table):** Add `dredged_areas` row (Tier 1, `source_type='enc'`), and retain `fairways` row. Edge-level `source_id` stays topology provenance (which layer's geometry produced the edge, e.g. `coastal_water`/`inland_waterways`) and is not reassigned by attribute-refining layers — `_edge_attr_worker` already applies this same rule to `DEPARE`, bridges, locks, and fairways, none of which reassign `source_id` either when they override an edge's depth/cost/width. `DRGARE`'s presence for a given edge is still fully queryable via the `dredged_areas` row's geometry (was this edge inside a `DRGARE` polygon?) without needing a redundant per-edge pointer.
- **POIs (`add_pois_to_db.py`):** No change — fairway POIs remain from `FAIRWY` features; `DRGARE` does not generate POIs.

### 4.3 Database

- No schema migration required for `edges` (reuse `cost_factor`, `min_depth`, `traffic_mode`).
- Optional: new `dredged_areas` provenance row; existing DBs remain readable (old code ignores extra layer; new code handles absent `dredged_areas.geojson`).

### 4.4 Frontend / API

No API change. `cost_factor` distribution will shift (more `0.8` edges in US); route shape may improve with no contract break. Consider exposing `src_objl` in edge debug payload if unified.

## 5. Verification Plan

- **Unit:** Preprocessor on `US5NYCUG`/`US5NYCEG` yields `dredged_areas_polygons.geojson` with `DRVAL1` preserved.
- **Integration:** Rebuild PR + one US east-coast stitched region (e.g. VA) with harmonized layer; assert:
  - `fairways_unified` feature count ≈ `FAIRWY`+`DRGARE` minus overlap.
  - Edge `cost_factor=0.8` share rises (PR expected 1.7%→~8–15%, check against `DRGARE` footprint area).
  - No `TRAFIC` regression (one-way edges still only where `FAIRWY.TRAFIC` set).
  - Depth override: sample edges fully inside a known `DRGARE` (e.g. Hudson `DRVAL1=3.9`) have `min_depth` within 0.5 m of `DRGARE.DRVAL1`, not generic `DEPARE` floor.
- **Route probes:** Lake Worth / NY Harbor A-B tests — distance delta within 5%, no new `crosses_land` violations, `SANITY_CHECK_NO_LAND_CROSSINGS` still clean.

## 6. Risks and Open Questions

- Double-counting where `FAIRWY` and `DRGARE` overlap (common at channel entrances): union handles, but attribute precedence (`DRGARE.DRVAL1` over `FAIRWY` null `DRVAL1`) must be explicit.
- `DRGARE` slivers crossing land due to digitization — same guard as fairways: `_crosses_land` check before connector creation.
- Historical builds without `DRGARE`: seamless fallback — `dredged_areas` empty → behavior identical to today.
