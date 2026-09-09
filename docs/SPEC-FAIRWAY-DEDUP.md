# Spec: Fairway/Dredged-Area Boundary Preference — Reducing Medial-Axis Density Near a Marked Channel

Status: Draft — analysis only, no code changes. **§6.1's split+reunion mechanism, as
written, has a confirmed geometric flaw (see §6.1's own correction note and §10 item 3):
it does not reduce density for a fairway that sits wholly interior to a piece, and any
change it does produce there is a seam artifact rather than legitimate simplification.
The next session should start by prototyping the single-pass, vertex-weighted-simplify
alternative §10 item 3 already names, not by implementing §6.1 as currently sketched.**
**§2.4 also corrects an earlier, wrong negative finding: the original location query used
the wrong reference point, and real `FAIRWY`/`DRGARE` coverage (`Cobb Island Channel`,
`Neale Sound Channel`) is now confirmed to sit directly at the motivating Coltons Point
screenshot location, not merely nearby — see §2.4 for the corrected query and its
downstream consequences.**
Written as a standalone document rather
than a new section of `SPEC-FAIRWAY-HARMONIZATION.md`: that spec is about depth/cost/
classification signal (what `min_depth`/`cost_factor`/`laned` a fairway contributes to an
edge *after* the graph exists); this spec is about graph *topology generation* itself —
whether `build_skeleton_network` should treat a fairway/dredged-area polygon as ground
truth for how much boundary detail to carry into the medial axis. That makes it the direct
sibling of `SPEC-GRAPH-DENSITY.md` §4.3/§6.3 (axis-dedup) and §9 (boundary-simplify), not
of the harmonization spec — this document builds on and cross-references both of those
sections throughout, and complements (does not duplicate) `SPEC-FAIRWAY-HARMONIZATION.md`
and `SPEC-RECOMMENDED-TRACK.md`.
Complements: `SPEC-GRAPH-DENSITY.md` (§4.3, §6.3, §9), `SPEC-FAIRWAY-HARMONIZATION.md`,
`SPEC-RECOMMENDED-TRACK.md`
Scope: `nautical_routing_pipeline.py` (`build_skeleton_network`, `_split_wide_narrow`/
`build_network`'s dispatch loop, `ClassificationConfig`)
Measured against: `data/us_east_md_stitched_v3.sqlite` (55,074 nodes / 129,976 edges) and
its source `data/geojson/us-east-md-stitched-v3/*.geojson`; `data/geojson/md_reclip/*`;
cross-referenced against the already-published Zeeland numbers in
`SPEC-GRAPH-DENSITY.md` §2.3/§4.3.1

## 1. Symptom

Following `SPEC-GRAPH-DENSITY.md` §9's fix (`skeleton_boundary_simplify_m`, which
addressed the specific dense "bowtie" tangle a rendered Potomac River / Coltons Point
screenshot showed), the user reviewed a follow-up screenshot of the same area and pointed
out that a real marked/buoyed federal channel runs through or near it — chart labels
visible in the screenshot include "Potomac River Channel Buoy 13/14/14A/15" and
"Dukeharts Channel". The user's stated next priority: the pipeline should **prefer the
marked channel over its own independently-generated medial-axis skeleton** wherever an
authoritative marked-channel/fairway source already exists, rather than reconstructing
(and then needing to declutter) an independent mesh over the same water.

This is the same idea `SPEC-GRAPH-DENSITY.md` §4.3 ("prefer the authoritative axis over a
generated twin") already ships for `inland_waterways` (RWS `wtwaxs` / NOAA `RECTRC`/
`NAVLNE`) — but keyed to the `fairways`/`dredged_areas` (`FAIRWY`/`DRGARE`) layer instead.
§2 below establishes, from real data, why that layer cannot simply reuse axis-dedup's
mechanism unmodified: axis-dedup's whole design assumes a **line** to carve a suppression
buffer around a distance field; `FAIRWY`/`DRGARE` are, confirmed against real NOAA and RWS
extracts, always **polygons** — a corridor area, not a centerline.

## 2. Measurements

### 2.1 Geometry type — confirmed polygon, not line, in both NOAA and RWS data

`enc_preprocessor.py:42-60`'s `layer_mapping` settles this definitively for what this
pipeline actually ingests:

```python
'FAIRWY':  'fairways_polygons.geojson',        # OBJL 51 (polygon)
'DRGARE':  'dredged_areas_polygons.geojson',   # OBJL 46 (polygon)
'RECTRC':  'inland_waterways_lines.geojson',   # OBJL 109 (line)
'NAVLNE':  'inland_waterways_lines.geojson',   # OBJL 85 (line)
'WTWAXS':  'inland_waterways_lines.geojson',   # (line)
```

Confirmed directly against real feature properties in this repo's own data (not assumed
from the S-57 catalogue alone):

| layer | file | sample geometry | `OBJL` observed |
|---|---|---|---|
| `fairways` | `data/geojson/md_reclip/fairways_polygons.geojson` | `Polygon` | 51 |
| `dredged_areas` | `data/geojson/us-east-md-stitched-v3/dredged_areas_polygons.geojson` | `Polygon` | 46 |
| `inland_waterways` | `data/geojson/md_reclip/inland_waterways_lines.geojson` | `LineString` | 85 (`NAVLNE`) |

Note: `SPEC-FAIRWAY-HARMONIZATION.md` §2 states `DRGARE` as "OBJL 53" — the real data here
carries `OBJL=46` on every `dredged_areas` feature sampled. 53 is `DRYDOC` in the S-57
object catalogue, not `DRGARE`. Worth reconciling in that doc; not fixed here (out of this
spec's scope), flagged as a minor correction for whoever next touches that file.

`_build_fairways_unified()` (`nautical_routing_pipeline.py:1639`) concatenates `fairways`
+ `dredged_areas` into `fairways_unified` — both are `Polygon`/`MultiPolygon` throughout,
so `fairways_unified` is a polygon layer end to end. This confirms investigation point 3
from this document's own brief: **there is no line-type "authoritative axis" for a marked
NOAA/RWS fairway.** The line-type analogue that *does* exist — `RECTRC`/`NAVLNE` — is a
different, separate S-57 object class that `enc_preprocessor.py` already merges into
`inland_waterways_lines.geojson` today (unconditionally, `SPEC-RECOMMENDED-TRACK.md`
"Option A"), and therefore *already* gets axis-dedup treatment wherever it exists and
`--axis-dedup-cap` is enabled. §4 below establishes that this pre-existing path is real,
though (per §2.4's corrected finding) it still does not reach the motivating Coltons
Point location specifically — zero `inland_waterways` features sit within 15 km of it,
even though `fairways`/`dredged_areas` coverage (a different layer, this spec's own
subject) does.

### 2.2 Fairway/dredged polygons are channel-shaped and far coarser than natural coastline

Across `data/geojson/us-east-md-stitched-v3/{fairways,dredged_areas}_polygons.geojson`
(362 + 420 real features):

| metric | `fairways` (FAIRWY) | `dredged_areas` (DRGARE) |
|---|---|---|
| minimum-rotated-rectangle aspect ratio (long/short edge), median | 5.8 | 7.6 |
| aspect ratio, p25 / p75 | 3.4 / 11.0 | 3.8 / 19.8 |
| share with aspect ratio ≥ 3 ("channel-shaped", not a blob) | 80.4% | 84.8% |
| boundary vertex density, median (vertices/km of perimeter) | 6.95 | 5.81 |
| boundary vertex density, mean | 9.74 | 9.55 |

Most fairway/dredged polygons in this real dataset are long, thin corridors — not blobs —
and are digitized far more coarsely than natural coastline. `Kettle Bottom Shoal` (used
here as a sample feature, NOT the nearest named `FAIRWY`/`DRGARE` pair to Coltons Point —
see §2.4's correction, which found `Cobb Island Channel`/`Neale Sound Channel` actually
sit at that location) is close to the coarse end of that distribution: 2 polygons, 11-12 vertices
each, ~6.6-6.9 km long, ~80-94 m wide (minimum-rotated-rectangle short edge), area
541,000-587,000 m² — **≈0.8-0.9 vertices per km**.

Compare the natural `coastal_water` polygon containing the same point in the same clip:
20,371 vertices over a 445.4 km perimeter (its own connected component, computed
directly) — **45.7 vertices/km**, roughly **6-8× the fairway/dredged median** and
**≈53× `Kettle Bottom Shoal`'s own local density**. This is the same phenomenon
`SPEC-GRAPH-DENSITY.md` §9.1 already root-caused for the *unmarked* case (494,363
vertices on one connected body elsewhere in the MD dataset) — but here there is a second,
independent, *already-curated* boundary sitting right on top of some of that noise: the
charting authority's own fairway/dredged polygon. Where it exists, it is materially
cleaner than the coastline it sits inside, without needing a guessed simplify tolerance.

### 2.3 Real duplication, measured against a full build

Queried directly against `data/us_east_md_stitched_v3.sqlite` (55,074 nodes / 129,976
edges) — the same build `SPEC-GRAPH-DENSITY.md` §8.6 used for its own (negative) Coltons
Point investigation — distance from every node to the nearest `fairways_unified`
(`fairways` ∪ `dredged_areas`) polygon:

| distance to nearest fairway/dredged polygon | node count | share of 55,074 |
|---|---|---|
| inside (distance = 0) | 2,652 | 4.8% |
| ≤ 10 m | 2,954 | 5.36% |
| ≤ 50 m | 3,543 | 6.43% |
| ≤ 100 m | 4,121 | 7.48% |
| ≤ 250 m | 5,209 | 9.46% |
| ≤ 1,000 m | 8,927 | 16.21% |

Median distance across all 55,074 nodes: 5,191 m (most of the graph is nowhere near a
fairway — expected, fairway/dredged area is only 0.83% of total `coastal_water` area in
this region: 145.9 km² + 45.6 km² fairway/dredged vs. 23,176.5 km² coastal water).

Of the 3,217 nodes within 25 m of a fairway/dredged polygon, **87.7% (2,823) are
`node_kind_id=0` ("point" — skeleton/medial-axis nodes), only 12.3% (394) are
`navmesh_vertex`.** This directly confirms the mechanism this spec targets: the
duplication concentrates in exactly the medial-axis path, not incidental overlap with
open-water triangulation (which naturally follows the same water polygon whether or not a
fairway sits inside it, so has nothing to "duplicate").

This is the same phenomenon `SPEC-GRAPH-DENSITY.md` §2.3 already measured for Zeeland
(NL RWS `FAIRWY`, before axis-dedup shipped): **7,109 nodes = 14.6%** of that 48,553-node
DB fell inside a fairway polygon — a bigger share than this MD sample's 4.8%, consistent
with NL's much denser `FAIRWY` charting (`SPEC-FAIRWAY-HARMONIZATION.md` §1's own
NL-vs-US density table) against a sparser NOAA sample here. Real in both datasets;
secondary in scale in both (§4.3's own words: "~5% of the graph, against ~33% for
over-sampling" — this MD measurement, 4.8%, lands in the same range).

### 2.4 The motivating screenshot's own location — CORRECTED (found in review; the original query used the wrong point)

**Correction, found in review before this spec went further: the original version of this
section queried the wrong reference point** — a single point (38.209°N, 76.859°W) that
does not actually match the screenshot's own documented coordinates (START 38.2696°N
76.8189°W, DEST 38.2628°N 76.8716°W) — and concluded, wrongly, that no fairway/dredged
feature sits near the motivating location. Re-querying against the REAL screenshot
bounding box (`box(-76.8716, 38.2628, -76.8189, 38.2696)`, both points included) gives the
opposite answer:

**Six `fairways`/`dredged_areas` features directly intersect the real screenshot bounding
box** — two named `Cobb Island Channel` (a `FAIRWY`/`DRGARE` pair) and two named `Neale
Sound Channel` (also a `FAIRWY`/`DRGARE` pair), each counted once per layer. `Cobb Island
Channel`'s own extent (`-76.8416, 38.2648` to `-76.8392, 38.2656`) sits well inside the
bbox; `Neale Sound Channel` spans roughly `-76.868` to `-76.855` longitude at
`38.267-38.271` latitude, also inside it. Both are real, named marked channels sitting
directly on top of the motivating "bowtie" location — not 3.9 km away as the original
(wrongly-targeted) query concluded.

`Kettle Bottom Shoal` (§2.2's own sample feature, still a real, correctly-measured
`FAIRWY`/`DRGARE` pair in this dataset) is NOT the nearest feature to Coltons Point — it
was only the nearest to the mistakenly-used reference point. §2.2's vertex-density
measurement of it stands on its own merits (a real feature, independently sampled), but
the "nearest named pair to Coltons Point" framing there is corrected below.

The `inland_waterways` negative finding, by contrast, **does still hold** against the
correct bbox: zero `inland_waterways` features intersect it, and the nearest is 17.3 km
away (re-queried directly; well past the 15 km radius originally checked).

Separately, `fairways_polygons.geojson` (across the whole MD dataset) does carry 5
features named exactly `"Potomac River Channel"` — but all 5 sit near Washington DC
(38.55-38.70°N), a different reach of the same river, roughly 40-50 km upriver from
Coltons Point. Whatever chart label the user saw reading "Potomac River Channel" near
Coltons Point most plausibly refers informally to this general reach of the Potomac
rather than to one specific named `FAIRWY` feature — `Cobb Island Channel`/`Neale Sound
Channel` are the actual named vector features this pipeline has at that exact location.
Still not fully resolved by static investigation alone (which one, if either, the user's
own chart rendering specifically labeled "Potomac River Channel" at that point) — carried
forward as this document's first open question (§9), now substantially narrowed.

**Consequence for this design, corrected**: this mechanism IS now expected to be directly
relevant to the original Coltons Point bowtie location — real `FAIRWY`/`DRGARE` coverage
(`Cobb Island Channel`, `Neale Sound Channel`) sits right on top of it. `SPEC-GRAPH-
DENSITY.md` §9 (`skeleton_boundary_simplify_m`, already shipped on this branch) fixed the
mechanism it targets there (unsimplified boundary noise); this spec's mechanism, once
implemented (§5.3's own deferred status still applies — the split+reunion construction
itself is broken and needs redesign first, independent of this location correction), would
address the *separate* fairway/skeleton duplication this section's own siblings
(`Bonum Creek Channel`, `Saint Catherine Sound Upper Entrance Channel`, `Monroe Creek
Channel`, `Saint Patrick Creek Channel`, `Cuckold Creek Channel` — all real, named,
nearby `FAIRWY` features in the same ~15 km stretch of the Potomac this dataset covers,
including now Cobb Island Channel and Neale Sound Channel at Coltons Point itself) and,
per §2.3's Zeeland cross-check, generally.

## 3. Current behaviour, confirmed in code

`fairways_unified` is built once in `parse_shapefiles` (`_build_fairways_unified`,
`nautical_routing_pipeline.py:1639`) and is read from exactly four places, all downstream
of graph topology already existing:

1. `_edge_attr_worker` (line ~453): `cost_factor = 0.8` if an edge's chord intersects a
   fairway candidate.
2. `classify_water_body` → `_has_regulatory_structure` (line ~2818): a narrow water body
   overlapping a fairway is classified `"laned"` instead of plain `"skeleton"` — this only
   changes downstream traffic-mode defaults (§`_extract_buoyage_direction` — currently a
   no-op stub, since no source in this data carries structured lane-direction attributes),
   **not** the geometry generation path. A `"laned"` piece still goes through the exact
   same `build_skeleton_network` call as a plain `"skeleton"` piece.
3. `_add_opening_bridge_edges`/`_add_lock_crossing_edges` (lines ~5898, ~6097): fairway ∪
   inland-waterways intersection with a bridge/lock polygon locates the opening/chamber
   node.
4. POI generation (line ~7446): fairway features become `POI_TYPE_FAIRWAY` points.

**None of these four consumers ever reach `build_skeleton_network`, `_rasterize_water_
polygon`, or `_extract_medial_axis_skeleton`.** `fairway_gdf`/`fairways_unified` is not
passed as an argument to `build_skeleton_network` at all (confirmed: its signature is
`build_skeleton_network(self, polygon, source_tier=..., source_id=None)` — no fairway
parameter). The medial-axis machinery that generates skeleton topology has no visibility
into where an authoritative marked channel already exists, and `classify_water_body`'s
`"laned"` distinction — the one place fairway overlap is even checked before geometry
generation — is a label, not a geometric input.

## 4. Relationship to already-decided levers

### 4.1 Axis-dedup (§4.3) already covers the line-type case, where a line exists

`SPEC-GRAPH-DENSITY.md` §4.3/§6.3's axis-dedup mechanism (`_axis_dedup_suppression_mask`,
`_axis_dedup_carve_navmesh_pieces`, carve-reconnect via `_connect_waterway_crossing`) is
keyed to `inland_waterways` (`wtwaxs`/`RECTRC`/`NAVLNE`). §2.1 confirms `RECTRC`/`NAVLNE`
already merge into `inland_waterways_lines.geojson` unconditionally
(`SPEC-RECOMMENDED-TRACK.md` "Option A", already shipped, no code change needed). So
**wherever a NOAA `RECTRC`/`NAVLNE` line exists, enabling `--axis-dedup-cap` already gives
the exact "prefer the authoritative axis" behaviour the user is asking for**, with zero
new code — this spec's own contribution is strictly for the case axis-dedup cannot reach:
a marked channel charted **only** as a `FAIRWY`/`DRGARE` polygon, with no accompanying
`RECTRC`/`NAVLNE` line. §2.4 confirms this is exactly the situation at every fairway
checked near Coltons Point in this dataset (`inland_waterways` count within 15 km: 0).

This also means `SPEC-RECOMMENDED-TRACK.md` §4's deferred "Option B" (promote `CATTRK=1`
`RECTRC` tracks into `fairways_unified` for `cost_factor` purposes) is **orthogonal** to
this spec, not a prerequisite or a duplicate: Option B is about the *cost/classification*
tier a `RECTRC` line's own topology gets, not about whether a polygon-only fairway (no
`RECTRC` at all) gets a density-reducing treatment. Both can ship independently.

### 4.2 Why axis-dedup's own line-based design cannot simply be pointed at `fairways_unified`

Axis-dedup's entire tolerance formula (`tol = clip(fraction * local_width, floor, cap)`,
§4.3.1) and its carve-reconnect machinery (§6.3) are built around one assumption: **the
authoritative source is a zero-width line, and removing generated water near it is safe
because that line already carries real routing topology elsewhere in the graph** (via
`_build_inland_network`, which ingests every `inland_waterways` `LineString` directly as
graph edges). Neither premise holds for `fairways_unified`:

- It is a polygon (area), not a line — "distance to the nearest pixel of a rasterized
  line" is not the right primitive; a water pixel *inside* a fairway polygon is not "near
  an axis", it *is* the marked channel.
- **Nothing ingests `fairways_unified` as routing topology.** Unlike `wtwaxs`/`RECTRC`,
  no `_build_fairway_network`-equivalent exists. If this spec's mechanism removed water
  near a fairway polygon the way axis-dedup removes water near an axis line, it would
  delete real navigable water with **nothing replacing it** — the carve-reconnect safety
  net §6.3 built specifically because of this failure mode (the Hansweert regression) has
  no fallback line to reconnect to here. A mechanism for `fairways_unified` must therefore
  either inject new routing topology of its own, or — the design taken in §6 below — never
  remove water in the first place.

## 5. Design options considered

### 5.1 Option 1 — derive a synthetic centerline from each fairway polygon, reuse axis-dedup verbatim: rejected

Run the existing medial-axis machinery once per fairway/dredged polygon to derive a
`LineString`, inject it as if it were an `inland_waterways` feature (mirroring
`_build_inland_network`'s ingestion), then let §4.3/§6.3's suppression + carve-reconnect
run unmodified against it.

Rejected: this doubles the work for no real gain over acting on the polygon directly.
A centerline *we* derive from the fairway polygon is not more authoritative than a
medial-axis derived from the real coastal water polygon — it is the *same kind* of
derived approximation, just computed from a cleaner (coarser, less noisy) input. Building
a whole new topology-injection path (mirroring `_build_inland_network`, needed because —
per §4.2 — nothing currently ingests `fairways_unified` as graph edges) purely so that the
*existing* medial-axis machinery can regenerate an equivalent line back through
carve-then-reconnect is circular. Everything this option would deliver, Option 3 (§5.3)
delivers by simply letting `build_skeleton_network`'s own medial-axis step run on a
cleaner input directly — no synthetic line, no injection path, no reconnect machinery.

### 5.2 Option 2 — carve the fairway-covered footprint into its own separate skeleton piece, stitched at the seam: rejected

Split each narrow water piece into "inside a fairway/dredged polygon" and "outside",
skeletonize each independently (the inside piece is cheap and clean — few vertices), and
let the existing stitching passes (Pass 0-2 / gap-resolve) reconnect them at the shared
boundary, the same way any two adjacent water fragments already get stitched.

Rejected on direct precedent already recorded in this codebase, not speculation: exactly
this idea — split a narrow-water component into separate `build_skeleton_network` calls
per fragment — **was tried and reverted** for a different reason (Multi`Polygon`
fragmentation of a single connected component). `build_network`'s own dispatch loop
(`nautical_routing_pipeline.py`, around line 2033) carries the scar tissue: *"Exploding
this into separate per-fragment `build_skeleton_network` calls was tried first and
fragmented real channel networks into hundreds of disconnected pieces, since raster-
derived medial-axis endpoints don't land on the exact seam coordinate the fragments were
cut at."* Deliberately carving a piece into fairway-covered / not-covered sub-pieces along
an essentially arbitrary polygon boundary (the fairway's own edge, which has no relation
to where a clean raster seam would naturally fall) reproduces precisely that failure mode.
It might be recoverable with the same reconnect machinery §6.3 built for axis-dedup's own
carve fragmentation — but that reintroduces most of Option 1's complexity (dead-end
tracking, per-line reconnect caps, tie-break edge cases) for a mechanism that, per §6.3
itself, took three follow-on rounds (§6.3.2, §6.3.6, §6.3.7) to close real gaps in. Not
worth it when Option 3 needs none of it.

### 5.3 Option 3 — fairway-scoped stronger boundary simplification: CHOSEN IN PRINCIPLE, its §6.1 split+reunion IMPLEMENTATION IS DEFERRED

**Status correction (found in review, before implementation): the specific
split+reunion mechanism §6.1 originally sketched for this option is confirmed broken
for a fairway that sits wholly interior to a piece (§6.1's own correction note, §10
item 3) — it is DEFERRED, not ready to build. The general APPROACH below (a second,
fairway-scoped simplification tolerance) is still the right direction; what's deferred
is specifically the split-then-reunite construction, in favour of the single-pass
vertex-weighted alternative §10 item 3 names. §7's CLI flag proposal describes the
deferred mechanism's intended contract, for whoever designs its replacement — it is
not ready to implement as written either.**

Extend `SPEC-GRAPH-DENSITY.md` §9's already-shipped mechanism
(`skeleton_boundary_simplify_m`, a flat Douglas-Peucker tolerance applied to the whole
piece before rasterizing) with a **second, stronger tolerance that applies only to the
sub-boundary lying within (a small buffer of) `fairways_unified` coverage.** The INTENT
is that no water is added or removed — this should only change how many vertices
survive along the portion of the piece's *own real boundary* that an authoritative
fairway/dredged polygon already independently confirms is the marked channel. §6.1's
split+reunion construction does not actually guarantee that intent on its own (see
§6.2's own risk list and §6.1's correction note: a seam gap, overlap, or accidental
interior hole can change area, connectivity, or rasterized water pixels) — it is only
as safe as §6.2's validation checks make it, which is why those checks are load-bearing
rather than defensive padding. Because the INTENDED effect never moves water, this
needs (once a construction is found that actually delivers that intent) **no
carve-reconnect, no dead-end tracking, no new topology injection, and no interaction with
the `crosses_land` safety gate beyond what §9 already established** (the land mask is
rasterized separately from the unmodified land layer and re-intersected after any
simplify, so a slightly-off boundary can never produce a routable pixel over land).

Rejected alternative within this option: raise `skeleton_boundary_simplify_m` itself
(the single, global tolerance) instead of adding a second, fairway-scoped one. Rejected
for the same reason §4.1.1 rejected a single flat sagitta tolerance: §9.2's own measured
plateau (17%/27%/35% node reduction at 5m/15m/30m, flattening past ~30m) is a real ceiling
on how far a *global* tolerance can safely go before eroding genuine detail on narrow,
un-marked, twisty creeks elsewhere in the same build. §2.2's vertex-density numbers show
fairway/dredged polygons are curated to a materially coarser standard (median 6-7
vertices/km, `Kettle Bottom Shoal` itself under 1/km) than even a generous *global*
simplify tolerance would reach without also flattening real detail everywhere else. Fairway
coverage is an independent, authoritative signal for *where* aggressive simplification is
safe — using it is a strictly better lever than pushing one number higher for the whole
build, mirroring exactly why §4.1.1/§4.3.1 both rejected a flat number in favour of a
locally-coupled one.

## 6. Chosen design, in detail

### 6.1 Mechanism

In `build_skeleton_network`, immediately after §9's existing (uniform)
`skeleton_boundary_simplify_m` step and before `_rasterize_water_polygon`:

1. Bounding-box prefilter `fairways_unified` against this piece (same
   `_lonlat_margin_deg` + `_candidates_by_bounds_static` pattern §4.3.2/§6.3.2 already
   use) — a piece with no fairway/dredged candidate nearby returns immediately, no
   rasterization or extra geometry work at all. This matters here for the same reason it
   mattered for `_axis_dedup_carve_navmesh_pieces` (§6.3's docstring): most pieces in a
   build are nowhere near a fairway (§2.3: median node-to-fairway distance 5.2 km), so
   this must be a cheap no-op for the overwhelming majority of calls.
2. `fairway_near_m = unary_union(candidates).buffer(fairway_boundary_buffer_m)` in the
   piece's local metric CRS — the buffer exists so the stronger tolerance's reach doesn't
   end with a hard edge exactly on the fairway polygon's own boundary (which would leave a
   visible density seam right at the transition); see §7's open question on tuning this.
3. `covered_m = poly_m.intersection(fairway_near_m)`; `remainder_m = poly_m.difference(
   fairway_near_m)`.
4. `covered_simplified_m = covered_m.simplify(fairway_boundary_simplify_m,
   preserve_topology=True)` — the stronger tolerance, applied only to this sub-polygon.
   `remainder_m` is left as `poly_m` already stands (i.e. as already processed by §9's
   uniform tolerance, if any).
5. `candidate_m = unary_union([covered_simplified_m, remainder_m])`.

   **Correction (confirmed geometric flaw, found in review before implementation —
   see §10 item 3 for the recommended alternative):** when `fairway_near_m` sits wholly
   *inside* `poly_m` — never touching `poly_m`'s own exterior ring, e.g. a fairway
   narrower than the charted water it runs through — `remainder_m` (`poly_m.difference(
   fairway_near_m)`) keeps `poly_m`'s ENTIRE exterior ring unchanged; only an interior
   hole is cut out of it. `candidate_m`'s own exterior ring after the reunion is
   therefore identical to `poly_m`'s original exterior ring (step 4 only simplifies the
   *interior* `covered_m` piece, and step 5 just fills that interior hole back in with
   the simplified version) — meaning this specific, common case (a fairway that doesn't
   reach the piece's own banks) yields **no boundary-vertex reduction at all**, since the
   coastline vertices actually driving medial-axis junction density (§9's own root
   cause) live on the exterior ring, not inside it. Any raster/skeleton change §9's
   mechanism would then observe from this step, for a wholly-interior fairway, comes
   from a seam gap/overlap/accidental hole at the `covered_m`/`remainder_m` cut (§6.2's
   own named risk) rather than legitimate simplification — i.e. exactly the failure mode
   §6.2 already flags as a risk, but here it would be the ONLY source of any observed
   effect, not just noise on top of a real one. This mechanism, as sketched, therefore
   only has a legitimate density-reduction path for a fairway that DOES touch/cross
   `poly_m`'s own exterior boundary (a fairway spanning bank-to-bank) — it needs either
   (a) an explicit precondition checking `covered_m` actually includes part of `poly_m`'s
   exterior ring before attempting this reunion at all, discarding/no-op'ing the
   wholly-interior case, or (b) replacing this split+reunion approach entirely with the
   single-pass, vertex-weighted-simplify alternative §10 item 3 already names, which
   does not have this failure mode by construction (there is no seam, and every vertex
   on the real exterior ring is considered, wholly-interior fairway coverage or not).
   **Recommendation: do not implement the split+reunion approach as written; prototype
   (b) first.**

6. **Validate before accepting** (see §6.2 — this is the load-bearing safety step): if
   `candidate_m` fails validation, discard it and fall back to `poly_m` (today's/§9's
   already-shipped output) for this piece, unchanged. Never risk correctness for this
   optimization.
7. Feed `candidate_m` (or the `poly_m` fallback) into the existing, completely unmodified
   `_rasterize_water_polygon` → `_extract_medial_axis_skeleton` → `_skeleton_raster_to_
   graph` pipeline. No other line in `build_skeleton_network` changes.

### 6.2 Validation before accepting the fairway-simplified candidate — the real engineering risk in this design

Independently simplifying two polygons that share a cut boundary (`covered_m` and
`remainder_m`, both derived from the same `poly_m` by `intersection`/`difference`) and
then reuniting them via `unary_union` is a known Shapely/GEOS hazard: each side's
Douglas-Peucker pass can keep a *different* subset of the shared seam's vertices, which
can leave a sliver gap, a thin self-intersecting overlap, or (rarely) split what was one
connected piece into two along that seam. This is a real, unresolved implementation risk
this document does not claim to have solved — see §9's open questions — but it is
boundable with checks mirroring `_safe_negative_buffer`'s own established "retry, then
degrade gracefully, never risk correctness" convention:

- `candidate_m.is_valid` (repair via `make_valid()`/`buffer(0)` first, same as
  `_build_obstacle_layer`'s existing validity cleanup, before this check).
- `len(_explode_polygonal(candidate_m)) <= len(_explode_polygonal(poly_m))` — the reunion
  must not have silently split one connected piece into more disconnected fragments than
  the input already had.
- `abs(candidate_m.area - poly_m.area) / poly_m.area < AREA_TOLERANCE` (e.g. 0.5%) — real
  navigable water must not measurably disappear or appear at the seam.

If any check fails, **use `poly_m` unmodified** (today's output, or §9's uniform-simplify
output if that flag is also set) for this specific piece and log a counter
(`fairway_boundary_simplify_stats["seam_rejected"]`, matching this file's established
per-mechanism diagnostic-counter convention) rather than aborting the build. A piece where
this optimization can't be safely applied is not a piece that should silently get worse
output — it should get today's already-verified-safe output.

### 6.3 Why this stays scoped to `build_skeleton_network`, not extended to `build_navmesh_region`

`build_navmesh_region` already has its own boundary-simplify pass
(`NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0`, unconditional, not gated behind a flag — confirmed
at `nautical_routing_pipeline.py:685`) and triangulates the polygon's own vertex set
directly rather than deriving a medial axis from a raster, so it does not exhibit §9's
"any small boundary wiggle spawns its own branch" failure mode in the first place —
`triangle`'s PSLG triangulation is far less sensitive to boundary vertex density than
`skimage.morphology.medial_axis` is. Per §4.3's own precedent (axis-dedup shipped
skeleton-only first, §7 extended it to navmesh only once a real deployed database showed
a measured gap — the Oosterschelde-approach node), this spec proposes skeleton-only for
the same reason: extend to navmesh only if a real build shows navmesh pieces overlapping
fairway coverage still carry excess density after this ships, not speculatively now.

## 7. CLI flag proposal

**This section describes the flag contract for §6.1's deferred split+reunion
mechanism (see §5.3's correction) — it is a target shape for whoever designs the
replacement, not a ready-to-implement proposal.** The flag NAME/convention below would
likely carry over to a vertex-weighted-simplify replacement largely unchanged (same
tolerance semantics from the caller's point of view); the mechanism it configures is
what's deferred.

Following this file's established convention (§4.1/§4.3/§6.4-§6.8: a numeric tolerance,
default `0.0` = disabled, a piece with no fairway candidate nearby is a no-op regardless
of value, `0.0` reproduces prior output byte-for-byte):

- **`--fairway-boundary-simplify-m`** (float, default `0.0`). `ClassificationConfig.
  fairway_boundary_simplify_m`. The Douglas-Peucker tolerance applied, in place of (or on
  top of, per §6.1 step 4) `skeleton_boundary_simplify_m`, to the sub-boundary of a piece
  lying within `fairway_boundary_buffer_m` of a `fairways_unified` polygon. Bounded by a
  ceiling constant `FAIRWAY_BOUNDARY_SIMPLIFY_MAX_M` — reuse `SKELETON_BOUNDARY_
  SIMPLIFY_MAX_M`'s existing value (200.0) rather than inventing a second ceiling; §9.3's
  own reasoning for that number (measured gains plateau well below it; a much larger value
  risks eroding real channel shape) applies unchanged here, since this is the same
  `simplify()` primitive on the same class of input.
- **`--fairway-boundary-buffer-m`** (float, default `0.0`). `ClassificationConfig.
  fairway_boundary_buffer_m`. How far beyond a fairway/dredged polygon's own edge the
  stronger tolerance's reach extends (§6.1 step 2). At `0.0`, only pixels whose true
  boundary segment falls strictly inside the fairway polygon itself get the stronger
  tolerance — the narrowest, most conservative starting point. **Not measured/tuned in
  this document** — see §9's open questions; unlike `axis_dedup_cap_m` (§4.3.1, tuned
  against 23,614 real measured nodes before shipping), no equivalent real-node
  distance-to-fairway-edge study has been run yet for this buffer specifically. Ship at
  `0.0` until one is.
- **Validation** (mirroring `_validate_classification_overrides`'s pattern, a new
  `_validate_fairway_boundary_simplify` alongside it): both must be finite and `>= 0.0`;
  reject (`ValueError`, not silent clamp — matching `_validate_connector_merge_m`'s
  "reject the mistake, don't mask it" convention, §6.4.3's own lesson from the
  `NaN`/`shapely.segmentize` incident) any value that is non-finite; if
  `fairway_boundary_simplify_m > 0.0` and `fairway_boundary_simplify_m <
  skeleton_boundary_simplify_m`, reject — the fairway-scoped tolerance being *weaker* than
  the global one it's meant to strengthen is certainly a configuration mistake, not a
  deliberate choice, the same class of check `axis_dedup_floor_m <= axis_dedup_cap_m`
  already enforces (§`_validate_classification_overrides`).

Gate 1 (byte-identical at default) holds by construction the same way every other flag in
this file does: `fairway_boundary_simplify_m == 0.0` skips the entire §6.1 block, `poly_m`
reaches `_rasterize_water_polygon` exactly as it does today (or exactly as §9 alone
produces it, if `skeleton_boundary_simplify_m` is separately enabled).

## 8. Risks

- **The seam-reunion validity risk (§6.2) is real and not fully resolved by this
  document** — it is the central open engineering question for whoever implements this
  (§9). The degrade-gracefully fallback bounds its *safety* impact to zero (a rejected
  piece is exactly as safe as today's output), but a seam that fails validation often
  enough would silently erode this mechanism's actual payoff without erroring — worth
  logging and watching in the same `fairway_boundary_simplify_stats` counter §6.2
  proposes.
- **`fairways_unified` mixes `FAIRWY` and `DRGARE`**, which can disagree in shape at their
  edges (`SPEC-FAIRWAY-HARMONIZATION.md` §6: "a centroid test on `US5NYCEG` shows most
  `DRGARE` centroids fall outside any single containing `DEPARE` polygon... digitization
  differences"). Where a `FAIRWY` polygon and a `DRGARE` polygon covering roughly the same
  channel disagree at their edges, `unary_union`ing them (as `_build_fairways_unified`
  already does) could produce a slightly jagged combined boundary at exactly the kind of
  seam this mechanism is trying to smooth. Not expected to be a *safety* issue (§6.2's
  checks still apply), but could reduce the achieved simplification locally; worth a
  real-build measurement, not assumed.
- **Interaction with axis-dedup where both a fairway polygon and an `inland_waterways`
  line cover the same water** (a `RECTRC CATTRK=1` harbour approach with a `FAIRWY`
  polygon around it, say): this mechanism runs at the `poly_m` stage, strictly before
  axis-dedup's raster-level carve (§4.3.2 operates on the mask, after rasterization,
  later in the same function) — no ordering conflict, this mechanism's output simply
  becomes axis-dedup's input unchanged in kind. Not expected to need a real-build check
  beyond the standard combined-flags note `SPEC-GRAPH-DENSITY.md` §7 already carries for
  `--inland-densify-max-segment-m` + `--axis-dedup-cap` together — re-run the standard
  gates when enabling both rather than assuming independence.
- **Depth attribution**: like axis-dedup (`SPEC-GRAPH-DENSITY.md` §7's own risk note),
  this mechanism must not change which `DEPARE`/`DRGARE` depth an edge samples — it only
  moves *where along the boundary* vertices survive, not the water polygon's extent or
  the depth-sampling logic (`_edge_attr_worker` samples depth independently, after
  topology exists). No expected interaction, but worth confirming in the verification
  build (§9) rather than assumed.

## 9. Verification plan

Same five-gate discipline as every mechanism in `SPEC-GRAPH-DENSITY.md` (§4.3.3, §6.3.4,
§8.5, §9.4) — **none of this has been run against a real build**; this is a design
document only.

- `--fairway-boundary-simplify-m 0.0` must reproduce a build byte-for-byte against the
  same input with the flag omitted (gate 1, holds by construction per §7, confirm anyway).
- `crosses_land` stays 0.
- Largest-component connectivity measured by edge length, not node count (§6.1's own
  established method) — must not regress.
- POI-pair reachability — zero real place-pairs may lose routability.
- Node/edge counts measured against the **build's own pre-change baseline** — not just
  the immediately-prior build (§6.5's hard-won lesson: five prior rounds in this file each
  verified only against their own predecessor and compounded into a net regression before
  anyone checked against the original baseline).
- Rebuild the MD region with a real, nonzero value; measure the actual node-count effect
  at the named fairway locations in §2.4 (`Bonum Creek Channel`, `Cobb Island Channel`,
  `Neale Sound Channel`, `Saint Catherine Sound Upper Entrance Channel`, `Monroe Creek
  Channel`, `Saint Patrick Creek Channel`, `Cuckold Creek Channel`, etc.) specifically —
  §2.4's correction found `Cobb Island Channel`/`Neale Sound Channel` sit directly at
  Coltons Point itself, so this now includes the original motivating location too, not
  a disjoint set of "other" locations.
- Report `fairway_boundary_simplify_stats` (pieces processed, seam-validation rejections,
  vertices before/after in the fairway-covered sub-region) in the `data/BUILD_LOG.md`
  entry for whichever build first exercises this, matching this repo's established
  build-log convention.
- Given §8.6's own lesson (two mechanisms shipped, tested clean, and neither fixed the
  motivating case they were built for) — **do not assume this mechanism will visibly
  change the original Coltons Point screenshot just because real fairway coverage now
  confirmed sits there (§2.4's correction).** `SPEC-GRAPH-DENSITY.md` §9 already fixed
  the specific boundary-noise mechanism driving that screenshot's density; whether this
  spec's mechanism (once its own §5.3 design flaw is resolved) additionally moves that
  exact location, versus only the other named fairway locations, is itself an open
  question a real rebuild must answer — do not assume either way from static analysis.

## 10. Open questions for the next session

1. **Narrowed by §2.4's correction, not fully resolved**: is the "Potomac River Channel"
   label the user saw near Coltons Point referring informally to the general reach
   (plausible, given `Cobb Island Channel`/`Neale Sound Channel` are the actual named
   `FAIRWY`/`DRGARE` vector features this pipeline has right at that location), or a
   distinct, more specifically-named feature/label this pipeline's current extraction is
   still missing? Static investigation of this repo's already-extracted GeoJSON cannot
   fully resolve this — it would need either (a) `ogrinfo`/GDAL inspection of the *raw*
   NOAA S-57 `.000` cell(s) covering that exact reach, or (b) directly asking the user
   which chart/rendering produced the exact labels they saw. Lower priority than before
   the correction, since real fairway coverage IS now confirmed at that location either
   way.
2. **`fairway_boundary_buffer_m` needs a real measurement before shipping a nonzero
   default**, the same way `axis_dedup_cap_m`/`fraction`/`floor` were tuned against 23,614
   real measured Zeeland nodes before §4.3 shipped (§4.3.1). This document proposes the
   flag and its validation but does not propose a tuned value — that needs an equivalent
   study (distance from real skeleton nodes to the nearest fairway/dredged polygon edge,
   in water genuinely the same channel vs. genuinely different, mirroring §4.3.1's
   width-band table) against real MD or NL data before recommending anything but `0.0`.
3. **The split+reunion approach in §6.1 is confirmed broken for the wholly-interior-
   fairway case, not merely seam-risky (§6.1's own correction note, found in review
   before implementation).** When a fairway doesn't touch `poly_m`'s own exterior ring,
   the reunion yields zero real boundary-vertex reduction, and any observed effect is a
   seam artifact rather than legitimate simplification. This elevates the single-pass,
   vertex-weighted Douglas-Peucker alternative (simplify the whole ring at once, with a
   *per-vertex* tolerance that varies by fairway proximity, rather than independently
   simplifying and reuniting two sub-polygons) from "worth prototyping for engineering-
   cost reasons" to the design the next session should very likely start with — it has
   no seam hazard AND correctly handles wholly-interior fairway coverage by construction
   (every exterior-ring vertex is considered directly, no split). Prototype it against a
   real noisy piece (e.g. the same Coltons Point piece §9.2 already extracted) before
   writing any CLI flag or shipping code; do not implement §6.1's split+reunion sketch
   as a starting point.
4. **Should `DRGARE`'s own `DRVAL1` (maintained depth) factor into which fairway
   candidates get the stronger tolerance?** This document treats `fairways_unified`
   uniformly, matching every other consumer of that layer (§3). A `DRGARE` polygon with a
   real maintained depth is arguably a stronger authority signal than a `FAIRWY` polygon
   with none — not investigated here, flagged for whoever tunes §9's open question 2.
5. **Does §2.2's polygon-vs-coastline vertex-density gap hold up outside this one MD
   sample?** Re-run §2.2's aspect-ratio/vertex-density measurement against Zeeland's own
   `fairways_polygons.geojson` and at least one other US region before generalizing the
   "fairway polygons are reliably coarser than natural coastline" claim beyond this single
   dataset.
6. **Minor, low-priority**: reconcile `SPEC-FAIRWAY-HARMONIZATION.md` §2's stated `DRGARE`
   `OBJL=53` against this document's directly-observed `OBJL=46` (§2.1) — 53 is `DRYDOC`
   in the S-57 catalogue, not `DRGARE`. Does not affect any code (`enc_preprocessor.py`
   maps by the `DRGARE` string key, not by numeric `OBJL`), so this is a documentation-only
   correction.
