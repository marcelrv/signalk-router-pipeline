# Next Phases — Navmesh-Hybrid Pipeline (Post Phase 0)

Status: Phase 0 (schema migration, skeleton centerline extraction, bridge
attachment, provenance stamping) is implemented and committed (`e6f1fc5`).
Phase 1 (below) has since been **implemented and verified this session**
— see "Phase 1 — Implementation Verification & Memory-Safety Hardening"
for what was checked and what's solid. The two remaining scaling risks
flagged there (unbounded PSLG size, whole-dataset connectivity buffer)
have since also been closed out (see that section's updates) and the
Zeelandbrug pilot re-verified end-to-end after the fix. Ready to commit
and move on to Phase 2 (separate repo) or scale-out to the full Zeeland
clip.

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
