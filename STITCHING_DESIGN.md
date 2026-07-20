# Cross-Database Seam Stitching — Design Note (for review)

**Status: DESIGN — for user review before implementation. No stitching
code has been written.** Recommended mechanism (user direction): a **shared
global-node registry** (§3) — seam nodes authored once and shared across
builds so IDs coincide by construction. Consolidates the two de-risking
experiments run 2026-07-20 (Chunk 1, Chunk 2) that ruled out
recompute-based coincidence, and the recommended design. Supersedes the
mechanism discussion in `PHASE_4_DESIGN.md` §4a.1 (that section's
dynamic-loading interplay still applies; its build-time candidate-stamping
mechanism is superseded by the findings below).

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
