# Next Phases — Navmesh-Hybrid Pipeline (Post Phase 0)

Status: Phase 0 and Phase 1 (this repo, `signalk-router-pipeline`) are
implemented, verified, and committed (`e6f1fc5`, `7e64df9`, `dee556c`,
`3c108b1`). Phase 2 (funnel-algorithm consumption, in the separate
`routeiq` repo) also turned out to already be implemented and committed
(`bfd3560`), and its own "Boundary Shortcut Sparsification" hardening pass
(`ece6278`) and "Round 3 — Navmesh Entry/Exit Correctness" pass
(`2ebd29b`) are committed too. **Round 3 is independently re-verified this
session**: rebuilt fresh, 39/39 tests pass (including the real, meaningful
`test/zeelandbrug.test.ts` regression test against a committed real-data
fixture — read its assertions directly, they check actual route shape and
constraint compliance, not just "did it return something").

**Phase 2 Hardening, Round 4 is DONE, independently re-verified today
against a fresh build + the current full-scale `data/zeeland.sqlite`
(commits `72c0a43` routeiq, `512188f` this repo)**: 39/39 tests pass,
`loadGraph()` is 69s (was 237s), and the Zeelandbrug scenario genuinely
passes at full scale — `Through opening: true`, 4,962m, zero warnings,
confirmed directly, not just re-reading the commit message.

**But Round 5's real-world bug report (§5.2) is not resolved by Round
4 — re-tested today, it's worse by every number that matters:**

| | Before Round 4 | After Round 4 (today) |
|---|---|---|
| Total distance | 36,946m | **62,398m** |
| Southernmost point | lat 51.609 | **lat 51.497** |
| Air-draft warning | yes (Zeelandbrug low span) | gone |
| Depth warning | yes (19 legs, departure area) | still there |

Fixing the narrow Zeelandbrug corridor did not fix — and may have
exposed or worsened — the broader routing-quality problem the real user
bug report is about. **Do not treat Round 4 as closing out the original
bug report.**

**Round 6 made real, verified progress — two genuine, committed fixes —
but the reported bug (§5.2) is still not resolved, and its real root
cause turned out to be architecturally deeper than either fix.** Round 6
found and fixed a connectivity gap (a skeleton mid-chain node, never
seam-tagged, sitting 88m from a navmesh boundary node) by broadening
Pass 0 to query every node in the component. A same-session follow-up
found and fixed a second, distinct gap class (a 94.8m skeleton-to-boundary
gap that survived the first fix because dense same-type neighbors starved
it out of a plain top-6 KNN query) with a type-aware cross-KNN Pass 0b.
Both are committed, both are independently verified safe (full
regenerations, no memory/performance regression, Zeelandbrug scenario
unchanged at 4,962m/zero warnings). **Neither moved the reported-bug
scenario's numbers at all (56,833m / 51.544°N, unchanged by the second
fix)** — because its real cause is different: essentially the entire
southward excursion runs on `inland_waterways`-sourced graph edges
(`node_type="inland"`), a separate data source and network that
`_ensure_coastal_connectivity` categorically excludes from all stitching,
with no other mechanism connecting it to the coastal network at all.
**Not done — see "Phase 2 Hardening, Round 6" §5.2.1 for the full
verified picture, the evidence trail, and the concrete next step**
(a real design decision about inland/coastal network stitching, not a
tuning fix), including working scratch scripts in `routeiq`'s working
tree, a real starting point for whoever continues this.

**Also from the Round 5 investigation, still relevant:** (c) "edges
crossing land" — confirmed the debug view's API genuinely omits
`path_points` (so it can only ever draw straight chords), but **whether
the actual returned route geometry is affected is still unverified — user
correctly pushed back on treating this as resolved, see §5.3, do not
downgrade this**; (d) small non-navigable retention basins are confirmed
absurdly over-tessellated (up to 1,760 vertices for a 34,000 m² pond) and
are a real, comparatively cheap-to-fix contributor to both load time and
visual clutter; (e) a good new architectural idea — deriving skeleton/lane
nodes from real buoy/beacon positions instead of raw coastline vertices —
is scoped as a future design item. See "Phase 2 Hardening, Round 5" for
details.

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

Implemented in `routeiq` per the design below, with one deliberate
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

Rebuilt `routeiq` fresh (38/38 tests still pass) and re-verified against
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

Built `routeiq`, ran the full automated suite (35/35 pass, including
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

1. **`routeiq`/`navmesh.ts`: SSFA funnel algorithm had `left`/`right`
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
2. **`routeiq`/`routing.ts`: `astarSearch`'s goal test ignored the
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
   the bridge's own opening corridor.** Even with both `routeiq` fixes,
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
   the same session, `routeiq`/`routing.ts`): previously always blamed
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
Verified three ways: the `routeiq` test suite (39/39), a direct script
against the regenerated `data/zeeland.sqlite` via `RoutingDatabase`/
`RoutingEngine`, and live through the running plugin's HTTP API
(`POST /signalk/v1/api/router/route`, 1.3s response, deployed via
`deploy.sh`). Did 4.2 first as originally suggested, then 4.1.

**4.2 fix** (`routeiq`, `src/navmesh.ts` + `src/database.ts`): profiling
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

#### 5.1 Correction to §4.2's profiling guess — DONE, committed as part of Round 4 (`72c0a43`)

**Re-verified today, independently, against a fresh build and the current
full-scale `data/zeeland.sqlite` (33,895 nodes/56,211 edges/23 regions,
post-4.1-fix): `loadGraph()` took 69s** (consistent with Round 4's
claimed ~76s), 39/39 tests pass. This item is closed — see the historical
notes below for how it was found, kept for the reasoning trail.

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

#### 5.2 Reported bad route: STILL BROKEN after Round 4's fix, and worse by the numbers — this is the priority for Round 6

**Re-tested today against the post-4.1-fix `data/zeeland.sqlite`
(33,895 nodes/56,211 edges/23 regions, confirmed current) — the specific
Zeelandbrug low-bridge symptom is gone, but the overall route is now
measurably worse, not fixed:**

| | Before Round 4 (this doc, original) | After Round 4 (re-tested today) |
|---|---|---|
| Total distance | 36,946m | **62,398m** |
| Southernmost point reached | lat 51.609 | **lat 51.497** (~21km south of the direct line) |
| Warnings | depth (19 legs) + air-draft (Zeelandbrug low span) | depth (19 legs) only — **air-draft warning gone** |

The air-draft warning disappearing is consistent with 4.1's fix working
(the route no longer needs the low Zeelandbrug span) — full coordinate
dump confirms it now correctly threads through the real opening bbox
(`51.626-51.628, 3.910-3.912`) near the very end of the route. But getting
there now involves a bizarre, much larger detour: south from the start
through Keeten/Krabbenkreek as before, but this time continuing **far**
past Krabbenkreek down to ~51.50°N (near Tholen/Volkerak, well outside any
sensible path between these two points) before turning back north to
finally approach the real opening. **Fixing the narrow Zeelandbrug
scenario exposed or worsened this broader problem — it did not fix it.**
Do not treat Round 4 as having resolved the original bug report; §4.1's
narrow-scenario fix and this broader one are evidently not the same
defect, or not the whole of it.

**Ruled out, checked directly, don't re-suspect it**: the new Pass 0
seam-stitching k-NN pass (§4.1's fix) is not a likely cause of this
specific detour — read `_stitch_component_pieces`'s Pass 0
(`nautical_routing_pipeline.py:1078-1117`) directly and confirmed it (a)
routes every candidate through the same `try_add` land-crossing/
containment checks every other stitching pass uses, and (b) is hard-capped
to `snap_radius_m=500m` connections only — it cannot itself be the source
of an 11km+ jump.

**More plausible, not yet checked**: Pass 2's pre-existing "guarantee"
stitching pass has no equivalent distance cap — its job is to connect
*any* two remaining components regardless of distance, checked only for
land-crossing and staying within the overall component polygon, not for
"is this actually a sane route a boat would take." A component polygon
covering the whole Oosterschelde/Krammer/Volkerak system is large and
non-convex enough that a long straight connector between two faraway
points could pass both checks while being a poor navigational choice if
it ever gets used as a literal edge rather than just a fallback-of-last-
resort. Also plausible: a genuine issue in how the TS runtime's navmesh/
anchor consumption behaves for a route that must cross *several* regions
in sequence (this scenario, unlike the 2-region Zeelandbrug case, likely
crosses many), not just one hop — Round 4's fixes were verified against
the Zeelandbrug's simpler 2-region case, not a long multi-region crossing
like this one.

**Concrete next step — same proven method as Rounds 3/4, applied to
*this* scenario specifically**: temporary live instrumentation in
`astarSearch`/`seedNavmeshCandidates`/`trySameRegionNavmeshRoute` (added,
verified, then reverted — not a bypass) to see exactly which edges/regions
the winning path actually traverses around the 51.50-51.52°N excursion,
and whether that stretch is one long literal edge (a Pass-2-style
guarantee connector — check its `source`/`target`/`distance` directly in
the exported `.sqlite` if so) or many small hops through a real but
poorly-connected sub-region. Do this before assuming it's the same root
cause as §4.1 — today's evidence suggests it might not be.

The `via_constrained` warning text is still misleading and still worth
fixing separately: it reports "depth 0.0m < required 2.3m" but the actual
per-segment dump shows no segment with `minDepth` below 1.0m along this
route (18 segments in the 1.0-2.5m range, clustered right at departure —
likely a genuinely shallow, unavoidable approach channel near the start
point, not a routing defect). Not the cause of the bad route itself, but
worth fixing so future warnings are trustworthy for diagnosis.

### Phase 2 Hardening, Round 6 — real, genuine progress; NOT yet correct — continue from here

**A separate session found and fixed a real, distinct bug (this repo,
`nautical_routing_pipeline.py`, `_stitch_component_pieces` Pass 0), then
was interrupted before the broader §5.2 problem was fully resolved.**
Verified independently below — do not re-litigate what's confirmed
fixed, but do not mistake it for the whole fix either.

**What was found**: Round 4's Pass 0 only queried nodes already tagged
`navmesh_seam_node_ids` (perimeter vertices flagged at a real narrow/wide
split, or skeleton dead-ends). Round 6 found a distinct gap class: a
skeleton chain's **mid-chain** node (degree 2, never a dead end, never
seam-tagged) can sit a stone's throw (confirmed case: 88m) from a navmesh
region's boundary node without ever getting connected, because neither
side was ever a candidate for Pass 0's KNN query — the overall component
was already technically fully connected via some far longer route
elsewhere, so the later "guarantee" passes had no reason to add this much
shorter one either. Confirmed via live instrumentation in `astarSearch`
(the same method Round 3/4 used — temporary logging + `debug_region13.json`/
`debug_region16.json` node-ID watchlists, still present in the working
tree, not yet reverted).

**Fix applied**: broadened Pass 0 to KNN-query **every** node in the
component, not just seam-tagged ones — safe because `try_add`'s
union-find check (`find(u) == find(v)`) rejects an already-connected pair
in O(1) before any expensive shapely check runs, so this doesn't
reintroduce Pass 1's original "materialize every pair" blowup. Full
reasoning is in the code comment itself (`_stitch_component_pieces`,
around the Pass 0 block) — it's thorough and worth reading directly
rather than duplicating here.

**Independently re-verified today** (fresh build, fresh full-scale
regeneration — `data/zeeland_round6.sqlite`, two successful runs logged
in `data/zeeland_round6_run.log`/`_run2.log`):

- **No memory/performance regression**: full pipeline run completed in
  ~8m8s (was ~6.5min post-Round-4) — slower, not dangerously so, no OOM,
  no hang. 33,801 nodes / 56,899 edges / 4,056 stitching edges added
  (more than Round 4's 3,016 — consistent with catching a real,
  previously-missed class of gap, not noise).
- **Zeelandbrug scenario: still passes cleanly.** `Through opening: true`,
  4,962m, zero warnings — no regression from this change.
- **The reported bug scenario: genuinely improved, but still broken.**

| | Original (pre-Round 4) | Post-Round-4 | Post-Round-6 (today) |
|---|---|---|---|
| Total distance | 36,946m | 62,398m | **56,833m** |
| Southernmost point | lat 51.609 | lat 51.497 | **lat 51.544** |
| Air-draft warning | present | gone | gone |
| Depth warning | present (departure area) | present | present (departure area, same class) |

The trend is in the right direction across both post-Round-4 and
post-Round-6 fixes, but **the route is still worse than the original
pre-Round-4 baseline** (straight-line distance between these two points
is ~29km — 56,833m is 1.96x that; the original 36,946m was already only
1.27x). Full coordinate dump confirms the route now correctly threads
through the real Zeelandbrug opening near the end (`~51.6269,3.91083`)
but takes a large, still-unexplained loop down to ~51.54-51.57°N (near
Tholen) first. This has the same *shape* of problem as the one just
fixed — very possibly another instance of the same missing-connector
class of gap, somewhere in that southern area — but has **not been
confirmed** as the same root cause; don't assume it without checking.

**Status: committed, real, and safe — but this fix alone did not close
out §5.2.** (Confirmed and superseded by the follow-up work below, done
in the same session as this write-up.)

#### 5.2.1 Round 6 follow-up — a second real fix landed, but the reported bug's actual root cause is deeper than either stitching fix

Resumed exactly where the interrupted session left off, using its own
artifacts (`round6-*.mjs` scratch scripts, `debug_region13.json`/
`debug_region16.json` watchlists, the `[DEBUG round6]` instrumentation
already wired into `astarSearch`) against a **freshly regenerated**
`data/zeeland_round6.sqlite` (confirmed to reflect the just-committed
Pass 0 fix, not a stale copy). Reproduced the documented numbers exactly
(56,833m, southmost 51.54366°N) before changing anything.

**Second real gap found and fixed, but it wasn't the one causing this
bug.** Hop-by-hop analysis of the reproduced path found one dominant
outlier: a single 9,358m `edge_kind_id=1` (navmesh boundary/anchor
shortcut) edge from (51.54546, 3.86751) to (51.61983, 3.90169) — a real,
legitimate funnel-computed edge (`hasPathPoints=true`, distance matches
its own path length), not a bug itself, but suspicious as the one hop
that jumps the whole 51.54-51.61°N gap in one step. Tracing backward from
its source node along the route's skeleton-only prefix found a **second,
distinct 94.8m gap**: skeleton node `1157916690409570` (51.64352, 4.0957)
sits 94.8m from navmesh boundary node `509919030409659` (51.64417,
4.09659) of the very region the route eventually detours ~20km south to
enter — no edge either direction, confirmed directly against the fresh
data. This survived the just-committed Round 6 fix.

Root-caused via direct inspection of `_stitch_component_pieces`'s Pass 0:
querying `k=6` nearest neighbors **without regard to node type** means
that inside a densely triangulated navmesh region (this one has 2,877
boundary nodes packed a few meters apart), a node's own top-6 nearest
neighbors are almost always same-type immediate neighbors — crowding out
a real cross-type connector that might be 50-100m away. Both this
skeleton node's and this boundary node's own top-6 lists were full of
same-type points closer than 94.8m, so Pass 0 never tried the pair,
despite it being well within `snap_radius_m`. **Fix applied**: a new Pass
0b splits the KNN query by type (`node_kind_id`, already stamped on every
navmesh perimeter vertex, seam-tagged or not) via two separate KD-trees —
every skeleton/other node looks at its own k nearest navmesh vertices,
and vice versa — guaranteeing the true nearest cross-type candidate is
always considered regardless of same-type local density on either side.

**Verified via full regeneration**: no regression (~9m19s, was ~8m8s),
4,618 stitching edges added (up from Round 6's 4,056 — a real, additional
class of gap, not noise), Zeelandbrug scenario unchanged (4,962m, zero
warnings, re-verified against the final full-scale `data/zeeland.sqlite`
itself, not just the small test fixture). **Committed on its own
merits**, same as Round 6's fix.

**But re-testing the reported-bug scenario against this fix produced
*exactly* the same numbers as before it: 56,833m, southmost 51.54366°N —
zero change.** This was the first sign that the 94.8m gap, despite being
real, wasn't this bug's actual cause. Direct investigation (adding
temporary trace logging to `_stitch_component_pieces`, reverted before
committing — see method note below) confirmed why: **node
`1157916690409570` is not a normal coastal skeleton node at all — it's
`node_type="inland"`, built from the separate `inland_waterways_lines.geojson`
layer, not `coastal_water` polygons.** This is directly decodable from
the node ID itself (`_coord_to_id`'s `type_int` bit: IDs ≥
648,000,000,000,000 are inland-typed; confirmed against both the raw
`nodes.node_kind_id`/lat/lon columns and by reconstructing the ID formula
by hand) — no live instrumentation was even needed to prove it, once
suspected.

**This is the real root cause, and it's bigger than a stitching-pass
tuning problem: `_ensure_coastal_connectivity`
(`nautical_routing_pipeline.py:1713`) unconditionally excludes every
`node_type="inland"` node from `coastal_nodes` before any component or
candidate is even gathered** — so no amount of tuning `_stitch_component_pieces`'s
KNN logic (broader Pass 0, type-aware Pass 0b, anything) can ever reach
an inland-typed node; it's never offered as a candidate in the first
place. And **there is no other mechanism in the pipeline that connects
inland nodes to the coastal network at all** — the only path is
incidental exact-coordinate reuse in `_get_or_create_node` (an
inland_waterways vertex that happens to round to the same (lon, lat) as
an already-existing coastal node, to 5 decimal places / ~1.1m). Checking
every node ID along the reported-bug route's southward excursion
confirms the practical consequence directly: **essentially the entire
route, from very close to the start point all the way down to the
51.54366°N southmost point, runs on `inland_waterways`-sourced edges**,
not the coastal skeleton/navmesh network the last three rounds of
investigation had been focused on. The route only rejoins the coastal
network right at the southern end, via what is almost certainly one of
these incidental exact-coordinate coincidences, which is why it happens
exactly where it does (near Tholen) rather than somewhere closer to a
direct line between the two requested points.

**What's genuinely unknown, and shouldn't be assumed either way without
checking**: whether the pre-Round-4 baseline (36,946m) *also* used this
inland network (in which case this is a pre-existing characteristic of
how these two data sources compose, not a regression), or used a purely
coastal route that's since become unreachable due to some other gap
(in which case the inland detour is a fallback masking a *different*,
still-undiscovered coastal-network gap). An attempt to check this
directly against `data/zeeland_pre_round4.sqlite.bak` this session hit an
unrelated tooling problem (a guessed `RoutingDatabase` constructor option
for a custom filename doesn't exist; it silently fell back to a stale
`data_round6_test/zeeland.sqlite` state and the search pathologically
never terminated in region 16 — a tooling dead end, not a finding about
the baseline route, and not investigated further this session).

**Concrete next step for whoever picks this up**: this needs an actual
design decision, not a quick tuning fix. `_stitch_component_pieces`'s
existing safety checks (`within(poly_m)`, `_crosses_land`) are purely
geometric and type-agnostic — extending `_ensure_coastal_connectivity`'s
candidate set to also include inland nodes near a given coastal
component's polygon, and letting those same checks gate what actually
gets connected, is architecturally consistent with how every other
stitch in this function already works, and is the most likely-correct
fix. But do the comparison above first (does the good pre-Round-4 route
also use this inland chain?) before assuming that's sufficient, and
think through whether there's a real reason inland and coastal networks
were kept separate before touching it (lock/depth/dimension semantics
that might differ between the two data sources and shouldn't be silently
merged without preserving them).

**Method note, for reproducibility**: this session's diagnostic work
(hop-distance analysis, node-ID type-bit decoding, a temporary
`try_add`/component-membership trace added to and then removed from
`_stitch_component_pieces` before committing) is not preserved in the
committed diff — only the real Pass 0b fix is. The `round6*.mjs` scripts
in `routeiq`'s working tree (now including several new ones from this
session: `round6b-check*.mjs`, `round6c-region1.mjs`,
`round6d-region-correct.mjs`) remain as a real, reusable starting point
for whoever continues this, same as before.

**Housekeeping, done this session**: `routeiq/src/routing.ts`'s
temporary `[DEBUG round6]` console logging and the two hardcoded
`fs.readFileSync` calls reading `debug_region13.json`/`debug_region16.json`
from a deployed-path location have been reverted (`git checkout --
src/routing.ts`) — the built code no longer throws for anyone without
those files at that exact path. All 39 tests pass on the clean build.

#### 5.2.2 Follow-up on §5.2.1's open questions (analysis, not yet implemented)

Read the actual code (`_add_opening_bridge_edges`, `_ensure_coastal_connectivity`,
the `locks` handling in `_edge_attr_worker`) to answer §5.2.1's "think through
whether there's a real reason inland and coastal networks were kept
separate" before anyone reaches for the broad fix it proposed:

- **Movable bridges already link inland and coastal nodes today, with no
  type filter at all.** `_add_opening_bridge_edges`'s quadrant search
  (`nautical_routing_pipeline.py:1558`, `for nid, data in
  self.graph.nodes(data=True)`) considers every node regardless of
  `node_type` — so a movable bridge over a fairway/inland-waterway
  intersection is already a real, working, type-blind inland↔coastal
  connection. This is useful precedent: type-blind linking at a genuine
  physical interface is already safe and shipping, not hypothetical.
- **Locks don't create connectivity at all, by design** — the `locks_gdf`
  usage in `_edge_attr_worker` (~line 94-167) only consults lock polygons
  to annotate an *already-existing* edge's attributes/cost, never to add a
  new edge. That's fine: a lock chamber sits on a waterway that's already
  one continuous line feature, so it doesn't need a separate connectivity
  mechanism the way an inland/coastal seam does.
- **So the actual, narrower gap is specifically: inland waterway reaching
  open coastal water with neither a movable bridge nor any other
  intersection-based hook** — an unlocked tidal creek or a canal mouth
  opening directly into an estuary. Today that case has *no* mechanism at
  all, other than an incidental exact-coordinate collision in
  `_get_or_create_node` — which is exactly the accident that let the
  reported-bug route rejoin the coastal network near Tholen.

**Refined recommendation** (narrower than "extend `_ensure_coastal_connectivity`'s
candidate set to include inland nodes near a coastal component," though
that's the right shape of fix): don't make it a blanket per-component
merge. Scope the added candidates to inland nodes that geometrically
terminate at or inside a `coastal_water` polygon (the inland waterway
line-work's own endpoint touches/crosses the coastal polygon boundary) —
not every inland node within stitching radius of the component. That
keeps the same physically-grounded-interface property the bridge case
already has (only link where the two networks genuinely meet), and avoids
inventing a routable shortcut through a canal reach that was never meant
to carry coastal traffic. The existing `within(poly_m)`/`_crosses_land`
checks in `_stitch_component_pieces` still gate the actual edge once a
candidate pair is proposed, same as every other stitch pass — only the
candidate-selection step needs to change, not the safety checks.

**Unblocking the pre-Round-4-baseline comparison** (§5.2.1 called this
"genuinely unknown," blocked by a tooling dead end guessing a
`RoutingDatabase` constructor option): don't guess a constructor arg —
stage the backup file the same way every other manual test in this repo
does it (see §"Validation" under Phase 0, `cp <db> /tmp/test_route/netherlands.sqlite`):
`cp data/zeeland_pre_round4.sqlite.bak /tmp/test_route/netherlands.sqlite`
before running the reported-bug scenario, so `RoutingDatabase` opens it
via its normal default path instead of a nonexistent custom-filename
option. Do this before deciding whether the inland-network fix above is
sufficient — if the pre-Round-4 baseline *also* routed through the inland
chain, the fix is still correct but not urgent-regression framing; if it
didn't, there's a separate, still-undiscovered coastal-only gap worth
finding first.

#### 5.2.3 Separately raised (not from this session's coding-agent work, discussed directly with the user) — density/precision tuning and a second pilot region

Three related items, not yet implemented, flagged as worth doing before
investing further Round 7 time or a fresh region build:

1. **`_split_wide_narrow`'s `simplify_tol_m=1.0` default is too precise
   and leaks into the final database.** Douglas-Peucker at 1.0m barely
   removes any vertices from survey-grade coastline data (small real bends
   every few meters mean almost nothing is under-tolerance) — and that
   same simplified polygon flows straight into `_polygon_to_pslg`/
   `build_navmesh_region` with no further coarsening, becoming the actual
   exported `navmesh_regions.vertices`. This is very likely the same
   mechanism behind §5.4's finding below, not a separate issue. Proposed
   fix (not yet implemented): keep the fine tolerance for the wide/narrow
   *classification* decision and medial-axis centering (where precision
   matters), but add a separate, much coarser simplify pass on the navmesh
   region boundary specifically before it becomes PSLG input/output.
2. **`min_navmesh_radius_m=300.0` triggers navmesh treatment for water
   the user considers channel-like, not genuinely wide** — mesh was
   expected to appear only for the North Sea/IJsselmeer/wide Oosterschelde
   scale, with medium water instead getting one or two navigation lines
   (centerline or offset sides). Raising it substantially (~800m
   suggested) is a separate, complementary fix to item 1 — item 1 reduces
   vertex count *within* regions that get navmesh treatment; this reduces
   *how many* regions get it at all.
3. **Direct connection to this round's Pass 0b fix, worth flagging to
   whoever tunes items 1-2**: Pass 0b's own code comment
   (`_stitch_component_pieces`) cites "a real region in this dataset has
   up to 2,877 perimeter vertices packed a few meters apart" as the exact
   reason a plain top-6 KNN crowds out real cross-type connectors — the
   same over-tessellation §5.4 independently measured (566-1,760 vertices
   for basins as small as 185m across). Fixing items 1-2 would likely
   reduce how often Pass 0b-style same-type-crowding gaps occur in the
   first place, not just cut load time/visual clutter. Not a reason to
   skip Pass 0b (it's committed and correct on its own merits regardless),
   but worth re-checking whether Pass 0b's crowding rate drops materially
   once items 1-2 land.
4. **Second Phase 2 hardening pilot region: Puerto Rico.** Zeeland-only
   tuning risks overfitting to one water-body character; a prior
   pre-redesign Puerto Rico attempt exists (`routeiq/data/pr_routing2.sqlite.disabled`,
   schema_version=3, dense-grid/unconstrained-Delaunay signature — not
   reusable, would need a full fresh run against the current architecture).
   NOAA ENC data for the region is already downloaded (`python3
   data/download_noaa.py --region us-caribbean` from `routeiq/data/`,
   154 `.000` files at `routeiq/data/US/us-caribbean/PR`, ~35MB).
   **Recommended sequencing**: land items 1-2 above and re-verify Zeeland
   first, so the Puerto Rico build isn't spent validating soon-to-be-stale
   parameters; then run `enc_preprocessor.py` (10-30+ min) and a full
   pipeline build against it as the second reference region for whatever
   Round 7 hardening follows.

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

### Phase 2 Hardening, Round 7 — navmesh boundary edges systematically shallow (real bug, fixed and verified)

**User's diagnosis, confirmed exactly right by the code**: `classify_water_body`
gates `navmesh` classification on `_has_navigable_depth`, which is an
**any-overlap test** (`deep.sindex.query(polygon, predicate="intersects");
return len(hits) > 0`) — a wide polygon qualifies as navmesh-eligible if a
*single* deep DEPARE polygon intersects it anywhere, not if it's deep
throughout. Since `build_navmesh_region` registers **every** vertex of the
qualifying polygon's own perimeter as a real graph node and connects
adjacent ones with real, depth-sampled `edge_kind_id=1` edges
(`navmesh_boundary`), a region's outer boundary — which is exactly where
real bathymetry shoals fastest — becomes the source of that region's
actual routable depth data. Nodes never get placed further offshore in
the confirmed-deep interior, because the architecture deliberately avoids
grid/interior point injection.

**Confirmed empirically, not just by reading code**, against the real
full-scale `data/zeeland.sqlite`: of 32,210 `edge_kind_id=1` edges,
**28,854 (89.6%) were shallower than the 6.0m depth ceiling**, averaging
3.17m — actually shallower on average than the narrow-channel `edge_kind_id=0`
skeleton centerline edges (6.72m avg), which is backwards from the
architecture's intent. This directly explains a symptom flagged as far
back as Round 5 (§5.3) and left as "probably a real shallow patch, not a
path-choice problem" — that framing was too narrow; it's systemic, not a
one-off patch.

**Fix**: new `_split_deep_shallow(poly_m, utm, depth_gdf, depth_ceiling_m)`,
the depth-side analog of the existing `_split_wide_narrow` — intersects a
width-eligible polygon against the union of DEPARE polygons that clear
the ceiling, keeps the confirmed-deep sub-piece(s) as the real navmesh
candidate(s), and routes the shallow/unsurveyed remainder through the
same skeleton/laned classification path a narrow piece already uses.
Wired into `build_network`'s wide-piece loop, right before
`classify_water_body`'s navmesh branch.

**Two real robustness problems found and fixed during implementation, not
after**:
1. A first version (spatial-filtered, `make_valid()`-cleaned, but only a
   `.buffer(0)` on the final union) **segfaulted** `_triangle.triangulate`
   — real DEPARE polygons from adjacent survey contours don't align
   cleanly with each other or the water body's own boundary, so a straight
   intersection/difference chain produces thin slivers and small holes
   that are topologically pathological, not just visually messy. Fixed
   with a `.simplify(1.0)` (same tolerance `_split_wide_narrow` already
   uses) plus a 5m morphological closing (`.buffer(5).buffer(-5)`) on the
   deep mask before it's ever used to cut anything.
2. Even after that fix, a **knife-edge cut exactly at the depth-ceiling
   contour** put 84% of resulting boundary edges in the 5.0-6.0m band
   specifically — technically still "below ceiling" per the strict
   `<6.0` count, but from ordinary DEPARE-band/sampling noise right at the
   transition line, not genuine shallow water (only 0.47% of edges were
   actually `<3.0m` at this point). Fixed with a further
   `DEPTH_SPLIT_SAFETY_MARGIN_M = 20.0` erosion past the ceiling contour,
   so the region boundary sits inside confirmed-deep water with real
   clearance instead of exactly on the line.

**Verified twice**: first on the small `zeelandbrug_tight` clip (navmesh-
boundary-edge average depth 5.64m → 7.06m after adding the margin;
`<6.0m` count 84% → 5.7%), **then confirmed at real full scale**
(`data/zeeland_depthsplit.sqlite`, fresh regeneration, 13m39s, exit 0,
no crash):

| | Before (`zeeland.sqlite`, original) | After (`zeeland_depthsplit.sqlite`) |
|---|---|---|
| navmesh_boundary (`edge_kind_id=1`) edges | 32,210 | 164,826 |
| — average depth | 3.17m | **6.88m** |
| — `<6.0m` (below ceiling) | 89.6% | **8.2%** |
| — `<3.0m` (genuinely shallow) | not separately measured, but dominant | **0.68%** |
| centerline (`edge_kind_id=0`) average depth | 6.72m | 4.15m (now correctly *shallower* than navmesh boundary, not deeper — the inversion is fixed) |
| `navmesh_regions` | 23 | 201 |

The core fix is real and holds at full scale, not just a small-clip
artifact.

**New trade-off this surfaced, not yet resolved — check before calling
this done**: total edge count grew substantially (navmesh_boundary ~5.1x,
centerline ~2.9x) — expected, since splitting by depth both multiplies
region count (23→201, mostly smaller/simpler pieces) and moves shallow
remainders into real skeleton treatment that previously didn't exist at
all for those areas. Region vertex-count distribution is mostly
reasonable (201 regions, median 226 vertices) but has a real tail: p90 =
3,210, **max = 34,851** (one region alone exceeds `NAVMESH_PSLG_BUDGET`
before even counting segments, meaning it's hitting the simplify-retry
path hardened above on every build, not as an edge case).

**Load-time check done, and it's good news, not a new regression**:
staged `zeeland_depthsplit.sqlite` and timed `loadGraph()` the same way
Round 4 did. **27.9s** for 114,607 nodes / 248,537 edges — *faster* than
Round 4's ~70s baseline despite ~3.4x the nodes and ~4.5x the edges.
Splitting large, complex, monolithic regions into many smaller, simpler
ones (23→201) evidently costs the anchor-sparsification precompute less
in aggregate than a handful of pathologically large regions did — this
tracks with Round 4's own finding that load time was dominated by a
*few* outlier regions with huge boundary-node counts, not by total graph
size. The depth-split fix is a clear win on both axes verified so far
(depth accuracy and load time); the PSLG-budget/simplify-retry cost on
the one 34,851-vertex outlier region is the remaining open question, not
overall load time.

**Not yet done**: whether `navmesh_seam_node_ids`/`boundary_node_ids`
correctly include the *new* depth-cut boundary (as opposed to only the
pre-existing width-based wide/narrow seam) wasn't specifically verified —
the generic, broadened Pass 0/Pass 0b stitching from Round 6 should still
connect a depth-split navmesh piece to its own shallow remainder via
plain distance-based KNN even without seam tagging, but this hasn't been
confirmed with a real connectivity check the way Round 6's fixes were.
Worth a real route-through-a-depth-split-region test before this is
considered fully closed, not just a depth-distribution check.

### Phase 2 Hardening, Round 8 — depth-split region fragmentation (real bug, fixed and verified)

**Confirmed, not just observed**: a fresh area-distribution check on Round
7's output (not run at the time) found `navmesh_regions` at 201 was
heavily skewed — 143 of the 201 (71%) were tiny (roughly under 100m
across), while a handful were genuinely large, for a region the size of
Zeeland where fewer than 10 real open-water bodies are expected.

**Root cause, confirmed by direct measurement, not guessing**: `_split_deep_shallow`'s
5m morphological closing (`.buffer(5).buffer(-5)`, added in Round 7 to fix
a `triangulate()` segfault) was far too small to bridge the real gap it
needed to. Isolating just the deep-DEPARE-band union (drval1 >= 6.0m,
9,246 polygons in `zeeland_clip`) and re-running the closing step alone at
increasing radii, holding everything else fixed:

| closing radius | resulting pieces | tiny (<10,000m²) |
|---|---|---|
| 5m (Round 7) | 238 | 187 |
| 25m | 142 | 95 |
| 50m | 108 | 68 |
| 100m | 85 | 51 |
| 200m | 50 | 31 |

Monotonic, real, and large — confirming lead 1 from this round's brief (a
thin unsurveyed/misaligned seam between adjacent DEPARE survey-contour
bands, not a genuine shallow gap). Direct evidence this isn't abstract:
the most extreme fragments were sub-1m² polygons (one measured 0.007m²)
sitting 3-48m from a 706,728,203m² neighbor region — pure GEOS
intersection-boundary noise from the depth cut, not real hydrography. 117
of the 201 regions sat within 2km of that one giant region, i.e. were
shrapnel from the same original wide polygon.

Separately, a handful of isolated small basins (a few thousand m², no
large neighbor nearby even at 1000m) also existed — the class of feature
Round 5 §5.4 already flagged, confirming lead 2 is real too, just a
smaller secondary contributor.

**Fix, addressing both leads**: (1) `DEPTH_SPLIT_CLOSING_RADIUS_M` raised
5m → 50m (order of magnitude, per the brief, not fine-tuned further — the
marginal gain past 50m drops off while the risk of over-merging distinct
real features rises). (2) After the depth cut, each deep sub-piece is
re-run through the *existing* `_split_wide_narrow` width test (reusing
`min_navmesh_radius_m`, not a new invented threshold): a piece that no
longer contains a disk of that radius — either genuine GEOS cut noise or
a real-but-tiny non-navigable pocket — folds back into the shallow side
instead of becoming a degenerate `navmesh_regions` row.

**Verified with the same method Round 7 used**, full rebuild against
`data/zeeland_clip`:

| | Round 7 (before) | Round 8 (after) |
|---|---|---|
| `navmesh_regions` count | 201 | **25** |
| region area: min / median / p90 / max (m²) | tiny tail down to 0.007 | **382,691 / 3.19M / 41.6M / 503.8M** |
| regions under 10,000m² | 143 | **0** |
| navmesh_boundary avg depth | 6.88m | 6.90m (no regression) |
| navmesh_boundary <6.0m | 8.2% | 14.3% (still vastly better than pre-Round-7's 89.6%; the width re-filter trades a little margin-band noise for eliminating the fragmentation) |
| nodes / edges | 114,607 / 248,537 | **60,572 / 125,275** |
| build time | 13m39s–13m56s (reproduced) | **9m24s** |
| PSLG-budget simplify-retry triggers | on every build (34,851-vertex outlier) | **once**, for one 40,228-vertex region, succeeded on first retry |

25 regions is closer to real Zeeland hydrography than 201, though still
above the "fewer than 10" estimate — the remaining 25 read as genuinely
distinct open-water bodies (min area 382,691m², none of the previous
degenerate sliver tail), not fragmentation artifacts.

**`loadGraph()` re-verified in `routeiq`, same method as Round 4/7**:
staged the rebuilt db, called `RoutingDatabase.init()` +
`loadGraph()` directly (`dist/database.js`) via the node:22 Docker
pattern. **1.84s** — not just "no regression" but a further ~15x
improvement over Round 7's already-improved 27.9s, consistent with Round
4's own finding that the anchor-shortcut precompute cost is dominated by
a few structurally-complex outlier regions, not total graph size: Round 7
still had one 34,851-vertex region hitting the PSLG simplify-retry path
on every build; Round 8 has none.

**Stitching check done (Round 7's other open item), with a real
connectivity test, not just code-reading**: built the full graph
(nodes+edges) with `networkx` and checked connected-component membership
for every `navmesh_regions` row's own perimeter vertices (matched to
`nodes` by coordinate at the actual 5-decimal precision `_get_or_create_node`
rounds to — importantly *not* 6 decimals, which silently fails almost
every match). Result: **98.3% of all region-perimeter nodes across all 25
regions are in the single giant connected component** (23 of 25 regions
at 99.7-100%), confirming the generic Pass 0 KNN stitching (Round 6) does
correctly reach the new depth-cut boundary even without explicit
`boundary_node_ids` seam-tagging for it (confirmed separately: 24 of the
25 regions have zero tagged `boundary_node_ids` — the seam-coordinate set
passed into `build_navmesh_region` for a depth-split piece is still only
the original width-based wide/narrow seam, never updated to include the
depth cut — yet stitching still finds these nodes because Pass 0 scans
every coastal node in the original connected-water component, not just
tagged ones).

**New, smaller finding, not yet fixed — flag for a future round, don't
conflate with the fix above**: 2 of the 25 regions (a linked pair, ~950
combined nodes, near 51.63°N/3.59°E) sit in a component that's fully
disconnected from the main graph despite being only **57m** from it —
well inside the 500m `_stitch_component_pieces` snap radius. Checked
whether this is a Round 8 regression: **it is not** — the same
"near-miss" class (a disconnected component within 500m of the main
network that the stitch guarantee pass logs as "could not find a valid
in-polygon connector among sampled candidates") already existed at
similar relative scale in the reproduced Round 7 baseline (237 near-miss
components / 1,488 nodes there, vs 256 / 1,961 n here) — this is the
same pre-existing, already-tracked Round 5/6 stitching-guarantee-pass gap,
not a new bug class. Round 8's fix does modestly shift more nodes into
that bucket in relative terms (1.3% → 3.2% of total nodes), most likely
because the larger closing radius and width re-filter change piece
boundaries at some seams enough to remove a previously-valid land-avoiding
connector — worth keeping in mind next time Round 5/6's stitching gap
gets picked back up, but out of scope for this round's fix.

### Phase 2 Hardening, Round 9 — issue collection from live route review (NOT root-caused or fixed yet — collection phase only, by explicit request)

User reviewed a real full-scale route in the live webapp (Oude-Tonge
51.6890,4.2124 → near Zierikzee 51.6257,3.8370 — the same general area as
Round 5/6's original reported bug) and flagged multiple distinct issues
from screenshots. Per explicit instruction: **do not attempt fixes until
all issues are collected and understood together** — this section is a
precise record of what was observed, with grounded hypotheses noted
separately from confirmed root causes. Nothing below is fixed.

**Issue A — ~2x distance detour via a large southward loop through
Zandkreeksluis/Veerse Meer.** 51.4nmi actual vs. an expected ~20-25nmi
direct distance. Route panel's own constraint-violation summary: "constrained
for 49 legs — 3.3nm — depth 0.0m < required 1.5m; air draft 11.0m <
required 17.3m." Geographically **distinct** from Round 6's original
southward detour (which ran toward Tholen/Sint-Philipsland, further
east) — Veerse Meer is a former estuary connected via a lock, plausibly
also touching `inland_waterways`-sourced data, so whether this is the
same underlying mechanism (Round 6's inland/coastal exclusion,
§5.2.2) manifesting at a second location, or a distinct cause, is **not
yet determined**. The charted 0.0m depth figure is notable on its own
(conventionally means drying/intertidal) and is being tracked as a
separate data point, not assumed to share a cause with the distance
issue.

**Issue B — opening bridge not taken, at a bridge the existing automated
test doesn't cover.** Symptom matches the Round 4/5 bridge-avoidance
regression class, but occurs near Zandkreeksluis ("brug over
buitenhoofd") — a different bridge from the Zeelandbrug that
`test/zeelandbrug.test.ts` (`routeiq`) actually exercises. Not yet
determined whether this is: the same underlying defect recurring
somewhere untested, a regression from later pipeline changes (Rounds
6-8 all touched navmesh/depth classification), or specific to this
bridge.

**Issue C — route abandons a correctly-followed fairway near a bridge,
heads the wrong direction.** Consistent in character with the stitching
gaps found across Rounds 4/6/8 (a missing connection at a graph-piece
transition point forcing an unwanted reroute), but this specific
location/instance has not been isolated.

**Issue D — real route doesn't appear to use navmesh interior at all,
only the boundary contour.** Two parts, kept separate: (1) no interior
edges shown in the debug "Graph edges" view is very likely by design —
navmesh regions store no interior edges at all, only the boundary ring;
the interior is meant to be consumed live via the funnel algorithm, not
browsed as a static list — **not confirmed as a bug**. (2) The *actual
returned route geometry* visually hugging the region's outer contour
instead of cutting a taut interior line is a materially different, more
concerning claim — if real, it directly confirms the question left open
and unverified since Round 5 §5.3 ("whether the actual returned route —
not the debug view — correctly follows an edge's curved `path_points`").
**Not yet verified against real data.**

**Issue E — dense fan of edges at bridge/lock POIs, including edges that
visibly cross land/roads.** New screenshot (Zandkreeksluis area) shows
many straight lines radiating from multiple close-together "SS(Bridge)"
points to scattered nodes, several clearly crossing the N256/1e Deltaweg
road and land. **A strong, code-grounded candidate mechanism already
exists, not yet confirmed against this specific data**:
`_add_opening_bridge_edges`'s "quadrant ray-casting" connection step
(`nautical_routing_pipeline.py` ~line 1556) does a nearest-node search
per quadrant within a **0.015° (~1.5km) bounding box** around each bridge
opening point, with **no land-crossing check at connection time** — and
the edge it adds is created with `crosses_land=0` **hardcoded**, not
computed from real geometry. Separately, `_sanity_check_no_land_crossings`
explicitly documents that "opening-bridge edges are never touched" —
i.e. they're the one edge category exempt from the land-crossing
strip/audit pass every other edge kind goes through. Together these mean
a bridge-opening edge is currently the *least* verified edge type in the
whole pipeline for land-crossing risk, structurally, not just by bad
luck on this one bridge.

**CONFIRMED, not just hypothesized** (`data/zeeland_round8_verify.sqlite`):
the Zandkreeksluis bridge-opening node (`509558598386510`, 51.54405,3.8651)
has a 434m edge to node `509558850385884` (51.54412,3.85884) stored with
`crosses_land=0` — independently checked against the real
`land_polygons.geojson` and it **genuinely intersects land**. This one
edge is also one of exactly 4 edges from the bridge node, matching the
"connect to nearest node per quadrant" behavior precisely — strong
evidence this specific edge is a direct product of the unchecked
quadrant search, not coincidence.

**Issue F — navmesh classification still triggering for water that
reads as too narrow.** Second new screenshot (near Zoommeer/Bergsche
Diep) shows a dense, ring/loop-shaped perimeter of many closely-packed
nodes around a modest-width body of water — the classic navmesh-boundary-
ring signature for water that doesn't look like it should qualify as
"open water, no interior nodes needed." This directly reconnects to an
**already-written-up but never-implemented** recommendation
(§5.2.3 item 2, from earlier this same investigation): raise
`min_navmesh_radius_m` (currently 300.0m) substantially. **Confirmed
still needed**: Round 7/8's depth-split fix (`_split_deep_shallow`) only
added a depth-based sub-split *on top of* the existing width-based
split — it never touched `min_navmesh_radius_m` or the width-eligibility
threshold itself, so this axis is exactly as under-tuned as it was
before Round 7 started.

**Issue G — navmesh boundary still tracing through visibly shallow/drying
terrain near Yerseke, even after the Round 7/8 depth-split fix.**
Screenshot with a real IENC depth-contour basemap underneath shows the
navmesh_boundary ring (the thick teal ring with circular node markers,
same visual signature as Issue F) running across areas the chart clearly
shades as drying/intertidal in a complex, braided tidal-flat channel
network near Yerseke (Oosterschelde), rather than tracking the
darker-blue deep channel visibly winding through the same area on the
same chart. This is notable specifically *because* Round 7/8 already
fixed the general version of this problem elsewhere in Zeeland — so
either this particular area wasn't helped by that fix, or something
about this terrain type defeats it. **Two candidate hypotheses, neither
confirmed yet**:
1. `_split_deep_shallow` explicitly falls back to treating a whole piece
   as fully deep when it finds *no* confirmed-deep DEPARE coverage at
   all (deliberate, to avoid being stricter than the pre-existing
   `_has_navigable_depth` for genuinely unsurveyed gaps — see Round 7's
   writeup). If DEPARE coverage is patchy specifically over this area's
   real channel, the split may simply never trigger here.
2. Round 8's `DEPTH_SPLIT_CLOSING_RADIUS_M` (50m, raised specifically to
   stop false fragmentation elsewhere) may be **too large for this
   terrain type** — a tidal mudflat's braided channels are often
   separated by drying banks/spits narrower than 100m, exactly the width
   a 50m closing radius would bridge over, incorrectly folding a real
   shallow separator into the "deep" mask instead of correctly excluding
   it. If true, this is a direct tension with Round 8's own fix, not an
   unrelated bug — the same parameter, tuned to fix over-fragmentation
   in one terrain type, may be under-fragmenting (over-including) a
   different one. Would need checking against DEPARE coverage and the
   deep-mask shape *before* closing, specifically in this bbox, to tell
   these two apart (or confirm both are contributing).

**Issue H — a chain of nodes cuts straight across farmland near
Middelburg, between a lock and a resumed canal segment.** Screenshot
shows the canal through Middelburg (Kanaal door Walcheren) correctly
traced by the usual blue-circle chain down to a cluster of small nodes
right at a visible lock (lock-gate icons, near Havenpoortweg) — then a
**separate-colored chain of black-dot nodes** continues in a near-straight
line across open fields, over the "Zeeuwse lijn" railway, to reconnect
with the blue chain at a canal segment further south near Keetweg. The
color change (black vs. blue) is itself a real signal, not just
description — it's consistent with the node-ID type-bit distinction
(`inland` vs `coastal`) Round 6 decoded directly from real data, so this
may be an `inland_waterways`-sourced segment stitched to the rest of the
network by a generic distance-based connector, not the fairway/skeleton
machinery. **Two already-identified, real gaps make this a plausible
mechanism, not a wild guess**: (1) Round 6/8 already found and documented
that **locks have no dedicated connectivity-generating mechanism** the
way bridges do (`_add_opening_bridge_edges` only handles bridges;
locks are only ever used to annotate an existing edge's attributes,
never to create one) — so if this canal is genuinely interrupted at the
lock chamber, the *only* way these two pieces could connect at all today
is via a generic stitching pass. (2) That generic stitching
(`_stitch_component_pieces`) only guards against land-crossing via
`within(poly_m)`/`_crosses_land` checks against the *land layer* — if the
lock structure itself is mapped as a small land/building feature (a
believable gap, not confirmed), a straight connector routed near/through
it could pass those checks incorrectly.

**CONFIRMED, at smaller scale than E**: independently checked every long
(>150m) inland-to-inland edge in the wider Middelburg area against real
`land_polygons.geojson`. Most (14/15) genuinely don't cross land — long
inland_waterways edges are mostly legitimate sparse-vertex representations
of real waterway geometry, not automatically suspicious. But one, `1157178078357594`
(51.43835,3.57594) → `1157195826356045` (51.44329,3.56045), 1209m, stored
`crosses_land=0`, **genuinely crosses land** when checked directly. Lower
hit-rate than Issue E's bridge-quadrant mechanism (which is closer to
100% unchecked by construction), but confirms the same underlying gap —
`crosses_land` isn't reliably computed/verified for at least some
inland-typed edges either. Exact identity of *this specific database's*
edge vs. the one visible in the screenshot not pinned down precisely
(couldn't correlate screenshot pixels to exact coordinates), but the
mechanism is real regardless of which exact edge was photographed.

### Investigation update — a master root cause found connecting Issues A, C, and D (user gave the go-ahead to start investigating)

Reproduced the reported route live (`routeiq`'s `RoutingEngine.calculateRoute`,
same coordinates as the original report, against a fresh
`zeeland_round8_verify.sqlite`) rather than continuing to theorize from
screenshots. Distance came back different from the screenshot's 51.4nmi
(37.4nmi here) — **expected**, since the screenshots were almost
certainly taken against the live-deployed database, which predates
Round 7/8's depth-split fix; the qualitative problems (constrained legs,
`depth 0.0m`, `air draft 11.0m`) persist in the reproduction too, so the
underlying issues are real and not resolved by Round 7/8, just shifted.

**Found the actual mechanism, not just a symptom.** One "segment" in the
real returned route: 6,666.75m long, exactly **2 coordinates** (a straight
chord, no interior detail), `minDepth=0`. Traced it back to the database:
a **stored `edge_kind_id=1` (navmesh_boundary) edge** between two ring-
adjacent perimeter vertices, `drval1=-2.0` (genuinely charted as drying/
intertidal at that sample point) but `crosses_land=0` (the land-crossing
check only looks at the land layer, not DEPARE/drying data — a real,
separate gap from Issues E/H, noted for later). **The format spec is
explicit that `edge_kind_id=1` edges are "not directly traversed as a
weighted graph edge"** — real navigability through/across a region is
supposed to come from the funnel-computed upgrade
(`upgradeRingBoundaryEdges`) and anchor shortcuts
(`addAnchorShortcutEdges`) that `precomputeFunnelEdges` builds at load
time. This edge was traversed directly, unupgraded, by the live route.

**Root cause, confirmed by direct query, not inference**: both
`upgradeRingBoundaryEdges` and `addAnchorShortcutEdges`
(`routeiq/src/database.ts`) iterate `region.boundaryNodeIds` — the
region's own `boundary_node_ids` column — to decide which ring edges to
upgrade and which anchors to build shortcuts between. **24 of the 25
`navmesh_regions` rows in the current database have a completely empty
`boundary_node_ids` array.** Round 8's own writeup already noted this in
passing (in the context of confirming generic Pass 0 stitching still
works without seam tags) but didn't connect it to this: the depth-split
fix (Round 7/8) creates new navmesh region boundaries at the depth-cut
contour, but the seam-coordinate set passed into `build_navmesh_region`
was never updated to include that new boundary — only the original
width-based wide/narrow seam. With an empty `boundaryNodeIds`, both
`for` loops in `precomputeFunnelEdges` simply never execute — **the
entire funnel-upgrade and anchor-shortcut mechanism is structurally
disabled for 96% of navmesh regions**, leaving raw, unupgraded
ring-adjacency chords (however long or badly-placed Round 7/8's
simplification/closing left them) as the only way through nearly every
navmesh region in the database.

**This plausibly explains three of the collected issues at once, not
three separate bugs**:
- **Issue D** (route never uses the navmesh interior) — directly
  explained: there's no funnel computation happening for these regions
  at all, so there's no interior path *to* use.
- **Issue A** (~2x distance detour) — a strong candidate explanation:
  without real interior shortcuts, the router either gets forced through
  bad raw chords (like the one found here) or has to find a much longer
  real path around regions it can't shortcut through.
- **Issue C** (route abandons the fairway near a bridge) — plausible but
  not directly traced yet: erratic behavior at a region transition is
  consistent with falling back to whatever raw ring edge happens to be
  nearby instead of a real computed path, but this specific instance
  wasn't isolated this session.

**Not yet done**: fixing this (the obvious direction is making the
depth-split boundary contribute to the seam-coordinate set the same way
the width-based seam already does, so `boundary_node_ids` gets populated
correctly for depth-split regions too) — per the explicit process for
this round, confirming the connection was the goal, not fixing yet.
Also not yet done: isolating Issue B (bridge avoidance) and Issue C
specifically against this same mechanism, and Issues F/G (Yerseke
depth-split tuning) remain uninvestigated.

**Issue I — navmesh boundary node density is still high enough that
fixing the master finding above is not a free lunch.** User's own
observation, and an important one to weigh together with the fix above,
not after it: `precomputeFunnelEdges`'s cost is a direct function of
`boundaryNodeIds.length` (ring-adjacency upgrades) and
`anchorNodeIds.length` (anchor-to-anchor pairs, **O(n²) per region**).
Round 8's "27.9s → 1.84s" load-time numbers were measured while this
mechanism was **structurally disabled** for 24/25 regions (empty
`boundary_node_ids`) — those numbers say nothing about real precompute
cost once it's actually populated and the mechanism starts running for
real. This is exactly the same density concern already raised earlier
in this same investigation (§5.2.3 items 1-2: raise `min_navmesh_radius_m`,
add a coarse simplify pass on navmesh boundary *output* specifically) —
raised then, written up, but **never implemented**, and now more urgent:
fixing `boundary_node_ids` without also addressing boundary density
risks trading a correctness bug (silent fallback to bad raw chords) for
a reintroduced performance bug (Round 4/5's original ~198s-for-one-region
problem, or worse, since this time it's O(anchors²) rather than the
older O(boundary_nodes) issue). **Recommendation, not yet acted on**: do
the `boundary_node_ids` fix and the density-reduction work
(`min_navmesh_radius_m` + boundary-output simplify) together, then
re-measure load time the same way Round 4/7/8 all did, rather than
fixing connectivity first and discovering the cost regression after.

**Next step, per explicit instruction**: issues A/C/D now look
connected with real evidence, not just suspected — but B, and F/G,
still need their own look before any fix is attempted, per the same
"don't conflate, don't fix from a partial picture" discipline as before.

**Issue J — Vlissingen town-center canal (Binnenhaven) appears to have
no edges/nodes at all in the live webapp, despite the IENC basemap
showing water there.**

**Correction to the original writeup here**: this section initially
concluded the live server was running a stale, pre-Round-7/8 database,
and recommended redeploying before investigating further. **That was
wrong — checked directly, not assumed.** The actual live-deployed file
(`/home/node/signalkdev/signalk-routeiq/data/zeeland.sqlite`,
`last_update_date=2026-07-13T13:31:23Z`) is already Round-8-level (25
`navmesh_regions`, 24/25 with empty `boundary_node_ids`, matching
depth-distribution numbers) — not stale. Querying it directly (not a
separately-built copy) found **185 nodes and 374 edges already present**
in the Vlissingen town-center bbox — real, connected graph coverage.
So the data is genuinely there; the webapp's debug view just isn't
showing it.

**Real, more likely explanation, found by reading the webapp code**:
`graphEdgesUrl()` (`public/index.html`) caps the `/graph/edges` query at
`limit=5000` for whatever the current map viewport's bbox is. The
screenshot's viewport spanned a wide area including Zandkreeksluis and
the Yerseke tidal-flat network — both dense in edges. It's a strong,
plausible (not yet proven) candidate that the 5000-edge cap was
exhausted by denser parts of the same viewport before Vlissingen's
edges were ever included in the response — a display/query-limit issue,
routeiq-side, not a pipeline data-completeness issue. Not yet
confirmed by directly checking what the endpoint actually returns for
that exact viewport, but the mechanism is real and the data-presence
check already rules out the pipeline as the cause.

**Issue A's discrepancy also corrected**: re-ran the reproduction
against the *actual live database* (not a separate rebuild) — got
**51.4nmi, an exact match to the original screenshot**. The earlier
"37.4nmi, probably a stale-screenshot artifact" conclusion was wrong;
it was an artifact of testing against a different (separately-built)
database with real geometry nondeterminism between builds, not the one
actually deployed. **The master finding (24/25 regions with empty
`boundary_node_ids`) is now confirmed directly against the live
database itself**, not just a separate rebuild — re-running the same
long-straight-chord check against the live file found another
unupgraded 2-point, 2,448m ring edge, same signature as before. **No
deployment was actually needed — the live server was already current.**

**Issue J follow-up — a real, separate gap found at a different location,
after the user restarted the server and re-checked.** The edge-limit-cap
hypothesis above doesn't hold for this: the new screenshot (Middelburg's
town canal loop specifically, *not* Vlissingen town center — a different
bbox from what was checked above) shows a genuinely uncluttered,
non-dense viewport, ruling out a `limit=5000` truncation. Checked
directly, live database, correct bbox this time
(51.495-51.510°N, 3.585-3.608°E, the canal loop's western/northern
stretch): **zero nodes**. Checked the pipeline's own source data for the
same bbox: **zero features in `coastal_water_polygons.geojson` and zero
in `inland_waterways_lines.geojson`** — both of the layers the pipeline
currently ingests for water topology. **This is a genuine source-data
gap, not a generation bug**: the current ENC/IENC-derived input simply
has no charted water feature there at all, most likely because a narrow
town-center canal like this isn't covered by official hydrographic
survey data that prioritizes commercial/larger-vessel navigation. The
basemap tiles showing water there are a completely independent
rendering (OpenStreetMap/OpenSeaMap tiles), not derived from the same
GeoJSON the pipeline consumes — "the chart shows water" and "the
pipeline's input has a water polygon" are not the same claim, and this
is a concrete case where they diverge.

**This is exactly the gap class Phase 3a (`PHASE_3_DESIGN.md`) already
exists to fill** — OSM/OpenSeaMap tier-3 fusion, specifically scoped for
"small harbors, minor canals, features official charts don't bother
with." Not a new phase needed, a concrete real-world confirmation that
3a's stated purpose is correct and needed, not hypothetical.

**Correcting the record precisely**: two different things were true at
two different bboxes, both checked directly rather than assumed —
Vlissingen town center (the original Issue J bbox) has real, present
data; Middelburg's canal loop (this new bbox) has a genuine, confirmed
source-data absence. Don't conflate the two; they're different issues at
different locations, and only this second one is a real, confirmed gap.

### Triage — pipeline vs. routeiq, for parallel work

Every root cause confirmed so far is pipeline-side
(`signalk-router-pipeline`); routeiq's role has been either "correctly
exposed a pipeline data problem" or "needs its own separate look." Split
out so each repo's work can proceed independently:

**`signalk-router-pipeline` (this repo) — root causes confirmed or
strongly implicated here:**
- **Master finding (A/C/D)**: `build_navmesh_region`'s seam-coordinate
  set never includes the depth-split boundary, leaving
  `boundary_node_ids` empty for 24/25 regions and silently disabling
  `routeiq`'s funnel-upgrade mechanism. Fix direction: make the
  depth-split cut contribute to the seam set the same way the width-based
  split already does.
- **Issue I**: do the above together with finally implementing the
  already-recommended `min_navmesh_radius_m` increase + navmesh-boundary
  output-simplify pass (§5.2.3), not after — same underlying density
  problem, and fixing connectivity alone risks a performance regression
  once the funnel mechanism actually starts running.
- **Issue E** (confirmed): bridge-opening quadrant search creates
  land-crossing edges with hardcoded `crosses_land=0`, exempt from the
  land-crossing audit pass.
- **Issue F**: navmesh classification still too permissive for narrow
  water (same `min_navmesh_radius_m` lever as Issue I).
- **Issue G**: navmesh boundary still crosses drying terrain near
  Yerseke even after Round 7/8 — DEPARE coverage gap and/or
  `DEPTH_SPLIT_CLOSING_RADIUS_M` too large for braided tidal-flat
  terrain specifically (not yet investigated).
- **Issue H** (confirmed): generic stitching connector crosses land near
  a Middelburg-area lock — same land-crossing-check-gap family as E.
- **New, surfaced during the A/C/D investigation, not yet its own
  lettered issue**: the land-crossing check (`crosses_land`) only tests
  against the `land` polygon layer, never against DEPARE/drying
  (negative `DRVAL1`) data — the 6,666m edge central to the master
  finding had `drval1=-2.0` (genuinely charted as drying) but
  `crosses_land=0`. Worth fixing alongside E/H since it's the same class
  of gap, one layer wider.
- **Issue J (confirmed, corrected)**: not a display bug — a genuine
  source-data gap. Middelburg's canal loop (west/north stretch) has zero
  features in both `coastal_water_polygons.geojson` and
  `inland_waterways_lines.geojson`. Not a pipeline processing bug to fix
  in `nautical_routing_pipeline.py` — the fix is **data fusion**, i.e.
  Phase 3a (OSM/OpenSeaMap tier-3), already designed for exactly this
  class of gap. Real-world confirmation that 3a is needed, not a new
  work item.
- **Issue K (new, confirmed, high priority) — `VERCLR=0` treated as a
  literal zero air-draft clearance instead of "not surveyed."** Found
  by `routeiq` while isolating Issue B (that specific bridge-avoidance
  claim didn't reproduce, but this did, and looks like the real
  mechanism behind Issue A's route inflation). `zeeland.sqlite` has 58
  fixed bridges; several — including Koningin Beatrixbrug and one of the
  two Wilhelminabrug POIs — carry `"height": 0.0`. Traced to
  `_is_valid()` (`nautical_routing_pipeline.py:30`): it only rejects
  `None`/NaN, so `VERCLR=0.0` passes as valid and
  `attrs['max_air_draft'] = clearance = 0.0` gets written to the edge —
  independently re-verified directly against this exact code, confirmed
  correct. Per S-57 convention, `VERCLR=0` means "vertical clearance not
  surveyed," not "genuinely zero clearance" (a charted navigable bridge
  with real 0m clearance is implausible). Practical effect: any vessel
  with air draft > 0m is hard-blocked from every fixed bridge whose
  height was never surveyed, forcing detours to whichever bridges
  happen to have a real charted height instead. `routeiq` measured the
  real cost directly: a 10.4km direct crossing near Zandkreeksluis
  inflated to 90.5km (8.73x) for one tested vessel profile, detouring
  through an entirely unrelated lock/bridge chain near Vlissingen first.
  **Fix direction**: in the bridge air-draft block (~line 152-158),
  treat `VERCLR=0` the same as "not present" — fall back to the `999.0`
  default the "no bridge found" branch already uses, the same pattern
  already used for movable bridges just above it in the same block.

**`routeiq` (TypeScript runtime) — needs its own investigation,
separate from the pipeline fixes above:**
- **Issue B**: bridge avoidance near Zandkreeksluis, a different bridge
  than `zeelandbrug.test.ts` covers. Not yet isolated whether this is a
  pipeline data problem (like E) or an `astarSearch`/bridge-avoidance
  logic issue on this side — needs its own trace before assuming either.
- **Possible defensive hardening (not a confirmed bug, a design
  question)**: should `precomputeFunnelEdges` warn/log when it finds a
  region with empty `boundaryNodeIds` instead of silently doing nothing?
  The master finding above was only caught because this session went
  looking for it — a loud failure mode here would have surfaced it much
  earlier, and would catch the *next* instance of this class of gap
  automatically instead of needing another live-instrumentation session.
- **Re-verification, blocked on the pipeline fix landing**: once
  `boundary_node_ids` is populated correctly, re-run the same load-time
  check Round 4/7/8 all used (`loadGraph()` timing) — Issue I's whole
  point is that this number is very likely to change once the funnel
  mechanism actually runs, and needs to be re-confirmed acceptable, not
  assumed from Round 8's now-known-incomplete measurement.

### Phase 2 Hardening, Round 9 fixes — Issue K, master finding + Issue I/F, Issue E/H, DEPARE-drying gap (all pipeline-side, verified against real full-scale rebuilds)

Every item below was implemented, then verified against a fresh full-scale
`data/zeeland_clip` rebuild and real data queries (not just code review),
same discipline as Rounds 7/8. `data/zeeland_round9_final.sqlite` is the
final deliverable, combining all fixes below with the tuned parameters
chosen after the sweep in Issue I/F.

#### Issue K — `VERCLR=0` treated as a literal zero clearance

Fix: `_is_valid(verclr) and float(verclr) != 0.0` gates the clearance
branch in the bridge air-draft block (~line 155); `VERCLR=0` now falls
back to the same 999.0 default the movable-bridge and no-bridge-found
branches already use.

**Verified directly against the database, not just code review**:

| | Before fix | After fix |
|---|---|---|
| fixed bridges | 58 | 58 |
| fixed bridges with `height==0.0` | 16 | 16 (unchanged — this is the raw source data, correctly still recorded) |
| edges with `max_air_draft=0.0` | 408 | **0** |
| edges with `max_air_draft=999.0` | (not counted) | 126,653 (Issue-K-only build) |

**Route-level reproduction — partially done, not fully clean**: attempted
the same reproduction `routeiq` used (10.4km crossing near 51.550,3.800 ->
51.550,3.950, both directions, two vessel profiles) via a throwaway Node
script against `RoutingEngine.calculateRoute`, comparing `data/zeeland.sqlite`
(bug present) against a fresh Issue-K-only rebuild (bug fixed). The
comparison was **confounded by two things, both already documented
elsewhere in this file as real traps**: (1) real geometry nondeterminism
between separate pipeline rebuilds (same caveat as the master-finding
investigation's first "37.4nmi vs 51.4nmi" false alarm), and (2) the
Issue-K-only build still had the master-finding bug active (built before
that fix landed), so both "before" and "after" routes were independently
distorted by the then-still-broken funnel-upgrade mechanism, on top of
whatever Issue K itself changed. The data-level fix (408 -> 0 edges) is
solid and unambiguous; the specific "8.73x inflation gone" route-level
claim is **not cleanly reproduced this round** — a proper isolated test
(same code except VERCLR handling, both builds otherwise identical, e.g.
by diffing a single reverted hunk and rebuilding back-to-back) was staged
but not completed before this session's environment had a prolonged Bash/
Docker outage. Flagged as open, not assumed fixed at the route level.

#### Master finding (Issues A/C/D) + Issue I — `boundary_node_ids` population

Fix: `_split_deep_shallow` now also returns the depth-cut boundary
(`deep.boundary.intersection(shallow.boundary)`, computed the same way
`_split_wide_narrow`'s wide/narrow seam already was), and `build_network`
merges that into the seam-coordinate set passed to `build_navmesh_region`
for every deep piece, instead of only the original width-based seam.

**Verified directly against the fresh full-scale rebuild
(`data/zeeland_round9_final.sqlite`)**:

| | Before (live `zeeland.sqlite`, Round 8-era) | After (Round 9 final) |
|---|---|---|
| `navmesh_regions` count | 25 | 15 (see Issue I/F below for why) |
| regions with **empty** `boundary_node_ids` | **24 / 25 (96%)** | **0 / 15 (0%)** |
| regions with populated `boundary_node_ids` | 1 / 25 | **15 / 15** |
| `boundary_node_ids` per populated region (min/median/max) | n/a (only 1 region) | 32 / 63 / 695 |

This is a direct, unambiguous fix of the master finding: every region now
has real seam nodes tagged, so `routeiq`'s `precomputeFunnelEdges` (both
`upgradeRingBoundaryEdges` and `addAnchorShortcutEdges`) will actually run
for every region instead of silently no-op'ing for 24 of 25 of them.

**Land-crossing spot-check on the specific edge central to the master
finding's own writeup** (the 6,666.75m, `drval1=-2.0`, `crosses_land=0`
navmesh_boundary chord): the drying-aware `_crosses_land` check (see DEPARE
gap below) now catches this class of edge directly — the "Ring/stitch
drying-safety" pass in `_sanity_check_no_land_crossings` strips exactly
this kind of edge before export (see DEPARE section for the real strip
count from this build).

#### Issue I / Issue F — `min_navmesh_radius_m` + navmesh-boundary simplify pass, done together as instructed

Fix 1 (`min_navmesh_radius_m` 300.0 -> 800.0m): raises the disk-radius
threshold for navmesh eligibility, per §5.2.3 item 2's recommendation
(the same lever Issue F's "narrow water still getting ring-boundary
treatment" symptom was tracked to).

Fix 2 (`NAVMESH_BOUNDARY_SIMPLIFY_M`, new constant): a coarser
Douglas-Peucker simplify pass applied to a navmesh piece's own boundary,
right before it becomes PSLG input, on top of (not instead of) the fine
1.0m tolerance still used for the wide/narrow and deep/shallow
classification decisions and medial-axis centering. Safe with exact-match
seam tagging because `simplify()` only ever removes vertices, never moves
a retained one.

**Region eligibility (radius fix), verified against the real rebuild**:

| | Round 8 (`radius=300`) | Round 9 (`radius=800`) |
|---|---|---|
| `navmesh_regions` count | 25 | **15** |
| region area min/median/p90/max (m²) | 382,691 / 3.19M / 41.6M / 503.8M | **2,023,016 / 6,641,065 / 53,999,316 / 461,488,520** |
| regions under 10,000 m² | 0 | 0 |

Min area roughly 5.3x larger, consistent with a disk-radius threshold
increase of 800/300 ≈ 2.67x (~7.1x by area) filtering out channel-scale
water — a real, measured effect, not a fluke.

**Boundary-simplify tolerance — tuned empirically via a 3-way full-scale
sweep, not guessed**, because an early version (15.0m) traded away more
depth-safety margin than expected:

| `NAVMESH_BOUNDARY_SIMPLIFY_M` | vertices/region (min/median/max) | `boundary_node_ids`/region (min/median/max) | navmesh_boundary edges | avg depth | `<6.0m` | `<3.0m` |
|---|---|---|---|---|---|---|
| none (~0, ablation) | 202 / 1247 / 16317 | 67 / 191 / 2246 | 15,668 | 6.88m | 24.7% | 0.9% |
| **5.0m (chosen)** | 59 / 125 / 3414 | 32 / 63 / 695 | 3,870 | 7.17m | 30.9% | 3.9% |
| 15.0m (first try, rejected) | 41 / 80 / 2532 | 24 / 39 / 376 | 2,350 | 7.25m | 30.9% | 6.0% |

5.0m captures most of the vertex-count reduction (the whole point of
Issue I — bounding `precomputeFunnelEdges`'s O(boundary) ring-upgrade and
O(anchors²) shortcut cost) while costing much less real depth-safety
margin than 15.0m did (`<3.0m`, genuinely shallow: 3.9% vs 6.0%). Both
`<6.0m` and `<3.0m` are real regressions vs. the no-simplify ablation
(24.7%/0.9%) — any coarsening trades some of Round 7/8's depth-accuracy
win for density reduction — but 5.0m is the better-measured point on that
curve, not an assumption. **Honest framing, not spun as free**: this is a
genuine trade-off, not a strict improvement over Round 8's 14.3% `<6.0m`
figure (Round 8 didn't separately report `<3.0m`) — accepted because
Issue I's whole point is that leaving boundary vertex density
unaddressed risks reintroducing Round 4/5's load-time blowup once the
master-finding fix actually lets `precomputeFunnelEdges` run for every
region (previously it was silently disabled for 96% of them). The
`loadGraph()` re-time this trade-off is supposed to justify was **not
completed this round** (see "Not yet done" below) — that number is what
would confirm 5.0m was the right place to land, not just a reasonable one.

#### Issue E — bridge-opening quadrant connector, no land-crossing check

Fix: `_add_opening_bridge_edges`'s quadrant search now walks each
quadrant's candidates nearest-first and takes the first one that doesn't
genuinely cross land/drying terrain (via `_crosses_land`), instead of
blindly connecting to the nearest node and hardcoding `crosses_land=0`.
Also removed opening-bridge edges' blanket exemption from
`_sanity_check_no_land_crossings`'s audit pass (defense in depth, not the
only check now).

**Verified two ways against the real rebuild**:
1. **The exact edge originally flagged** (bridge node `509558598386510` ->
   `509558850385884`, 434m, confirmed genuinely crossing land against real
   `land_polygons.geojson`): the bridge node is present in the new build
   with the same coordinates, but **no longer connected to that target** —
   it now has 3 quadrant edges instead of 4 (the fourth quadrant's nearest
   candidate crossed land and was correctly skipped, with no fallback
   candidate found in that quadrant).
2. **Broad sample**: all 109 edges from the 36 opening-bridge POI nodes in
   the rebuilt database, checked directly against real `land_polygons.geojson`
   and the drying-DEPARE layer (not the stored `crosses_land` column) — **0
   genuine land crossings, 0 genuine drying crossings**.

#### Issue H — generic stitching connectors crossing land

Fix: `_stitch_component_pieces`'s `try_add` already called `_crosses_land`
before adding a connector; it now also benefits from the drying-aware
extension (below).

**Verified against the real rebuild**: all 135 `_stitch_component_pieces`-
created connector edges (`edge_kind_id=1`, `source_id IS NULL`, the exact
signature `try_add` stamps) checked directly against real land + drying
polygons — **0 genuine crossings**.

**Important correction, found while verifying — not fixed this round**:
the *specific* edge originally cited for Issue H
(`1157178078357594` -> `1157195826356045`, 1209m, near Middelburg) is
**still present and still genuinely crosses land** in the new build.
Re-checked its `edge_kind_id`: **0 (centerline), not 1** — it is **not**
a `_stitch_component_pieces` artifact at all, but a raw `inland_waterways`
centerline edge from `_build_inland_network`, which takes source line
topology as-is with **no land-crossing check of any kind**. Broader sample
confirms this is a real, larger, previously-uncharacterized gap: of 1,189
long (>150m) inland-waterways centerline edges in the rebuilt database,
**55 (4.6%) genuinely cross land** — much higher than the ~0.17% rate
Round 5/6 found for coastal skeleton edges. Plausible mechanism, consistent
with Round 6/8's own already-documented gap: locks have no dedicated
connectivity mechanism, so a source line that's digitized straight across
an interrupted lock chamber produces exactly this signature. **Deliberately
not fixed this round**: `_ensure_coastal_connectivity`'s stitch-guarantee
pass explicitly excludes inland-type nodes
(`node_type != "inland"`), so naively stripping these 55 edges the way
skeleton edges are stripped could fragment the inland network with no
repair mechanism at all — needs its own careful investigation (most likely
alongside a real lock-connectivity mechanism, not a blind strip pass)
before touching it. Recorded here precisely so it isn't lost.

#### New — DEPARE/drying gap (surfaced during the master-finding investigation)

Fix: new `_drying_gdf()` (cached DEPARE polygons with `DRVAL1 < 0.0`,
i.e. charted drying/intertidal) and `_crosses_land` extended to check
against it in addition to the `land` layer. `_sanity_check_no_land_crossings`
gained a new "Ring/stitch drying-safety" pass, scoped to
`edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY` (ring-perimeter + stitch
connectors, not medial-axis skeleton centerlines — deliberately excluded
per Issue G's braided-tidal-flat concern, see below) — checks and strips
genuine drying crossings the land-only check structurally could never see.

**Verified directly from the real rebuild's own log**
(`data/zeeland_simplify5m_run.log`): `Ring/stitch drying-safety: stripped
4 directed edges (0.11% of 1,796 navmesh-boundary-kind edges crossed
charted drying/intertidal terrain)` — a real, small, working correction,
not a no-op. `_add_opening_bridge_edges` and `_stitch_component_pieces`
both benefit from the same extension at edge-creation time (see Issue E/H
verification above — 0 crossings of either kind for both edge classes).

#### Issue G — investigated, root cause narrowed, NOT fixed (per explicit instruction)

Investigated the two candidate hypotheses from the Round 9 collection
write-up directly against real data in the Yerseke bbox
(4.18-4.25°E, 51.48-51.53°N, Oosterschelde braided tidal-flat area):

**Hypothesis 1 (DEPARE coverage gap) — ruled out.** DEPARE covers
99.9999...% of `coastal_water` in this bbox (23,516,174 m² coastal water,
23,516,174 m² DEPARE coverage, 0 m² with no DEPARE data at all). Not a
coverage gap.

**Hypothesis 2 (`DEPTH_SPLIT_CLOSING_RADIUS_M=50m` too large for this
terrain) — confirmed, with a real measurement, not just plausibility.**
Isolated the deep (`DRVAL1>=6.0`) DEPARE union in this bbox before any
closing: 30 real interior holes (drying/shallow separators fully enclosed
within the deep mask), totaling 32,862 m². At Round 7's original 5m
closing radius, 25 of those 30 holes survive (32,691 m², essentially
unaffected). At Round 8/current's 50m closing radius, **only 1 hole
survives (8,390 m², a 74.5% reduction in real excluded drying area)** —
the closing operation is filling in genuine interior drying separators,
not just bridging misaligned survey-contour seams between distinct deep
polygons the way it was intended to. Separately confirmed this isn't
about bridging *between* distinct channels: the two largest deep pieces
in this bbox are 1,855m apart, far beyond what a 50m closing could ever
connect — the effect is entirely from filling small *interior* holes
within a single already-connected deep polygon.

**Not fixed this round, per explicit instruction to investigate before
fixing.** A real lead exists for a future round (distinguish "thin
survey-contour misalignment seam" from "real charted drying separator" by
checking whether the gap already has its own shallow/drying DEPARE
polygon covering it, rather than treating any gap under the closing
radius as noise) but implementing and verifying it needs its own session.

### What's still open after this round — read before assuming Round 9 is fully closed

- **Issue K route-level reproduction** — data-level fix (408 -> 0 edges)
  is solid; the route-level "8.73x inflation gone" claim needs a clean,
  isolated before/after rebuild (same code except the VERCLR fix, both
  builds otherwise byte-for-byte from the same input) that wasn't
  completed this round (blocked by a prolonged environment Bash/Docker
  outage near the end of the session — see below).
- **`loadGraph()` re-verification in `routeiq`** — Issue I's whole
  point (raising `min_navmesh_radius_m` + adding the boundary simplify
  pass) is to keep `precomputeFunnelEdges`'s real cost bounded now that
  the master-finding fix lets it actually run for every region, instead
  of the 96%-silently-disabled state Round 8's "1.84s" was measured
  under. **This number was not re-measured this round** — do not assume
  it still holds, exactly the same caution this file already gave for
  Round 8's figure before this round started. Needs the same
  `loadGraph()` timing check Round 4/7/8 all used, against
  `data/zeeland_round9_final.sqlite`.
- **Inland-waterways centerline land-crossing gap (new, found verifying
  Issue H)** — 55/1,189 (4.6%) of long inland centerline edges genuinely
  cross land; not fixed, needs its own investigation given
  `_ensure_coastal_connectivity` doesn't cover inland nodes at all (see
  Issue H write-up above for the full reasoning).
- **Issue G** — root cause narrowed to a real, measured mechanism
  (`DEPTH_SPLIT_CLOSING_RADIUS_M` over-filling genuine interior drying
  separators specifically in braided tidal-flat terrain), not fixed.
- **Issue B** — already resolved from `routeiq`'s side (does not
  reproduce as originally described; see that repo's `ROUTEIQ_NEXT_PHASES.md`).
- **Environment note, not a code finding**: this session hit a prolonged
  (~35+ minute) Auto Mode Bash-safety-classifier outage near the end,
  which blocked further `python3`/`sqlite3`/Docker-based verification
  (plain shell commands like `cp`/`grep`/`ls` kept working throughout).
  The two items above (Issue K route-level check, `loadGraph()` re-time)
  are exactly the two verification steps that outage prevented completing
  — not skipped by choice.

### The two blocked verification items, completed independently — and a real root cause found for the persistent route inflation

Picked up exactly where the previous session left off, in a fresh
session with a working environment. Rebuilt from current `HEAD`
(`029487e`) independently — `data/zeeland_round9_verify.sqlite` — and
re-confirmed all six fixes' numbers directly, not just trusted the prior
commit messages: **0** edges with `max_air_draft=0.0` (was 408), **0/15**
`navmesh_regions` with empty `boundary_node_ids` (was 24/25), 15 regions,
navmesh_boundary depth avg 7.17m, median region vertex count 125. All
match the previous session's claims.

**`loadGraph()` re-timed**: **1.72s** against the fresh build (`routeiq`,
same Docker node:22 pattern as every prior round) — confirms Issue I's
tuning genuinely holds even with the funnel-upgrade mechanism now
actually running for every region, not the 96%-disabled state Round 8's
1.84s was measured under.

**Issue K's route-level check, done — and it surfaces a real, distinct,
still-open root cause.** Reproduced the exact scenario from the previous
session's report (10.4km direct crossing straddling Zandkreeksluis,
both directions, two vessel profiles) against the fresh build:

| profile | distance | straight-line | ratio |
|---|---|---|---|
| draft 1.2m / air 17.0m, A→B | 76.28km | 10.37km | 7.35x |
| draft 1.2m / air 17.0m, B→A | 76.25km | 10.37km | 7.35x |
| draft 2.3m / air 11.5m, A→B | 76.28km | 10.37km | 7.35x |
| draft 2.3m / air 11.5m, B→A | 76.25km | 10.37km | 7.35x |

Still severe — **but notice the ratio is identical across every profile**,
including a shallow-draft/generous-air-draft vessel that should have no
constraint problem at all crossing here. That rules out depth/air-draft
avoidance as the current cause (Issue K's own fix is real and confirmed,
it just isn't what's driving this specific number). Traced the actual
route geometry: it runs all the way south to ~51.494°N,3.613°E (the
Middelburg canal area) before swinging east — the same detour signature
as the original Issue A report.

**Root cause, confirmed by direct query, not inference**: checked for
any edge directly connecting the Oosterschelde side of the Zandkreeksluis
lock chamber to the Veerse Meer side. **Zero exist** — 20 real nodes on
the west side, 14 on the east side, no edge between any pair of them.
This is the structural "locks have no dedicated connectivity-generating
mechanism" gap this project has *identified* several times (Round 6/8's
writeups, `PHASE_4_DESIGN.md` §4c) but **never actually implemented a
fix for** — bridges get a real, precise opening-point edge
(`_add_opening_bridge_edges`); locks only ever annotate an edge's
attributes, never create one. **None of Round 9's six fixes touched
this** — they were never going to resolve this specific route's
inflation, because this was never their target. The VERCLR fix, the
master finding, and the density work are all real and independently
confirmed above; they just aren't the reason the Zandkreeksluis-area
route is still bad.

**Not reproduced**: the previous session's cut-off message mentioned a
possible regression for one profile/direction that was previously
direct. This fresh, clean rebuild shows consistent (not regressed)
severe inflation across all four tested combinations — either that was
specific to a transient build state before the session ended, or it
self-resolved; not chasing it further without a reproducible case.

**Concrete next step**: implement real lock-crossing connectivity,
mirroring `_add_opening_bridge_edges`'s pattern (a precise opening-point
node where the lock chamber meets navigable water on each side,
connected via a real edge, not a generic distance-based stitch) — this
is very likely the actual fix for Issue A/the original user report, not
anything in this round's six fixes. Worth its own round, same rigor as
everything else: verify with a real before/after route reproduction of
this exact scenario, not just that an edge now exists.

### Phase 2 Hardening, Round 10 — lock-crossing connectivity implemented (real, measured improvement; inflation NOT fully resolved)

**Implementation**: `_add_lock_crossing_edges` (`nautical_routing_pipeline.py`,
called from `run_pipeline` right after `_add_opening_bridge_edges`),
mirroring the bridge version's pattern but adapted for a lock's two-gate
topology instead of a bridge's single mid-span opening. For each of the
17 lock polygons in `locks_gdf`: intersect each fairway/inland-waterway
feature crossing the polygon against the polygon's own *boundary*
(`lock_geom.boundary.intersection(hw_row.geometry)`) to get that
feature's own entry/exit point pair — kept per-feature rather than
pooling every point across every intersecting feature and taking the
global farthest pair, which real data showed can otherwise pick two
points from unrelated features (a convergence of short unrelated channel
segments near one lock produced a spurious "pair" that genuinely
crosses land). Candidate pairs are tried widest-span-first, gated by
`_crosses_land` (Issue E's land/drying-crossing check, applied here from
the start, not bolted on after) — the first pair that clears land wins;
if none do (or no fairway/waterway crosses the polygon at all), falls
back to a single bridge-style centroid node. Each side gets a real node
connected outward via the same quadrant ray-casting + `_crosses_land`
gate `_add_opening_bridge_edges` uses, plus one explicit chamber-transit
edge directly between the two side nodes — the actual connectivity fix,
since two independently-shore-connected side nodes still don't connect
to *each other* without it. Edges are tagged `requires_lock`/`lock_id`
(new `edges` columns, additive, no `schema_version` bump), the marker
`PHASE_4_DESIGN.md` §4c called for — analogous to `is_opening_bridge_edge`
but kept separate since a lock chamber transit and a bridge opening are
physically different enough (cycle time vs. instantaneous) that
`routeiq`'s `feature-bridge-lock-waits.md` will eventually need to tell
them apart; the transit edge itself additionally carries
`is_lock_transit_edge` (in-graph only, not exported) for that same
future distinction. Also extended `_edge_attr_worker`'s obstacle-crossing
exemption (previously opening-bridge-edges-only) to cover lock-crossing
edges too, same reasoning as the existing bridge exemption.

**Generalizes across the dataset, not just Zandkreeksluis**: rebuilt
against `data/zeeland_clip` (`data/zeeland_round10_locks.sqlite`) —
"Added 198 lock crossing edges (30 chamber-transit) across 17 lock
polygons (2 single-node fallback)." Direct query confirms all 17 lock
polygons got `requires_lock=1` edges (`lock_id` 1-17, 2-18 edges each);
15/17 got the full two-node-plus-transit treatment, 2/17 (locks with no
fairway/waterway crossing their polygon at all in this dataset —
"Kleine Sluis - kolk 1" and one unnamed lock) fell back to the
single-centroid-node pattern as designed.

**Direct-query verification of the specific Zandkreeksluis gap — with an
important complication found along the way**: re-ran the exact west/east
node-set check that found "zero edges" last round. The new build does
show a direct edge connecting the two sides (`1157557482386202` west,
`1157558706386550` east, 244.37m) and BFS from any west node now reaches
100% of east nodes. **But checking the *same coordinates* against the
pre-fix database (`data/zeeland_round9_verify.sqlite`) found that exact
edge already there** — `edge_type_id=1` (inland), sourced from
`_build_inland_network`, not from anything this round added. The
inland-waterway centerline threading through Zandkreeksluis happens to
have a digitized vertex pair that brackets the chamber closely enough
that this round's new opening-point nodes snapped onto the *same*
pre-existing coordinates (`_get_or_create_node`'s coordinate-dedup keys
on rounded lon/lat only, not node type, so a "coastal" lookup silently
returns an already-existing "inland" node at the same spot). **This
means the original "zero edges connect either side" finding, while
correctly identifying a real structural gap in general, was not actually
demonstrating a gap at Zandkreeksluis specifically in this exact rebuild
— an inland-network coincidence already bridged it.** Coordinate-level
diffing of the routed path confirmed the same thing operationally: the
route's geometry through the immediate Zandkreeksluis chamber area is
*byte-identical* between the pre-fix and post-fix builds (same 6-vertex
sequence either side of the crossing) — this round's fix did not change
how that specific crossing is traversed at all. Whatever *is* different
about the route (see below) comes from elsewhere along the path, most
likely one of the other 16 locks (`data/zeeland_clip` also has locks near
Veere — "Grote Sluis"/"Kleine Sluis" — on the same detour route) gaining
real connectivity this round where it previously had none; a full
edge-by-edge diff of the two ~700-vertex routes to pin the exact segment
wasn't completed this round given time already spent, but the aggregate
before/after effect below is real and cleanly measured regardless of
which specific lock produced it.

**Route-level reproduction — clean, isolated, back-to-back A/B, not a
comparison against an older/separately-built snapshot.** The repo's own
history (see Round 9's "8.73x inflation gone" note above) already
documents real geometry nondeterminism between *separately-run* pipeline
builds as a confound for exactly this kind of before/after claim — hit
it again this round (a fresh `data/zeeland_round9_verify.sqlite` rebuild
this session reproduced the historical 7.35x exactly, but the base
network's own raw node/edge counts before any bridge/lock code runs
still varied run-to-run: 29,590/62,939 vs. 30,127/64,579 edges for
supposedly-identical code). Controlled for it the way this file's own
Round 9 writeup recommended: one code commit, one line
(`self._add_lock_crossing_edges()`) toggled off for a control build, two
back-to-back rebuilds from the same process session, otherwise identical
input and code (`data/zeeland_round10_control.sqlite` vs.
`data/zeeland_round10_locks.sqlite`):

| scenario | profile | control (fix disabled) | fix enabled | change |
|---|---|---|---|---|
| Zandkreeksluis crossing (51.550,3.800→51.550,3.950), A→B | 1.2m/17.0m | 86.45km, **8.34x** | 57.52km, **5.55x** | -33.5% distance |
| Zandkreeksluis crossing, B→A | 1.2m/17.0m | 86.43km, 8.33x | 57.49km, 5.54x | -33.5% distance |
| Zandkreeksluis crossing, A→B | 2.3m/11.5m | 86.45km, 8.34x | 57.52km, 5.55x | -33.5% distance |
| Zandkreeksluis crossing, B→A | 2.3m/11.5m | 86.43km, 8.33x | 57.49km, 5.54x | -33.5% distance |
| Issue A repro, Oude-Tonge→Zierikzee | 1.2m/17.0m | 67.80km, 2.53x | 61.48km, 2.29x | -9.3% distance |
| Issue A repro, reverse | 1.2m/17.0m | 67.77km, 2.53x | 61.45km, 2.29x | -9.3% distance |
| Issue A repro, Oude-Tonge→Zierikzee | 2.3m/11.5m | 71.06km, 2.65x | 64.33km, 2.40x | -9.5% distance |
| Issue A repro, reverse | 2.3m/11.5m | 70.97km, 2.64x | 64.70km, 2.41x | -8.9% distance |

(straight-line: Zandkreeksluis 10.37km, Oude-Tonge→Zierikzee 26.84km;
straight-line distances match a straightforward haversine and are
identical across both builds, as expected.)

**Verdict — real, causally-confirmed, but partial.** The fix has a
genuine, sizable, reproducible effect in a properly-controlled test
(-33% on the direct crossing that motivated this round, -9% on the
original Issue A report), and generalizes structurally across all 17
locks in the dataset, not just Zandkreeksluis. **It does not come close
to resolving the inflation**: 5.5x and ~2.3-2.4x are both still severe
detours for what should be near-direct crossings. Whatever is causing
the *remaining* inflation is a separate, still-open problem — not
constraint-avoidance (Round 9 already ruled that out for the
Zandkreeksluis scenario; identical ratios across profiles held in every
build this round too) and, per the diffing above, not exclusively the
lock-connectivity gap either, since fixing it structurally and confirming
it via direct query still leaves a >5x detour. Worth a fresh
investigation of its own, ideally starting from an edge-by-edge diff of
a control-vs-fix route pair (not attempted this round) rather than
another hypothesis-then-verify cycle.

**`loadGraph()` re-checked, no regression**: 1,660ms (fix enabled) /
1,764ms (control, fix disabled) / 1,755ms (fresh Round 9-equivalent
rebuild) — all within the same ~1.6-1.8s band Round 9 measured (1.72s),
consistent with the small edge-count addition (+198 edges out of
~65,000) being cheap, as expected.

**Round 11 update (routeiq-side, not this repo — see that repo's
`ROUTEIQ_NEXT_PHASES.md`)**: following up on "the remaining inflation
needs a fresh investigation" above, found a real, precisely-located bug
in `routeiq`'s path reconstruction (`aggregateSegmentEdges`,
`src/database.ts`) — confirmed 0 of 270 segments in a real reproduced
route carry any funnel-computed interior geometry, despite the
funnel-upgrade mechanism itself working perfectly (100% success,
verified with temporary instrumentation). The aggregation function used
whenever path-smoothing skips over a direct edge silently drops
`path_points`, so any funnel-curved stretch the smoothed path spans gets
flattened to a straight chord in the *displayed* route. Likely **not**
the cause of the distance/cost inflation itself (the aggregated distance
is real and correctly summed) — a separate, real bug in what gets drawn,
not necessarily in what gets chosen or its reported length. Not fixed
yet; full writeup and the reasoning for why it's probably distinct from
the inflation problem is in the `routeiq` doc.

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
nodes, the *current, unmodified* `routeiq` TS runtime — which doesn't
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

## Phase 2 — Funnel-algorithm consumption (separate repo: `routeiq`)

Out of scope for `signalk-router-pipeline` — this is TypeScript work in
`routeiq/src/routing.ts` / `src/database.ts`, implementing the full
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

## Phase 3 and beyond (pointer only — see `PHASE_3_DESIGN.md`)

Detailed design (data sources, conflation strategy, concrete file/script
names, schema additions, phase ordering) now lives in
[`PHASE_3_DESIGN.md`](PHASE_3_DESIGN.md), not here — that document assumes
Phase 0-2 and Phase 2 Hardening are working and does not re-litigate their
verification, which stays in this file. Six sub-phases: OSM/OpenSeaMap
(tier 3) and GEBCO/EMODnet (tier 4) data fusion; the human/AI-assisted
override-authoring workflow against `router-data`'s `overrides/`
directory; EMODnet vessel-density and MarineCadastre AIS
validation/gap-filling; scale-out to full NL, then a first NOAA-charted US
region; supernode/macro-edge hierarchical routing. None of these are
blocked by Phase 1/2, but doing them before the navmesh-region generation
was real (Phase 1) or consumable (Phase 2) would have meant building on
top of a graph that's still fundamentally a point cloud in open water —
sequence mattered, and now that both are working, this is the next real
work after the still-open Phase 2 Hardening item above (§5.2/"Round 6") is
resolved.

Three more sub-phases, checked against the original project roadmap and
confirmed genuinely new rather than a gap in it, are designed in
[`PHASE_4_DESIGN.md`](PHASE_4_DESIGN.md): position-/route-aware dynamic
database loading (`routeiq` only — stop loading every downloaded
region into memory unconditionally); AI-vision-assisted resolution of
genuinely ambiguous path choices (extends 3c's override workflow with a
new trigger category and a concrete vision-model input/output design,
still human-reviewed, never a live per-query call); and bridge/lock
wait-time & schedule data (new `pois` fields, sourced via 3c's override
workflow same as the AI-vision case — the routing-cost/ETA consumption
side is `routeiq`'s `feature-bridge-lock-waits.md`, not this repo).

## Critical files

- `signalk-router-pipeline/nautical_routing_pipeline.py` — Phase 1, all of it
- `signalk-router-pipeline/requirements.txt` — add `triangle`
- `router-data/specs/routing-database-format-specification.md` §2.9, §6 —
  authoritative schema/consumption contract for both phases
- `routeiq/src/db-worker.ts` — defines the "must stay routable by the
  unmodified runtime" compatibility bar for Phase 1; Phase 2's actual
  target for the funnel-algorithm implementation
- `routeiq/test/zeelandbrug_test.ts` — manual validation target, unchanged
