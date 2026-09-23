# Spec: Graph cleanup — removing the graph no boat will ever use

Status (refreshed 2026-09-21): **merged** as PR #25 (Pass A, renderer, Pass B, `render_diff`) and
PR #26 (the `nearest_poi_m` blind-spot fix, §6.8). **Pass C is not built**: only the prompt
`graph_cleanup/prompts/trace.md` exists; there is no junction-numbering candidate generator
and no runner wiring. Pass B has only been run through the mock backend and by Claude Code acting
as the reviewer (§6.6-6.7); no local-model/Message-Batches run has been executed.
Original status line: Pass A, the renderer, and Pass B (prune) implemented on branch `graph-cleanup`
(2026-09-12/14): `graph_cleanup/`, `apply_cleanup.py`, `review_region.py`,
`tests/test_graph_cleanup*.py`, `tests/test_review_region_cli.py`. Verification builds:
`data/BUILD_LOG.md` #40 (Pass A), #41 (Pass B pilot), #42 (Pass B expanded, 3x area,
323 candidates, §6.7). The renderer's visual before/after is §5.5; `render_diff` (a
one-picture overlay, §6.7) is the current best way to see a cleanup's real effect.
Pass B's harness verification (mock backend, real data) is §6.4; real reviewer results
(no live API key used — see §6.5) are §6.6-6.7. Pass C (model
route tracing) is specified (§6) but not built.
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
| `counts` | nodes and edges only go down. Opt-in for edge-adding changes: `validate.check(..., max_edge_growth=N)` tolerates up to N extra edges over the baseline (default 0 = strictly shrink-only, unchanged). The parameter lives in `validate.check` for baseline-vs-candidate comparisons of separate builds: the edge-adding cases (#45-#50) were cross-build comparisons made by an ad-hoc script calling `validate.check` against a different baseline DB, not by `apply_cleanup.py`. The `apply_cleanup.py --max-edge-growth N` flag is only forwarded to the same check; that CLI loads one DB and applies `drop_node`/`splice_node`/`drop_edge`/`move_node` (`graph_cleanup/ops.py`), none of which can add an edge, so today the flag cannot change its result and exists for future edge-adding ops (a negative N is rejected at argument parsing). The node-count check stays strict: node growth is never tolerated, so a cross-build comparison whose node set drifts between runs (e.g. #45, +34 nodes) still fails the counts gate on nodes; the bound only helps a same-node-set comparison, e.g. #50 (`max_edge_growth=654`, its 654 dead-end connections) |
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

## 6. Pass B — implemented; Pass C — not built

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

* **Pass B, prune** (implemented) — `{"6": {"verdict": "drop", "why": "ends in the
  marsh"}}` over candidates Pass A could not prove. `unsure` is treated as `keep`, so a
  weak model degrades into doing nothing rather than into breaking the graph.
* **Pass C, trace** (prompt written, not implemented) — `{"main": [14, 27, 31, 55]}`, an
  ordered list of numbered junctions. Anything on a traced route is protected from
  deletion. This is what makes deleting the rest safe: keep what is on a sensible route,
  rather than deleting by heuristic and hoping. Needs a junction-numbering candidate
  generator distinct from §6.1's two kinds — deferred, see §7.

No design vessel. The prompt asks for the path most boats take; where two boat classes
genuinely diverge, the model returns both and says why. `nodes.node_depth` already exists,
so per-vessel filtering stays downstream.

### 6.1 What Pass B actually looks at (`graph_cleanup/candidates.py`)

Two candidate kinds, chosen because both render unambiguously with one numbered marker:

* **`dead_end_stub`** — walk a degree-1 node inward to its first junction. 7,368 exist on
  the cleaned MD graph (build #40); most are legitimate (a marina entrance, a creek), some
  are medial-axis artifacts into a marsh. Capped at `--max-stub-length-m` (default 3000 m)
  — a longer dead end is assumed to be a real charted approach, not a fragment, and asking
  "keep or drop this obviously-real thing" wastes a review on a question with one answer.
* **`small_component`** — a connected component other than the largest, capped at
  `--max-component-size` (default 30 nodes). The cleaned MD graph's *second*-largest
  component alone is 7,818 nodes — almost certainly real, unstitched water, not a
  single-verdict candidate; bulk-judging something that size from one tile would be
  exactly the "one bug becomes a hundred patches" mistake §3 warns against.

A small component that is itself a dangling line would otherwise generate a
`small_component` candidate *and* separate `dead_end_stub` candidates for the same nodes
— confusing and redundant. `find_all` computes components first and excludes their nodes
from the stub search.

**Anchor placement matters and was wrong on the first pass.** `_walk_stub` returns
`[tip, ..., junction]`; the first version anchored the numbered marker (and measured
"nearest POI" from) `path[-1]` — the junction, shared with the rest of the graph — instead
of `path[0]`, the actual dangling tip where the question "does this lead anywhere" has to
be judged. Caught by a unit test with a real assertion on the anchor id, not by inspection;
confirmed visually afterward (§6.4) that the marker lands on the tip.

Not attempted: the ~34% of edges that are merely *unused* rather than provably redundant
(Pass A §4 only removes ~2,161 of them). A useful "this duplicates that other path"
candidate needs a pairing between the unused edge and whichever kept edge serves the same
journey; getting that pairing wrong produces a confusing tile rather than a useful one.
Left as documented future work (§7) rather than shipped half-considered.

### 6.2 Tiling and work items (`graph_cleanup/tiles.py`, `prepare.py`)

Candidates are bucketed directly into ~6 km cells (`--tile-m`) — only a cell with at least
one candidate's anchor produces a tile, so tile count is proportional to how much judgement
is needed, not to the region's raw area. A cell with more than `max_per_tile` (default 25)
candidates is recursively re-bucketed at half the cell size; the recursion's real stopping
condition is a ~1.1 m cell-size floor, not a depth count — an early version capped depth at
4, which silently left oversized, unreadable tiles for a genuinely crowded spot (many stubs
in one small marina).

Each tile writes four files: `chart.png` (context, no graph), `candidates.png` (graph +
numbered markers), `context.json` (facts sent to the model), and a `manifest.json` **not**
sent to the model — it carries the full node list per number, which `context.json`
deliberately omits (a 30-node component's raw id list is noise to a reviewer and would
bloat the prompt for nothing) but which `runner.answers_to_ops` needs to turn a verdict
back into the exact nodes it was about.

### 6.3 Backends and the runner (`graph_cleanup/backends/`, `runner.py`)

Every backend implements one method, `answer_tile(tile_dir) -> str`, so the runner doesn't
know which one it's driving:

* `MockBackend` — deterministic, offline, not a review of any kind. Exists to test the
  harness (and to dry-run a real region's tile counts/sizes) without an API key.
* `ClaudeBackend` — `client.messages.create`, `model="claude-sonnet-5"`, adaptive thinking,
  `output_config.effort` (default `medium`), the system prompt cached (`cache_control:
  ephemeral`) since it is identical across every tile in a run. Plus `submit_batch`/
  `poll_batch`/`collect_batch` using the Message Batches API for the full gold-set run —
  **untested against a live key**, see §6.5.

`runner.run_tile` parses and validates a backend's JSON (every referenced number must exist
in that tile, every verdict must be `keep`/`drop`/`unsure`), retrying once before giving up;
a tile that never validates is marked `unanswered` and contributes no ops — never a silent
drop. `run_all` writes `<tile>/answer.json` as soon as each tile is answered, so a killed
run resumes by skipping tiles that already have a valid one (details, incl. stale/degraded
answers and the circuit breaker, in §6.9). `answers_to_ops` turns every `drop`
into `Op` records: a `small_component` drop removes every node in it; a `dead_end_stub`
drop removes the stub's own chain (`nodes[:-1]`, excluding the junction) — confidence is a
flat 0.7 (there is nothing in a `keep`/`drop`/`why` answer to read a real number from),
marking these as needing the human sign-off the tier-5 override workflow (README.md
"Community override workflow") already calls for.

### 6.4 Verification

Full harness run end-to-end against the real cleaned MD graph and real chart data at the
Coltons Point bbox, `MockBackend`: 104 candidates → 16 tiles → 76 `drop`/28 `keep`
verdicts → 110 ops. `candidates.png` inspected directly (not just counts) — numbered
markers land on the dead-end tips, not on shared junctions (§6.1's anchor fix), and the
tile also shows the dense skeleton mesh near Cuckold Creek that a real reviewer would need
to judge as a separate class of noise from the two candidate kinds implemented so far.

### 6.5 `ClaudeBackend`/the Batches API: not run for real — no credentials available

This session had no `ANTHROPIC_API_KEY`, no `ant auth login` profile, and no `ant` CLI —
`ClaudeBackend` and the batch functions are written directly from the Anthropic SDK's
documented request/response shapes (not guessed), installed and import-checked
(`anthropic` 1.5.0), but **never executed against a live key**. Before trusting
`submit_batch` for a paid run: `review_region.py --backend claude --limit 2` first, read
the two verdicts, confirm they're sane.

**Rough cost estimate**, from the real Coltons Point measurement (104 candidates / 16
tiles) and Sonnet 5 pricing ($2/$10 per MTok): two 1536×1536 images (~3,100 tokens each per
the standard image-token estimate) + `context.json` (a few hundred tokens for ~6-7
candidates) ≈ 7,000-8,000 input tokens/tile after the cached system prompt, plus a few
hundred output tokens for the JSON verdict and whatever adaptive thinking at `effort:
medium` spends. That puts the Coltons Point pilot at well under $1, and the plan's ~300-tile
gold-set sample (`--sample-tiles 300`) at roughly $10-20 — an estimate, not a measurement;
confirm with `response.usage` on the first real batch.

Note what this section is *not* saying: it is not saying Pass B itself is unverified.
§6.6 covers a full real review, done a different way.

### 6.6 A real review, done without the API — Claude Code itself as the backend

Claude Pro/Max subscription usage (claude.ai, and interactive Claude Code sessions) and
the Anthropic Console API are different products with different billing — a subscription
does not fund `ClaudeBackend`'s per-token API calls. But nothing about Pass B requires
that specific path: the review is "read two images and a JSON file, write a JSON verdict",
and a Claude Code session already does exactly that with its `Read` tool. So the actual
gold-set pilot for build #40's Coltons Point tiles was done by having the building session
(running as Sonnet 5) read all 16 prepared tiles directly and write `answer.json` into each
by hand, in the exact schema `runner.answers_to_ops`/`review_region.py` already expect —
zero API spend, normal Claude Code usage instead.

**This is a real result, not a demo.** All 104 candidates across all 16 tiles answered
(zero `unanswered`), reasoning recorded per candidate, applied through the full pipeline
including a fresh gate check:

| | mock backend (rule-based) | real review (this session) |
|---|---|---|
| `keep` | 28 | 63 |
| `drop` | 76 | 35 |
| `unsure` | 0 | 6 |
| ops produced | 110 | 40 |

The two disagree by a lot, and the disagreement is informative, not noise. The mock's rule
(`drop` past a fixed `nearest_poi_m` threshold) turned out to systematically over-drop,
for a reason found only by actually looking at the charts: **`nearest_poi_m` is measured
against the `pois` table only, which does not include lateral marks, lights, or named
daybeacons** — so a stub sitting right next to "Combs Creek Daybeacon 4" or in the middle
of a named, marked, real tidal creek can still show a `nearest_poi_m` of several kilometres,
because the nearest *POI-table* entry happens to be a distant channel. Real review caught
this every time by looking at the rendered chart context, not the number alone; a purely
numeric backend cannot. **Action item (DONE 2026-09-15, see §6.8 -- text below is the original finding): extend `nearest_poi` to also search
lateral marks (`lateral_marks_points`) and named waterway lines, not just the `pois`
table** — this would remove the single biggest source of misleading context in the current
`context.json`.

**A second, more useful pattern emerged and generalizes past this one region**: every
`dead_end_stub` judged `drop` fit one shape — a short stub reaching a *plain, unremarkable
point of open shoreline*, with no cove, marsh, marina, or named feature under it, usually
part of a small cluster of 4-8 such stubs around the same headland. Every `keep` fit a
different, equally consistent shape — the stub reaches real charted marsh/creek water, sits
near a named channel or marked feature, or is the last few metres of an already-marked
fairway. This is a plausible candidate for a **third deterministic Pass A rule**: a stub
whose `min_depth_m` is the unknown sentinel, whose length is short, and whose corridor
touches no `caution`/`fairway`/marsh-classified water polygon is very likely droppable
without needing a model at all — worth measuring against a larger sample before trusting
it as a rule, but the pattern held with no exceptions across the 35 stubs actually dropped
here.

**One candidate kind showed no artifacts in this sample**: all 8 `small_component`
candidates were judged `keep` (real, if disconnected, marsh ponds) — 8 is too small a
sample to conclude components rarely need dropping, but worth tracking as more tiles are
reviewed.

Applied and gated exactly like Pass A's own ops (`apply_cleanup.py --replay`): all seven
gates pass, 53,930 → 53,890 nodes (-40, -0.1% — expected at this pilot's scale, one bbox
out of the whole state). The result was not deployed; it exists to validate the harness and
the prompt before spending on a wider run, which is what it did.

### 6.7 Expanded review (~3x area) and the structural findings it confirms

Same method as §6.6, same session, area grown from the Coltons Point bbox
(`-76.90,38.15,-76.68,38.27`, 104 candidates) to `-76.95,38.10,-76.63,38.32`
(323 candidates, 37 tiles) — a superset, so the 104 already-judged candidates were
carried forward by `candidate_id` (stable, node-id-derived) rather than re-reviewed, and
only the 219 genuinely new ones needed fresh judgement. All 323 answered, zero
`unanswered`.

| | count |
|---|---|
| `keep` | 262 |
| `drop` | 52 |
| `unsure` | 9 |
| ops produced | 69 |

**Regional composition changes the keep rate a lot, and that is itself a finding.** The
expanded area is dominated by Maryland's Potomac-tributary tidal creeks (Nomini Creek,
Lower Machodoc/Glebe Creek, Cuckold Creek, the Wicomico River, Breton Bay/Saint Clements
Bay) rather than open headland coastline, and the keep rate rose from 61% (pilot) to 81%
(expanded) accordingly — entire tiles of 5-23 candidates came back 100% `keep` because
every stub genuinely sat inside a real, substantial, branching creek. **The pilot's drop
rate is not a regional constant**; whatever the eventual statewide run measures will depend
heavily on how much open headland coastline versus creek system each stratified sample
covers, and a gold-set sampler should stratify by that if it doesn't already.

**The "plain shoreline stub" pattern (§6.6) held up at 3x scale with no exceptions.**
Every one of the 52 drops was the same shape: a short stub reaching an unremarkable point
of open shoreline, usually one of 4-8 near-identical stubs around the same headland, with
no cove, marsh, or named feature under it — including, this round, an isolated candidate
at a headland shoreline point 7.3 km from the nearest charted POI (`t1_1418_-2235`). The
candidate deterministic-rule idea in §6.6 is now supported by 52 drops, not 35, still with
zero counterexamples found.

**Two new observations, worth recording precisely because they *don't* fit the existing
rules cleanly:**

* All 20 `small_component` candidates seen so far (8 pilot + 12 here) were judged `keep`.
  Every one sat inside real, if disconnected, marsh or cove water — none looked like a
  rendering artifact. Twenty is still not a large sample, but it is a second sample
  agreeing with the first, and it argues against spending Pass A effort on a
  small-component-specific drop heuristic: on this evidence there may be little to find.
* A handful of candidates were marked `unsure` on inspection rather than forced to a side:
  two narrow, unnamed barrier-island breaches inside a charted restricted zone
  (`t0_708_-1121`), and stubs reaching toward small mid-water islets with no POI nearby
  (`t0_707_-1119`, repeated in `t2_2839_-4471`). These are not cases the "plain point"
  vs. "real cove" rule resolves — a narrow real inlet and a stray fragment can look
  identical at this resolution — and are exactly what `unsure → keep` (§6, "the prompt
  guidance") is for: real ambiguity that a wider chart view or a second look could
  resolve, not something to force a confident wrong answer on.

**Visual verification, not just counts** (this is what closes the loop the Coltons Point
render in §5.5/§6.4 opened, at the scale the user asked to see): `render_diff`
(`graph_cleanup/render.py`, new) draws one picture instead of a before/after pair — the
surviving graph in its ordinary colours, with everything removed drawn as a bold dashed
red line (and a red dot at every vanished vertex, not just chain ends) directly underneath
it. Two renders were produced over the full expanded-area bbox:

* Pass A output → Pass A + expanded Pass B review: every one of the 69 removed nodes shows
  as a short red dash at a headland, exactly where §6.7's "plain shoreline stub" pattern
  predicts, and nowhere inside the creek systems — visually confirming the review did not
  touch real water.
* The original pre-cleanup build (`us_east_md_channel_axes.sqlite`, i.e. before Pass A
  existed at all) → the final reviewed result: the dominant feature is a dense red trail
  running along every `channel_axes` line — thousands of collinear vertices Pass A's
  Douglas-Peucker removed, exactly reproducing §4.5's over-density finding as a picture —
  with the Pass B headland drops visible as the same short red dashes on top.

Both were reviewed directly (not just generated) before being treated as confirmation:
the second render is what first made visible that Pass A's node reduction is concentrated
almost entirely on the `channel_axes`/`inland_waterways` layers, matching §2.2's original
measurement rather than contradicting it.

### 6.8 `nearest_poi` blind spot — IMPLEMENTED

§6.6/§6.7's blind spot, found twice independently: `_nearest_poi`
(`graph_cleanup/candidates.py`) only ever searched the `pois` table, so a stub next to a
named daybeacon or inside a marked creek could report a `nearest_poi_m` of several
kilometres — the nearest *POI-table* entry happening to be a distant marina or channel —
even though a human or model looking at the rendered tile could see the real feature
right next to it.

**Fix**: `trace.load_lateral_marks(input_dir)` reads
`<input_dir>/lateral_marks_points.geojson` (the already-clipped BOYLAT/BCNLAT layer used
elsewhere, e.g. `derive_channel_axes.py`) and returns `(lat, lon, name)` tuples for every
named point, the same shape `trace.load_pois` returns. `candidates.find_all` gained an
`input_dir` parameter; when given, it concatenates lateral marks onto the `pois` list
before candidate generation, so `_nearest_poi`'s search is unchanged — it just now sees a
longer list. `review_region.py` passes its already-required `--input-dir` through.
Unnamed marks are skipped (an anonymous buoy doesn't give a reviewer anything to read off
the tile). Regression tests: `tests/test_graph_cleanup_trace.py` (`load_lateral_marks`)
and `tests/test_graph_cleanup_review.py`
(`test_find_all_fixes_the_nearest_poi_blind_spot_with_lateral_marks`).

**Re-checked against the real 323-candidate review** (build #40's
`us_east_md_cleanup_a.sqlite`, the same `--bbox` as §6.7's expanded round): regenerating
the identical candidate set with and without `input_dir` shows `nearest_poi_m` changes for
**281 of 323 candidates (87%)**, many dropping from several kilometres to a few hundred
metres once a nearby named mark is found instead of a distant POI-table entry — confirming
the blind spot was not a minor edge case but the dominant source of misleading distance
context in that round. Of the 69 candidates actually dropped, 42 had a changed
`nearest_poi_m`; none of this is grounds to revisit those verdicts by itself, since the
real reviewer (§6.6) already judged every one from the rendered tile, not the number — the
fix corrects the diagnostic a reviewer reads alongside the picture, not the picture itself.
It does mean any *future* review (and especially the still-untested `ClaudeBackend`/API
path, §6.5, which has no rendered tile to fall back on for context the way a session
reading tiles directly does) will see substantially more accurate numbers.

### 6.9 `LocalOpenAIBackend` — the local model path (`backends/local_openai.py`)

The "local second" half of §6's plan: an OpenAI-compatible server (llama.cpp
`llama-server`) at `POST <base>/chat/completions`, standard library only (`http.client`, no new
dependency). Same inputs as `ClaudeBackend` (identical PNG bytes and `context.json`, in this
API's message format): `prompt.txt` as the system message, then `chart.png`, `candidates.png`
as base64 `data:image/png;base64,...` `image_url` parts (each preceded by a short text label),
then `context.json` as text.

```
./review_region.py --db ... --input-dir ... --out-dir data/review/md \
    --backend local --limit 3                    # smoke on 3 tiles first
    # --local-url http://192.168.10.111:8000/v1  (default; or $LOCAL_LLM_URL)
    # --model Qwen3.8-27B-GSQ-RCO-IQ3_S          (default; or $LOCAL_LLM_MODEL)
    # --temperature 0.2 --max-tokens 4096 --timeout 300 --connect-timeout 10
    # --max-consecutive-backend-errors 5  (0 = never abort)
    # --prompt-label v2-tighter  --enable-thinking  --local-json-schema
```

Defaults: temperature 0.2, top_p 0.8, `max_tokens` 4096, read timeout 300 s, connect timeout 10 s (separate: a dead host fails fast, a slow generation
is still allowed). **Thinking is off by
default** via `chat_template_kwargs: {"enable_thinking": false}` in the request body
(`--enable-thinking` to switch on). Measured on the server in use: with the default template
the model still *generates* reasoning (a one-word answer cost 39 completion tokens instead of
2, 3.0 s instead of 0.7 s) but llama.cpp returns it in a separate `reasoning_content` field,
not in `content`; with `enable_thinking=false` neither is produced. `<think>...</think>`
blocks in `content` are stripped anyway for servers that inline them.
`--local-json-schema` additionally sends a per-tile `response_format: json_schema`
(grammar-constrained decoding); it worked on the smoke test but is off by default.

**Safety rule: whatever cannot be trusted becomes `unsure`, and `unsure` is `keep`.** The
reply is parsed for exactly one JSON object (code fences, leading/trailing prose and think
blocks tolerated); then validated strictly against the tile's own candidate numbers.

| Situation | Result |
|---|---|
| reply unparseable; truncated (`finish_reason=length`) or `content_filter`; two *different* JSON objects anywhere (an unfenced one next to a fenced one included); a repeated key (`{"1":keep,"1":drop}`, any level); a top-level array; JSON that starts but does not parse while `"verdict"` entries follow (unbalanced wrapper, unclosed array, truncated: inner objects are never salvaged); a `<think>` that is never closed, even after a complete answer; `content` that is not text (a list of `text` parts is joined) | one resample (`parse_retries=1`), then every candidate of the tile `unsure`, and the tile is *degraded* (below) |
| one candidate missing / not an object / verdict not exactly `keep`/`drop`/`unsure` | that candidate `unsure`; the others keep their verdicts |
| numbers not in `context.json` | discarded (recorded) |
| connection error (refused, DNS, reset, connect timeout, truncated/garbled HTTP response such as `IncompleteRead`/`BadStatusLine`) or HTTP 408/425/429/5xx, still failing after 2 retries (backoff 2 s, 4 s) | `BackendError` |
| read timeout (server connected but silent for 300 s) or any other HTTP status (e.g. 4xx context overflow) | `BackendError` at once — **not** retried by the backend |

A `BackendError` leaves the tile *unanswered* (no `answer.json`, no ops, picked up again on the
next `--resume` run). Retries stack: `runner.run_tile` makes its own extra attempt
(`max_retries=1`, i.e. 2 calls to `answer_tile`) after a `BackendError` as well as after an
invalid answer, so a tile whose server accepts the connection but never answers costs about
`2 x 300 s`, and a refused connection up to `2 x 3` connection attempts.

**Circuit breakers.** `run_all` raises `BackendCircuitOpen` (exit code 2 in `review_region.py`)
* after `--max-consecutive-backend-errors` (default 5, 0 = off) consecutive tiles that ended in a
  `BackendError`; a tile that got any answer resets the count. **This default now also applies to
  the Claude backend** (a missing credential or API outage used to grind through every tile);
* after `--max-consecutive-degraded` (default 10, 0 = off) consecutive *degraded* tiles: a server
  that answers HTTP 200 with garbage never raises `BackendError`, so the first breaker cannot see
  it. A non-degraded answer resets this count.

Tiles skipped because they already have a valid answer neither count nor reset either streak.
Answers already written stay; fix the server/prompt and re-run with `--resume`.

**Degraded answers.** When no reply was usable the backend returns the all-`unsure` fallback and
reports the tile through `consume_degraded`. `run_all` records this in
`<tile>/answer.meta.json` (`{"manifest_sha256": ..., "degraded": true}`; `answer.json` itself is
unchanged so its schema and external batch collectors are unaffected). `--resume` treats a
degraded answer as not done and asks the model again, and `--limit N` counts degraded and stale
tiles as *pending* (it answers the first N tiles that are not `valid`); `answers_to_ops` never derives ops from one.

**Round history (archive, not delete).** When a tile is re-run (`--no-resume`, or `--resume` of
a degraded/stale tile) or re-prepared with a changed manifest, its previous results are *moved*,
not deleted:

```
<tile>/answer.json, answer.meta.json, error.txt, manifest.json   current round (only these are read)
<tile>/history/0001/{answer.json, answer.meta.json, [error.txt], [manifest.json]}   oldest round
<tile>/history/0002/...                                           next superseded round
<tile>/local_audit.jsonl                                          append-only, never moved
```

`manifest.json` in a history dir is a copy of the manifest the answer was made against (omitted
when that answer was already stale). `answer_status`/`answers_to_ops` never look inside
`history/`, so a failed or degraded re-run can never fall back to an older answer -- yet a
backend outage during `--no-resume` no longer destroys paid-for work, and two rounds of a tuned
prompt can be diffed (`jq -S . history/0001/answer.json` vs the current one, next to the
per-round records in `local_audit.jsonl`). A lone `error.txt` (no answer) is simply deleted.

**Integrity of the current round.**
* `answer.json`, `answer.meta.json`, `error.txt`, `manifest.json` and `context.json` are written
  atomically (temp file in the same directory, `fsync`, `os.replace`); the meta goes first, so a
  crash leaves "no answer" (re-run), never a truncated or degraded-looking-valid one.
* Every answer records the sha256 of the manifest that was current *when the tile was sent to
  the backend*. If the manifest changes during the call (the tile was re-prepared), the answer is
  discarded (tile counted `unanswered`, reason in `error.txt`) rather than stamped with the new
  build.
* `answers_to_ops` raises `StaleAnswerError` if an answer's recorded manifest is not the tile's
  current one, or if its numbers are not exactly the manifest's. In `review_region.py` this
  aborts with exit code 2 **and no ops file is written at all** -- kept deliberately: one
  unverifiable tile means the whole batch is suspect, and the fix (`--no-resume` for that tile)
  is cheap.
* **Legacy limit:** an answer with no `answer.meta.json` (older version, external batch
  collector) is still accepted, but it cannot be tied to a build -- candidate numbers are always
  1..N, so a stale answer from another graph with the same tile id and candidate count is
  indistinguishable. `answers_to_ops` prints a warning on stderr naming such tiles; re-run them
  with `--no-resume` to stamp them.

**URL and transport.** The URL (`--local-url`, `$LOCAL_LLM_URL`) is validated up front
(`http://` or `https://`, a host, a numeric port); a scheme-less `192.168.10.111:8000/v1` or a
bad port is rejected before any graph is loaded or tile rendered (`review_region.py` exits 2).
`urllib_transport` maps everything else (bad URL, `InvalidURL`, ...) to the normal error path.

Only an exact `"drop"` on a validated entry can produce an op. The backend is thread-safe and
caps in-flight requests at 4 (the server's slot count) via a semaphore, but `runner.run_all`
is sequential, so today's run uses one slot.

**Per-round audit artefacts.** Every `answer_tile` call appends one JSON line to
`<tile>/local_audit.jsonl` (append-only: later rounds never overwrite earlier ones): UTC
timestamp, `model`, sampling `params`, `prompt_sha256` (first 12 hex of `prompt.txt`, changes
whenever the prompt does) and the optional `--prompt-label`; per sample the `latency_s`,
server `usage` (prompt/completion tokens), `finish_reason`, the **raw model output verbatim**
(plus `reasoning_content` when thinking is on) and any error; and `outcome` with the final
per-candidate verdicts and a `problems` map naming each candidate that was downgraded to
`unsure` and why. Comparing two rounds of a prompt is `jq` over these files. The run ends with
a one-line summary (calls, total latency, tokens, fallback tiles, downgraded candidates).

**Not validated as a reviewer.** The backend was smoke-tested for transport, image
understanding and JSON output on synthetic input only; how well the model judges real tiles
(and whether it clears the §6 bar of matching Sonnet's gold set) is unmeasured.

## 7. Known limits / follow-ups

- The skeleton wobble between junctions (§2.3) is untouched and needs Pass B/C or a
  generation-time fix.
- `NAVMESH_BOUNDARY_SIMPLIFY_M` is now the default of `--navmesh-boundary-simplify-m`
  (built 2026-09-21, see `SPEC-GRAPH-DENSITY.md` §10.6 item 1), but no real build has
  yet been run at a raised tolerance (§4.4).
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
- **Pass B has not been run against the `ClaudeBackend` API path** (§6.5) — real review
  data exists (§6.6-6.7, 323 candidates over 3x the original area) but was produced by
  the building session itself acting as the reviewer, not by a scripted API call. The
  Message Batches path (needed for the eventual weeks-long local-model production run) is
  still only import-verified, never executed.
- ~~The `nearest_poi` blind spot (§6.6: only searches the `pois` table, not lateral marks
  or named daybeacons)~~ — **IMPLEMENTED, see §6.8.**
- The "plain shoreline stub → drop" pattern (§6.6-6.7) now holds across 52 real drops with
  zero counterexceptions and is ready to prototype as a deterministic Pass A rule; it would
  need a held-out sample (tiles not yet hand-reviewed) to validate against, not just the
  same data that produced it.
- Regional composition swings the keep rate by 20 points (§6.7: 61% pilot vs 81% expanded)
  — any statewide sampling plan should stratify by creek-density/coastline-type or its
  headline drop-rate number will mostly reflect which regions got sampled.
- Pass C (route tracing, §6) has a prompt but no junction-numbering candidate generator —
  needed before "which of these five parallel lines is real" can be asked of a model.
- Only two candidate kinds exist (§6.1). The ~34% merely-unused (not provably redundant)
  edge set has no candidate generator yet, and needs a same-journey pairing to be useful
  rather than confusing.
- A handful of `unsure` verdicts (§6.7: narrow unnamed inlet breaches, stubs toward mid-water
  islets) are cases the current "plain point vs. real cove" heuristic can't resolve even by
  eye at this render scale — worth a closer render (`render_tile` at a tighter bbox) or
  aerial imagery before either is built as a candidate generator input.
- Rendering was the dominant per-tile cost before the layer cache: a bbox-filtered
  `read_file` re-scans the whole source file every call regardless of tile size (~3-4s
  against the real 84 MB `depare_polygons.geojson`, measured), so preparing many tiles
  from one region paid that cost per tile, per layer — ~14s/tile measured on the first
  Coltons Point run (16 tiles, MockBackend, 4m26s). Fixed by caching each whole layer
  once per process and slicing it in memory (`render._LAYER_CACHE`, `.cx[]`), turning an
  O(tiles × layers) disk-scan cost into O(layers): the same 16-tile Coltons Point run
  after the fix took 27s (~1.7s/tile, ~8x). §6.4's verification predates the fix; the
  16-tile / 104-candidate numbers there are still accurate, only the wall-clock changed.
