# Spec: Channel Axes — "Prefer the marked channel" from polygons *and* buoys

Status: **Implemented and merged** (status refreshed 2026-09-21). Core feature merged as PR #24
(implemented 2026-09-09 on branch `channel-axes`): `derive_channel_axes.py`, new
`enc_preprocessor.py` layers, `--channel-axes` in `nautical_routing_pipeline.py` and
`build_region.sh`. The buoy-chain dead-end stitch (§10, v1 to v3) was merged in PR #26.
Verification builds: see §8 and `data/BUILD_LOG.md`.
Deployment caveat: `channel_axis_deadend_stitch_m` (`--channel-axis-deadend-stitch-m`)
**defaults to `0.0` (disabled)** and `build_region.sh` does not set it by default (it now has an
opt-in `--channel-axis-deadend-stitch-m <m>`, which requires `--channel-axes`); the deployed builds
(MD #49, Zeeland #50) pass `--channel-axis-deadend-stitch-m 1500.0` explicitly on the command
line (see BUILD_LOG). A build without that flag has no dead-end stitching.
Still open (not built): `_extract_buoyage_direction` (stub); see `docs/ROADMAP.md`.
Spatial-chaining fallback for unparseable buoy names (§9) is implemented.
Supersedes the mechanism sketched in `docs/archive/SPEC-FAIRWAY-DEDUP.md` (moved to docs/archive/ 2026-09-21) (whose measurements
of fairway/skeleton duplication remain valid and are reused here).
Complements: `SPEC-GRAPH-DENSITY.md` §4.3/§6.3 (axis-dedup, carve-reconnect),
`SPEC-FAIRWAY-HARMONIZATION.md` (FAIRWY + DRGARE), `SPEC-RECOMMENDED-TRACK.md`.

## 1. Requirement

Where an authoritative marked channel exists, routes should follow it, preferring it
over the pipeline's own independently generated medial-axis skeleton — for the US
as well as for other countries (`docs/archive/SPEC-FAIRWAY-DEDUP.md` §1, Coltons Point / Potomac
screenshot: "Potomac River Channel Buoy 13/14/14A/15").

## 2. What the charts actually contain (measured)

Probed directly on raw S-57 cells: 60 NOAA cells (`data/raw/us-east-coast/MD`) and
314 RWS IENC cells (`signalk-routeiq/data/NL`, incl. Waddenzee).

| finding | MD (NOAA, IALA B) | NL (RWS IENC, IALA A) |
|---|---|---|
| unique lateral marks (`BOYLAT` + `BCNLAT`) | 1,833 | 2,350 |
| `OBJNAM` parses to `<channel> <number>` | 96 % | 87 % |
| number parity ↔ `CATLAM` hand consistent | 100 % (odd = port) | 98 % (even = port) |
| channels with ≥ 3 marks | 189 | 102 |
| … with **no** `FAIRWY`/`DRGARE` polygon within 200 m of any mark | **116 (61 %)** | 80 (also no `wtwaxs`/`RECTRC`/`NAVLNE`) |
| marks with an opposite-hand partner within 400 m | 48 % | 48 % |
| Voronoi centerline of a `FAIRWY`/`DRGARE` polygon succeeds | 372 / 375 | — |

Decisive consequences:

1. **The Potomac main channel at Coltons Point has no polygon at all.** Its marks
   exist only as `BOYLAT` ("Potomac River Channel Buoy 14A … 28", `CATLAM`, `COLOUR`,
   `OBJNAM`). The `FAIRWY`/`DRGARE` there (Cobb Island, Neale Sound, Kettle Bottom
   Shoal) are side channels and a dredged cut. A polygon-only design can never satisfy
   the motivating case, and 61 % of marked channels in the region behave the same way.
2. Buoy-only channels are not a US quirk: Wadden Sea gullies ("ZOL 5") and the
   Oosterschelde ("O 12") are buoy-only in the Dutch data too.
3. Half of all marks are single-sided. "Pair port with starboard and take the midpoint"
   is not a method on its own; the charted water shape and depth bands must constrain
   the derived line (a naive single-sided offset crossed land in Neale Sound).
4. The pipeline ingested no buoy/beacon layer at all (`enc_preprocessor.py`
   `layer_mapping`; `_extract_buoyage_direction` is a stub).
5. Nothing ingested `FAIRWY`/`DRGARE` as topology. The only "authoritative axis" path
   is `inland_waterways_lines.geojson` → `_build_inland_network` + axis-dedup carve +
   reconnect + `cost_factor 0.8` + DEPARE/DRGARE depth sampling — proven on Zeeland
   `wtwaxs`. Any line handed to that path gets the full treatment for free.

## 3. Architecture: a derivation step between preprocessing and the graph build

```
enc_preprocessor.py  ──► layers (+ lateral_marks_points, safe_water_marks_points, nav_systems_polygons)
clip_pilot_data.py   ──► (clips the new layers too)
derive_channel_axes.py ──► channel_axes_lines.geojson, channel_axes_rejected.geojson, channel_axes_stats.json
nautical_routing_pipeline.py --channel-axes ──► axes appended to inland_waterways before densify
        (unchanged: _build_inland_network, axis-dedup, waterway-crossing connectors, Pass 0d,
         cost_factor 0.8, depth sampling, _sanity_check_no_land_crossings)
```

Why a separate step: pure geometry in/out, testable with synthetic fixtures, output
inspectable per channel in QGIS (`channel_axes_rejected.geojson` carries a `reason`
per dropped candidate), and source-agnostic — NOAA, RWS, USACE IENC, later OSM
seamarks or the USACE National Channel Framework all reduce to "line", "polygon" or
"ordered marks". The main pipeline only sees a line layer, the primitive its whole
"prefer the authoritative axis" machinery is built for (`docs/archive/SPEC-FAIRWAY-DEDUP.md` §4.2's
objection — carving water near a polygon with nothing replacing it — disappears
because the axis *is* ingested as topology).

## 4. Three source tiers

| tier | input | method | confidence |
|---|---|---|---|
| 1 | `inland_waterways_lines.geojson` (wtwaxs / RECTRC / NAVLNE) | already ingested by the pipeline; used only as *coverage* so lower tiers never duplicate it | — |
| 2 | `fairways_polygons` ∪ `dredged_areas_polygons` | touching/overlapping polygons unioned into channel components; Voronoi medial axis of the densified boundary; spurs pruned (all short junction-ending branches of a sweep removed together); leaf ends extended to the polygon boundary; branched components yield one polyline per branch sharing exact junction vertices | 0.9 (0.8 for a round polygon that marks run through; round polygons without marks are skipped) |
| 3 | `lateral_marks_points` (+ `safe_water_marks_points`) | §5 | 0.5–0.8 |

Cross-tier dedup: a lower-tier axis is cut wherever it lies within
`clamp(½ channel width, 50 m, 250 m)` of a higher-tier axis; remaining parts shorter
than 200 m are dropped; a cut end is snapped to the nearest vertex of the higher-tier
line (within 2× that distance, not across land) so the two share an exact node in
the graph (`_build_inland_network` joins lines only at identical coordinates).

## 5. Tier 3: from an ordered mark chain to a channel axis

1. **Group and order.** `OBJNAM` is parsed with a US pattern
   (`<channel> [Lighted] [Ice] <Buoy|Daybeacon|Light|…> <num>[suffix]`) and a European
   one (`<abbrev> <num>[suffix]`); trailing "Entrance/Approach/Junction" is folded into
   the bare channel name. Same-named marks are clustered spatially (single linkage,
   8 km) because names recur in distant places (two Wicomico Rivers). Marks are
   de-duplicated per (channel, number, suffix) within 60 m, keeping the largest-scale
   cell's copy (usage-band overlaps, seasonal "Ice" buoys). Order = (number, suffix).
2. **Direction and hand.** Direction of buoyage = increasing number (IALA A and B
   alike). `CATLAM` 1/2 gives the hand relative to it; `COLOUR` is never used for the
   hand (it flips between systems). `CATLAM` 3/4 and safe-water marks are on-axis
   points. Parity ↔ hand mismatches against the region's dominant convention lower
   confidence.
3. **Centre chain.** Consecutive marks of opposite hand straddle the channel: their
   midpoint is a centre point and their across-chain separation the local width.
   Consecutive marks of the same hand lie on one edge: their midpoint is pushed
   toward the channel by the locally estimated half width (nearest opposite pairs,
   else chain median, else ¼ of the median mark spacing, 50–500 m). Directions come
   from the centre sequence itself, so an alternating-side chain with marks 3–4 km
   apart in a wide river (the Potomac) yields a straight centre line, not a zigzag.
4. **Split** at gaps > 8 km or turns > 120°.
5. **Corridor.** Buffer of the centre polyline (radius 1.5 × local half width + 50 m,
   clamped 100–1500 m) ∩ charted water minus charted land, minus a thin *wall* on the
   shoal side of every lateral mark (from the mark outward, perpendicular to the local
   direction, past the corridor edge). The wall forces the axis past each mark on its
   correct side.
6. **Depth tightening** (default on, `--no-depth-tighten`): the corridor is restricted
   to DEPARE water at least as deep as the marks themselves stand in (median `DRVAL1`
   at the marks − 0.5 m) whenever that still connects the chain's ends — "the deepest
   continuous water inside the buoyed corridor".
7. **Centerline.** Voronoi graph of the corridor, largest component, shortest path
   between the graph nodes nearest the first and last centre point, with edge weights
   length × (1 + distance-to-centre-chain / r) so the axis hugs the marks where the
   water allows. In wide water this reproduces the buoy line; in a narrow creek it
   follows the water between the beacons.
8. **Validation.** Water-only parts (a dredged creek that a coarser cell covers with
   land keeps its water fragments), no land crossing > 5 m, median DEPARE depth along
   the axis not shallower than at the marks (where bands exist; otherwise −0.1
   confidence), ≥ 3 marks, ≥ 200 m. Failures go to the rejected layer with a reason.

Confidence (tier 3): 0.6 base, +0.1 if ≥ half the consecutive pairs are opposite-hand,
+0.1 for ≥ 8 marks, −0.1 for parity mismatches, −0.1 if depth could not be checked.

Output properties: `axis_kind`, `tier`, `channel_name`, `confidence`, `length_m`,
`direction_deg`, `corridor_width_m`, `depth_median_m`, `depth_checked`,
`dedup_removed_m`, `n_marks`, `n_gates`, `drval1` (tier 2), `src_objl`, `src_cscl`,
`parity_mismatch`, `depth_tightened`.

## 6. Pipeline integration (`--channel-axes`, default off, byte-identical otherwise)

- `data_sources["channel_axes"]` → `channel_axes_lines.geojson` (optional file).
- `ClassificationConfig.use_channel_axes` / `channel_axes_min_confidence` (default
  0.5; validated finite, 0..1). CLI `--channel-axes`, `--channel-axes-min-confidence`.
- `parse_shapefiles` → `_merge_channel_axes`: filtered axes are appended to
  `inland_waterways` *before* `_densify_inland_waterways`, so
  `--inland-densify-max-segment-m` bounds their vertex spacing too (Pass 0d lateral
  connectors and the §6.4 hub-fan-out fix key on it). Rows carry `layer_key`.
- `_default_data_sources(include_channel_axes)` appends a `channel_axes` row
  (`source_type='derived'`) last, only when enabled; `_build_inland_network` stamps
  merged axes with that id so DB consumers can tell them from charted `wtwaxs`.
- Lateral connectivity of an axis inside open navmesh (traced): boundary crossings
  and carve-reconnect contribute a handful of joins per line, but Pass 0d
  (`MAX_LOCAL_CONNECTORS_PER_INLAND_NODE = 2`, `INLAND_LOCAL_RADIUS_M = 300`) joins
  every densified axis vertex to up to two navmesh nodes — roughly continuous
  hop-on/off at 120 m spacing.
- Derived axes are **excluded from the navmesh-side axis-dedup carve** by default
  (`_axis_dedup_suppression_mask(exclude_layer_key="channel_axes")`, opt back in
  with `--channel-axes-navmesh-carve`). A triangulated mesh has no generated
  centerline twin to suppress; carving a strip along 900 km of axes only added
  boundary vertices on both sides (run 1 below: +12k navmesh vertices). The
  skeleton-side carve — where the medial-axis twin is real — always applies.
- The derivation simplifies every output axis (`--simplify-m`, default 5 m) so the
  pipeline's `--inland-densify-max-segment-m` sets the vertex spacing; run 1's raw
  Voronoi paths carried a vertex every ~55 m, doubling axis nodes and Pass 0d
  connectors.

## 7. Real-data results, Maryland (`data/geojson/us-east-md-v5`, 125 NOAA cells)

| | count | km |
|---|---|---|
| marks parsed / de-duplicated | 3,579 / 2,157 (101 unparsed names) | |
| tier 2 components → axes | 129 → 282 | 259 |
| tier 3 chains → axes | 318 → 165 | 790 |
| rejected (with reason) | 248 | |

Potomac River Channel: one chain of 24 marks (19 opposite-hand pairs, corridor width
263 m), 22 + 15 km of axis, passing every mark on its correct side (visual check),
handing over to the Kettle Bottom Shoal dredged-cut axis (5.8 km de-duplicated) and
continuing past Coltons Point to buoy 35. Wicomico River (single-sided beacons):
20 km, correct side at every mark. Runtime ~3.5 min single-threaded.

Rejections are dominated by `duplicate_of_higher_tier` (75, by design), `too_few_marks`
(71), `outside_water` (21: fairway polygons that a coarser cell covers with land),
`corridor_disconnected` (16: the walls cut the corridor — i.e. the hand/direction
assumption failed for that chain, which is exactly when we must not guess).

### 7.1 Full MD build, run 1 (raw Voronoi vertices, navmesh carve on) vs. build #34

| | #34 baseline | run 1 |
|---|---|---|
| nodes / edges | 51,519 / 121,168 | 78,246 / 210,240 |
| crosses_land / hubs (>30) / max out-degree | 0 / 0 / 16 | 0 / 0 / 15 |
| largest component | 38,429 nodes, 8,817 km | 66,488 nodes, 13,375 km |
| channel-axis nodes / edges | — | 15,765 / 32,818 |
| navmesh vertices | 3,975 | 15,996 |
| connector edges (no source) | 12,728 | 46,862 (31,938 touch an axis node) |
| Potomac route buoy 13 → 33: share within 100 m of a derived axis | 2 % | **96 %** (cost 42.2 → 25.7) |
| Wicomico 1W → 13W | 15 % | 48 % |

The requirement metric moved as intended; the graph grew by 52 % nodes / 74 % edges
for the three reasons listed in §6 (axis vertex spacing, two lateral connectors per
axis vertex, navmesh carve). Run 2 (`data/BUILD_LOG.md` #36) applies the two
mitigations: 62,309 nodes / 162,482 edges (+21 % / +34 %), navmesh vertices back at
3,807, skeleton nodes flat (39,895 vs 39,932), 12,449 axis nodes, 18,150 Pass 0d
connectors; Potomac route 95 % on-axis, Wicomico 49 %. The remaining growth is the
explicit axis topology itself plus its lateral joins.

### 7.2 Dutch IENC (Zeeland and Wadden clips of `data/geojson/nl-v5`)

Wadden Sea: 2,523 marks parsed → 107 buoy-chain axes (330 km) + 63 polygon axes
(184 km); the tidal gullies have no polygon or charted axis at all, and the derived
chains trace the buoy rows across the whole area (visual check). Zeeland: 67 polygon
axes + 64 chain axes (101 km; 28 further chains dropped as duplicates of charted
`wtwaxs`/fairway axes, as intended); the Westerschelde buoys carry bare numbers
("27", "25 C"), accepted as one proximity-grouped key at reduced confidence.

## 8. Verification (per `SPEC-GRAPH-DENSITY.md`'s gate discipline)

- Unit tests: `tests/test_derive_channel_axes.py` (25, synthetic), `tests/test_channel_axes_ingest.py` (15).
- Gate 1: a build without `--channel-axes` is byte-identical (the layer is loaded, never merged; the `data_sources` row is only added when enabled).
- MD rebuild with `--channel-axes` on the v4 tuning: `crosses_land` 0, connectivity, POI-pair reachability, node/edge counts vs #34, and the requirement-specific metric — share of a Potomac route's length within 100 m of a derived axis — logged in `data/BUILD_LOG.md`.
- Zeeland/Wadden run of the derivation for the "other countries" claim.

## 9. Known limits / follow-ups

- Unparseable names (4 % US, 13 % NL) **IMPLEMENTED**: lateral marks (a usable
  `CATLAM`; safe-water marks are excluded -- no hand/channel evidence) with an
  unparseable `OBJNAM` are grouped under `SPATIAL_KEY` and clustered by spatial
  proximity alone (reusing the existing `cluster_link_m` 8 km single-linkage
  knob; `CATLAM` is used afterward, same as any other chain, for anchor gating
  and wall placement -- it does not partition the clustering itself), ordered
  by nearest-neighbour walk from a PCA-picked start (`order_marks_spatial`),
  and given a lower confidence baseline (-0.2) than the existing bare-number
  fallback (-0.1) since it has neither a parsed channel name nor a bare number
  to order by. Because that walk order has no relation to true buoyage
  direction, `center_chain`/`build_corridor`'s direction-dependent same-hand
  offset and shoal wall are both skipped for `SPATIAL_KEY` chains
  (`reliable_direction=False`) rather than risk guessing the wrong side.
  Deduplication buckets `SPATIAL_KEY` marks by charted name instead of the
  shared placeholder identity, so distinct nearby aids aren't merged.
- A chain whose walls disconnect the corridor is rejected, not repaired.
- `M_NSYS.ORIENT` is extracted but not yet used to cross-check direction of buoyage.
- `_extract_buoyage_direction` could read `direction_deg` from an axis to light up the
  existing `laned` classification.
- USACE National Channel Framework polygons would add a tier-2 source for US federal
  channels without `DRGARE`; USACE IENC (`SPEC-USACE-IENC.md`) adds tier-1 lines.

## 10. Buoy-chain dead ends — IMPLEMENTED

Found via a live user report, not a synthetic test: a real route near the Wicomico
River, MD abandoned a marked fairway partway along it and detoured onto a longer,
shallower path instead. The hovered node in the report's screenshot
(`38.2476°N, -76.8246°W`) turned out to be a real, degree-1 terminal node at the end
of a tier-3 `mark_chain` channel axis — a true graph dead end. Present already in
baseline build #39 (not introduced by builds #43/#44), so this is a longstanding gap,
not a regression.

**Root cause**: `derive_channel_axes.py` derives a buoy-chain axis from the buoy line
alone — it has no idea where the nearest medial-axis skeleton branch ends, so a
derived axis can terminate far from the rest of the network. Measured statewide on
the deployed MD build: 221 such dead ends, median gap to the nearest non-axis node
211m, p90 948m, max 3.4km. A degree-1 node can only ever be entered and backtracked
out of — Dijkstra never routes through one — so the marked channel becomes unusable
for through-routing past that point, and a route that would otherwise follow it has
to detour instead.

Why the *existing* connectivity guarantee (`_ensure_coastal_connectivity` /
`_stitch_component_pieces`) doesn't already catch this: that pass is scoped per
original `coastal_water` polygon component (`.within(component polygon)`) with a
500m `snap_radius_m` tuned for a different problem — reconnecting pieces of the
*same* water body split by width classification. A channel-axis dead end's nearest
neighbour may belong to a different classified piece, or simply sit further than
500m away. `_stitch_component_pieces`'s own docstring records a past attempt to
widen that radius (500m → 700m) making Pass 2's connectivity measurably *worse*
(union-find candidate-pair combinatorics) — ruling out just raising the general
radius as the fix.

**Fix**: `channel_axis_deadend_stitch_m` (`--channel-axis-deadend-stitch-m`, default
`0.0` = disabled, same convention as `skeleton_junction_merge_m`). A new pass,
`_connect_channel_axis_deadends`, runs once after `_ensure_coastal_connectivity`:
finds every node whose only edge is channel_axes-sourced and degree 1, and connects
it to its nearest ELIGIBLE graph node within the configured radius, via a k-nearest
search (`scipy.spatial.cKDTree` in a local UTM projection) gated on BOTH:

1. **Not itself a terminus** (degree 1, any source) — connecting two dead ends
   together helps neither; each still has exactly one way out, just a longer
   shared one.
2. **"Anchored"** — touches at least one non-channel_axes edge, i.e. already part
   of the real skeleton/inland network, not just another orphaned buoy-chain
   fragment. A nearby buoy string that is itself a real, connected through
   channel is a perfectly good bridge target; this only excludes fragments
   isolated the same way the dead end being fixed already is.

A candidate also still needs a straight connector that doesn't cross `land` or
charted drying/intertidal terrain (`_crosses_land` — the same safety bar every
other connectivity pass in this file uses). Deliberately a separate, narrowly-
scoped pass rather than a change to the general stitcher, so it can afford a much
larger radius without reintroducing the Pass 2 regression above. Unit tests:
`tests/test_channel_axis_deadend_stitch.py` (16, synthetic).

### 10.1 v1 shipped without rule 2 and was a no-op — caught by a second live user report

The first version connected each dead end to the plain nearest OTHER node with no
eligibility check at all (rule 1 didn't exist yet either, though it wouldn't have
mattered here). Built, gated, visually confirmed, and deployed as builds #45 (MD)
and #46 (Zeeland) — all of which looked right: gates passed, `render_diff()` showed
new edges landing at buoy-marked locations, the reported node went from degree 1 to
degree 2. **None of that proved the fix actually helped a real route.**

The user came back skeptical, with a second screenshot: the same reported fairway's
end was clearly within 1500m of a real skeleton node, yet the route was still
6.7nmi instead of the expected ~1nmi. Checking directly settled it: the
shortest-path Dijkstra **cost between the reported start/dest points was measured
byte-identical (11759.4) on build #45 and on baseline #43** — the 176 connections
v1 made changed nothing for this route. Root cause: the node v1's search picked
(137.7m away) was NOT itself a terminus (degree 2) — a "don't connect two dead
ends" rule alone would still have accepted it — but both its edges were also
channel_axes-sourced, i.e. it was a through-point of a *different, equally
isolated* buoy-chain fragment in the same local tangle. Both ends were already in
the same connected component the long way round (86% of MD's edge length is one
component; "already connected, eventually" is a very weak signal on this graph),
so the new edge was a real edge that added zero routing value. The actually-useful
real skeleton node was right there, 948m away — just farther than 137.7m, so v1's
plain nearest-node search never reached it.

Rule 2 (anchored) is what closes this: it requires the candidate to touch a
non-channel_axes edge, which the 137.7m fragment-mate does not and the 948m real
skeleton node does. Rule 1 (non-terminus) is kept too, for the symmetric case a
live user report didn't happen to hit here: a real but degree-1 charted stub is
"anchored" by a plain edge-source check yet is itself just as much a dead end as
the one being fixed. `tests/test_channel_axis_deadend_stitch.py`'s
`TestPrefersAnchoredCandidate` class reproduces the exact real-world shape of both
failure modes as regression coverage.

**Real-build verification, MD v2** (`data/us_east_md_deadend_stitch_v2.sqlite`,
same 1500m radius): 220 dead ends found, 164 connected, 1409 rejected for crossing
land, 56 gave up. Fewer connections than v1's 176 (expected — many of v1's were the
exact useless kind this fix now refuses to make), but this time the reported route
actually improves: Dijkstra cost `11759.4 -> 3821.8` (path length 69 hops -> 5), and
the reported node's new edge reaches the real skeleton node 948m away — the same
one v1's search saw and discarded for being farther than a useless 137.7m
alternative. All six `validate.py` gates pass except `counts` (same expected
reason as v1 — see below). `render_diff()` now shows one clean, direct connector
line from the buoy cluster to the nearby skeleton, not just more short local
stitches inside the tangle.

`graph_cleanup/validate.py` gates vs. baseline (build #43):

```
[PASS] crosses_land: 0 -> 0
[PASS] largest_component_by_length: 0.8620 -> 0.8627 (-0.07pp, limit 0.5pp)
[PASS] poi_pair_reachability: 48539 pairs -> 48539, 0 lost
[PASS] poi_snap_drift: 0 POIs snap >50m further than before
[FAIL] counts: nodes 51919 -> 51953 (-0.1%), edges 64808 -> 65002 (-0.3%)
[PASS] hubs: 0 nodes with out-degree > 30, max 15
```

**The `counts` gate "failure" is still expected, not a regression** (unchanged
reasoning from v1): that gate assumes a post-hoc *cleanup* context where a graph
should only ever shrink, not a generation-time fix whose entire point is to add
edges. The node-count delta is the same background pipeline non-determinism v1's
writeup measured (431/465 nodes present only in one build or the other, scattered
statewide, unrelated to any dead-end location) — this pass adds zero new nodes by
construction, only edges between IDs already in `self.graph.nodes`.

**Zeeland got the same corrected fix** (build #48, superseding #46).

### 10.2 One connection per dead end could still miss a bank — angular-sector search, up to 3 connections

User feedback on the deployed v2 fix: a marked channel typically runs down the
middle of open water, so real skeleton usually exists on both banks — connecting
each dead end to only its single nearest eligible candidate risks picking one
bank arbitrarily and never offering the other. Follow-up feedback sharpened this
further: simply taking the 3 nearest-by-distance candidates isn't enough either,
since they can easily all sit on the same bank if it happens to be locally
denser or closer than the other side.

**Fix**: new `channel_axis_deadend_max_connections` (`--channel-axis-deadend-
max-connections`, default `3`). `_connect_channel_axis_deadends` now partitions
the full circle around each dead end into this many equal angular sectors and
takes the nearest eligible candidate (rules 1+2 from §10.1, unchanged) FROM
EACH sector, not just the N nearest overall. This is what actually guarantees
spread: two candidates 10° apart fall in the same sector, so only the nearer of
the two is ever taken, freeing nothing for the farther one just because it's
technically within radius. Unit tests added: `TestAngularSectorSpread`
(connects across multiple sectors when candidates are genuinely spread out;
only the nearer of two same-sector candidates is used even though the farther
one is in-radius and eligible; a `max_connections=2` cap actually caps the
count at 2 even with 4 well-spread candidates available) and
`TestValidateMaxConnections` (positive-integer bounds).

**Real-build verification, MD v3** (`us_east_md_deadend_stitch_v3.sqlite`,
same 1500m radius, `max_connections=3`): same 164/220 dead ends get at least
one connection as v2 (the eligibility rules didn't change), but now averaging
~2.4 connections each (398 total) instead of exactly 1. The reported node's
degree went from v2's 2 to 4 — three new edges at 947.6m, 962.7m, and 1130.2m,
confirmed via `render_diff()` to fan out in three visibly different compass
directions, not cluster in one. **The reported route improved again**: Dijkstra
cost `3821.8 -> 1953.5` (path length 5 hops -> 3 hops) — close to the user's
original "~1nmi" expectation, and roughly 6x better than the original broken
v1/baseline (11759.4). All gates pass except `counts` (same already-documented
reason — an edge-adding fix, not a shrink-only cleanup); `largest_component_by_
length` moved a little more than v2's (-0.24pp vs -0.07pp, still well inside
the 0.5pp limit), consistent with more real connections pulling more of the
graph's edge length into the largest component.

Zeeland got the same fix (build #50). Both v3 builds (#49 MD, #50 Zeeland) are
deployed live in `signalk-routeiq/data`, replacing the v2 builds (#47/#48),
which are kept as `.disabled`, not deleted. Full details and exact numbers:
`data/BUILD_LOG.md`.
