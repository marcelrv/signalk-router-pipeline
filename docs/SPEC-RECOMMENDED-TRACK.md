# Spec Supplement: Recommended Track (RECTRC / NAVLNE) — Coastal vs Inland

Status: Draft — analysis only, no code changes
Complements: `SPEC-FAIRWAY-HARMONIZATION.md`, `SPEC-USACE-IENC.md`
Scope: `enc_preprocessor.py`, `nautical_routing_pipeline.py`

## 1. What exists in the charts

S-57 object **RECTRC (Recommended Track, OBJL 109, LineString)** is *present* in NOAA ENC for the US, but sparse compared to `DRGARE`/`FAIRWY`. Sampling ~200 US east-coast cells (bands 3–6):

- `DRGARE` 697 features, `FAIRWY` 207, **`RECTRC` 75**, `NAVLNE` 35
- NL RWS IENC: `wtwaxs` 3689 features in the same `inland_waterways_lines.geojson` (PR US only 61) — RECTRC is *not* the US analogue of `wtwaxs`.

Per-cell check:
- `US4NY1JH` (Lake Ontario, band 4): 8 `RECTRC` + 1 `FAIRWY` — offshore passage tracks (e.g. *“Thirty Mile Point to Oswego (089°)”*, `CATTRK=2`, `TRAFIC=2/4`, `ORIENT 89°`).
- `US5NYCEG` (NY harbor, band 5): 2 `RECTRC` (`CATTRK=1`, `TRAFIC=3`) + 6 `FAIRWY` + 8 `DRGARE`.
- Many band-6 harbour cells: 0 `RECTRC`.

**NAVLNE (Navigation Line, OBJL 85, LineString)** — range/leading line (e.g. `CATNAV=3`). Even sparser, same band/usage.

Attributes (S-57 Cat.):
- `CATTRK`: `1` = based on a system of fixed marks (buoyed/channel centerline — harbour approach), `2` = not based on fixed marks (offshore open-water recommended track).
- `DRVAL1/DRVAL2`: **always null** in US sample (no depth). `ORIENT`, `TRAFIC` (1=inbound,2=outbound,3=one-way,4=two-way), `INFORM` free text (e.g. *“FROM 0.7 NM off Oswego Outer Pier … TO …”*).
- Contrast `DRGARE`: always has `DRVAL1` (maintained depth 1.5–12 m). Contrast `WTWAXS`: Inland waterway axis, no `CATTRK`, carries `catccl`/`OBJNAM` like *“Hollandsche IJssel”*.

## 2. How the pipeline currently handles it

`enc_preprocessor.py:58-60`:
```python
'RECTRC': 'inland_waterways_lines.geojson',
'NAVLNE': 'inland_waterways_lines.geojson',
'WTWAXS': 'inland_waterways_lines.geojson',
```
All three are merged into **one** inland centerline layer. `nautical_routing_pipeline.py:_build_inland_network` then emits them as `edge_type=inland` edges, stitched to coastal navmesh only where they physically intersect a `coastal_water` polygon via `_inject_waterway_crossings` + Pass 0d (`inland_idx` local connectors, `INLAND_LOCAL_RADIUS_M=300m`).

Consequences:
- Harbour-approach `RECTRC CATTRK=1` (e.g. East River, Newtown Creek) — which is functionally the *centerline of the DRGARE/FAIRWY polygon* — ends up as an inland edge, not a coastal fairway lane. It is still routable, but only via the 300 m inland↔coastal connector, not via the coastal `cost_factor` preference or `FAIRWY` bridge-crossing logic. If its endpoint falls just outside the `coastal_water` polygon (digitization gap), it can be disconnected.
- Offshore `RECTRC CATTRK=2` (Lake Ontario 5–10 NM open-water tracks) — intended as a *passage-planning* suggestion across open navmesh, not a narrow channel. As an inland edge, it becomes a long straight line floating inside a navmesh region, stitched only at its endpoints where it crosses the region boundary. This works accidentally, but its routing cost is inland-default (no `cost_factor` harmonization) and it is invisible to the `fairways_unified` preference.

## 3. Is RECTRC safe? Depth?

**No depth association — do not use as depth constraint.** `DRVAL1` is null by design; the track assumes the surrounding `DEPARE` water is deep enough. Safety comes from the *enclosing* `DEPARE`/`DRGARE` depth, not the line itself. Treat RECTRC as **soft preference only** (`cost_factor=0.8` where it exists), same as `FAIRWY`, never as `min_depth` override. This is the same stance as the fairway harmonization spec: `DRGARE` is the depth authority, `FAIRWY`/`RECTRC`/`NAVLNE` are topological preference.

`TRAFIC`/`ORIENT` can refine `traffic_mode` (one-way) exactly as `FAIRWY.TRAFIC` does in `_edge_attr_worker`.

## 4. Proposed handling (no behaviour change for inland `WTWAXS`)

Keep `WTWAXS` (true inland axis, USACE IENC) in `inland_waterways_lines.geojson` — no change per `SPEC-USACE-IENC.md`.

For NOAA coastal `RECTRC`/`NAVLNE`, split at read time by `CATTRK`:

- **Option A — minimal change (recommended for this spec):** Keep current merge, but document the distinction and add `CATTRK` to piped GeoJSON properties. No code change now; revisit if offshore track disconnection is observed in Great Lakes builds.
- **Option B — faithful harmonization:** In `enc_preprocessor.py`, emit `RECTRC` to *both* layers — retain in `inland_waterways_lines.geojson` for backward compat, and additionally merge `CATTRK=1` tracks into `fairways_unified_polygons` (as buffered centerlines or as line-preference for `_edge_attr_worker` line-intersection cost). Implement as secondary line-intersection check in `_edge_attr_worker` alongside the polygon check: `if gdf_line.intersects(edge_geom): cost_factor=0.8`. This elevates harbour-approach recommended tracks to the same preference tier as `DRGARE`/`FAIRWY` without misclassifying them as inland rivers.
  - Offshore `CATTRK=2` tracks remain inland-like (long open-water connectors) — do *not* promote to fairway, keep line-cost only if desired.

Choice: **Ship Option A now, gate Option B on a Great Lakes/estuary re-build probe** (rebuild one Lake Ontario region and one NY harbor region, measure `cost_factor` coverage and connector stats with vs without the extra line check). The current 75-feature count is too low to justify complexity unless the probe shows disconnected approaches.

## 5. Verification

- Preprocessor on `US4NY1JH` + `US5NYCEG`: `inland_waterways_lines.geojson` gains ~75+35 line features with `CATTRK/TRAFIC/ORIENT` preserved; `fairways_unified` unchanged in Option A.
- Build NY + Lake Ontario: `waterway_crossing_stats` and Pass 0d counters for RECTRC-bearing builds should be >0 where tracks cross navmesh boundaries; no new `crosses_land` violations.
- Route probe: harbour approach via `RECTRC CATTRK=1` should already be routable through existing inland↔coastal connector; offshore `CATTRK=2` track is optional preference, not safety-critical.
