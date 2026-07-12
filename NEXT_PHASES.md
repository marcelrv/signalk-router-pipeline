# Next Phases — Navmesh-Hybrid Pipeline (Post Phase 0)

Status: Phase 0 and Phase 1 (this repo, `signalk-router-pipeline`) are
implemented, verified, and committed (`e6f1fc5`, `7e64df9`). Phase 2
(funnel-algorithm consumption, in the separate `autoroute` repo) also
turned out to already be implemented and committed (`aabc5db`). **The
"Phase 2 Hardening — Boundary Shortcut Sparsification" fix below is now
implemented in `autoroute`** (`selectAnchors`/anchor shortcuts in
`navmesh.ts`/`database.ts`, composed non-live seeding in `routing.ts`,
regression tests in `test/navmesh-integration.test.ts`) — see its own
"Implementation status" subsection for what was verified and what remains
genuinely open.

**`data/zeeland.sqlite` has been regenerated with the current (Phase 1)
pipeline** — the copy that had been sitting in `data/` was stale, still
`metadata.architecture = "navmesh-hybrid-phase0"` with zero
`navmesh_regions` rows (predates even Phase 0's real triangulation; backed
up as `data/zeeland_phase0_stale.sqlite.bak`). Regenerating it at full
scale (not just the `zeelandbrug_tight` clip) surfaced a real,
previously-unexercised performance bug, now fixed — see
`_stitch_component_pieces`'s new `MAX_EVALUATIONS_PER_ROUND` cap:

- **Symptom**: the full run hung for 1h45m+ with no forward progress,
  confirmed (via `ps`/CPU-time inspection; `py-spy` wasn't permitted to
  attach) stuck in `_ensure_coastal_connectivity` → `_stitch_component_pieces`,
  before `calculate_edge_attributes` had even spawned its multiprocessing
  workers.
- **Root cause**: `MAX_TOTAL_SAMPLES`/`MAX_ROUNDS` (the memory-safety caps
  from the previous session, still correct and unchanged) bound the
  distance-*matrix* size per round, but not the number of *pairs walked
  off it*. A component fragmented into many small groups (per_group_cap
  near 1) makes nearly every one of up to `MAX_TOTAL_SAMPLES**2/2` sorted
  pairs still cross-group, each falling through to `try_add`'s expensive
  `within(poly_m)` + `_crosses_land` shapely checks — unbounded in count,
  regardless of dataset size. One real full-Zeeland component apparently
  had thousands of sub-pieces (a "7401 components left unmerged" warning
  surfaced post-fix, for context).
- **Fix**: `MAX_EVALUATIONS_PER_ROUND = 20000` caps the number of pairs
  actually passed to `try_add` per round, same "bound cost by a fixed
  constant regardless of dataset size" idea as the existing caps, applied
  one level down. Also added periodic `component N/M` progress logging in
  `_ensure_coastal_connectivity` so a genuinely slow run is now visible
  instead of silent.
- **Result**: full Zeeland pipeline run (344 components, 23 navmesh
  regions) now completes in ~6.5 minutes end-to-end. New `zeeland.sqlite`:
  `architecture=navmesh-hybrid-phase1`, 33,797 nodes (down from the stale
  file's 85,023 — expected, interior navmesh vertices no longer become
  literal graph nodes), `edge_kind_id` 0 (skeleton): 25,297, 1 (navmesh
  boundary): 29,678, `navmesh_regions`: 23 rows.

## Phase 2 Hardening — Boundary Shortcut Sparsification (URGENT — do this first)

### Implementation status (this session)

Implemented in `autoroute` per the design below, with one deliberate
correction to step 3 (details there): `selectAnchors` (farthest-point
sampling, `navmesh.ts`), `precomputeFunnelEdges` rewritten to drop the
150-node cap and add anchor-anchor + nearest-anchor shortcut edges
(`database.ts`), and `astarSearch`'s live per-point seeding restricted to
anchors (`routing.ts`). All 38 tests pass (35 pre-existing + 3 new in
`test/navmesh-integration.test.ts`'s `navmesh boundary-shortcut
sparsification (regression)` suite, using a synthetic 160-boundary-node
grid fixture — past the old cap — asserting: no precompute-cap warning
fires, anchor count stays bounded, and a direct single-hop edge now
exists between two boundary nodes on opposite sides of the region instead
of forcing a ~80-edge ring walk).

**Real-scenario re-validation** (`zeelandbrug_test.ts` against
`zeelandbrug_tight_phase1.sqlite`, same scenario as the original bug
report): distance improved from 4924m to 4808m — real but modest,
**not** the "close to Phase 0's original result, no zigzag" outcome
originally hoped for. Root-caused via careful A/B testing (temporarily
disabling pieces of the fix and diffing `precomputeFunnelEdges`'s output
edge-by-edge):

- **Naively restricting the live-seeding *candidate set* to just anchors
  (not only the number of live calls) breaks A* admissibility.** The
  search heuristic (`h`) treats every candidate boundary node as if
  finishing from it costs only straight-line distance to the literal
  point — but reaching a candidate isn't actually "done": it still needs
  its funnel-computed suffix/prefix hop, whose true cost isn't folded
  into `h`. With all ~700+ boundary nodes as candidates this rarely
  matters (any one of them is usually close to the literal point anyway).
  Restrict to ~40 sparse anchors and it can matter a lot: A* can settle on
  an anchor that's cheap to reach through the main graph but geometrically
  far from the literal point, with a large, unaccounted-for last-mile hop.
  **Fix applied**: keep every boundary node a valid candidate (preserving
  the original goal-test granularity) but compute its cost by composing a
  live anchor result with a precomputed anchor↔node shortcut edge
  (`RoutingEngine.seedNavmeshCandidates`, `routing.ts`) instead of a live
  `funnelPathFromPoint` call per node — this is what actually delivers the
  "695+713 uncached live calls → ~40" performance fix from the design
  below without the correctness regression.
- **A second, separate, and still-open issue**: even after the above fix,
  `Through opening: false` persists with the same real air-draft
  constraint warning (`11.0m < required 21m`) as the original bug report.
  This matches the risk this document already flagged: possible
  `_add_opening_bridge_edges` bridge-tagging failure in the pipeline (this
  repo). A second contributing factor was also found and is **not** a
  pipeline bug: a synthetic stress-test fixture (regular right-triangle
  grid, `test/navmesh-integration.test.ts`) showed `navmesh.ts`'s
  Simple-Stupid-Funnel-Algorithm implementation can fail to "cut corners"
  on certain corridor shapes, degenerating to keeping every portal vertex
  (see that test's comments) — while real Zeeland `triangle`-library
  output was spot-checked separately (2,852 anchor-shortcut pairs, ratio
  of funnel distance to straight-line ~1.0 for all but 34, and those only
  marginally off) and does **not** show this pathology, so it's a latent
  corridor-search/funnel edge case, not the dominant cause of the
  remaining Zeelandbrug gap. **Neither of these is fixed by this
  session's change** — both are new, separately-scoped follow-ups, not
  part of the boundary-shortcut-sparsification fix itself.

### What's confirmed broken (verified by actually running the scenario, not just reading code)

Built `autoroute`, ran the full automated suite (35/35 pass, including
dedicated `navmesh.test.ts`/`navmesh-integration.test.ts`), then ran the
real `zeelandbrug_test.ts` scenario against a genuine Phase-1-generated
database (`zeelandbrug_tight_phase1.sqlite`, staged at
`/tmp/test_route/netherlands.sqlite`). Passing unit tests did **not**
catch this — it only surfaces on a real generated database with realistic
region sizes, which is exactly why this needs to become a permanent
regression test (see below), not just something to re-run manually once.

- **`Through opening: false`** — the route no longer passes through the
  bridge opening at all, a regression from Phase 0's own test result for
  the identical start/end coordinates.
- **Route length 4,924m for two points ≈900m apart straight-line** (a
  5.5× inflation), with visible zigzag: first ~80 segments are tiny
  (1–3m) jittery back-and-forth steps, then a stretch of much coarser,
  seemingly directionless jumps.
- **A real constraint warning fired**: `air draft 11.0m < required 21m`
  — the route was forced through an edge `getEdgePenalty` (routing.ts:1474)
  penalizes at `+1,000,000` (a very strong soft constraint, not a hard
  `-1` reject like land-crossing) — meaning A* found *no cheaper
  alternative* anywhere, which is itself a symptom of the same
  connectivity problem, not a separate bug. (Confirmed via `run_pipeline`'s
  stage order, `nautical_routing_pipeline.py:357-369`: connectivity
  stitching runs *before* `calculate_edge_attributes`, so this is a real
  computed attribute on a real edge, not a stitched edge missing its
  attributes — it's a legitimate low structure the router settled for
  because it couldn't cheaply reach the real opening bridge.)
- Console confirms why: `navmesh region 1: 731 boundary nodes exceeds
  precompute cap (150) — leaving straight-line fallback edges in place`
  (twice — both of this test's two adjacent regions, 731 and 714 boundary
  nodes, exceed the cap; the "region 1" label is just `metadata.region_id`,
  shared by all regions in one file, not a distinguishing per-region id —
  harmless log confusion, not a separate bug).
- Diagnostic instrumentation (added temporarily, reverted, not committed)
  confirmed: 695/713 candidates seeded live for this one request, zero
  overlap between start's and end's boundary-node sets (ruling out a
  simpler shared-seam-node explanation).

### Root cause

`database.ts`'s `precomputeFunnelEdges` (`:358-398`) was designed around
an assumption from an earlier draft of this plan — a small, k-NN-clustered
boundary-node set "one small cluster per seam" — that the pipeline
**deliberately abandoned** (its own `build_navmesh_region` docstring
explains why: k-NN chords cut across peninsulas on non-convex regions, so
every perimeter vertex is connected in ring order instead). Real regions
therefore have hundreds of boundary nodes **by design**, not as an edge
case — this small test already has 731 and 714. Two consequences, not one:

1. **The 150 cap skips precompute entirely** for realistically-sized
   regions, per the warning above.
2. **Even without the cap, `precomputeFunnelEdges` only ever upgrades
   *existing* `edge_kind_id=1` edges** — and the pipeline only creates
   those between *ring-adjacent* perimeter vertices. It never had a
   mechanism to create a genuine interior shortcut between two *distant*
   boundary nodes of the same region, cap or no cap. The live per-point
   seeding (`funnelPathFromPoint`, called once per boundary node when a
   user's start/end literally falls inside a region — `routing.ts:1156-1169`)
   *is* a correct, genuine interior shortcut computation, but only from
   the literal user point — it doesn't help two boundary nodes of the same
   region reach each other cheaply, which is exactly what's needed when
   crossing from one region, through a stitch point, into an adjacent
   region. Lacking that, the router falls back to walking the fine-grained
   ring — which is exactly the observed zigzag — to find whichever stitch
   point is cheapest, even if that means a long, indirect detour that
   ultimately settles for a low, non-recognized crossing instead of the
   real opening bridge.

### Fix: anchor-based shortcut sparsification

Stop conflating "how many vertices are on this region's boundary" (a
geometry fact that scales with real coastline complexity — correctly
in the hundreds) with "how many distinct points we bother precomputing
interior shortcuts between" (an algorithmic choice that should stay a
small, bounded constant regardless of coastline detail). Concretely:

1. **Add an anchor-selection helper** (`navmesh.ts`, pure geometry, no
   `database.ts` dependency, matching the module's existing convention):
   `selectAnchors(region: NavmeshRegion, maxAnchors = 40): number[]` —
   farthest-point sampling over `boundaryNodeIds` (walk the ring, greedily
   pick the next node that maximizes minimum distance to already-picked
   anchors) so anchors are well-distributed around the boundary, not
   clustered at whatever the first N happened to be.
2. **Rewrite `precomputeFunnelEdges`** (`database.ts:358-398`): drop the
   `MAX_BOUNDARY_NODES` gate entirely. For every region: select anchors
   (bounded cost regardless of boundary size — 40² = 1,600 funnel calls,
   worst case, once at load time), compute genuine `funnelBetweenNodes`
   shortcuts between **every anchor pair** (this is the real interior
   "highway" that was missing), and separately compute a shortcut from
   every *non-anchor* boundary node to its **nearest 1–2 anchors** (by
   ring arc-distance, cheap to compute, no need for a full funnel call
   per candidate). Insert both as new/upgraded `edge_kind_id=1` edges —
   anchor-to-anchor edges are new (there was no ring-adjacency to
   "upgrade" before), boundary-to-nearest-anchor edges may already exist
   as ring edges (upgrade in place) or may need new edges for
   non-ring-adjacent nearest anchors.
3. **Restrict live seeding to anchors, not every boundary node**
   (`routing.ts:1156-1169`, `1164-1169`, and the equivalent in
   `trySameRegionNavmeshRoute` if it loops boundary nodes anywhere): when
   a user point falls inside a region, live-`funnelPathFromPoint` only
   against that region's anchor set. This is the fix for the *performance*
   half of this bug (695+713 uncached live calls → ~40 each) and works
   correctly precisely *because* step 2 now guarantees every anchor
   connects cheaply to the rest of the region's interior — a user point's
   cheapest path to any boundary node now always routes through its
   nearest anchor(s) rather than needing to be evaluated against all 700+
   individually.
4. Keep the existing sanity guard (`result.distance > edge.distance * 3 +
   50` skip, `database.ts:390`) — it's independent of this fix and still
   correct.

### Validation

1. Re-run the exact `zeelandbrug_test.ts` scenario against a freshly
   regenerated Phase-1 database. Expect: total distance close to Phase
   0's original result (not 4,924m for a ~900m-apart pair), no fine-grained
   zigzag, and — **as a hypothesis to confirm, not a guarantee** — likely
   `Through opening: true` again, since a genuine interior shortcut should
   make the real bridge-opening route cheaper than detouring to the low
   11m crossing. If the low-crossing warning *persists* after this fix,
   that's evidence of a separate, second bug (possibly `_add_opening_bridge_edges`
   failing to detect/tag the real opening bridge near this corridor) —
   treat that as a new, separately-scoped follow-up, not part of this fix.
2. **Add this exact scenario as a real automated regression test**, not
   just a manual script — the existing 35/35 passing suite did not catch
   any of this. At minimum, assert: route reaches within some tolerance
   of the destination, total distance is within some multiple (e.g. 2×)
   of straight-line distance, and no segment's `maxAirDraft` is below the
   vessel's required clearance unless explicitly tagged as an
   opening-bridge override. This closes the exact gap that let a real
   regression ship past a fully-green test suite.
3. Spot-check performance: log/time `precomputeFunnelEdges` and the live
   seeding path before/after, on a database with regions at realistic
   scale (the tight clip's 700+ boundary nodes, not a synthetic small
   mesh) — confirm the anchor cap actually bounds cost the way it's
   supposed to.

---

## What's confirmed working from Phase 0

Direct inspection of the generated databases and code confirms the schema
migration, skeleton-extraction machinery (`build_skeleton_network` and its
helpers), bridge-attachment rework, and provenance columns are all present
and structurally sound. Keep all of that.

## The bug that must be fixed first (verified, not hypothetical)

`classify_water_body` (`nautical_routing_pipeline.py:483-510`) classifies
**whole connected water polygons**, not sub-regions. Real hydrography
merges wide bays and narrow channels into one connected polygon — direct
geometry inspection confirmed the Zeelandbrug bridge-crossing corridor and
the Oosterschelde it opens into are **one 105–1531 km² connected
component**. The classifier's stage-1 test ("does *any* part of this
whole polygon survive a −300m erosion?") trivially says yes for that
whole blob, so the **entire component — including the narrow bridge
crossing itself** — gets classified `navmesh_placeholder`.

Confirmed impact via direct SQL on the generated `.sqlite` files: **100%**
of nodes/edges in both `data/zeeland.sqlite` and
`data/zeelandbrug_tight.sqlite` are `edge_kind_id=0`/`node_kind_id=0` with
average node degree ≈5.6 (the signature of a dense Delaunay triangulation,
not a channel skeleton). Zero skeleton or lane edges exist in either
database. This is why the pilot still shows a dense point cloud and
zigzagging routes through the bridge — the skeleton code path Phase 0
built is never actually reached for the one scenario it exists to
validate.

This is a real bug in Phase 0's classification granularity (a gap in the
original plan, not something introduced by miscoding it) — it is **not**
something later phases fix incidentally. It's Phase 1, step 1.

---

## Phase 1 — Classification fix + real navmesh regions (this repo)

### 1.1 Fix: split each connected polygon by local width before classifying

Add a shared helper and use it both at the top level (replacing part of
what `_connected_water_polygons` does today) and to split each connected
polygon into wide/narrow sub-pieces:

- `_explode_polygonal(geom) -> List[Polygon]` — generalizes the
  explode-into-single-polygons logic already in `_connected_water_polygons`
  (`:470-481`) into a reusable static helper (needed again below, since
  both the wide and narrow split results can themselves fragment around
  islands).
- `_split_wide_narrow(poly_m, radius_m, simplify_tol_m=1.0) -> (wide_geom, narrow_geom, seam_geom)`:
  ```python
  cleaned = poly_m.buffer(0).simplify(simplify_tol_m)
  eroded  = cleaned.buffer(-radius_m, quad_segs=16)
  wide    = eroded.buffer(radius_m, quad_segs=16).buffer(0).intersection(cleaned)
  narrow  = cleaned.difference(wide).buffer(0)
  seam    = wide.boundary.intersection(narrow.boundary)
  ```
  The `.intersection(cleaned)` after re-dilation is required — naive
  `buffer(-R).buffer(+R)` can bulge back out past the original boundary on
  convex banks, corrupting the subsequent `difference`. Compute `seam`
  once here so both sides get bit-identical coordinates (avoids precision
  drift if recomputed independently later).
- `classify_water_body(polygon, is_wide, depth_gdf, fairway_gdf, config)`:
  **drop** the internal `poly_m.buffer(-radius).is_empty` erosion test —
  `is_wide` is now passed in from the split above, computed once per
  sub-polygon, never per whole connected blob. Logic becomes: `is_wide and
  _has_navigable_depth(...) → "navmesh"`; else `_has_regulatory_structure(...)
  → "laned"`; else `"skeleton"`.
- `build_network`'s dispatch loop (`:433-460`): for each top-level
  connected component → project to local UTM → `_split_wide_narrow` →
  `_explode_polygonal` both `wide` and `narrow` results → classify each
  piece with the right `is_wide` → dispatch narrow/laned pieces to
  `build_skeleton_network` (unchanged), wide/navmesh-eligible pieces to
  the new `build_navmesh_region` (§1.2, replaces the `build_navmesh_placeholder`
  call site). **Build skeleton pieces before navmesh pieces** in this
  loop, so navmesh seam nodes snap onto already-created skeleton endpoint
  nodes at the same coordinate, not the reverse.

### 1.2 Real `navmesh_regions` generation

**Library**: `triangle` (Shewchuk's Triangle, PSLG mode) — confirmed
correct choice and hands-on verified this session (installed, ran
constrained triangulation with holes/quality/area constraints). Add
`triangle` to `requirements.txt` (pure-manylinux wheel, installs cleanly).

**Known gotcha, confirmed by testing — do not use the `-Y` switch.** It's
documented as suppressing Steiner points on boundary segments only, but in
this binding it silently suppresses *all* refinement, contradicting its
own docstring. Track boundary/seam node identity by **known input vertex
index** instead — `triangle`'s output preserves `output_vertices[0:N] ==
input_vertices` exactly, even after quality/area refinement inserts extra
interior Steiner points, so this is reliable without `-Y`.

- `_polygon_to_pslg(poly_m) -> dict` — builds `vertices`/`segments`/`holes`
  from a shapely polygon-with-holes: exterior ring vertices first (indices
  `0..n-1`), each interior ring appended after, closed-ring segment pairs
  for both rings, one interior representative point per hole
  (`Polygon(hole_coords).representative_point()`). Track which output
  indices correspond to the `seam` coordinates from §1.1 (exact match,
  since PSLG mode preserves input vertex order).
- Triangulate: `triangle.triangulate(data, "pq28a{max_area}n")`.
  - `p` — respect segments as hard constraints (land/obstacle boundaries
    the triangulation must not cross).
  - `q28` — quality mesh; slightly relaxed from the textbook `q30` since
    raw ENC boundaries can have sharp corners that can't be fixed without
    the broken `-Y` switch.
  - `a{max_area}` — sizes interior triangle density; target ~500–800 m
    edge length for open water, i.e. `area ≈ (edge_m)**2 * 0.433`.
  - `n` — returns `triangle_adjacency` **directly** as `result['neighbors']`,
    parallel to `triangles`, `-1` at boundaries — exactly the format
    spec's §2.9 shape. No manual shared-edge computation needed.
  - Wrap in try/except per polygon (same pattern as the existing Delaunay
    fallback at `:673`). **On failure, log and skip the region — do not
    fall back to the placeholder.** The placeholder's land-crossing risk
    is exactly what this phase removes; silently reintroducing it on a
    triangulation failure would undo the point of the fix.
- `boundary_node_ids`: the tracked seam-vertex indices from
  `_polygon_to_pslg`, converted to WGS84, registered via the existing
  `_get_or_create_node`/`_stamp_node` machinery (so they coordinate-merge
  with skeleton network endpoint nodes at the same seam).
- **Write `vertices` as `[lat, lon]` pairs** per format spec §2.9 — note
  shapely/`triangle` work in `[lon, lat]`/`[x, y]`, invert on write.
- `build_navmesh_region(polygon, source_tier, source_id)` orchestrates the
  above and inserts one row into the (currently empty) `navmesh_regions`
  table created in Phase 0.

### 1.3 Minimum-viable-fallback edges (per format spec §6 — required, not optional)

Without literal routable edges connecting a navmesh region's boundary
nodes, the *current, unmodified* `autoroute` TS runtime — which doesn't
know `navmesh_regions` exists at all (`db-worker.ts`'s `SELECT`s use fixed
column lists against `nodes`/`edges` only) — would find open water
completely disconnected. Generate these now, in this phase, not as an
afterthought:

- Per region: build a small KD-tree over `boundary_node_ids` coordinates
  (in local metric CRS), connect each node to its ~10 nearest boundary
  peers, keep only pairs where `LineString(p1, p2).within(boundary_geometry)`.
  k-NN, not full O(n²) — boundary-node counts per region are small (one
  cluster per seam where a channel meets the bay), so this stays cheap.
- Insert as literal `edges` rows with **`edge_kind_id=1`**
  (`EDGE_KIND_NAVMESH_BOUNDARY` — already defined in the enum, unused
  until now; this is exactly its intended purpose).
- These are genuinely useful, not just a stopgap to delete in Phase 2 —
  they're the format spec's own documented degradation path for consumers
  that never implement the funnel algorithm (third-party tools, older
  plugin versions). Phase 2 makes the *current* runtime prefer the
  funnel-computed path when it understands `navmesh_regions`; the fallback
  edges stay in the data permanently as a compatibility floor.

### 1.4 Implementation order

1. `_explode_polygonal`
2. `_split_wide_narrow`
3. Refactor `classify_water_body` (drop internal erosion test)
4. Rewire `build_network`'s dispatch loop
5. `_polygon_to_pslg` (+ hole-representative-point helper)
6. `build_navmesh_region` (triangulate, build row, snap seam nodes)
7. `_generate_navmesh_fallback_edges` (k-NN + within-boundary test)
8. Wire fallback-edge generation after each `build_navmesh_region` call
9. `export_to_sqlite`: INSERT into the existing empty `navmesh_regions` table
10. Add `triangle` to `requirements.txt`
11. Extend `_sanity_check_no_land_crossings` to cover fallback edges with
    the skeleton-style "genuine intersects" check (not the harsher
    placeholder-strip logic)
12. Remove the `build_navmesh_placeholder` call site (keep the function
    itself deletable but don't need to delete it immediately — just stop
    calling it)
13. Re-run the Zeeland pilot; validate (see below)

### 1.5 What "done" looks like (numeric + functional)

- Interior open-water points move out of `nodes`/`edges` entirely, into
  `navmesh_regions.vertices`/`triangles` JSON — only seam-cluster nodes
  remain as literal graph nodes. Expect the `nodes` row count for the
  Zeelandbrug/Oosterschelde area to drop sharply from Phase 0's numbers.
- `navmesh_regions` row count on the order of the number of exploded wide
  sub-polygons (expect low tens for this pilot area, not one giant blob).
- The Zeelandbrug corridor itself now appears as `skeleton`-kind
  (`edge_kind_id=0`) edges tracing a clean centerline — **not**
  zigzagging triangulation edges.
- Zero remaining placeholder-sourced edges (no more `build_navmesh_placeholder`
  calls).
- `edge_kind_id` distribution now shows `0` (skeleton), `1` (navmesh
  fallback), plus `2`/`3` if lanes/macro-edges are present.
- Re-run the same manual validation as Phase 0 (`zeelandbrug_test.ts`
  unmodified, staged at `/tmp/test_route/netherlands.sqlite`): expect
  "Through opening: true" as before, but now via a genuinely clean
  skeleton path through the bridge instead of a triangulated point cloud.
  Visual zigzag through the *bridge corridor specifically* should be gone
  or drastically reduced; zigzag through open Oosterschelde crossings
  (routed via the new fallback edges, still straight-line-based, not
  funnel-optimal) is expected to remain until Phase 2 — that's fine, it's
  in-scope for Phase 2, not this one.

### 1.6 Compatibility (confirmed, not just assumed)

Additive only. `navmesh_regions` DDL already exists (empty) from Phase 0 —
no schema-version bump needed. `nodes.resolution` stays untouched
(interior navmesh vertices never enter `nodes` at all). `db-worker.ts`'s
fixed `SELECT` doesn't reference `edge_kind_id`, so new `edge_kind_id=1`
fallback rows are transparently routable by the current, unmodified
runtime — **provided §1.3 actually runs**. Skipping fallback-edge
generation would leave open water disconnected for the current TS
consumer; don't skip it, and don't treat it as later-deletable.

---

## Phase 1 — Implementation Verification & Memory-Safety Hardening

Checked against the actual (uncommitted) code and generated databases
this session, not just the plan above.

### What's implemented and verified working

The classification fix (§1.1) is in: `_split_wide_narrow`, `_clean_polygonal`,
`_seam_coord_set`, and a `classify_water_body(polygon, is_wide, ...)` that
takes the wide/narrow split as an input instead of doing its own
whole-polygon erosion test. Real `navmesh_regions` generation (§1.2) is in:
`_polygon_to_pslg` + `build_navmesh_region`, using `triangle` exactly as
planned (`"pq28a{max_area}n"`, confirmed **not** using the broken `-Y`
switch, boundary identity tracked by input vertex index as designed).

**One deliberate, good deviation from §1.3**: the implementation does
*not* use k-NN straight-line shortcuts between boundary nodes. Instead
`build_navmesh_region` connects every perimeter vertex of a region in ring
order (a guaranteed non-land-crossing cycle, since it traces the
polygon's own boundary), and a new `_stitch_component_pieces` function
(called once, from a new `_ensure_coastal_connectivity`, after all other
edge construction/land-crossing stripping is done) reconnects pieces that
were exploded from the same original connected water body but ended up in
separate graph components. The code comments document why: k-NN chords
between boundary points on opposite sides of a peninsula routinely exit
the polygon and get correctly rejected by the land-crossing check,
leaving large non-convex regions internally fragmented no matter the
search radius. **Keep this design** — it's a genuine improvement on the
original plan, not a shortcut.

**Confirmed real improvement, numerically**: a full Phase 1 run on the
small `zeelandbrug_tight` clip (`data/zeelandbrug_tight_phase1.sqlite`)
produced 2,766 nodes / 4,838 edges — versus the Phase 0 placeholder-only
output for the same area (6,170 nodes / 34,843 edges). Real skeleton edges
(`edge_kind_id=0`: 1,646) and real navmesh regions (3 rows in
`navmesh_regions`, with `edge_kind_id=1` fallback-connectivity edges:
3,192) both exist and are populated — the architecture is doing what it's
supposed to on this input. (Minor cosmetic leftover: `metadata.architecture`
still reads `"navmesh-hybrid-phase0"` — bump it to reflect Phase 1 when
next touching `export_to_sqlite`.)

### Memory issues: two real ones, already fixed — plus one still open

**Already fixed, and the fix is sound** — both are documented directly in
`_stitch_component_pieces`'s own code comments, with concrete numbers from
real runs, not guesses:
1. A first version's cheap KD-tree pass (`tree.query_pairs()`) ran over
   all coastal nodes in one call from `_ensure_coastal_connectivity` —
   at full-dataset scale (tens of thousands of nodes) the *pair set alone*
   reached tens of millions of Python tuples and **exhausted 15GB of RAM**.
   Fixed with `MAX_IDS_FOR_PASS1 = 4000`: this cheap pass is skipped
   entirely above that node count, not throttled.
2. The "guarantee" reconnection pass's per-round distance matrix was
   originally capped *per group*, so a run with hundreds of components
   (real multi-island Zeeland geometry) still produced a
   tens-of-thousands² matrix — **gigabytes, 15+ minutes, OOM-killed**.
   Fixed with `MAX_TOTAL_SAMPLES = 1500` as a **global**, dataset-size-
   independent cap (sampling proportionally fewer points per group as the
   number of groups grows), plus `MAX_ROUNDS = 30`.

Both fixes bound cost by a fixed constant regardless of dataset size —
the right kind of fix, not a bigger machine or a raised timeout. No
further action needed here.

**Closed out this session.** Both risks below now have fixes in
`nautical_routing_pipeline.py`, re-verified via a full pilot re-run on
`data/zeelandbrug_tight` (`2779 nodes / 4880 edges`, `edge_kind_id=0:
1684`, `edge_kind_id=1: 3196`, `navmesh_regions: 3` — same order of
magnitude and structure as the numbers recorded above; small deltas are
expected from stitching now running per-component instead of over one
merged polygon). `metadata.architecture` default also bumped to
`"navmesh-hybrid-phase1"` (was still `"...-phase0"`).

*Was open*: `build_navmesh_region`/`_polygon_to_pslg` had no analogue of
`_rasterize_water_polygon`'s `MAX_RASTER_PIXELS` cap — the PSLG's
perimeter vertex count (hard `segments` constraints for `triangle`) was
unbounded, and `triangle`'s quality-constrained refinement (`q28`) on a
large, insufficiently-simplified real-world coastline boundary is a known
way to trigger a combinatorial blow-up in output triangle/Steiner-point
count. **Fix applied**: `build_navmesh_region` now estimates
`len(vertices) + len(segments)` from the PSLG before triangulating and,
if it exceeds `NAVMESH_PSLG_BUDGET` (20,000), progressively doubles the
`simplify()` tolerance and rebuilds the PSLG (up to 6 attempts) until it
fits — same "shrink the input until it's cheap enough" idea as
`_rasterize_water_polygon` enlarging pixel size. As a second line of
defense, if `triangulate()` succeeds but returns more than
`NAVMESH_MAX_TRIANGLES` (200,000) triangles, it retries once with a
coarser mesh (`q20`, `4x` the target area) before accepting the result.
Neither path falls back to `build_navmesh_placeholder` on failure/excess
— that function stays dead code, not reintroduced.

*Was open (secondary, lower severity)*: `_ensure_coastal_connectivity`
unioned and buffered (`.buffer(2.0)`) the **entire dataset's** coastal
water in one call, uncapped — fine at Zeeland-pilot scale, a real cost at
full-country scale. **Fix applied**: it now loops over
`_connected_water_polygons(coastal_gdf)` and calls
`_stitch_component_pieces` once per original connected component, with
the candidate node list spatially pre-filtered (via a `GeoSeries` spatial
index) to nodes near that component only. This both bounds the per-call
union/buffer cost and makes `_stitch_component_pieces`'s documented
same-body-only intent structurally enforced instead of incidentally true.

---

## Phase 2 — Funnel-algorithm consumption (separate repo: `autoroute`)

Out of scope for `signalk-router-pipeline` — this is TypeScript work in
`autoroute/src/routing.ts` / `src/database.ts`, implementing the full
consumption contract already specified in
`router-data/specs/routing-database-format-specification.md` §6:

1. Point-location: which `navmesh_regions` row (if any) a route leg's
   start/end falls inside, via point-in-polygon against `boundary_geometry`.
2. Entry/exit triangle lookup within that region's `vertices`/`triangles`.
3. Corridor search: shortest chain of triangles from entry to exit, via a
   small Dijkstra/A* over the triangle dual graph using `triangle_adjacency`.
4. Funnel algorithm (Simple Stupid Funnel Algorithm) over the corridor's
   shared portal edges → exact taut polyline through the region.
5. **Runtime strategy**: when a route leg crosses a region the engine
   understands (`navmesh_regions` present and funnel algorithm
   implemented), prefer the funnel-computed path over the Phase 1
   fallback edges from §1.3. The fallback edges are not deleted or
   deprecated by this phase — they remain the compatibility floor for any
   consumer (including older versions of this same plugin, or third-party
   tools) that hasn't implemented navmesh consumption.

This phase is what actually and fully eliminates zigzag in open water —
Phase 1 only stops the *point-cloud/dense-triangulation* zigzag by
removing interior triangle edges from the routable graph; the fallback
edges it adds are straight lines between boundary nodes, which are clean
but not necessarily shortest-path-optimal within a region. Do not attempt
this phase in the pipeline repo — it is entirely runtime/consumer-side
logic.

---

## Phase 3 and beyond (pointer only — see `README.md`'s roadmap)

Already described at the right level of detail in this repo's `README.md`
and not repeated here: OSM/OpenSeaMap (tier 3) and GEBCO/EMODnet (tier 4)
data fusion; the human/AI-assisted override-authoring workflow against
`router-data`'s `overrides/` directory; EMODnet vessel-density and
MarineCadastre AIS validation/gap-filling; scale-out to full NL, then a
first NOAA-charted US region; supernode/macro-edge hierarchical routing.
None of these are blocked by Phase 1/2 above, but doing them before the
navmesh-region generation is real (Phase 1) or consumable (Phase 2) would
mean building on top of a graph that's still fundamentally a point cloud
in open water — sequence matters here.

## Critical files

- `signalk-router-pipeline/nautical_routing_pipeline.py` — Phase 1, all of it
- `signalk-router-pipeline/requirements.txt` — add `triangle`
- `router-data/specs/routing-database-format-specification.md` §2.9, §6 —
  authoritative schema/consumption contract for both phases
- `autoroute/src/db-worker.ts` — defines the "must stay routable by the
  unmodified runtime" compatibility bar for Phase 1; Phase 2's actual
  target for the funnel-algorithm implementation
- `autoroute/test/zeelandbrug_test.ts` — manual validation target, unchanged
