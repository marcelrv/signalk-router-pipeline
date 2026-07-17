# Phase 3+ Design — Data Fusion, Community Overrides, Vessel-Traffic Validation, Scale-Out, Hierarchical Routing

## Scope and how this document relates to the others

This is the **forward design** document for everything after Phase 2
(navmesh/funnel-algorithm routing). It assumes Phase 0-2 and Phase 2
Hardening are working — **do not re-verify them here**; that history and
any still-open hardening work lives in `NEXT_PHASES.md`, which stays the
tactical bug-tracking log. `README.md` stays the short, stable
architecture overview for a new reader. This document is where "Status &
roadmap" and "Phase 3 and beyond (pointer only)" in those two files
should point for actual design detail — update both pointers to reference
this file once this lands.

Six sub-phases, ordered by dependency, not necessarily by calendar:

| | Sub-phase | Depends on | Parallelizable with |
|---|---|---|---|
| 3a | OSM/OpenSeaMap tier-3 fusion | Phase 1 schema | 3b, 3c |
| 3b | Bathymetry gap-fill (GEBCO/EMODnet, tier 4) | Phase 1 schema | 3a, 3c |
| 3c | Community override workflow | Phase 1 schema (`override_provenance` already exists) | 3a, 3b |
| 3d | AIS/vessel-density validation + tier-6 gap-filling | 3c's anomaly queue (natural feed target) | — |
| 3e | Scale-out: full NL, then a first US region | 3a-3d validated on the Zeeland pilot first | — |
| 3f | Supernode/macro-edge hierarchical routing | 3e (no value hierarchizing one small region) | — |

---

## 3a. OSM/OpenSeaMap tier-3 data fusion

**Purpose**: fill coverage gaps where no ENC/IENC (tier 1/2) data exists —
small harbors, minor canals, features official charts don't bother with —
using OpenStreetMap's waterway/seamark tagging, which the sailing
community (via the OpenSeaMap sub-project) already maintains at real
density in many such gaps.

**Sourcing**: use **Geofabrik regional `.osm.pbf` extracts**, downloaded
and cached locally, filtered with `osmium`/`pyosmium` — not live Overpass
API queries. Overpass has rate limits and isn't reproducible (the same
query can return different results as OSM changes under you); a pinned,
dated `.osm.pbf` extract is a stable, versioned input exactly like the
existing ENC/IENC downloads, and its date becomes the `data_sources.accessed_date`
value.

**Tags to extract** (three separate layers, mirroring the existing
layer-per-concern convention):
- `natural=water` / `waterway=*` polygons and lines → candidate fill for
  `coastal_water_polygons`/`inland_waterways_lines` gaps.
- `seamark:type=harbour|marina|...` → candidate additions to `pois_points`.
- `seamark:type=buoy_*` / `seamark:type=beacon_*` (lateral, cardinal,
  special-purpose) → a **new** `seamarks_points` layer, not previously
  ingested at all. This is the concrete implementation of the buoy-based
  node placement idea from the Phase 2 Hardening Round 5 investigation
  (see `NEXT_PHASES.md` §5.5) — see below.

**Conflation strategy — additive to gaps, never overrides tier 1/2**:
1. Compute the union of all tier-1/2 (ENC/IENC-sourced) `coastal_water`/
   `inland_waterways` coverage for the region.
2. Clip the OSM water layer to areas *outside* that union (a straightforward
   `difference()`).
3. Only the clipped remainder enters the pipeline as new input polygons/
   lines, tagged `source_tier=3` end to end (through `_get_or_create_node`/
   edge attribute stamping, using the existing `source_tier`/`source_id`
   machinery from Phase 1 — no new plumbing needed there, just a new
   `data_sources` row with `source_type='osm'`, `license='ODbL-1.0'`,
   `attribution_text='© OpenStreetMap contributors'`).
4. Where OSM data overlaps existing tier-1/2 coverage, it's simply
   discarded for polygon/line purposes — tier 1/2 always wins on
   overlap, no merge logic needed there.

**Buoy/beacon-informed node placement** (the real payoff of ingesting
seamarks, and the fix for last round's "way too many edges, follow
buoyage instead of raw coastline" observation): in `classify_water_body`/
`build_skeleton_network`, where a channel has seamark points along it,
**snap the medial-axis skeleton's sampled vertices to nearby buoy
positions** (within some tolerance, e.g. 1.5x the local channel
half-width) instead of the raster-derived point, before the existing
`_resample_long_skeleton_edges`/`_prune_skeleton_spurs` steps run. A real
lateral buoy pair also lets `_extract_buoyage_direction` (currently a
stub reading only S-57 `TRAFIC`, per Phase 1's plan) resolve genuine
IALA side/colour instead of guessing, making `add_lane_edges` actually
buoyage-aware for OSM-covered channels, not just where an ENC/IENC
fairway polygon happens to carry a one-way flag.

**Concrete tasks, in order**:
1. `download_osm_extract.py` — fetch/cache a dated Geofabrik `.osm.pbf`
   for the target region, `osmium tags-filter` down to the three tag sets
   above, export to GeoJSON matching the existing layer schema.
2. `_fuse_osm_gaps(coastal_gdf, inland_gdf, osm_water_gdf) -> (gdf, gdf)`
   — the difference/clip step above, called from `parse_shapefiles` before
   `build_network` runs, so gap-filled polygons flow through classification
   exactly like tier-1/2 input.
3. `_seamark_snap_targets(seamarks_gdf) -> KDTree` — built once per region,
   queried from `_skeleton_raster_to_graph`'s vertex-placement step.
4. Extend `_extract_buoyage_direction` to use real lateral buoy
   pairs when available, S-57 `TRAFIC` as the existing fallback.
5. New `data_sources` rows, `osm-fused` catalog tag (already defined in
   the format spec's tag taxonomy, unused until now).

---

## 3b. Bathymetry gap-fill (GEBCO / EMODnet Bathymetry, tier 4)

**Purpose**: where no DEPARE/sounding polygon exists at all (open-ocean
gaps, or a country with thin charted sounding coverage), fall back to a
continuous bathymetric surface instead of leaving depth unknown.

**Sourcing**: **GEBCO** global grid (NetCDF, ~450m/15 arc-second, public
domain) as the universal fallback; **EMODnet Bathymetry** DTM (~115m,
free, higher resolution) preferred for European regions when available.
Both are downloaded once as static grid files (not queried per-request),
cached locally like the OSM extracts.

**Integration point**: `_has_navigable_depth` and the edge-attribute
depth fast-path in `_edge_attr_worker` currently only ever look at DEPARE
polygon candidates; if none intersect, behavior is currently "unknown,
treat conservatively." Add a fallback tier: rasterize the cached GEBCO/
EMODnet grid once per region (`rasterio`, already a dependency), and
where DEPARE candidates are empty for a given edge/polygon, sample the
raster instead (zonal minimum along the edge geometry, same "physical
distance spacing, not point count" approach as the Phase 1 background
notes recommend for tier-1 depth sampling too — worth unifying both paths
through one `_sample_depth_raster(geom, raster)` helper rather than two
separate implementations).

**Tier-4 handling, per the format spec (already normative, just needs
real data flowing through it)**: tag `source_tier=4`, `source_id` pointing
at the GEBCO/EMODnet `data_sources` row, and apply the extra safety
margin already specified (`routing-database-format-specification.md` §5)
on the consumer side — no pipeline-side margin inflation needed, the
tier tag alone is enough for the runtime to do the right thing. Surface
GEBCO's own terms-of-use disclaimer (already in `LICENSE-DATA.md`: *"shall
not be used for navigation or for any other purpose involving safety at
sea"*) as a genuine reason this tier is a fallback, not a first resort —
the design should make tier-4 usage rare (only where tier 1/2 genuinely
has nothing), not a routine substitute.

**Concrete tasks**:
1. `bathymetry_raster.py` — download/cache GEBCO + (region-conditional)
   EMODnet grids, a `_sample_depth_raster(geom, raster_path)` helper.
2. Wire into `_has_navigable_depth` (classification-time) and
   `_edge_attr_worker` (attribute-time) as the fallback when DEPARE
   candidates are empty — not as a replacement for the existing
   ≥depth-ceiling fast-path, which stays DEPARE-only (tier 1/2) since it
   gates the `navmesh` vs `skeleton` classification decision and shouldn't
   be influenced by a coarser, non-surveyed surface.
3. `bathymetry-filled` catalog tag (already defined, unused).

**Candidate additional/alternative source to evaluate before implementing:
Open Water Software's "Seascape" bathymetry compilation**
(`openwaters.io/charts/seascape`, checked directly — not just linked).
Worth a real look before committing to GEBCO/EMODnet alone as the only
tier-4 fallback:

- **What it is**: a pre-fused compilation of 23 open bathymetry datasets,
  including several this project already trusts elsewhere at tier 1/2 —
  NOAA (S-102, CUDEM), Rijkswaterstaat (**Dutch waters at ~20m
  resolution** — directly relevant to the Zeeland pilot, and a real step
  up from GEBCO's ~450m or even EMODnet's ~115m), INFOMAR, AusSeabed —
  plus lake bathymetry and select high-resolution coastal areas down to
  ~8m. If the Zeeland-area resolution claim holds up, this could be a
  meaningfully better tier-4 fallback than GEBCO/EMODnet alone for
  exactly the pilot region this project has been validating against all
  along.
- **Explicitly "Not for navigational use"** (their own wording — depths
  aren't chart-datum-reduced, don't account for tides, and blend sources
  of varying age/resolution). This maps exactly onto how this project
  already treats GEBCO — confirms tier-4-only, never a basis for
  promotion to a higher tier, is still the right call if this is added,
  not a reason to reconsider the tier model.
- **License nuance to resolve at implementation time, not assumed here**:
  the *compiled tiles* are CC BY 4.0 (attribution to Open Water Software),
  but Seascape's own docs state individual source datasets **retain
  their original licenses**. Since this project's own output is itself a
  redistribution point (`router-data`), confirm whether a single CC BY
  4.0 attribution to Open Water Software actually discharges this
  project's obligations for redistributing derived depth data, or
  whether the 23 underlying sources' own terms still separately apply
  the way GEBCO's/EMODnet's already do in `LICENSE-DATA.md` — don't
  assume the simpler answer without checking, same care already applied
  to every other source in that file.
- **Access-model mismatch to plan around**: served as web map tiles
  (raster DEM + vector tiles, MapLibre/Mapbox GL, TileJSON) at
  `tiles.openwaters.io/seascape` — a live tile server, not the static
  bulk-download grid file `bathymetry_raster.py` above is designed around
  (GEBCO/EMODnet ship as downloadable NetCDF/GeoTIFF). Check whether a
  bulk export exists beyond the tile endpoint before assuming a
  tile-mosaic-to-raster step needs to be built; if not, that mosaic step
  is real, additional work this source needs that GEBCO/EMODnet don't.
- **Recommended framing**: treat as a *candidate replacement or
  supplement* for the EMODnet leg specifically (better resolution where
  it has Rijkswaterstaat coverage), not a reason to drop GEBCO (still
  needed as the global fallback for anywhere Seascape's 23 sources don't
  reach) — decide after checking the license and access questions above,
  not before.

---

## 3c. Community override workflow

**Purpose**: the actual, working mechanism for "someone — human or AI —
fixes one specific wrong location, and it survives every future
regeneration" — this is the direct answer to the original project
question about handling situations free chart data gets wrong or leaves
ambiguous (bridges, locks, local knowledge).

**Where overrides live**: `router-data/overrides/{continent}/{country}/{region}/*.yaml`
— one small file per fix, human-readable, git-diffable, PR-reviewed
exactly like a code change. Required fields (matching
`override_provenance`'s columns, format spec §2.11):

```yaml
entity_type: edge            # node | edge | poi
entity_ref: "509856426391101:509856642391127"   # node id, or "source:target" for an edge
action: upsert                # upsert | delete
reason: >
  Movable Zeelandbrug span miscategorized by the mariculture obstacle
  bug; the fix already landed in the pipeline, this override is an
  example only.
evidence: "Round 3 investigation, see NEXT_PHASES.md"
contributor: "agent:claude"    # or a human GitHub username
reviewer: ""                   # filled in by the human who approves the PR
date: ""                       # filled in at merge time
fields:                        # only for action: upsert
  max_air_draft: 999.0
```

**Compiler**: a new standalone script, `apply_overrides.py`, mirroring
`add_pois_to_db.py`'s existing pattern (a separate, idempotent
enrichment tool run *after* the main pipeline, not a flag on
`nautical_routing_pipeline.py` itself — keeps the core pipeline's
already-complex stage order untouched). Reads every `overrides/**/*.yaml`
matching a region, validates required fields, and for each: upserts the
target node/edge/poi with `source_tier=5` and the override's field
values, inserts a row into `override_provenance`, and — critically —
**does this by writing into the same load-time overlay mechanism
(`user-edits.sqlite`) `db-worker.ts` already merges on top of the base
graph**, so a full pipeline regeneration of the base `.sqlite` never
needs to touch or re-apply overrides at all; they're sourced and merged
independently, exactly as designed back in Phase 0.

**Automatic anomaly queue**: a new pipeline stage (or standalone script,
`find_anomalies.py`) run after the main pipeline finishes, emitting a
GeoJSON list of flagged locations:
- A lock/bridge POI with no through-edge within N meters.
- Two graph components within a small distance of each other that remain
  topologically disconnected after `_ensure_coastal_connectivity` (i.e.
  its own "components left unmerged" warning, structured as data instead
  of a log line).
- An edge whose bottleneck constraint is tier-3/4-sourced only, with no
  tier-1/2 corroboration nearby.
- (After 3d lands) a real vessel-traffic ridge with no corresponding
  charted feature at all.

**Review workflow**: anomaly queue → either a human or an AI agent (with
satellite imagery, chart attributes, and OSM tags for that location as
context) proposes a fix as one of the YAML files above → opened as a PR
against `router-data` → human review and merge → `reviewer`/`date` filled
in → next `apply_overrides.py` run for that region picks it up
automatically. This is the same model OSM itself uses for crowd-sourced
correction, applied to a routing graph instead of a basemap — deliberately
not automated end-to-end, since a human sign-off is what promotes
something to tier-5 (spec-normative) trust.

**Concrete tasks**:
1. Override YAML schema + a JSON-schema validator (fail loudly on a
   malformed file rather than silently skipping it).
2. `apply_overrides.py`.
3. `find_anomalies.py`.
4. `community-overrides` catalog tag (already defined, unused) set
   automatically when `apply_overrides.py` finds ≥1 file for a region.

---

## 3d. AIS / vessel-density validation and tier-6 gap-filling

**Purpose**: use real, aggregated vessel-traffic data as (a) a QA signal
against the generated graph, and (b) a source of gap-filling candidates
in areas free chart data doesn't cover well — feeding the same anomaly
queue from 3c, never merged directly into the routable graph.

**Sourcing**: **EMODnet Human Activities** vessel-density GeoTIFFs for
Europe (1km grid, monthly, by ship type — use the `Sailing` and `Pleasure
Craft` categories specifically, since generic all-traffic density would
be dominated by commercial shipping lanes irrelevant to this router's
vessels). **MarineCadastre/AccessAIS** for the US (bulk broadcast data or
the pre-aggregated "AIS Vessel Transit Counts" product — prefer the
pre-aggregated one if its resolution is adequate, to avoid reprocessing
raw AIS).

**Validation use**: after a region is built, overlay the density raster
against the graph. Two checks, both emitting anomaly-queue entries, not
pipeline failures:
1. **Charted-but-unused**: a skeleton/lane edge in an area with
   near-zero traffic density despite being charted as a through-route —
   candidate for "silted up / closed / superseded," worth a human look
   before trusting it as-is.
2. **Used-but-uncharted-well**: a real density ridge with no
   corresponding tier-1/2/3 feature nearby at all — candidate for 3c's
   gap-filling path below.

**Gap-filling candidate generation**: where a density ridge exists with
no adequate existing coverage, extract it as a skeleton-shaped candidate
using the **same medial-axis technique already used for real skeleton
extraction** (`skimage.morphology.medial_axis` on a thresholded density
raster instead of a water-minus-land mask) — a deliberate, pleasing
architectural symmetry: "where do people actually go" gets the same
centerline-extraction treatment as "where the charts say water is."
Tag the result `source_tier=6` and route it into the anomaly queue for
human/AI review (3c) — **never insert it into the routable graph
directly**. A real track tells you where boats go, not that the charted
depth is safe for a given draft; only a human-approved override (tier 5)
may promote it.

**Concrete tasks**:
1. `vessel_density_validate.py` — download/cache the density rasters,
   run both validation checks, emit anomaly-queue entries (extending
   3c's schema with a `vessel_density` evidence type).
2. `_extract_density_ridges(raster, threshold)` — the medial-axis-on-
   density-raster candidate generator.
3. `data_sources` rows for EMODnet Human Activities / MarineCadastre,
   each `default_tier=6`.

---

## 3e. Scale-out: full Netherlands, then a first US region

**Purpose**: prove the whole stack (3a-3d plus the existing Phase 0-2
architecture) at real country scale, not just the Zeeland pilot — the
precondition for 3f, and for the project's original stated goal (US +
Europe coverage) to mean anything concrete.

**Netherlands, full**: re-run the pipeline over all of NL's coastal +
inland IENC coverage (Rijkswaterstaat, already the data source in use).
Expect the memory/performance hardening from Phase 0/1/Round 4 (the
per-round evaluation caps, the PSLG budget, the seam-focused stitching
pass, the A*-based corridor search) to already scale — **but confirm,
don't assume**, at roughly 10x the current Zeeland area: watch
specifically for a repeat of Round 4's "thousands of union-find groups"
pattern at a size the current caps haven't been exercised against, and
for `addAnchorShortcutEdges`-class load-time cost on an even larger
handful of outlier regions than Zeeland's one ~5,000-boundary-node case.

**First US region**: pick a NOAA-charted area with a similar complexity
profile to the Zeeland pilot — a mix of open water, narrow buoyed
channels, and at least one opening bridge or lock — rather than either a
trivially simple stretch of open coast or an immediately maximal
challenge. (Examples fitting that profile: a section of Chesapeake Bay,
or the Alaska Inside Passage — a concrete choice should be made when this
phase starts, not locked in here.) New verification needed, not a
redesign: confirm NOAA ENC's S-57 attribute encoding for the fields the
pipeline already depends on (`CATBRG` bridge category codes, `VERCLR`/
`VERCCL` clearances, `TRAFIC` fairway direction) matches the same S-57
standard values the Dutch RWS data uses — these are IHO-standardized
codes, so they should generalize, but the pipeline's `_s57_col`/
`_s57_get_val` helpers' candidate-name lists were written against RWS
data specifically and may need US-ENC-specific aliases added.

**Concrete tasks**:
1. Full-NL regeneration run; capture timing/memory numbords analogous
   to `NEXT_PHASES.md`'s existing Round 4 tables, as a baseline.
2. Pick and download a first US NOAA ENC region; verify `_s57_col`
   candidate lists against its actual attribute encoding before assuming
   parity.
3. First US regeneration run; same verification pass as Zeeland's Phase
   0/1 (real skeleton edges, real navmesh regions, a chosen local bridge/
   lock test scenario analogous to `test/zeelandbrug.test.ts`).

---

## 3f. Supernode / macro-edge hierarchical routing

**Purpose**: sub-second routing across country-scale graphs on modest
hardware (the original "Raspberry Pi on a boat" target from the
project's very first spec) — without this, a long-haul route forces a
full-resolution search across the entire loaded graph.

**Supernodes**: no new concept needed — they're exactly the navmesh
region anchors already computed by `selectAnchors` (Round 1's fix, TS
side) plus skeleton junctions plus every lock/bridge/POI. This phase's
job is connecting them *across* regions and long skeleton stretches, not
inventing a new node type.

**Macro-edges, Pareto-optimal**: extending the anchor-anchor funnel
shortcuts that already exist *within* one navmesh region (Round 1) to
*between* adjacent supernodes across regions/skeleton stretches:
1. Find the shortest path between two adjacent supernodes.
2. Identify its bottleneck constraint (min depth or min air-draft along
   the path).
3. Temporarily penalize/remove that bottleneck edge, recompute — this is
   the alternative for a deeper-draft or taller vessel.
4. Repeat up to a small fixed count (e.g. 3) of alternatives.
5. Store each as an `edge_kind_id=3` (`EDGE_KIND_MACRO` — already
   reserved in the enum since Phase 1, unused until now) row, with
   aggregate `min_depth`/`max_air_draft`/`min_width` across the
   underlying path (per format spec §7, already normative) and the full
   geometry for rendering.

**Runtime consumption** (TS side, `routeiq`): long-haul `astarSearch`
first searches over the **sparse supernode/macro-edge graph** for a
candidate route, filtering macro-edges by the vessel's constraints
exactly like any other edge (§7 already specifies "a consumer evaluating
supernode-to-supernode hops MUST consider all rows for a given pair, not
just the first found, and pick the cheapest one that satisfies the
vessel's constraints" — this is already-normative, unimplemented
behavior, not new design). Full-resolution search is then only needed
for the first-mile (user point → nearest supernode) and last-mile
(nearest supernode → destination) legs, not the whole route.

**Concrete tasks**:
1. `build_macro_edges.py` (pipeline side) — Pareto-alternative computation
   between adjacent supernodes, per region-pair and per skeleton stretch.
2. `macro_edges` population in `export_to_sqlite` (schema already
   supports this via `edge_kind_id=3`; confirm the `edges` table's
   existing columns are sufficient for aggregate bottleneck values, or
   whether a dedicated `macro_edge_path` side-table is cleaner than
   overloading `width_profile`/similar columns originally meant for
   single-edge data).
3. TS-side: a coarse-graph search mode in `astarSearch` that runs the
   sparse supernode search first, only falling back to full-resolution
   search for first-mile/last-mile legs — likely a real, non-trivial
   refactor of the current single-pass `astarSearch`, not a small patch.

---

## Cross-cutting schema additions needed (summary)

All of these are additive to the existing `schema_version=1` format —
no version bump required, consistent with the "no versioning baggage"
principle already established:

- 3a: `seamarks_points` layer (pipeline input only, not a new DB table),
  new `data_sources` rows.
- 3b: new `data_sources` rows; no new columns (uses existing `source_tier`/
  `min_depth`).
- 3c: no new columns (`override_provenance` already exists from Phase 1);
  the `overrides/` directory lives in `router-data`, not this repo.
- 3d: new `data_sources` rows; anomaly-queue schema is a new, separate
  JSON artifact, not a database table.
- 3e: no schema changes, verification only.
- 3f: `macro_edges` population using the already-reserved
  `edge_kind_id=3` — decide during implementation whether the existing
  `edges` table's columns suffice or a side-table is warranted.

## Suggested order

3a and 3c first (independent, both low-risk, both directly useful even
alone — 3c in particular should land early since it's the mechanism that
makes every other phase's inevitable rough edges fixable without a full
pipeline rewrite each time). 3b in parallel with either. 3d once 3c's
anomaly queue exists to feed. 3e only after 3a-3d are proven on the
Zeeland/NL pilot — scaling up before the data-fusion model is validated
just multiplies whatever isn't working yet. 3f last, and only once 3e
gives it a graph large enough to matter.
