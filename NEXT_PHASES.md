# Next Phases — Navmesh-Hybrid Pipeline (Post Phase 0)

Status: Phase 0 and Phase 1 (this repo, `signalk-router-pipeline`) are
implemented, verified, and committed (`e6f1fc5`, `7e64df9`, `dee556c`,
`3c108b1`). Phase 2 (funnel-algorithm consumption, in the separate
`autoroute` repo) also turned out to already be implemented and committed
(`bfd3560`), and its own "Boundary Shortcut Sparsification" hardening pass
(`ece6278`) and "Round 3 — Navmesh Entry/Exit Correctness" pass
(`2ebd29b`) are committed too. **Round 3 is independently re-verified this
session**: rebuilt fresh, 39/39 tests pass (including the real, meaningful
`test/zeelandbrug.test.ts` regression test against a committed real-data
fixture — read its assertions directly, they check actual route shape and
constraint compliance, not just "did it return something").

**Two things confirmed this session, both needing a "Round 4" before
Phase 3 or full scale-out — see "Phase 2 Hardening, Round 4" below:**

1. The already-flagged full-scale bridge-avoidance issue is **real,
   reproduced live** against the actual `data/zeeland.sqlite` (not
   inferred) — the router still detours to a low fixed span of the same
   physical Zeelandbrug structure instead of the ~1.5-2km-further real
   opening, exactly as predicted, at a specific, now-identified edge.
2. **New finding, not previously documented**: loading the full-scale
   `data/zeeland.sqlite` (23 navmesh regions) into the routing engine
   takes **~237 seconds**. Isolated by direct timing to graph *load*
   specifically — the route calculation itself, once loaded, takes ~2.7s.
   So this is a one-time startup/reload cost, not a per-request problem,
   but a 4-minute plugin load is a real practical issue on its own,
   especially since it will only grow as this scales to all of NL and
   beyond per the roadmap.

**"Phase 2 Hardening, Round 5" added — a real user bug report
(screenshot of a bad live route) plus direct visual inspection of the
debug graph-edges overlay, both investigated and reproduced this
session.** Highlights: (a) an **uncommitted, in-progress fix already
sitting in `autoroute`'s working tree** (not made this session) genuinely
cuts load time 237s→75s (verified) but does **not** fix route quality —
commit it, it's good, but keep debugging §4.1/§5.2; (b) the reported bad
route (a ~5-6km unnecessary detour through Krabbenkreek/Keeten instead of
a direct Oosterschelde crossing) is reproduced and is a bigger, clearer
manifestation of the same still-open bridge-avoidance-class defect; (c)
"edges crossing land" — confirmed the debug view's API genuinely omits
`path_points` (so it can only ever draw straight chords), but **whether
the actual returned route geometry is affected is still unverified — user
correctly pushed back on treating this as resolved, see §5.3, do not
downgrade this**; (d) small non-navigable retention basins
are confirmed absurdly over-tessellated (up to 1,760 vertices for a
34,000 m² pond) and are a real, comparatively cheap-to-fix contributor to
both load time and visual clutter; (e) a good new architectural idea —
deriving skeleton/lane nodes from real buoy/beacon positions instead of
raw coastline vertices — is scoped as a future design item. See "Phase 2
Hardening, Round 5" for details and concrete next steps on all five.

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
  ~~This matches the risk this document already flagged: possible
  `_add_opening_bridge_edges` bridge-tagging failure in the pipeline.~~
  **Corrected in the follow-up session below — this lead was checked and
  ruled out.** A synthetic stress-test fixture (regular right-triangle
  grid, `test/navmesh-integration.test.ts`) separately showed `navmesh.ts`'s
  Simple-Stupid-Funnel-Algorithm implementation can fail to "cut corners"
  on certain corridor shapes, degenerating to keeping every portal vertex
  (see that test's comments) — while real Zeeland `triangle`-library
  output was spot-checked separately (2,852 anchor-shortcut pairs, ratio
  of funnel distance to straight-line ~1.0 for all but 34, and those only
  marginally off) and does **not** show this pathology, so it's a latent
  corridor-search/funnel edge case, not confirmed as the dominant cause of
  the remaining Zeelandbrug gap. **Neither of these is fixed by this
  session's change** — both are new, separately-scoped follow-ups, not
  part of the boundary-shortcut-sparsification fix itself.

### Round 2 — Follow-up investigation (verified this session, corrects the lead above)

Rebuilt `autoroute` fresh (38/38 tests still pass) and re-verified against
a **freshly regenerated** database rather than trusting the existing
`zeelandbrug_tight_phase1.sqlite` fixture — this mattered:

- **`_add_opening_bridge_edges` bridge tagging is fine — drop that lead.**
  Regenerated `zeelandbrug_tight` from its source GeoJSON with the current
  pipeline (`data/zeelandbrug_tight_retest.sqlite`) and queried it directly:
  a node at (51.62678, 3.91101) — the exact coordinate of the source
  data's `CATBRG=5` (movable) Zeelandbrug span — has multiple edges with
  `max_air_draft=999.0`, correctly distinguished from the flanking
  `CATBRG=1` (fixed) spans' genuine `VERCLR=11.0` edges. The bridge
  crossing is tagged exactly as it should be.
- **The test fixture itself was stale, and that was a real confound.**
  `zeelandbrug_tight_phase1.sqlite` (dated Jul 10) had **zero**
  `resolution=0.001` bridge-marker nodes at all — it predates a pipeline
  state where bridge edges were being generated correctly for this input.
  Testing against a fresh regeneration of the *identical* source directory
  gave a real, measurable improvement: **3,116m / 78 segments**, vs. the
  stale fixture's 4,808m / 221 segments. Better, but still not fixed.
  **Lesson for whoever picks this up: always regenerate test databases
  fresh before drawing conclusions about a routing bug** — a stale
  fixture can make a partially-fixed bug look completely unfixed, or vice
  versa.
- **The core issue survives the fresh regeneration and is now well
  isolated**: confirmed via direct shapely inspection that the start
  point (51.613, 3.885), end point (51.609, 3.896), and the bridge
  opening (51.627, 3.911) are **all three inside the same connected water
  polygon** at the source-data level (component #3, 105.7 km²) — so this
  is not a case of the real geometry genuinely lacking a connection.
  Despite that, and despite correct bridge tagging, the actual routed
  path never leaves the lat 51.608–51.614 band at all — it doesn't even
  attempt to head toward the bridge's latitude — while still zigzagging
  finely and still crossing an 11m-clearance edge.
- **A raw-graph bypass attempt to isolate pipeline vs. router responsibility
  was inconclusive by design flaw, not a finding — don't repeat it.** Ran
  a plain networkx Dijkstra directly against the exported `edges` table,
  using naive nearest-literal-node lookup for start/end. It reported "no
  path" from start's nearest node to the bridge — but that's not a
  meaningful result: once a point is confirmed inside a navmesh region
  (as start is, per earlier verification), the real engine never uses
  "nearest literal node" to enter the graph — it enters via
  `funnelPathFromPoint`-computed anchors. A bypass that ignores that
  entry mechanism isn't testing the same thing the engine actually does,
  so its "no path" result doesn't imply a real connectivity gap. Any
  future attempt to isolate pipeline-side vs. router-side responsibility
  needs to replicate the actual region/anchor entry logic, not substitute
  a simpler model that happens to be wrong for this case.

**Where this leaves it**: the bridge is correctly tagged, the water is
genuinely connected in the source data, and the router still doesn't
explore anywhere near it. That combination points at the router's
region/anchor/candidate-selection logic (or the SSFA corner-cutting gap
already found) rather than a pipeline data problem — but this needs
direct instrumentation of the real engine to confirm, not more inference
from outside it. See "Recommended next steps" below.

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

1. ~~Re-run the exact `zeelandbrug_test.ts` scenario... likely `Through
   opening: true` again...~~ **Outcome, verified**: re-run against a fresh
   regeneration gave a real improvement (4,924m → 3,116m) but not
   `Through opening: true` — see "Round 2 — Follow-up investigation"
   above and "Phase 2 Hardening, Round 3" below for the corrected
   diagnosis and concrete next steps. The bridge-tagging hypothesis this
   item originally pointed at as the likely follow-up has been checked
   and ruled out.
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

### Phase 2 Hardening, Round 3 — Navmesh Entry/Exit Correctness (DONE)

**Outcome**: the Zeelandbrug scenario now genuinely passes —
`Through opening: true`, 5,047m (close to Phase 0's original 4,924m
result), zero constraint warnings — verified both against a fresh
`zeelandbrug_tight` regeneration and live against a full-scale
`data/zeeland.sqlite` through the running plugin's HTTP API. Three
independent, confirmed bugs were found and fixed, spanning both repos —
the direct instrumentation approach this section originally specified
(live logging in `astarSearch`/`seedNavmeshCandidates`, not a bypass) is
exactly what surfaced all three:

1. **`autoroute`/`navmesh.ts`: SSFA funnel algorithm had `left`/`right`
   portal vertices swapped.** `funnel()`'s `getPortalEdge` consumption
   built each portal as `{ left: vertices[edge.b], right: vertices[edge.a]
   }` — backwards. Confirmed by reproducing the exact failure on a
   synthetic diagonal-split grid (corner-to-corner straight line, tested
   directly via `navmesh.ts`'s exported functions): the buggy algorithm
   returned a 19-point, 36%-inflated staircase where the true taut path is
   a straight 2-point line fully contained in the search corridor (proven
   by inspecting the corridor's triangle chain directly — every unit
   diagonal segment lies within one of the selected triangles, so a
   correct funnel algorithm *must* find the straight line). Swapping to
   `{ left: vertices[edge.a], right: vertices[edge.b] }` fixed the
   synthetic case exactly (1572.5m funnel distance vs. 1574.3m true
   straight-line) and cut the real Zeelandbrug funnel-prefix cost from
   4,211m to 2,209m for the same start-to-anchor leg (straight-line is
   2,082m) — this alone dropped the scenario's total route distance from
   3,115m to 1,231m. This was **not** the same defect as the earlier
   "SSFA fails to cut corners on certain corridor shapes" gap found via
   the right-triangle-grid regression test — that one is a genuine,
   separate, narrower funnel-tightening edge case (still covered by its
   own test); this one was a wrong-on-every-call sign/orientation bug.
   `test/navmesh-integration.test.ts`'s L-shaped-region fast-path test
   had its `totalCoords > 4` assertion tightened to an exact `=== 4` (the
   provably-minimal taut path around the region's one reflex corner) plus
   a direct distance check against the two-segment sum — the old,
   looser assertion had been accidentally passing *because* of the bug
   (extra zigzag points inflated the count past its threshold either
   way), so it wouldn't have caught a regression.
2. **`autoroute`/`routing.ts`: `astarSearch`'s goal test ignored the
   funnel "suffix" cost.** When a navmesh region resolves multiple
   boundary-node candidates (`seedNavmeshCandidates`), each candidate's
   own precomputed cost back to the literal destination point (its
   "suffix") is real remaining cost — but the search declared victory the
   instant it popped *any* node that was a member of `endCandidates`,
   comparing only the graph-side cost to reach that node, never adding the
   suffix before deciding. Confirmed via direct instrumentation
   (temporarily watching specific node IDs through the live search): a
   boundary node ~700m from the literal destination, reached only by
   crossing a genuinely low fixed bridge span (soft-constraint penalized
   at +1,000,000), got accepted as the goal while a bridge-adjacent
   boundary node with a clean, only slightly more expensive path sat
   unpopped in the open set. Fixed by tracking the best true total cost
   (`graph cost + suffix`) across all candidates seen, with the search
   only stopping once the open set's best remaining `f` (an admissible
   lower bound) can no longer beat the best total found — the same
   "seed multiple starts, let real cost comparison pick the cheapest"
   design the code already used for `startCandidates`, now applied
   symmetrically on exit. (`MinHeap` gained a `peek()` method to support
   this.)
3. **`signalk-router-pipeline`/pipeline: the obstacle layer hard-blocked
   the bridge's own opening corridor.** Even with both `autoroute` fixes,
   the route still couldn't reach the bridge at all — direct inspection
   showed every one of the bridge's own 4 crossing edges, plus dozens of
   navmesh boundary-ring edges leading to them, had `crosses_obstacle=1`,
   which `routing.ts`'s `getEdgePenalty` treats as a **hard, unconditional
   exclusion** (`-1`, never just a soft penalty) regardless of vessel
   dimensions. Root cause: an unrestricted mariculture (marine-farm)
   polygon's bounding box happened to overlap the entire bridge crossing
   area. Two fixes, both in `_build_obstacle_layer`/`_edge_attr_worker`
   (`nautical_routing_pipeline.py`):
   - **Mariculture areas now go through the same `_is_entry_prohibited`
     (S-57 `RESTRN=1`) filter `restricted_areas` already used** — a marine
     farm concession is a fishing-rights designation, not automatically a
     navigational hard-stop, exactly like a restricted area without an
     explicit entry prohibition isn't one either. None of this dataset's
     21 mariculture features had `RESTRN` set at all, so the obstacle
     layer went from 24 features (21 mariculture + 3 genuine
     obstructions) to just the 3 genuine ones.
   - **Opening-bridge edges (`is_opening_bridge_edge=True`, from
     `_add_opening_bridge_edges`) are now also exempt from the general
     obstacle-intersection check**, mirroring the `crosses_land=0`
     exemption those edges already got at creation time — they're
     precise, deliberately-computed crossings of a bridge's actual
     navigable opening via fairway/waterway-centerline intersection, not
     generic geometry that a broad-brush polygon-overlap heuristic should
     second-guess. Kept as defense-in-depth alongside the mariculture fix,
     since a *genuinely* `RESTRN=1` obstacle elsewhere could still
     otherwise block a real opening-bridge crossing.
4. **`No route found` error message improved** (unrelated bug report from
   the same session, `autoroute`/`routing.ts`): previously always blamed
   vessel dimensions ("checking vessel dimensions draft=Xm...") regardless
   of actual cause. `astarSearch` now tallies *why* edges were skipped
   during the failed search (land, obstacle, draft, air draft, beam, coast
   distance, bounding box) and reports the actual encountered causes, or
   explicitly says "graph may be disconnected here" / "exceeded iteration
   limit" when no constraint was ever hit at all.
5. **`zeelandbrug_test.ts` converted into a real automated test**
   (`test/zeelandbrug.test.ts`, wired into `npm test` and `npm run build`)
   — asserts distance is bounded relative to straight-line, the route
   passes through the opening's bounding box, no segment violates the
   vessel's air draft, and no constraint warning fires. Uses a **real**
   pipeline-generated database, not a synthetic fixture (committed at
   `test/fixtures/zeelandbrug/netherlands.sqlite`, ~2MB, with a narrow
   `.gitignore` exception) — deliberately, since all three bugs above only
   ever surfaced on realistic region/boundary sizes; the existing small
   synthetic navmesh fixtures passed throughout. All 39 tests pass.
6. **Full-scale `data/zeeland.sqlite` regenerated** with both pipeline
   fixes (344 components, 23 navmesh regions, 33,860 nodes, 55,139 edges)
   and deployed to the local dev Signal K instance (europe.sqlite
   disabled, zeeland.sqlite installed, plugin rebuilt, server restarted) —
   confirmed loading cleanly and serving real routes via
   `POST /signalk/v1/api/router/route`.

### Phase 2 Hardening, Round 4 — Full-Scale Correctness & Load Performance (DONE)

**Outcome**: both 4.1 and 4.2 fixed and independently re-verified this
session — full-scale `loadGraph()` down from ~237s to ~76s (3.1x), and the
Zeelandbrug scenario now genuinely passes at full scale (`Through opening:
true`, 4,962m, zero warnings), matching Round 3's small-fixture result.
Verified three ways: the `autoroute` test suite (39/39), a direct script
against the regenerated `data/zeeland.sqlite` via `RoutingDatabase`/
`RoutingEngine`, and live through the running plugin's HTTP API
(`POST /signalk/v1/api/router/route`, 1.3s response, deployed via
`deploy.sh`). Did 4.2 first as originally suggested, then 4.1.

**4.2 fix** (`autoroute`, `src/navmesh.ts` + `src/database.ts`): profiling
(`console.time` around `precomputeFunnelEdges`'s two sub-phases) found the
doc's own original lead wrong — `upgradeRingBoundaryEdges` was never the
problem (120ms total). The real cost was `addAnchorShortcutEdges`, almost
entirely from one ~5,000-boundary-node region (189s of the ~237s total),
split roughly evenly between the anchor-anchor and boundary-to-anchor
sub-loops. Two independent, correctness-preserving fixes, not a cap or a
truncation:
1. **`corridorSearch` was a plain Dijkstra; made it A\*.** Added a
   straight-line-distance-to-nearest-end-candidate heuristic. Since edge
   weights are haversine distances between triangle centroids (a metric
   satisfying the triangle inequality), the heuristic is admissible and
   consistent, so results are provably identical to the old Dijkstra's —
   pure speedup. Cut the total to ~140s.
2. **Boundary-to-nearest-anchor selection was picking anchors by ring
   arc-index distance, not real geometric distance** — a poor proxy on
   Zeeland's convoluted coastline that both chose lower-quality anchors and
   made their corridor searches needlessly expensive (a genuinely distant
   target visits far more of the triangle mesh). Switched to real haversine
   distance (40 cheap comparisons per node, negligible next to the corridor
   search it feeds). Cut the total further to ~76s.

**4.1 fix** (this repo, `nautical_routing_pipeline.py`, `_stitch_component_pieces`
+ `build_navmesh_region` + `build_skeleton_network`): live-instrumented
`astarSearch` (temporary logging, reverted after use — same method that
found all three Round 3 bugs) and found the search actually got within
34m of the real opening, at a cheap cost, but the boundary node it reached
had **zero edges** leading any further toward the bridge — confirmed via
direct SQL that the region on the *far* side of that 34m gap (containing
the bridge's own skeleton nodes) was correctly stitched, but this region's
own near-side node wasn't, despite the pipeline correctly flagging it as a
seam node in `boundary_node_ids`. Root cause, confirmed by adding temporary
`[DEBUG]` logging inside `_stitch_component_pieces` and rerunning the full
pipeline: at full-country scale, the giant Zeeland/Oosterschelde water
body's initial union-find state has thousands of separate groups (8,440 —
one per navmesh region ring, per island ring, per skeleton chain) before
any stitching runs. `MAX_TOTAL_SAMPLES // len(groups)` floors to exactly 1
sample per group, and — critically — **nothing rotates which single member
gets sampled across rounds**, so a 21-member group containing our target
node kept offering the *same* (wrong) representative for all 30 rounds.
The overall union-find *did* eventually converge to one component (so no
"components left unmerged" warning fired for this one), but only via some
other, unrelated node in that 21-member group connecting out through a
distant detour — never via our target's own trivial ~34m connector. This
reframed the bug: it was never really "disconnected" at the coarse
union-find level, just missing the *specific, cheap* edge a real route
needed, while the router correctly found the only path that did exist (via
the low fixed span).

Fix: a new **Pass 0** in `_stitch_component_pieces`, run before the
existing radius/sampling passes — a k-nearest-neighbor `cKDTree` query
restricted to `navmesh_seam_node_ids` (a new tracked set: navmesh perimeter
vertices already flagged as seam-adjacent in `build_navmesh_region`, plus
skeleton dead-end nodes now similarly flagged in `build_skeleton_network`).
This sidesteps group-sampling entirely — every seam node gets to examine
its own nearest few neighbors directly, an O(log n) KD-tree query
regardless of how many seam nodes exist in total (unlike the existing
`MAX_IDS_FOR_PASS1`-gated all-pairs-within-radius pass, which is skipped at
this scale specifically because materializing every candidate pair is what
caused the original 15GB OOM this cap protects against). Verified this
doesn't just paper over the one test case: raw-SQLite connectivity metrics
(a standalone BFS over nodes/edges, independent of the fix) improved
slightly overall — largest connected component 15.93% of nodes vs 13.86%
before, fewer isolated fragments (5,368 vs 5,390) — and node/edge counts
moved by a sane, small amount (+98 nodes, +1,072 edges from the added
stitching connectors), not a structural change. `data/zeeland.sqlite`
regenerated: 33,895 nodes, 56,211 edges, 23 navmesh regions,
3,016 stitching edges added (vs the pre-fix run's 2,034).

One dead end worth recording so it isn't retried: an earlier attempt at
this same fix tried to prioritize seam nodes *within* the existing
per-group sampling loop (still capped at 1 sample/group). That regenerated
database was byte-identical at the specific gap — the cap itself, not the
selection-within-a-group, was the actual bottleneck, which only a
sampling-independent pass (this one) could fix.

Both items below are the original investigation notes from earlier in the
session, kept for the reasoning trail; see the outcome above for what was
actually found and fixed.

Both items below were **reproduced live this session** against the actual
`data/zeeland.sqlite` (regenerated fresh, stats verified: 33,860 nodes,
55,139 edges, 23 navmesh regions) through the real `RoutingDatabase`/
`RoutingEngine` classes — not inferred from logs.

#### 4.1 Bridge-avoidance regression persists at full scale (confirmed, root cause not yet found)

Same request as the now-passing `test/zeelandbrug.test.ts` fixture
(`51.613,3.885` → `51.609,3.896`, draft 2.0m/beam 5.0m/airDraft 19.5m),
run against the full-scale database instead of the small
`zeelandbrug_tight` fixture: **still fails**. `Through real opening: false`,
route confined to lat 51.609–51.620 (never reaches the real opening at
51.627), and the same class of warning fires:
```
via_constrained: Route to destination: constrained for 1 leg(s) 0.0Nm
  — air draft 11.0m < required 21m
  (from 51.61364,3.89309 to 51.61352,3.89318)
```
Traced this specific edge: it's a genuine, correctly-computed base pipeline
edge (`max_air_draft=11.0`, confirmed via direct SQL — not a synthetic
anchor/shortcut edge with a missing or wrong attribute) belonging to the
Zeelandbrug's `CATBRG=1` fixed span (source feature index 97 in
`bridges_polygons.geojson`, `OBJNAM="Zeelandbrug, N256"`) — a single
polygon feature spanning enough length that this crossing point, ~500m
from that feature's centroid, is still part of the same physical fixed
span the earlier "still open" note flagged. **Not a newly-discovered
fifth crossing** — same structure, confirmed.

Given Round 3 already fixed the SSFA sign bug, the A* goal-test suffix
bug, and the obstacle-layer false block, and this exact symptom persists
at larger scale, the most likely explanation is a **new, scale-dependent
issue distinct from all three Round 3 fixes** — plausible candidates,
in the order to check them:

1. **Region/anchor reachability at longer range.** The real opening is
   ~1.5-2km from start, likely requiring the route to cross through more
   intermediate navmesh regions/skeleton stretches than the short
   `zeelandbrug_tight` scenario ever exercised. Check whether the anchor
   graph actually offers a connected, reasonably-costed path spanning
   that many region-crossings, or whether reachability degrades over
   multiple hops.
2. **Cost-domain sanity check.** The `+1,000,000` soft penalty
   (`getEdgePenalty`, `routing.ts:1474`) should trivially dominate any
   real few-km detour — confirm this is actually true in practice (log
   the winning path's total cost vs. what a path through the real
   opening would cost, using the same live-instrumentation technique
   that worked for Round 3, not a bypass). If the real-opening path's
   cost is somehow *also* very high, that's the actual lead, not the
   penalty math.
3. Re-apply the exact Round 3 method (temporary logging directly in
   `astarSearch`/`seedNavmeshCandidates`, reverted after use) — it
   correctly surfaced all three prior bugs and is the proven approach
   here, not more inference from outside the engine.

#### 4.2 New finding: full-scale graph load takes ~237 seconds (one-time, not per-request)

Timed `loadGraph()` and `calculateRoute()` separately against
`data/zeeland.sqlite`: **`loadGraph()` took ~237s**; the subsequent
`calculateRoute()` call took ~2.7s. This means it's a load-time cost
(paid once at plugin startup, or on hot-reload after downloading a new
region database per AGENTS.md's "hot-reload for routing engine" feature)
— not something a user experiences per route request — but still a real
problem: a ~4-minute load for one modest region is a genuine deployment
concern, and it will only get worse scaling to all of NL/Europe per the
roadmap.

**Where to look first**: `database.ts`'s `precomputeFunnelEdges` (called
once from `loadGraph`) runs two sub-phases per region —
`upgradeRingBoundaryEdges` and `addAnchorShortcutEdges`. The Round 1
anchor-sparsification fix bounded the *anchor-pair* cost
(`maxAnchors² = 1,600` funnel calls per region, worst case), but
**`upgradeRingBoundaryEdges` calls `Navmesh.funnelBetweenNodes` once per
boundary node's ring-adjacent neighbors — that's `O(total boundary nodes
across all regions)`, which was never bounded by the anchor fix and
scales with real coastline complexity** (potentially tens of thousands
across 23 regions, each call doing a Dijkstra corridor search + SSFA).
This is a strong, concrete lead, not yet confirmed as *the* dominant
cost — first step is to time the two sub-phases separately (a temporary
`console.time`/`console.timeEnd` around each call in
`precomputeFunnelEdges`, reverted after use, same discipline as Round
3's diagnostic logging) to confirm which one actually dominates before
designing a fix. Plausible fixes once confirmed: most ring-adjacent
pairs are geometrically very close together, so their funnel path is
almost certainly a trivial single- or two-triangle corridor — consider
skipping the funnel computation entirely for ring-adjacent pairs below
some small distance threshold (keep the existing straight-line distance,
which is already accurate at that scale) rather than computing a full
corridor search + SSFA for a pair that's obviously not going to benefit
from it.

#### 4.3 Suggested order

Do 4.2 first — it's better-isolated (a clean, reproducible timing split)
and a fix there won't be entangled with 4.1's routing-logic investigation.
Then 4.1, using the proven live-instrumentation method. Re-run the full
39-test suite plus a manual full-scale check after each fix; consider
adding a *second* automated regression fixture at full-Zeeland scale
(not just `zeelandbrug_tight`) once 4.1 is resolved, since that's exactly
the scale gap that let this round's issues hide from the existing test.

**Followed as suggested — see the "Outcome" note at the top of this Round
4 section for what was actually found and fixed.** The full-Zeeland-scale
regression fixture suggested above is still **not** done — a real gap,
since this is exactly the scale that hid both of this round's bugs from
the existing `zeelandbrug_tight`-based test. Committing a ~24MB full-scale
`.sqlite` fixture (vs the current ~2MB tight-clip one) is a real cost;
worth deciding deliberately rather than defaulting either way.

### Phase 2 Hardening, Round 5 — Real-world bug report + two new findings

Triggered by a live user report (screenshot of a genuinely bad route in
the deployed webapp UI, start `51.6889,4.2124` near Oude-Tonge to
`51.6306,3.8026` near Zierikzee) plus direct visual inspection of the
graph-edges debug overlay. Reproduced exactly (see below) and
investigated this session — this supersedes some of §4.2's guesses with
real measurements.

#### 5.1 Correction to §4.2's profiling guess — and a WIP fix already in progress, not yet committed

**§4.2 guessed `upgradeRingBoundaryEdges` was the dominant load-time
cost. Direct timing (this session, before finding the WIP fix below)
showed that guess was wrong**: `upgradeRingBoundaryEdges` took ~131ms
total across all 23 regions; `addAnchorShortcutEdges` took **4:27
(267s)** — essentially the entire load time — dominated by a handful of
outlier regions (one with 4,999 boundary nodes alone cost ~198s: 52s
anchor-anchor + 146s boundary-to-anchor).

**Found, while investigating, `src/database.ts` and `src/navmesh.ts`
already had real but uncommitted changes in the working tree** (not made
by this session, presumably in-progress work from elsewhere) that
directly target this, citing this exact session's measurements in their
own comments:
- `corridorSearch` (`navmesh.ts`) converted from plain Dijkstra to A*
  with an admissible straight-line-to-nearest-end-candidate heuristic
  (provably identical results, since triangle-centroid haversine
  distances satisfy the triangle inequality — a pure speedup).
- `addAnchorShortcutEdges` (`database.ts`) changed from picking each
  boundary node's "nearest" anchor by ring arc-index to real haversine
  distance — arc-adjacent isn't geometrically close on a convoluted
  coastline, so the old method was both picking worse anchors *and*
  making the corridor search for a genuinely distant, wrong target more
  expensive.

**Verified this session**: rebuilt with these changes in place, 39/39
tests still pass, and full-scale `loadGraph()` dropped from **237s to
75s** (3.15x). Real, substantial, and safe to build on — **but not yet
committed as of this session; commit it** (with its own test/verification
pass) before continuing, so it isn't sitting only in an editor's working
tree.

**Important: this WIP fix improves load time only, not route quality.**
Re-ran the exact reported bad route after rebuilding with it — identical
result (36,946m, same warnings, same southern detour). The routing-choice
defect below is a separate, still-unfixed problem.

#### 5.2 Reproduced: the reported bad route (still broken)

`51.6889,4.2124` → `51.6306,3.8026`, draft 2.0m/beam 6.0m/airDraft 17.0m,
against the full `data/zeeland.sqlite`: **36,946m** (matches the UI's
displayed 19.9nmi exactly), confirmed via full coordinate dump to dip
from the start (lat 51.689) down to **lat 51.609** — a genuine ~5-6km
detour south through Keeten/Krabbenkreek — before returning north to the
lat-51.631 destination. A direct crossing of the main Oosterschelde basin
(wide open water, per the zoomed-out map) is visibly available and not
used. This is very likely the same root-cause class as §4.1 (missing
interior navmesh shortcuts forcing a walk along boundary rings/narrow
channels instead of a clean cross-region path), now shown to be far more
consequential at full scale than the narrow Zeelandbrug corridor alone
suggested — **do §4.1's investigation on this scenario, not only the
Zeelandbrug one**, since it's a larger, clearer manifestation of the same
class of bug.

The `via_constrained` warning text is misleading and worth fixing
separately: it reports "depth 0.0m < required 2.3m" but the actual
per-segment dump shows no segment with `minDepth` below 1.0m along this
route (18 segments in the 1.0-2.5m range, clustered right at departure —
likely a genuinely shallow, unavoidable approach channel near the start
point, not a routing defect) — plus **one** separate segment at
`(51.61364,3.89309)→(51.61352,3.89318)` with `maxAirDraft=11`, which is
the *exact same* Zeelandbrug low fixed span from §4.1/Round 3. The
warning-aggregation logic in `routing.ts` is combining a depth-constraint
group and an air-draft-constraint group into one message and reporting a
value ("0.0m") that doesn't match any real constrained segment found —
worth a small separate fix so warnings are trustworthy for diagnosis, but
not the cause of the bad route itself.

#### 5.3 Answering the two direct questions asked

**"Do edges cross land a lot — is that as designed?" — genuinely unclear,
flagged by the user as likely bigger than a rendering nitpick, and they're
right to push back. Do not treat this as resolved.**

What's actually confirmed, not guessed: sampled 3,000 real edges from
`zeeland.sqlite` in the affected bounding box and tested each against the
real land polygons — only 5 (0.17%) genuinely cross land, all
`edge_kind_id=0` (skeleton, the one category never claimed to be
land-safe *by construction*). And separately, **confirmed by reading the
code, not inferring**: `RoutingDatabase.getEdgesInBBox` (`database.ts:826`)
and the `/signalk/v1/api/router/graph/edges` endpoint (`api.ts:509`) that
feeds the webapp's "Graph edges" debug view only return
`source_lat/source_lon/target_lat/target_lon` — **`path_points` is not
in that API's response shape at all**, so the debug view is structurally
incapable of drawing a curved path even for an edge that has one
internally. That much is fact, and it does mean every edge in that view —
including long anchor-shortcut edges, which are *deliberately* between
far-apart points (farthest-point sampling) — renders as a straight chord,
which explains why a fan of lines can visually span kilometers of real
farmland (screenshot: the Sint-Annaland/Sint-Philipsland peninsula) rather
than just skirting a single headland.

**What is NOT yet verified, and shouldn't have been asserted as
confidently as it was**: whether the *actual returned route* — not the
debug view — correctly follows an edge's curved `path_points` whenever
one of these long shortcut edges is used, everywhere it can be used
(`buildRouteResult`/`buildSubSegments` are known to consume `path_points`
where present, per earlier verification, but that verification predates
this specific class of long, land-spanning anchor shortcuts and the
concurrent WIP changes to `database.ts`/`navmesh.ts` found this session —
it hasn't been re-checked against *this* evidence). **Concrete next
step, priority, before assuming this is "just" a display issue**: take a
real route request that is known to traverse one of these long
fan-pattern anchor-shortcut edges (the reported bug's route is a good
candidate — check which specific edges its 161 segments actually used),
and confirm its *rendered, returned* polyline follows water the whole
way, not a straight chord over the peninsula. If it does, then this really
is display-only and the fix is extending the API/frontend to carry and
draw `path_points` (below). If any real returned route geometry cuts
across land, that's a distinct, more serious bug in how a shortcut edge's
path gets threaded through — most likely a specific code path that reads
`edge.distance`/`edge.lat`/`edge.lon` without also reading `path_points`
for this edge type, not caught by existing tests since they don't
exercise a full-scale mesh with far-apart anchors.

**Fix for the confirmed part regardless**: extend `getEdgesInBBox`'s
returned shape and the `/graph/edges` endpoint to include `path_points`
when present, and make the webapp's debug overlay draw it instead of a
straight chord. Do this either way — even if real routing is proven
correct, the debug view giving a false impression of gross land-crossing
is itself worth fixing since it's exactly what a user (or a future
debugging session) will look at first.

**"The shallowness warning is probably the chosen path, not a lack of
deep water" — confirmed, partially.** As above: most of the flagged
"shallow" segments are a real, likely-unavoidable shallow patch right at
departure, not a symptom of poor path choice. But the air-draft warning
*is* exactly the already-known low-bridge-avoidance defect (§4.1) — a bad
path choice forcing a crossing that a better route (through the real
opening, or simply not needing to come anywhere near that bridge on a
direct Oosterschelde crossing) would avoid entirely.

#### 5.4 New, confirmed: small non-navigable basins are absurdly over-tessellated, and are a real contributor to load cost

Direct observation from the debug overlay (screenshot: two near-circular
solid-green regions with dense red boundary rings, near the Philipsdam
between Hoogbekken and Laagbekken) prompted a direct check — **confirmed,
with real numbers**: these basins' source polygons carry **566 to 1,760
vertices** for physical areas as small as **34,000 m²** (roughly 185m
across) up to 236,000 m² (roughly 490m across). That's a raw, unsimplified
survey-grade vertex density wildly disproportionate to a feature this
small, and very likely explains why specific individual regions (the
4,999- and 2,877-boundary-node outliers that dominated §5.1's timing
breakdown) were so expensive relative to their real navigational
importance. These are Delta Works water-management retention/storage
basins (part of the Philipsdam/Grevelingen system), almost certainly not
realistically navigable or relevant to a routed vessel at all.

**Recommended fix, likely cheaper and more impactful than deeper
algorithmic tuning**: before classification/triangulation, either (a)
exclude small enclosed water bodies that have no connection to the wider
navigable network above some minimum width/area threshold (a "is this
reachable-by-boat-at-all" filter, not just a size filter, since some
small-but-genuinely-navigable marina basins should stay), or (b) apply a
much more aggressive `simplify()` tolerance scaled to the polygon's own
vertex-density-per-area *before* it ever reaches `_split_wide_narrow`/
`_polygon_to_pslg`, rather than only reactively simplifying when the PSLG
budget cap is exceeded (`NAVMESH_PSLG_BUDGET`, `build_navmesh_region`).
Either fix reduces both load time and rendered visual clutter (the user's
"way too many edges" observation) directly, and is likely faster to land
than §5.5 below.

#### 5.5 New architectural idea from this session, worth a real design pass (not urgent, but valuable): node placement from buoys/beacons, not raw coastline vertices

Direct, good observation: instead of deriving skeleton/navmesh node
positions purely from the (often needlessly detailed) coastline polygon
boundary, detect real navigation aids — buoys and beacons — and prefer
*those* positions as skeleton centerline vertices and lane-edge anchors
where they exist along a channel. This is a natural extension of the
architecture's own original vision (README's "paired lane edges follow
IALA buoyage") that was never actually wired up to a real buoy data
source: **the pipeline currently ingests no buoy/beacon layer at all** —
its input set is `land, coastal_water, inland_waterways, depth_areas,
bridges, locks, fairways, pois, restricted_areas, obstructions, hulks,
mariculture, caution_areas` — no S-57 `BOYLAT`/`BOYSPP`/`BCNLAT`/etc.
Two real benefits if implemented: (1) fewer, better-placed nodes along
buoyed channels (a real navigational reference beats an arbitrary
coastline-derived vertex), and (2) a natural, principled way to decide
*where* a channel needs a node at all — a stretch with no buoys and wide,
even depth needs far fewer nodes than one with a marked, winding fairway.
Scope this as its own future phase (data ingestion + a "prefer buoy
positions when building skeleton/lane geometry, fall back to today's
raster/medial-axis method where no buoy data exists" design) rather than
folding it into Round 4/5's bug-fixing work — it's a genuine improvement,
not a fix for something broken, and deserves its own focused design
session with the same rigor as Phase 1's original plan.

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
