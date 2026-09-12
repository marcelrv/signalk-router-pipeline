# Spec: Graph cleanup — removing the graph no boat will ever use

Status: Pass A and the renderer implemented on branch `graph-cleanup` (2026-09-12):
`graph_cleanup/`, `apply_cleanup.py`, `tests/test_graph_cleanup.py`,
`tests/test_graph_cleanup_render.py`. Verification build: `data/BUILD_LOG.md` #40; the
renderer's visual before/after is §5.5. Passes B and C (model adjudication, model route
tracing) are specified here but not built.
Complements: `SPEC-GRAPH-DENSITY.md` (the density investigation and the gate discipline
reused here), `SPEC-CHANNEL-AXES.md` (the layer whose over-density Pass A removes).

## 1. Requirement

The graph looks wrong on the map even when every headline metric is healthy. Build #39
passes on nodes, edges, hubs and `crosses_land`, and still shows a node every 75 m on
straight runs, zigzag chains, and a navmesh whose nodes mostly connect to nothing.
`SPEC-GRAPH-DENSITY.md` §9.4 has been recording the gap since build #34: *"treat the
original location as resolved by graph metrics, not as visually confirmed."*

## 2. What is actually wrong (measured)

All measurements on the live MD build #39, `us_east_md_channel_axes.sqlite` —
62,904 nodes / 82,233 undirected edges (164,468 rows; edges are stored bidirectionally
with identical attributes, plus 2 self-loops).

### 2.1 How much is unused

Routing all-pairs between the 262 POI-anchored nodes in the main component touches
**15.5% of nodes and 12.3% of edges**. 84.5% of nodes are on no route between two charted
destinations.

This is an **upper bound on what is deletable, not a target**. POIs are not the only
destinations — anchorages and creeks are not in the table, and a user must be able to
click anywhere. It sets the scale, not the goal.

### 2.2 The over-density and the wobble are different layers

Turn angle at degree-2 nodes, and segment length, by producing layer:

| edge source | deg-2 n | median turn | p90 | >20° | median segment |
|---|---|---|---|---|---|
| `coastal_water` (medial-axis skeleton) | 13,316 | **35.7°** | 107.7° | **63.3%** | 56 m |
| `channel_axes` | 6,951 | **0.5°** | 16.7° | 8.0% | **76 m** |
| `inland_waterways` | 2,725 | 0.2° | 0.5° | 0.3% | 119 m |
| stitch connectors | 660 | 22.3° | 96.8° | 53.0% | 179 m |

* **Over-density** is `channel_axes` and `inland_waterways`: dead straight, simply
  over-sampled. Pure redundancy, removable with no judgement.
* **Wobble** is the `coastal_water` medial-axis skeleton. Those vertices are genuinely
  off-line, so simplification will not remove them.
* `channel_axes` (`SPEC-CHANNEL-AXES.md`) is **not** the source of the wobble — it
  produces the straightest lines in the database.

### 2.3 The skeleton is a mesh, not a set of lines

This is the finding that bounds what any chain-based pass can achieve:

| | |
|---|---|
| nodes touching a `coastal_water` edge | 44,236 |
| …of which degree ≥ 3 | **22,095 (50%)** |
| `coastal_water` degree-2 chains | 5,832 |
| …holding exactly one interior node | **3,675 (63%)** |
| interior nodes in chains with ≥4 interior | 6,593 |
| median deviation-from-straight at a skeleton junction | **167.3°** |

Chain simplification and chain smoothing can only reach degree-2 runs. Most of the
skeleton's wobble lives *between* junctions, where no chain pass can see it.

### 2.4 Other waste

| | |
|---|---|
| Douglas-Peucker at 20 m on degree-2 chains (no width cap) | removes 26.4% of all nodes |
| navmesh nodes with no edge to a non-navmesh node | 2,604 / 3,807 (68.4%), median turn 7.3° |
| degree-1 dead ends | 7,350; 5,888 under 500 m |
| edges on no shortest-path tree from 60 spread origins | 33.7% |
| nodes outside the largest component | 11,692 (18.6%) |

## 3. Design

Three passes, all emitting **operations** into one append-only `ops.jsonl` per region
rather than writing derived databases:

```
built .sqlite ──► A. deterministic ──► B. model adjudication ──► C. model route trace
                        │                      │                         │
                        └───────────► ops.jsonl ◄─────────────────────────┘
                                          │
                                 apply + gate (§5)
                                          │
                                   cleaned .sqlite
```

Operations are `drop_node`, `splice_node`, `drop_edge`, `move_node`, each carrying a
reason, an author (`det:dp20`, `ai:claude-sonnet-5`, `ai:qwen-vl-32b`) and a confidence.
Applying is order-dependent and forgiving: an op whose target is gone, or no longer has
the shape it needs, is skipped and counted. Replaying a file onto an already-cleaned
database is a no-op.

**Why post-build, not another generation flag.** A rebuild is 15+ minutes and every
decision here is about the graph, not the charts. And node IDs are coordinate-derived
(`_coord_to_id`), so a decision keeps naming the same place after a rebuild — an
`ops.jsonl` is a durable artifact, which is what makes weeks of model inference worth
spending.

## 4. Pass A — deterministic (implemented)

`graph_cleanup/simplify.py`. Everything it emits is provable, so it runs first and
unattended.

1. **`contract_chains`** — Douglas-Peucker per degree-2 chain, emitting `splice_node`.
   Splicing rather than dropping is the safety property: the merged edge inherits the
   *worst* attribute of the pair (shallowest depth, narrowest width, highest cost factor,
   least-trustworthy tier, `crosses_land` OR-ed), so a simplified chain can never look
   safer or cheaper than what it replaced.
2. **`smooth_chains`** — the only pass that moves geometry. Chebyshev smoothing toward
   the neighbour midpoint, clamped per node.
3. **`drop_redundant_edges`** — an edge whose endpoints are already joined by a path
   costing no more than `slack ×` its own cost. Checked against the mutating graph, so a
   run of removals can never eat the last connection. Locks and bridges are exempt.

### 4.1 The width proof

The medial axis is equidistant from both banks, so a point on it has at least
`min_width / 2` of clearance. Both simplification tolerance and smoothing displacement are
capped at a fraction of that half-width, which is what makes them provably stay in water
**without loading any polygons**. `min_width` is the narrowest value along the whole
chain, so every node is budgeted by its tightest point.

`999.0` is the unknown sentinel — 100% of `channel_axes`/`inland_waterways` edges and 24%
of `coastal_water` ones. Chains with no charted width are simplified on the requested
tolerance alone and **are not smoothed at all**: without a width there is no proof, and a
plausible guess is what this whole effort exists to stop.

Simplification is held tighter (0.25) than smoothing (0.5) because its error is a chord
deviation that compounds along a run of removed vertices. At 0.5 a smoothed node keeps half
its original clearance, and takes the median turn on reachable skeleton chains from 36.5°
to 16.4° (0.25 only reaches 29.2°).

### 4.2 Shape versus resolution

Douglas-Peucker is right about shape and wrong about resolution. On a dead-straight 4 km
channel it correctly removes every interior vertex — and a click in the middle then has
nothing within 2 km to snap to. Measured: plain DP left *Brewerton Channel Eastern
Extension* snapping **2,123 m** from its charted position, which is how routeiq's
`coverage_gap` warning is born (a long straight connecting leg excluded from every
constraint check). `max_spacing_m` (default 500 m) restores the minimum density by keeping
the vertex nearest the middle of each too-long gap.

### 4.3 Invariants

1. **Nothing in `RoutingGraph.protected` is deleted or moved.** That is
   `navmesh_regions.boundary_node_ids` — how routeiq's funnel search enters a navmesh
   region — plus every node a POI snaps to.
2. **No point moves further than the pass's tolerance**, bounded by §4.1.

### 4.4 The navmesh is out of scope here

3,757 of the 3,807 navmesh nodes are referenced in `boundary_node_ids`. Worse, `vertices`
and `triangles` are a triangulation indexed positionally, not a node list — removing a
navmesh node means re-triangulating the region. **The navmesh waste in
`SPEC-GRAPH-DENSITY.md` §10 cannot be fixed post-build at all.** It belongs at generation
time, via §10.6 item 1 (parameterize the hardcoded `NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0`).

### 4.5 Measured result (build #40)

−14.3% nodes, −13.5% edges, all gates pass, 4.3 s. `channel_axes` degree-2 nodes −82%,
`inland_waterways` −96%. The skeleton's median turn moves only 35.7° → 30.8%, for the
reason in §2.3.

**This revises the deterministic ceiling down to ~14–16%**, against the 15.5% floor in
§2.1. Essentially the whole remaining gap is judgement work.

## 5. Gates

`graph_cleanup/validate.py`, run before anything is written. Five from
`SPEC-GRAPH-DENSITY.md` §9 plus two this work needs:

| gate | rule |
|---|---|
| `crosses_land` | never grows |
| `largest_component_by_length` | loses ≤0.5pp. **By edge length, never node count** — removing nodes is the point here, so a node-count ratio moves on every successful run (§6.1: the node-count form "sent two investigations chasing a 2.61pp 'regression' that does not exist") |
| `poi_pair_reachability` | zero pairs lost, re-snapping each POI to the cleaned graph. The gate that would have caught builds #11/#12, where 21 named POIs fell into an isolated 8-node island |
| `poi_snap_drift` | no POI snaps >50 m further than before (§4.2) |
| `counts` | nodes and edges only go down |
| `hubs` | no node above out-degree 30 (splicing joins neighbours, so it can raise a degree) |
| `route_shape` | no probe route's cost changes by more than 5%. A cleanup that *shortens* a route has usually deleted a constraint; one that lengthens it has deleted something real |

POIs are held as **coordinates, not node ids**. Simplification legitimately removes the
exact node a POI snapped to; the line stays and the POI re-snaps a few metres along it.
Gating on node identity fails every successful run.

## 5.5 Renderer (`graph_cleanup/render.py`, implemented)

Built to close the gap §1 describes, ahead of any tiling/prompt work: the plan called
for "look at the tiles yourself" before spending on a model, and doing that
immediately surfaced two real bugs that would otherwise have corrupted every Pass
B/C tile.

**Bug 1 — mixed-geometry layers silently miscolour their Point subset.**
`caution_areas_polygons` and `obstructions_points` each hold a mix of Polygon and
Point geometry (measured: 8 polygon / 5 point, and 88 point / 3 polygon, in the
Coltons Point bbox). `geopandas.plot()` routes a GeoDataFrame's Point rows through
`ax.scatter`, and a `facecolor="none"` meant for the polygon fill does not reliably
apply there — the points silently fell back to matplotlib's default colour cycle
instead of disappearing or taking the requested colour. The first render of the #40
before/after comparison shipped this: unstyled orange/blue dots over open water that
looked like real chart symbology. Fixed by splitting every layer's Point subset out
(`POINT_LAYERS`) and drawing it explicitly — obstructions especially, since they are
exactly the hazard a reviewer must see as what it is.

**Bug 2 — a navmesh region renders as an empty ring, and reads as disconnected
junk.** `edge_kind_id == EDGE_KIND_NAVMESH_BOUNDARY` is *only* the perimeter of a
navmesh region (`nautical_routing_pipeline.build_navmesh_region`); the interior is a
constrained Delaunay triangulation stored in `navmesh_regions.vertices`/`triangles`
and walked by the funnel algorithm at query time, never flattened into `edges` rows.
A renderer that draws only `nodes`/`edges` shows a wide, entirely normal open-water
region as a large dotted ring with nothing inside — at the Coltons Point bbox this
looked exactly like a disconnected artifact, the kind of thing a Pass B/C prompt
would have flagged as `graph_noise` at scale, wrongly, region by region. **Confirmed
in the pipeline source before drawing any conclusion from what it looked like on
screen** — this is the same mistake the `scope: systemic` design guard exists to
catch, and it would have reached that guard already mislabelled. Fixed by loading
`navmesh_regions` for the tile's bbox and tinting the interior, so a reviewer sees
"funnel-routed open water", not emptiness.

**Practical lesson for Pass B/C**: a tile fed to any reviewer, human or model, needs
the navmesh tint present or every open-water region becomes a false positive. This is
now load-bearing for whatever candidate/prompt work comes next, not optional polish.

Verified against build #39/#40 at the Coltons Point bbox `(-76.90, 38.15, -76.68,
38.27)`: the `channel_axes` line visibly changes from a beaded/oversampled look to a
clean line between before and after, matching §4.5's numbers exactly; the skeleton
mesh is visibly unchanged, matching §2.3; two large navmesh regions render as tinted
areas crossed cleanly by the channel axis, present identically in both renders (Pass A
protects navmesh nodes, confirmed visually as well as by the gate).

Tests: `tests/test_graph_cleanup_render.py` (11 tests, synthetic fixtures) — the two
bugs above each have a regression test.

## 6. Passes B and C — not built

Pass A caps out around 15% because the rest is judgement: *does this dead end lead
anywhere a boater wants to go*, *which of these five parallel lines is the real route*,
*is this water navigable at all*. Every attempt to encode those as a threshold is what
produced the current state.

Design constraint: this must ultimately run on a **local model over every tile for
weeks**, so it is built as a batch of self-contained work items, not an agentic loop —
small local models are unreliable at multi-turn tool use, and a loop that spirals on tile
4,000 costs a night. Claude Sonnet runs the same items first and its answers are the gold
set the local model is scored against, per candidate kind, before it is trusted.

The model never emits a coordinate. Both tasks are discrete choices over items numbered on
a rendered image:

* **Pass B, prune** — `{"6": {"verdict": "drop", "why": "ends in the marsh"}}` over
  candidates Pass A could not prove. `unsure` is treated as `keep`, so a weak model
  degrades into doing nothing rather than into breaking the graph.
* **Pass C, trace** — `{"main": [14, 27, 31, 55]}`, an ordered list of numbered junctions.
  Anything on a traced route is protected from deletion. This is what makes deleting the
  rest safe: keep what is on a sensible route, rather than deleting by heuristic and
  hoping.

No design vessel. The prompt asks for the path most boats take; where two boat classes
genuinely diverge, the model returns both and says why. `nodes.node_depth` already exists,
so per-vessel filtering stays downstream.

## 7. Known limits / follow-ups

- The skeleton wobble between junctions (§2.3) is untouched and needs Pass B/C or a
  generation-time fix.
- `NAVMESH_BOUNDARY_SIMPLIFY_M` is still a hardcoded 5.0 (§4.4).
- `drop_redundant_edges` finds only 2,161 provably-redundant edges against the 33.7% that
  are merely unused — the weaker criterion is a Pass-B candidate generator, not a deletion
  proof.
- 18.6% of nodes are outside the largest component and untouched; whether those are real
  disconnected water or artifacts is a Pass-B question.
- Visual before/after at Coltons Point is done (§5.5) — the check `SPEC-GRAPH-DENSITY.md`
  §9.4 flagged as missing since #34. It confirmed §4.5's numbers and found two renderer
  bugs before they could corrupt Pass B/C tiles; it did not turn up a new correctness
  problem in the cleaned graph itself.
- The renderer only draws what fits in the `RoutingGraph`/clipped-GeoJSON model: no
  triangle-level navmesh interior (a tint stands in for it), no depth colour banding
  within `depare_polygons` (single flat fill). Neither blocked the Coltons Point check;
  revisit if a Pass B/C candidate turns out to need finer chart detail than the tint.
