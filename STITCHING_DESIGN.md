# Cross-Database Seam Stitching — Design Note (for review)

**Status: IMPLEMENTED (registry, `0cd3467`) and now MEASURED end-to-end —
see §8 for the Section 6 regression result, which changes the picture: the
seam turns out to be crossable *without* the registry, because the clip
itself puts coincident vertices on the shared cut line. Read §8 before
acting on §3.** The mechanism (user direction): a **shared global-node
registry** (§3) — seam nodes authored once and shared across builds so IDs
coincide by construction. Consolidates the two de-risking experiments run
2026-07-20 (Chunk 1, Chunk 2) that ruled out recompute-based coincidence,
and the recommended design. Supersedes the mechanism discussion in
`PHASE_4_DESIGN.md` §4a.1 (that section's dynamic-loading interplay still
applies; its build-time candidate-stamping mechanism is superseded by the
findings below).

---

## 1. Problem

US East Coast coverage ships as adjacent per-area `.sqlite` files
(generated via `build_region.sh --clip-bbox` → `clip_pilot_data.py`).
Independently-built adjacent files **cannot route across their shared
border**: node IDs come from `_coord_to_id` (`nautical_routing_pipeline.py:1300`,
pure arithmetic on `round(lat,5)`/`round(lon,5)`), and adjacent builds do
not place nodes at coincident coordinates, so the two files' node-ID sets
don't overlap. When both load into one in-memory graph (routeiq's committed
dynamic loader), the result is two disconnected node sets side by side.

The question this note answers: **how do adjacent databases become one
connected routable graph across the seam?**

---

## 2. What we ruled out — two experiments

We tested whether adjacent builds could be made to emit **identical node
IDs at the seam** (which the committed loader already merges automatically,
via `nodesByDbIndex`/`nodeDbCount`). Both approaches failed for the same
architectural reason.

### 2.1 Chunk 1 — plain overlap band → REJECTED (4% coincidence)

Added a `--overlap-deg` clip-expansion (committed, `ac91c8a` — kept, it
feeds the recommended design). Built two overlapping Zeeland clips, measured
node-ID coincidence in the shared band:

- Raw ENC source **coordinates** agree at **99.4%** — the shared-data
  premise holds.
- But only **4.3%** of graph nodes are raw-vertex passthroughs; the other
  **~96% are pipeline-*derived*** (`_split_wide_narrow`'s
  `buffer(0).simplify()`, medial-axis skeleton sampling, navmesh
  triangulation).
- Derived-node coincidence: **0.6%**. Overall seam coincidence: **4.0%**.

### 2.2 Chunk 2 — global-tiling shared cut line → INSUFFICIENT (50% navmesh / 0% skeleton)

Made Round 23's tiling grid absolute (a fixed world grid instead of the
current per-piece `minx + i*width/nx`), built two clips sharing the global
line x=567000:

- **Navmesh seam nodes: 50%** coincidence (2/4) — better than 4%, not the
  ~100% needed.
- **Skeleton seam nodes: 0%** — tiling only runs on navmesh pieces; channel
  crossings get nothing.
- Two failure modes: (a) **FP rounding-boundary flip** — separate GEOS
  intersection calls land 51.53000 vs 51.52999 on the same physical point;
  snapping the on-grid axis helps but the along-line position stays
  nondeterministic; (b) **structural** — whether a global line even gets
  *cut* in both builds depends on each file's own piece decomposition,
  which differs by clip extent.

### 2.3 Root cause (both) and why we stop here

The pipeline's derived geometry — component decomposition, wide/narrow and
deep/shallow classification, medial-axis skeletons, navmesh triangulation —
is a **global function of each file's extent**. Two files see different
extents → different derived nodes, even in shared water. Global tiling fixes
only the navmesh *cut geometry*, not the upstream decomposition, and never
touches skeletons.

The **only** way to make build-time coincidence fully work is to
re-architect the pipeline **tile-first**: clip source water to a global tile
grid *before* classification, and process each tile independently so its
geometry is a function of `(source ∩ tile)` alone. This is a large change
and would alter routing quality (a channel classified on a tile fragment
instead of its whole water body). **Rejected as not worth it.**

---

## 3. Recommended design — shared global-node registry (build-time)

**(User direction, 2026-07-20.)** The experiments failed because they tried
to make two independent builds *recompute* the same seam geometry — which is
extent-dependent, so it never matches. This design **authors each seam node
once and shares it**, so IDs coincide *by construction*, never by
recomputation — the same principle that makes `user-edits`/overrides survive
regeneration (a single authoritative layer merged in, not two things hoping
to agree).

### 3.1 The registry
A small shared SQLite — the **global-nodes registry** — living alongside the
overrides/`user-edits` authority in `router-data`. One row per published
seam node: `{node_id, lon, lat, node_kind_id, node_depth, source_region}`,
bbox-indexed. It is the cross-build source of truth for seam-node identities.

### 3.2 Publish pass (after building region R)
Take R's **boundary nodes** — nodes within `stitchBandM` of R's coverage
envelope (its *clip* edge, the artificial data cut, not natural coastline) —
and write any not-already-global rows into the registry, tagged
`source_region=R`. Idempotent. Dedupe: skip a boundary node that sits within
a small radius of a node R just *adopted*, so the registry doesn't
accumulate near-duplicate seam nodes over many builds.

### 3.3 Adopt pass (during/after building region S)
Query the registry for global nodes within S's bounds. For each: insert it
**verbatim** (same `id`/`lon`/`lat` — S does NOT recompute it), and add
edge(s) from it to S's nearest **native** node(s) "where possible" — within
`stitchRadiusM`, same water polygon, `_crosses_land`-gated, ≤2 per node. If
no valid native neighbour exists, the node is included but left dangling on
S's side (harmless — it connects on the author's side; overlap sizing in
§3.5 is what keeps this from happening at a real seam).

### 3.4 Registry is the authority; every build adopts-then-publishes
Once a seam node is registered it is **frozen** — later adjacent builds adopt
it rather than generating their own. This is what makes it robust to
**rebuilds**: rebuilding R re-adopts R's own frozen boundary nodes (stable
IDs) and reconnects them to R's freshly-computed interior, so a rebuild never
silently shifts a seam and breaks the neighbour. `source_region` lets a
rebuild cleanly replace its own prior contribution. **One-sided adoption is
sufficient** — a seam needs only one shared, connected node-set, so whoever
builds second adopts the first's boundary nodes; order never breaks it, and
there is no direct neighbour-awareness (only a loose dependency through the
shared artifact, like a cache).

### 3.5 Overlap guarantees reachability (why `--overlap-deg` earns its keep)
An adopted node must have native nodes nearby in S's graph to attach to. Set
the committed **`--overlap-deg` ≥ `stitchBandM`** so R's published edge-nodes
land *inside* S's real water coverage with S-native neighbours to connect to.
This is the concrete overlap value, and the real reason overlap matters here
(it did not, for coincidence).

### 3.6 Covers skeleton seams; runtime is nearly free
The registry is **node-type-agnostic** — a published boundary node can be a
skeleton endpoint, navmesh vertex, anything — so Chunk 2's fatal "0% for
skeletons" gap disappears. At runtime there is **no proximity matcher, no
per-load pairing, no synthetic edges**: the seam edges are real, baked into
each `.sqlite`, and the **already-committed node-merge** (`nodesByDbIndex`/
`nodeDbCount`) unions the shared-ID nodes. The only routeiq work is the
coincident-node **edge-union test** PR1's investigation flagged as missing
(prove that loading two DBs sharing node X yields X with *both* files'
edges).

### 3.7 Edge cases to nail down
- **Stale frozen node** (ENC update moves water so a frozen seam node is now
  on land): the adopt pass's land-check skips it and flags it — needs a
  retirement path in the registry.
- **Attribute source:** `node_depth` is frozen from the first author; each
  side computes its own *connector-edge* attributes from its own data, so a
  slight seam-point depth disagreement is bounded and harmless.
- **Duplication:** the shared node lives in both `.sqlite` files (author +
  adopter) but merges to one at load — minimal overhead (boundary nodes are
  a small fraction).

### 3.8 Design fork for review
**(a)** Bake adopted seam nodes+edges into each region `.sqlite` at build
time — recommended: self-contained, re-downloadable files, existing runtime
merge does everything, almost no routeiq change. **(b)** Ship the registry as
a *runtime overlay* routeiq merges like `user-edits` — keeps region files
"pure" but pushes stitching into a load-time overlay-merge (closer to the
proximity matcher). Recommend (a).

---

## 3-ALT. Alternative — runtime proximity matcher (routeiq)

*(Kept as the fallback if the registry design is rejected.)* Connect
**nearby** cross-database nodes with short synthetic edges at load time — do
not rely on ID coincidence. Node-type-agnostic, fully per-file independent,
needs no regeneration. Design basis: `PHASE_4_DESIGN.md` §4a.1.

**In-memory only** — synthetic edges are never written into any `.sqlite`
(files stay independently re-downloadable), same convention as the funnel
anchor-shortcut edges.

### 3.1 Candidate nodes
- **With a modest overlap band** (recommended, ~`stitchRadiusM`): candidates
  are the nodes of each DB inside the shared band. Both sides have real
  nodes in genuinely shared water, so connectors are short and land-checks
  trivially pass.
- Bbox-prefilter to the seam region so the point tests only run near the rim.

### 3.2 Pairing (closest-first)
For each pair of loaded DBs whose coverage bboxes intersect (or come within
`stitchRadiusM`):
- KNN across the two candidate sets, **process pairs in ascending distance
  order** so the shortest, highest-confidence links form first.
- Cap **≤2 connectors per node** (the Pass 0c/0d convention).
- Node-type-agnostic — a west skeleton node in a channel connects to the
  nearest east skeleton node in the same channel; navmesh boundary nodes
  likewise. This is why it covers both seam classes.
- Near-identical pairs (< ~5m) can be merged rather than connected (rare —
  the committed loader's node-merge already handles a true ID collision).

### 3.3 Gating and attributes
- Gate every connector through the runtime line-of-sight land check
  (`isLineCrossingLand` sampling — build-time polygon data isn't available
  at runtime).
- Conservative attributes: distance-based cost; `min_depth` = min of the two
  endpoint depths; short by construction so attribute uncertainty is bounded.

### 3.4 Provenance registry + dynamic-loading integration
- Register each connector with `{dbIndexA, dbIndexB}` provenance in a
  dedicated registry (not mixed anonymously into `edgesBySource`).
- On **load** of a region: stitch it only against already-loaded neighbours
  (incremental — cost scales with the new rim, not the whole world).
- On **evict**: remove exactly the connectors whose provenance references
  that region.
- **Route-triggered loads must include transit regions** — load every
  `not_loaded` region whose coverage intersects the route's search bbox, not
  just waypoint containment (a route A→C through B needs B loaded).

### 3.5 Expected cost
R23-style modest seam-crossing optimality inflation on crossing routes
(acceptable; recoverable later via Phase 3f anchors/hierarchy). Per-load
pairing pass is cheap and incremental.

---

## 4. Already in place (no further work)
- **`--overlap-deg`** (pipeline, committed `ac91c8a`) — feeds §3.1.
- **Dynamic loading** (routeiq, committed `94a0a27`) — peek, per-file
  load/evict, node-ID merge, on-demand load, `/databases/*` endpoints.
  `dynamicLoading` default flipped `false→true` (uncommitted working tree).

---

## 5. Open questions for review (registry design)

1. **Design fork §3.8:** bake seam nodes+edges into each `.sqlite` at build
   time (recommended) vs. ship the registry as a runtime overlay like
   `user-edits`.
2. **Registry location & format:** a shared SQLite in `router-data`
   alongside overrides/`user-edits`? Confirm home and whether it's
   git-tracked/published like the override authority.
3. **Rebuild/refresh policy:** freeze-on-first-publish + `source_region`
   replacement on rebuild (recommended). Confirm — this is what keeps a
   rebuild from shifting a seam under an already-built neighbour.
4. **`stitchBandM` / overlap sizing:** set `--overlap-deg ≥ stitchBandM`
   (§3.5). First-cut values (`stitchBandM` ~a few hundred m, `stitchRadiusM`
   for adopt-connections ~500 m); tune against a real fixture.
5. **Retirement path** for a frozen node that a later ENC update puts on land
   (§3.7) — flag-and-skip now, or an explicit registry retire/GC step.
6. **Drop the Chunk 2 global-tiling probe?** The registry covers navmesh
   *and* skeleton seams, so tiling adds nothing here. (Recommend drop; keep
   the branch only as an experiment record.)

---

## 6. Testing plan (when approved)
- **Primary regression:** two adjacent overlapping Zeeland fixtures; a route
  with endpoints in different files crosses the seam and matches (within
  tolerance) the single-file route for the same coordinates.
- No stitch connector crosses land (LOS gate fires).
- Skeleton-channel seam and navmesh seam each covered by a case.
- Evicting one DB removes exactly its connectors (registry provenance),
  graph otherwise intact.
- Transit-region load: a route through a `not_loaded` middle region loads it.

---

## 7. Rejected alternatives (recorded so they aren't retried)
- **Per-node grid-snap** (Chunk 1) — 96% of seam nodes are extent-dependent
  derived geometry; snapping them all needs a coarse pitch that distorts
  skeleton/navmesh geometry. 4% coincidence.
- **Global navmesh tiling for ID coincidence** (Chunk 2) — 50% navmesh,
  0% skeleton, FP + structural failure modes.
- **Tile-first pipeline rewrite** — the only full fix for build-time
  coincidence, but large and changes routing quality. Not worth it.

---

## 8. §6 primary-regression RESULT (2026-07-30) — the seam already crosses; the registry is not what makes it cross

Ran §6's primary regression for the first time (it had never been run — §3's
verification stopped at node-ID coincidence and two-sided edge presence).
Harness and fixtures: `local_only/local_scripts/round25_seamroute/`
(`pick_seam_endpoints.py` picks cross-seam pairs on a single-file baseline;
`run_seam_route_test.mjs` drives real `RoutingDatabase`/`RoutingEngine` over
five configurations; `seam_route_results.json` holds the raw numbers).

**Setup.** Three endpoint pairs, each with one end well inside the west clip
(lon 3.80–3.85) and the other well inside the east clip (lon 3.93–3.98), all
in lat 51.56–51.67, each verified routable and seam-crossing in a
**single-file combined baseline** built over the union extent. Five configs:
baseline; overlapping clips ±registry; abutting (zero-overlap) clips
±registry.

### 8.1 Results

| pair (baseline) | overlap+registry | overlap, no registry | abut+registry | abut, no registry |
|---|---|---|---|---|
| pair1 (6361 m) | 6398 m ×1.006 | 6394 m ×1.005 | 6424 m ×1.010 | 6374 m ×1.002 |
| pair2 (6844 m) | 7611 m ×1.112 | 7620 m ×1.114 | 7707 m ×1.126 | 7658 m ×1.119 |
| pair3 (5988 m) | 7012 m ×1.171 | 6973 m ×1.165 | 7453 m ×1.245 | 7466 m ×1.247 |

- **§6's primary regression PASSES** for the registry pair: every pair routes
  across the seam and matches the single-file route within 0.6–17.1%
  (tolerance 25%, R23-style inflation).
- **The crossing is genuine, verified on the graph, not the geometry.** The
  returned polyline can't prove this (routing.ts expands `edge_kind_id=1`
  edges into funnel `path_points` and smooths, so most vertices aren't graph
  nodes), so the harness probes the merged in-memory graph directly: a
  shortest path's node-ownership string is `W…S…E` in every config — never a
  direct W→E hop — and **96.7–97% of the far file's exclusive nodes are
  reachable** from a node exclusive to the near file.
- **But every control passes too.** Overlapping clips with no registry cross
  just as well (and just as short), and so do **abutting clips with no
  registry at all**.

### 8.2 Why: the clip creates coincident nodes on the cut line

All 5 shared node IDs in the abutting no-registry pair sit at **exactly
lon 3.89** — the shared cut. `clip_pilot_data.py` clips both sides' polygons
to the same meridian, so both files get *identical* boundary vertices there
by construction; where those become graph nodes (`node_kind_id=0`,
source-vertex passthroughs), `_coord_to_id` hashes them identically and
routeiq's existing node merge unions them. Connectivity needs **one shared
node per water body**, not high coincidence — which is what Chunk 1's "4.0%
coincidence" number obscured: it measured the *fraction* of seam nodes that
coincide, a quantity routing doesn't care about.

Registry contribution, measured: shared IDs 60 → 211 (overlapping) and
5 → 8 (abutting); far-side reachability **unchanged** (96.7% → 96.7%,
97% → 97%); route distances within ~1% of the controls, sometimes slightly
worse. On abutting clips the adopt pass is nearly inert by construction —
east adopted **3** nodes, because west publishes nodes within
`stitch_band_m` *inside its own* coverage bbox (lon ≤ 3.89) while east
queries lon ≥ 3.89. That is §3.5's `--overlap-deg ≥ stitchBandM` requirement
showing up as a measurement.

### 8.3 What this does and does not settle

Settled: the US East Coast per-state files can route across their seams, and
`--overlap-deg` alone is sufficient on this fixture. Not settled: this is
**one seam in wide water (Oosterschelde) with dense ENC vertices along the
cut**. A seam that crosses a narrow skeleton channel, or open water with no
raw vertex near the cut line, may have *zero* coincident nodes — that is the
case the registry is for, and it is not exercised here. Before dropping or
keeping the registry, test a seam deliberately cut through a narrow channel
and through sparse open water. **→ Both now tested: §9.**

### 8.4 Two defects the run found

1. **Pipeline (fixed):** `_connect_adopted_node` took candidate ids from a
   pandas index, so they were `numpy.int64`. They hash/compare equal to the
   plain int (no duplicate node appears), but a freshly adopted node's
   adjacency dict is empty, so the numpy scalar became the stored key — and
   `sqlite3` binds numpy scalars through the buffer protocol, writing
   `edges.target` as an 8-byte **BLOB**. 273 rows in the 2026-07-20
   `east_stitched.sqlite` (100% of the outgoing halves of the adopt pass's
   own connector edges, `edge_kind_id=1`) were affected; routeiq drops them
   (target not in the node map), leaving those connectors one-way. Fixed with
   `int()` at the source plus an `int()` guardrail on every id at export;
   rebuilt pair verified all-`integer` and `missing=0`.
2. **routeiq (open):** the coincident-node merge unions both files' edges
   correctly (`missing=0`, `syntheticExtra=0`) but **does not de-duplicate**:
   62 of 211 shared nodes carry 118 duplicate adjacency entries where both
   files hold the same edge. Not fatal (A* just re-evaluates a neighbour) but
   it is exactly the gap `ROUTEIQ_NEXT_PHASES.md:37-42` predicted, and it is
   now measured rather than suspected.

---

## 9. The two seam classes §8.3 left open (2026-07-30) — the registry IS load-bearing in sparse open water, and nowhere else so far

§8 measured one seam in wide water and found the registry redundant there.
The two cases it flagged as untested are now built and measured, with the same
harness (`build_seam_case.py` builds a case; `run_seam_route_test.mjs
cases/<name>` runs it):

| case | seam | water at the cut | nearest source vertex to the cut |
|---|---|---|---|
| `narrow-channel` | lon 4.70, lat 51.88–51.92 | one ~120 m channel (stays ~120 m at ±0.02°/±0.04°) | 57 m |
| `sparse-openwater` | lon 5.33, lat 52.55–52.66 | IJsselmeer, 13.5 km wide | 5.6 km |

### 9.1 Narrow channel — self-stitches on a single node

| pair (baseline 3930 m) | overlap+reg | overlap, no reg | abut+reg | abut, no reg |
|---|---|---|---|---|
| pair1 | 3935 m ×1.001 | 3935 m ×1.001 | 3930 m ×1.000 | 3932 m ×1.001 |

Crossable in **all four** configs, 90.3–91.9% far-side reachability, every
path `W…S…E`. The abutting no-registry pair shares **exactly one** node — id
`1158828102470000` at lat 51.89669, **lon 4.70 exactly**, `node_kind_id=0` —
and that single coincident node carries the whole crossing (148-node path,
1 shared node on it). With overlap, 46 shared nodes appear spread across the
band (lon 4.690–4.707): raw channel-bank vertices inside shared water, free.
So the narrow/skeleton case behaves like §8: **one shared node per water body
is all connectivity needs**, and the clip line reliably produces one where the
banks have vertices.

### 9.2 Sparse open water — no crossing at all without the registry

| pair | baseline | overlap+reg | overlap, no reg | abut+reg | abut, no reg |
|---|---|---|---|---|---|
| pair1 | 5973 m | 12541 m ×2.10 | *(no graph crossing)* | *(none)* | *(none)* |
| pair2 | 12173 m | 13817 m ×1.135 | *(none)* | *(none)* | *(none)* |
| pair3 | 9855 m | 13448 m ×1.364 | *(none)* | *(none)* | *(none)* |

- **Without the registry: 0 shared node ids, 0/1633 far-side nodes reachable,
  no graph path — in the overlapping *and* abutting variants.** Both sides
  have nodes within 55–330 m of the cut; none coincide, because in vertex-free
  water every node is derived (skeleton sampling), which is Chunk 1's 0.6%
  derived-node coincidence at its limit.
- **With the registry (overlap): 36 shared ids, 86.3% far-side reachability, a
  real `W…S…E` path.** The registry is the *only* thing connecting this seam.
- **Registry + abutting clips: still nothing** (0 adopted — publish writes
  inside the publisher's own bbox, the adopter queries its own), confirming
  §3.5's `--overlap-deg ≥ stitchBandM` as a hard requirement, not advice.

Caveat on the ×-ratios: this IJsselmeer clip is **depth-degenerate** —
`min_depth = 0` on ~98% of its edges, and the single-file baseline is itself
flagged `via_constrained` — so the stitched routes run inside a penalized
search. The *connectivity* result (0% vs 86.3% reachable) is independent of
that and solid; the *magnitude* of the 1.14–2.10× inflation is not, and needs
a sparse-water fixture with real DEPARE coverage before being read as the cost
of stitching.

### 9.3 Third defect found: an unstitched seam fails silently, not loudly

In every sparse config with no graph crossing, `calculateRoute` still returned
a plausible-looking route — because routeiq projected the start onto the
nearest reachable waterway, which was **on the far side of the seam**, and
joined it with a straight line: `longestLegCrossingSeam` of **3485–4597 m**,
reported only as a `start_connecting` warning, with `minDepth: -1` synthetic
segments that bypass constraint checking. That is why §8's first pass showed
`crossesSeam=true` everywhere and why route distance alone is a useless
stitching metric (one un-stitched route came out *shorter* than the baseline,
×0.89, by cutting the corner). The wide-water and narrow-channel fixtures are
clean by comparison (connector legs 4–220 m, longest seam-crossing legs
189–1313 m, all real graph edges). **routeiq should distinguish "routed across
a seam" from "teleported over a gap"** — a multi-km `start_connecting`/
`end_connecting` leg is a data-coverage failure, not a connection.

### 9.4 Conclusion for the US East Coast rollout

Keep the registry, and always pair it with overlap:

- Seams cut through **coastline-adjacent water with bank vertices** (most
  state borders) self-stitch on clip-line coincidence; the registry is
  belt-and-braces there.
- Seams cut through **open sea** — exactly what a state-boundary meridian does
  offshore, and what the 9 shipped East Coast files have between them — do
  **not** stitch on their own. Those need the registry, so the regeneration
  should run with `--stitch-registry` **and** `--overlap-deg ≥ stitch_band_m`
  (0.01° ≈ 1.1 km ≥ 300 m default holds).
- Before trusting cross-seam route *quality* offshore, re-measure §9.2 on a
  fixture with real depth data.

---

## 10. US East Coast rebuilt with the registry (2026-07-30) — 9 regions, all adjacent seams crossable

Acting on §9.4: all 9 shipped regions were regenerated against **one shared
registry**, sequentially in geographic order (north→south) so each build adopts
what its already-built neighbour published. Driver:
`local_only/local_scripts/build_east_coast_stitched.sh` (`RESUME=1` skips
already-built regions). Non-destructive — writes `data/us_east_*_stitched.sqlite`
and leaves the shipped files untouched. Preprocessed GeoJSON was reused, so no
download or `enc_preprocessor` re-run. ~55 min of compute for 9 regions.

Registry after the run: **10,000 seam nodes** (NY 2736, CT 2227, DE 969,
SC+GA 870, RI 837, NH 650, MD 648, ME 562, NJ 501). Every region asserted
integer-only `edges.target` (the §8.4 numpy/BLOB regression stays fixed).

### 10.1 Are the seams actually crossable?

`verify_region_seams.mjs` loads each adjacent pair into one `RoutingDatabase`,
labels **every component** of the merged graph, and asks whether any single
component holds nodes exclusive to both files:

| pair | shared ids | joint component | crossable |
|---|---|---|---|
| ME ↔ NH | 240 | 136,816 nodes | yes |
| RI ↔ CT | 12,234 | 22,185 nodes | yes |
| CT ↔ NY | **1** | 75,168 nodes | yes |
| NY ↔ NJ | 53 | 90,188 nodes | yes |
| NJ ↔ DE | 1,194 | 56,565 nodes | yes |
| DE ↔ MD | 502 | 28,615 nodes | yes |

**All six geographically adjacent pairs are crossable.** (9/12 of the
bbox-overlapping pairs are; the 3 that are not — CT↔DE, DE↔RI, NY↔RI — are not
real neighbours, they only "overlap" because NOAA state ZIPs have huge extents.)
CT↔NY crosses on a **single** shared node, the §9.1 phenomenon again.

The remaining gaps are the known missing regions: MA (between NH and RI) and
VA/NC (between MD and SC+GA) still OOM and need the sub-splits noted in
`NEXT_PHASES.md`. So coverage is three connected clusters — ME–NH,
RI–CT–NY–NJ–DE–MD, and SC+GA — not one chain.

### 10.2 Real cross-state routes

`route_across_regions.mjs` asks the engine for a route with endpoints ~15 km
either side of a seam, in different files, and checks it was *routed* rather
than teleported (§9.3):

| pair | route | straight line | detour | longest seam-crossing leg | warnings |
|---|---|---|---|---|---|
| ME ↔ NH | 20,982 m | 12,233 m | ×1.72 | 0 m | start_connecting(893 m) |
| CT ↔ NY | 36,310 m | 26,868 m | ×1.35 | 0 m | start_connecting(45 m) |
| NY ↔ NJ | 53,941 m | 15,196 m | ×3.55 | 1,842 m | via_constrained |
| NJ ↔ DE | 26,913 m | 24,412 m | ×1.10 | 0 m | none |
| DE ↔ MD | 22,135 m | 11,008 m | ×2.01 | 0 m | none |

Five of six produce a genuinely routed cross-seam route, none teleporting.
RI ↔ CT is crossable but **marginal**: its joint component holds thousands of
exclusive nodes on both sides, yet none within 30 km of a shared seam node, so
no representative route exists — the shared nodes sit in open Atlantic water
away from either file's populated coastal graph. NY↔NJ's ×3.55 detour is the
one route quality result worth chasing.

### 10.3 Adoption is very uneven, and the publish rule is the reason

| region | registry candidates in bbox | adopted | already present | left unconnected |
|---|---|---|---|---|
| NH | 193 | 193 | 0 | 120 |
| CT | 2,540 | 827 | **1,713** | 821 |
| NJ | 2,223 | 2,218 | 5 | **9** |
| DE | 161 | 96 | 65 | 34 |
| MD | 478 | 478 | 0 | **471** |
| RI, SC+GA, NY | 0 | — | — | — |

Two things stand out. **CT found 1,713 of 2,540 candidates already in its own
graph** — real-data confirmation of §8.2/§9.1: adjacent states share NOAA cells,
so raw-vertex nodes coincide for free. And **MD adopted 478 nodes of which 471
have no native neighbour within 500 m**, i.e. adoption did almost nothing there.

The cause is structural: for an *unclipped* state build, `_get_coverage_bbox`
falls back to the data extent, so the publish pass writes nodes near that
**rectangle's** edges — which for a ragged NOAA state extent mostly lie offshore
or inland, not where the neighbour actually abuts. Adoption then lands them
nowhere useful. It works well where the rectangle edge happens to coincide with
the real seam (NJ: 9 unconnected of 2,218) and poorly where it does not (MD).
**Follow-up worth doing: publish against the real coverage *boundary* (the
`boundary_geometry` already stored in `metadata`) instead of the bbox rectangle**,
or build each region with an explicit `--clip-bbox` so the cut is a straight,
known line as in the fixtures.

### 10.4 Two more defects found by doing the real build

1. **Adopt pass did not scale (fixed).** `_connect_adopted_node` buffered the
   *entire* water component per adopted node, queried every native node inside
   it, and reprojected all of them to UTM — plus a linear scan over all
   components in the fallback. On NH it sat >9 minutes with no output and was
   killed. Now: a `stitch_radius_m` box prefilter (equivalent, since candidates
   are distance-filtered anyway and cross-body ones still fail `within(poly_m)`),
   a per-component projection cache, and `sindex.nearest` for the fallback.
   Result: **193 candidates against 57,891 native nodes in 3.8 s.** Adopt/publish
   progress logging added, since the pass was previously silent.
2. **`build_region.sh`'s stitching path was broken for every US region (fixed).**
   It passed `--coverage-bbox "$COVERAGE_BBOX"` as two argv entries; argparse
   only tolerates a leading `-` on a bare number, so `-74.3,40.4,-71.7,42.9`
   was read as an option and the build died with "expected one argument". Fixed
   to the `=`-form that `--bbox` already used. This is why NY failed in the run
   and had to be rebuilt.

### 10.5 Measuring lesson (applies to any future seam work)

A **single-anchor reachability probe is not a connectivity test.** The first
verification pass reported NJ↔DE and NY↔NJ as 0% reachable — both are in fact
crossable — because the seam-nearest anchor happened to sit in an isolated pond
present in both files. Only full component labelling answers the question. The
same trap applies to seeding walks from shared nodes: `getReachableNodes` is
*forward* reachability, so a shared node with inbound-only edges from one side
reaches just the other.

### 10.6 Root cause of the dangling adopted nodes — and the fix (2026-08-01)

§10.3 guessed the publish rule (rectangle vs. real boundary) was to blame.
**That was wrong**, on two counts, and the measurements say so:

- `metadata.boundary_geometry` is a **convex hull of the graph's nodes**
  (`_compute_boundary_geometry`) — 20–32 vertices covering 59–70% of the bbox
  area. For a ragged coastline it bridges open sea and land much like the
  rectangle, so publishing along it would not put nodes where a neighbour's
  graph is. Do not spend effort there.
- The published nodes were mostly **in the right place already**: 421 of
  Maryland's 478 adopted nodes had a native, coastal-typed MD node within 500 m,
  yet only 7 connected.

Per-reason counters added to `_connect_adopted_node` (and an
`only_inland_nodes_in_radius` probe, to test whether the known inland/coastal
gap was implicated — it was not) show two distinct failure modes:

| region | dominant reason | reading |
|---|---|---|
| New Hampshire | `no_node_of_any_kind_in_radius` = 113 of 127 | published into water NH does not cover — nothing to attach to, unfixable from the adopter's side |
| Maryland | `all_candidates_outside_water_polygon` = 392 of 471 | the gate rejected them |

**Root cause of the Maryland class:** `within(poly_m)` requires the *whole*
connector — including its start point, the adopted node — inside this build's
water component, which was buffered by a flat 2 m. But an adopted node is
authored from the **neighbour's** water geometry, digitised from different ENC
cells: **406 of MD's 478 adopted nodes lie outside MD's own `coastal_water`
polygons, median 11.3 m out** (p90 179 m); only 14 fell inside the 2 m buffer.
So the test failed by construction, before any candidate was considered. The
Zeeland fixtures never showed this because both sides clip the *same* source
water.

**Fix:** `ADOPT_POLY_TOLERANCE_M = 50.0`. A node already inside keeps the
original tight 2 m buffer (behaviour unchanged); a node outside by up to 50 m is
tested against a 50 m-dilated polygon; beyond that it is rejected with its own
counter (`node_too_far_outside_water`). `_crosses_land` remains the real safety
gate, so a dilated polygon cannot admit a connector over land.

**Measured on a Maryland rebuild against the registry as it stood at MD's turn:**

| | before | after |
|---|---|---|
| adopted nodes connected | **7** / 478 | **263** / 478 |
| `all_candidates_outside_water_polygon` | 392 | 2 |
| `node_too_far_outside_water` (>50 m out) | — | 135 |
| `no_node_of_any_kind_in_radius` | 52 | 52 |
| `all_candidates_cross_land` (correctly rejected) | 22 | 22 |

Seam-level effect on the DE↔MD pair: shared nodes carrying edges into **both**
files' interiors went **29 → 277**, and the cross-seam route's detour over the
straight line fell from **×2.01 to ×1.05** (different endpoints, since the joint
component changed — the ratio is the comparable figure; `bothSides` 29→277 is
the direct measure).

The remaining 215 unconnected split into the genuinely unfixable (52 with no
node of any kind in range, 135 more than 50 m outside MD's water — both the NH
class) and 22 correctly rejected land crossings.

### 10.7 Full East Coast rebuild with the tolerance fix (2026-08-01)

All 9 regions rebuilt from scratch against a fresh registry (~67 min, 10,004
seam nodes, integer-only edge targets everywhere). NY now builds in sequence,
since §10.4's `--coverage-bbox` fix landed.

**Adopt-pass connections** (`_connect_adopted_node`), with the new per-reason
counters:

| region | adopted | unconnected | dominant reason |
|---|---|---|---|
| MD | 476 | **211** (was 471) | 135 >50 m outside its water, 50 nothing in range, 21 cross land, **2** polygon-gate (was 392) |
| CT | 829 | 820 | `no_node_of_any_kind_in_radius` = 820 |
| NJ | 4,946 | 2,711 | `no_node_of_any_kind_in_radius` = 2,701 |
| NH | 195 | 125 | 117 nothing in range, 8 mixed |
| DE | 99 | 36 | 33 nothing in range, 3 no candidate |

Maryland is the case the fix targeted and it moved as predicted. Everywhere
else the dominant remaining reason is **`no_node_of_any_kind_in_radius`** — seam
nodes published into water the adopter does not cover at all. That is not a
gating problem and no tolerance fixes it; it is the structural cost of
publishing on a coverage rectangle (§10.3) and it is harmless (the nodes are
inert on the adopter's side).

**Seam connectivity: unchanged at 9/12 pairs, all six real neighbours crossable**
— but the seam *surface* is much denser where the fix applied: DE↔MD shared
nodes with edges into both files went **29 → 279**. NJ↔NY shared ids went
53 → 2,778, from NY now publishing before NJ rather than from the fix.

**Cross-state routes: 6/6 genuinely routed, and every warning is gone** (four of
six previously carried `start_connecting` or `via_constrained`):

| pair | before | after |
|---|---|---|
| ME ↔ NH | ×1.72, 2 warnings | **×1.17, none** |
| DE ↔ MD | ×2.01, none | **×1.08, none** |
| NJ ↔ DE | ×1.10, none | ×1.14, none |
| CT ↔ NY | ×1.35, 1 warning | ×1.49, none |
| NY ↔ NJ | ×3.55, 1 warning | **×13.16**, none |
| RI ↔ CT | no route | 159 m (degenerate) |

Two results are **not** wins and should not be read as such:

- **RI ↔ CT** now returns a route, but between endpoints 97 m apart — the
  picker's fallback, because that pair's joint component still has no exclusive
  node near a shared seam node. The seam remains effectively undemonstrated.
- **NY ↔ NJ is ×13.16** — 242 km for an 18.4 km straight line, Jamaica Bay to
  offshore of Sandy Hook. No warnings, so it is a real graph path, but that
  detour implies the direct route out of Rockaway Inlet is not connected. Not
  attributable to this fix (the endpoints differ from the earlier run, which
  scored ×3.55), but it is the clearest remaining routing-quality defect in the
  set and deserves its own investigation.
