# Spec: Graph Node Density — Over-Sampling and Fairway Duplication

Status: §4.1 implemented and verified. §4.1.2, §4.1.3, §5.1 and §6.1 fixed. §6.3, §6.4,
§6.5, and §6.6 implemented. None enabled by default. §6.5 is the fix for the net
density regression §6.3+§6.4 compounded; §6.6 is the fix for the residual hub-fanout
§6.5 alone did not resolve (traced to a separate mechanism, `_stitch_component_pieces`
Pass 2). See `data/BUILD_LOG.md` for every real build's measured effect before
assuming any of these should ship enabled by default.
Complements: `SPEC-RECOMMENDED-TRACK.md`, `SPEC-FAIRWAY-HARMONIZATION.md`
Scope: `nautical_routing_pipeline.py` (`build_skeleton_network`, `_resample_long_skeleton_edges`, `_skeleton_raster_to_graph`, `ClassificationConfig`)
Measured against: `data/zeeland_full.sqlite` (48,553 nodes / 137,718 directed edges), RWS source GeoJSON

## 1. Symptom

Rendering the Zeeland graph over OSM shows a dense mat of nodes and edges across open
water and along the buoyed fairway — visually cluttered, and heavier in the database
than the routing actually needs. The motivating view is the Krammersluizen lock complex
(51.655–51.670 N, 4.145–4.180 E, ~2.4 × 1.7 km): **342 nodes in 4 km²**.

The intuition prompting this spec was that auto-discovery is duplicating the fairway /
recommended route. That is real but is *not* the main driver — the measurements below
separate the two, because they need different fixes and have very different payoffs.

## 2. Measurements

### 2.1 The graph is over-sampled along single strands, not braided

| Metric | Value |
|---|---|
| Nodes of degree 2 (pure chain interior) | **22,499 = 46.3%** |
| Degree-2 within the centerline graph alone | **70.3%** |
| Node pairs <25 m apart but >3 graph hops apart (parallel strands) | 590 |
| Nodes with a neighbour closer than 10 m | 8.8% |

Douglas-Peucker over degree-2 chains only (junctions and endpoints pinned):

| Tolerance | Nodes removable | Share of whole DB |
|---|---|---|
| 5 m | 11,957 | 24.6% |
| **10 m** | **16,237** | **33.4%** |
| 25 m | 19,846 | 40.9% |
| 50 m | 21,111 | 43.5% |

Only 590 close-but-topologically-distant node pairs exist at 25 m, so this is **not**
duplicate parallel centerlines. It is one strand carrying far more vertices than its
shape requires.

### 2.2 The dominant source is the uniform 100 m resample

Coastal centerline edge lengths:

| Length | Edges | Share |
|---|---|---|
| 0–10 m | 1,538 | 1.9% |
| 10–25 m | 20,524 | 25.1% |
| 25–50 m | 4,048 | 5.0% |
| 50–90 m | 9,706 | 11.9% |
| **90–110 m** | **39,466** | **48.3%** |
| >110 m | 6,374 | 7.8% |

The 90–110 m spike is `ClassificationConfig.max_segment_m = 100.0` applied by
`_resample_long_skeleton_edges`, which splits every centerline into ~100 m segments
**unconditionally, regardless of curvature**. Nearly half of all coastal centerline
edges are that one constant.

The 10–25 m band (a further 25%) is the medial-axis raster emerging at pixel
resolution (`pixel_min_m = 2.0`, `pixel_max_m = 10.0`): `_skeleton_raster_to_graph`
chains are *split* by the resampler but never *simplified*, so short inter-junction
chains reach the graph at raw pixel spacing.

### 2.3 Fairway duplication is real but secondary

| Metric | Value |
|---|---|
| Nodes inside a fairway polygon | 7,109 = 14.6% |
| Coastal medial-axis nodes within 10 m of the RWS `wtwaxs` axis | **2,396 = 4.9% of the DB** |
| Generated centerline length inside fairways | 551.7 km |
| Authoritative axis length inside fairways | 2,138.8 km |

So the pipeline emits its own centerline within 10 m of an authoritative, already-imported
axis about 2,400 times. Worth fixing — but it is **~5% of the graph, against ~33% for
over-sampling**. Note the braiding ratio is 0.3×, i.e. *less* generated centerline than
axis inside fairways: the fairway areas are comparatively well-behaved, and the density
the screenshot shows is mostly open-water medial axis, not fairway duplication.

## 3. Why the 100 m constant exists, and what it is actually proxying for

`_resample_long_skeleton_edges`' docstring: re-inserting nodes "keeps the existing
straight-chord depth sampler valid on curved channels … without modifying that worker."
`_edge_attr_worker` samples depth at 5 points along the **straight chord** between an
edge's endpoints. On a curving channel a long chord leaves the water the edge claims to
follow, so depth and width get sampled from the wrong place.

The quantity that actually matters is therefore the **sagitta** — the maximum deviation
between the chord and the true centerline — not the chord's length. A 100 m cap is a
crude proxy: on a tight bend even 100 m may be too long, and on a straight reach a
1,000 m chord has near-zero sagitta and costs nothing in sampling fidelity. The current
constant pays the worst-case price everywhere, which is precisely the 48.3% spike above.

## 4. Proposed direction

Ordered by measured payoff. Each is independent.

### 4.1 Sagitta-bounded adaptive resampling (largest win) — IMPLEMENTED

Replace the uniform `max_segment_m` cut in `_resample_long_skeleton_edges` with a split
rule driven by chord deviation: walk the pixel-resolution polyline and close a segment
when the perpendicular distance from any skipped vertex to the running chord would exceed
`max_chord_sagitta_m`, **or** when a hard ceiling (`max_segment_m`, retained as a
backstop) is hit.

- Straight reaches collapse to a handful of long edges; bends keep — or gain — density
  exactly where the sampler needs it. This is strictly *more* faithful than today at bends.
  It is **not** free on straight reaches: sagitta bounds chord-to-centerline deviation, but
  `_edge_attr_worker` still samples depth at only 5 points along that chord, so a longer
  edge can step over a shoal or a dredged-channel boundary between samples (§7). The
  depth-non-optimism probe in §6 is therefore a **release gate**, not a nice-to-have —
  no straight-reach relaxation ships until it passes.
- Tolerance must be **coupled to local channel width**, not flat. See §4.1.1 — this is
  the decision that matters; the cap on top of it barely does.
- **Edges with no measured width are excluded from relaxation.** `width_profile` is absent
  on 18,002 of 99,112 centerline edges (navmesh-boundary, lock-transit, inland), and
  §4.1.2's `min_width` fallback for those is 999.0 — feeding that into
  `min(cap, 0.5 × width)` would apply the *most* aggressive simplification exactly where
  the channel is least known. Such edges keep today's uniform `max_segment_m` behaviour
  unchanged. The rebuild results must report how many edges took this fallback.
- `min_width` / `width_profile` merge safely: both are minima over the span, and a
  minimum over a union is the min of the minima. Depth is sampled after the split, so it
  picks up the new geometry automatically.
- Expected: most of the 33.4% node reduction that DP@10 m shows as available, without DP's
  blindness to the sampler contract.

#### 4.1.1 Choosing the tolerance — measured, not guessed

Node reduction saturates quickly, while the safety cost of a *flat* tolerance keeps climbing.
Douglas-Peucker over degree-2 chains, and the share of centerline edges whose channel is
narrower than twice the tolerance (i.e. where the chord would leave the water):

| Flat tolerance | Nodes removed | Marginal gain | Edges where sagitta > ½ channel width |
|---|---|---|---|
| 10 m | 33.4% | — | **1.9%** |
| 25 m | 40.9% | +7.4pp | 20.7% |
| 50 m | 43.5% | +2.6pp | 28.0% |
| 75 m | 44.4% | +0.9pp | **34.4%** |
| 100 m | 45.0% | +0.5pp | — |
| 200 m | 45.7% | +0.7pp | — |

The ceiling is 46.1% (every degree-2 interior node). Past ~25 m the curve is flat: going
10 m → 75 m buys 11pp, but 25 m → 75 m buys only **3.5pp** while taking the share of
edges whose chord leaves its channel from 20.7% to **34.4%**.

That safety column is driven by a strongly bimodal channel-width distribution — median
346 m, but **15.1% of centerline edges sit in water narrower than 25 m** and 26.6% in
water narrower than 100 m, against 52.5% wider than 300 m:

| Channel width (medial axis) | Share of centerline edges |
|---|---|
| 0–25 m | 15.1% |
| 25–50 m | 5.5% |
| 50–100 m | 7.3% |
| 100–150 m | 6.5% |
| 150–300 m | 13.1% |
| >300 m | 52.5% |

So no single flat number is right: the same tolerance that is wasteful in the Oosterschelde
puts the chord on the bank in a Zeeland creek.

**With width coupling — `sagitta ≤ min(cap, 0.5 × local width)` — the cap stops mattering:**

| Cap (coupled) | Nodes removed |
|---|---|
| 25 m | 36.2% |
| 50 m | 37.4% |
| 75 m | 37.8% |
| 100 m | 38.0% |
| 200 m | 38.3% |

From 50 m to 75 m is +0.4pp, and to 200 m only +0.9pp, because local width — not the cap —
is what binds. **Recommendation: adopt the coupling and set the cap generously (75 m is
fine, so is 150 m); do not raise a flat tolerance.** Expected yield ≈ 38% of nodes
(~18,400 of 48,553), edges roughly 137,718 → ~101,000.

#### 4.1.2 Prerequisite (FIXED): `min_width` was clobbered before reaching the database

The coupling above needs per-edge channel width, and the column that should hold it is
unusable: **every one of the 137,718 edges has `min_width = 999.0`.**
`build_skeleton_network` computes a real medial-axis width (`min_width=min(sub_widths)`,
line 3235), but `_edge_attr_worker` then sets `attrs['min_width'] = 999.0` unconditionally
(line 264) as its lock-clearance default, and `calculate_edge_attributes` writes every
worker key back onto the edge — so the skeleton's value is overwritten unless a lock's
`HORCLR` happens to intersect.

The data itself was not lost: `width_profile` survives with real values on 81,110 of 99,112
centerline edges (`{"min_m": 142.8, "samples_m": [...]}`).

**Implemented.** `edge_generator` now passes the edge's existing `min_width` into the
worker, which seeds from it instead of from 999.0, and a lock's `HORCLR` is applied as a
`min()` against that value rather than replacing it. The `min()` is the substantive part:
a 12 m gate still wins inside a 300 m basin, but a 20 m gate no longer *widens* a 6 m
creek to 20 m, which the old replace-outright branch did wherever a lock polygon was
wider than the channel it sits in. Edges that never carried a measurement (navmesh
boundary, lock transit) still default to 999.0, so their behaviour is unchanged.

Covered by `tests/test_edge_min_width.py`.

#### 4.1.3 Follow-on (FIXED): the lock width constraint never applied

Verifying §4.1.2 on a real pilot-clip build showed **zero** edges narrowed by a lock,
which is implausible in Zeeland. Cause: the branch only ever looked for an `HORCLR`
column, and no lock in this data carries one. Across the full RWS set, of **304 lock
polygons `HORCLR` is absent as a column entirely, while `HORWID` holds a real value on
247** — so this constraint has never applied to a single edge on any build.

S-57 uses `HORCLR` for a clearance *between* structures (the bridge sense) and `HORWID`
for a structure's own horizontal width, which is what a lock chamber publishes. Both
express the navigable width here.

**Implemented.** Prefer `HORCLR` where present, fall back to `HORWID`, with the same
case-variant tolerance `_s57_col` already gives `catbrg`/`vercop`/`verclr`, and treating
`0` as "not surveyed" exactly as the `VERCLR` branch does for bridges. This is
load-bearing rather than cosmetic: it makes a narrow lock chamber actually constrain
routing, where a 12 m gate was previously invisible.

Also covered by `tests/test_edge_min_width.py`.

### 4.2 Simplify the raster chain before it becomes graph edges

Apply a small Douglas-Peucker (≈ ½ pixel, so 1–5 m) to each `_skeleton_raster_to_graph`
chain *before* `_resample_long_skeleton_edges` sees it. Targets the 25.1% of edges in the
10–25 m band, which are raster discretisation, not channel shape. Cheap and independent
of 4.1, though 4.1 subsumes part of it.

### 4.3 Prefer the authoritative axis over a generated twin — SCOPED, ready to implement

Where a medial-axis centerline runs within a small tolerance of an imported
`inland_waterways_lines` axis (`wtwaxs`/`RECTRC`/`NAVLNE`), keep the axis and drop the
generated twin, rather than emitting both and stitching them. Addresses the 2,396 nodes
in §2.3, and directly the case a rendered screenshot of Krammersluizen surfaced after the
§4.1 resampler shipped: a tight cluster of medial-axis nodes sitting 3-10 m from a WTWAXS
line the pipeline had already ingested as authoritative, because `build_skeleton_network`
rasterizes and skeletonizes `coastal_water` with no awareness that `_build_inland_network`
already covers the same channel from a separate source.

This is the same lever `SPEC-RECOMMENDED-TRACK.md` §4 weighs as Option B, and the density
argument is a second, independent reason to take it — that spec deferred Option B purely
on feature count (75 US `RECTRC` lines). In NL the axis is dense (3,689 lines / 3,711 km),
so the two specs should be decided together, not separately: **Option B is much better
motivated by NL density than by US coverage.**

#### 4.3.1 Tolerance — measured, not guessed, and NOT a flat number

Naive framing: pick a flat distance (the screenshot suggested "50 m each side"). Measured
against every coastal medial-axis node in the deployed Zeeland database against its actual
`inland_waterways_lines` source (23,614 nodes with known local channel width):

The raw distance-to-axis histogram has **no clean gap** to anchor a flat number on — it
decays smoothly from 0 to 150 m+ rather than splitting into "duplicate" vs "not". A flat
cutoff is provably wrong in both directions:

- **Too loose in narrow water.** A flat 50 m rule flags nodes up to 50 m from the axis in a
  channel that is only 24 m wide — i.e. it reaches past the channel's own far bank into
  water the axis was never describing. 797 nodes in the Zeeland measurement would be
  wrongly suppressed this way.
- **Too tight (or simply wrong-shaped) in open water.** In a channel 833 m wide, a WTWAXS
  thread 192 m from the medial axis's geometric center is very plausibly still the same
  water body — the axis just doesn't run down the exact middle. A flat rule sized to catch
  that case would need to be huge, and would then over-reach into every other narrow
  channel it touches.

**Use the same coupling shape as the §4.1 resampler:**
`tolerance = max(floor, min(cap, fraction × local_channel_width))`, with **cap = 50 m,
fraction = 0.5, floor = 5 m** (the floor sits below the medial-axis raster's own pixel
resolution — `ClassificationConfig.pixel_min_m = 2.0` — so it never suppresses something
the raster itself couldn't have resolved as distinct anyway).

The cap and the fraction never fight each other — each governs a different regime:

| width band | median dist to axis | flat-50 vs coupled-50/0.5 agreement | what the cap/fraction is doing |
|---|---|---|---|
| <25 m | 680 m | fraction binds (`0.5×width` ≤ 12.5 m), cap never reached | prevents the flat rule's over-reach into unrelated nearby channels |
| 25–100 m | 385-406 m | fraction binds up to ~50 m | — |
| >400 m | 962 m | **identical to flat** — `0.5×width` always exceeds the 50 m cap | cap bounds how far suppression reaches into open water regardless of width |

Net effect on the deployed Zeeland database: **~8.0% of coastal centerline nodes** would
never be generated (1,888 of 23,614 measured), concentrated in narrow channels and locks —
only 289 of 4,507 nodes in water wider than 400 m (6.4%) are affected, so open-water medial
axis coverage is barely touched.

**Validated directly against the motivating screenshot.** Applying the rule to the 19
Krammersluis-area nodes measured by hand: the exact tight cluster (3.2-9.7 m, local width
24-73 m) is suppressed, two more just past it (17.1 m and 26.5 m, local width 69-73 m) are
correctly pulled in as proportionally still inside the same channel, and everything
genuinely separate (56 m+, and three nodes at 105-139 m in what is evidently a different,
wider basin near the lock) is correctly kept.

#### 4.3.2 Implementation — carve the raster before skeletonizing, not after

Per the "better" option this spec's original draft named but didn't choose between: do
this at raster time, in `_rasterize_water_polygon`/`_extract_medial_axis_skeleton`'s
inputs, so the twin is never generated rather than generated and then pruned.

Local channel width is already available at this stage without waiting for the skeleton:
`medial_axis(mask, return_distance=True)`'s distance transform gives, at every water
pixel, its distance to the nearest boundary — exactly the quantity `width_m()` in
`_skeleton_raster_to_graph` already turns into `width_profile` downstream, just computed
here for every water pixel instead of only skeleton pixels. Sketch:

1. `width_est = scipy.ndimage.distance_transform_edt(mask) * pixel_size_m * 2` — local
   channel width per water pixel, from the ORIGINAL (uncarved) mask, so estimates near the
   axis are not distorted by the carving that hasn't happened yet.
2. Rasterize the relevant `inland_waterways` lines (bounding-box prefiltered against this
   piece, same pattern as `_candidates_by_bounds_static`) onto the same grid; run
   `distance_transform_edt` on the inverse to get `axis_dist` per pixel.
3. `tol = np.clip(fraction * width_est, floor, cap)`; `suppress = axis_dist < tol`.
4. Feed `mask & ~suppress` into `_extract_medial_axis_skeleton` as usual.

Carving can fragment a piece's mask into disconnected pieces along a channel the axis runs
alongside. No new mechanism needed for that: `build_skeleton_network` already emits
degree-1 dead ends into `navmesh_seam_node_ids` for the existing stitching passes to
reconnect (§6.2's Pass 2 fix is exactly the machinery this would lean on), so a carved
raster is not a fundamentally different case from the fragmentation the pipeline already
handles.

Ship disabled by default (a `--axis-dedup-cap` flag, `0.0` = off, matching `--sagitta-cap`'s
convention) until verified on a real build.

#### 4.3.3 Verification plan

Same gates as §4.1, since this changes the same kind of thing (which nodes exist) for the
same underlying reason (redundant density):

- `crosses_land` must stay 0.
- Largest-component connectivity measured **by edge length, not node count** (§6.1) — must
  not regress against the build with `--axis-dedup-cap 0`.
- POI-pair reachability (§6.1's method) — zero real place-pairs may lose routability.
- Report the count of suppressed nodes and cross-check a sample against source `src_objl`/
  `OBJL` to confirm suppression only fires near genuine `wtwaxs`/`RECTRC`/`NAVLNE` lines.
- `--axis-dedup-cap 0` must reproduce the pre-change build exactly (same discipline as
  `--sagitta-cap 0` in §4.1/§6).

### 4.4 Not recommended: post-hoc DP on the exported graph

Simplifying after the fact would hit the 33% but silently invalidate every already-computed
`min_depth` / `min_width` / `crosses_land` attribute on the merged edges, since those were
sampled against the pre-merge geometry. Any decimation must happen *before*
`calculate_edge_attributes`, which is why 4.1 and 4.2 are placed where they are.

## 5. Measurement noise floor: builds were not reproducible (FIXED)

Before any before/after sweep can be trusted, the pipeline's own run-to-run variance has
to be known. Two builds of the **same clip, same commit, same input**, run sequentially:

| | Run 1 | Run 2 | Spread |
|---|---|---|---|
| Nodes | 33,470 | 33,726 | 256 (0.76%) |
| Edges | 87,703 | 88,331 | 628 (0.72%) |
| `crosses_obstacle` | 2,100 | 2,196 | 96 (4.6%) |
| `crosses_land` | 0 | 0 | — |

Counts are stable to under a percent. **Node identity is not:**

| | |
|---|---|
| Shared node ids | 25,786 — **62.3%** of the union |
| Only in run 1 | 7,684 |
| Only in run 2 | 7,940 |

Node ids are coordinate-derived (`_coord_to_id`), so a differing id means a node at a
different position: **15,624 nodes — 37.7% of the union — moved between two runs of
identical code on identical input.** Both graphs are valid (`crosses_land = 0` in each);
they are simply different meshings of the same water.

Two consequences:

1. **The §4.1 sweep is safe to run.** Its expected effect (~38% fewer nodes) is roughly
   50× the count noise floor, so count-based comparisons are meaningful. Run each config
   twice regardless, and treat any difference under ~2% in counts as noise.
2. **Anything keyed on node identity across builds is not safe.** This bears directly on
   Round 25 cross-database seam stitching, which matches seam nodes on coordinates
   (`_seam_coord_set`, `_publish_seam_nodes`, `_adopt_seam_nodes`): two independently
   built adjacent regions cannot be assumed to agree on a seam node's position, and
   rebuilding one region of a stitched set may silently break its seams with neighbours
   that were not rebuilt. This is pre-existing and unrelated to anything in this spec,
   but it deserves its own investigation.

### 5.1 Root cause and resolution

Not the capped passes guessed at above. `skimage.morphology.medial_axis` breaks ties by
processing pixels in an order drawn from a PRNG, and its `rng` parameter defaults to a
**fresh unseeded generator on every call** — "the PRNG determines the order in which
pixels are processed for tiebreaking", per its own documentation.
`_extract_medial_axis_skeleton` called it with no `rng`, so every build drew a different
centerline from the same raster.

That accounts for the whole signature above: the skeleton keeps its topology and length
(counts stable to under 1%) while ties broken differently nudge the axis by a pixel here
and there, and node ids are coordinate-derived. Demonstrated minimally — two unseeded
`medial_axis` calls on one fixed mask return different arrays; through a seeded wrapper
they are identical.

**Fixed** by seeding (`MEDIAL_AXIS_SEED`), with the keyword bound at import by inspecting
the signature, since it has been spelled `rng` / `random_state` / `seed` across the
versions `requirements.txt` allows (`scikit-image>=0.22`).

Verified on two full builds of the same clip:

| | Before (`995b9bc`) | After (`de98535`) |
|---|---|---|
| Run 1 / Run 2 nodes | 33,470 / 33,726 | **33,057 / 33,057** |
| Run 1 / Run 2 edges | 87,703 / 88,331 | **86,617 / 86,617** |
| Shared node ids | 62.3% | **100.0%** |
| Edge lists identical | no | **yes** |
| `crosses_land` | 0 / 0 | 0 / 0 |

Builds are now bit-for-bit reproducible, so the §6 sweep can compare configurations
directly rather than against a noise floor, and seam stitching has a stable basis. Note
the seeded build lands at a slightly different node count than either unseeded run —
expected, since it fixes one particular tie-breaking order rather than reproducing a
previous accidental one.

Covered by `tests/test_medial_axis_determinism.py`.

## 6. Verification plan

- §4.1.2 is done, so per-edge width is now available to drive the coupling, and §5.1
  makes builds reproducible, so a single run per configuration is now sufficient.
- Rebuild Zeeland with 4.1 width-coupled at caps 25/75/150 m, plus one flat-75 m control to
  confirm the predicted land-crossing damage is real and not an artefact of this estimate.
  Record node/edge counts, DB size, and `_sanity_check_no_land_crossings` violations — the
  coupled runs must not regress it at any cap; the flat control is expected to.
- Confirm the 90–110 m spike flattens and that new long edges appear only on straight reaches
  (assert measured sagitta ≤ tolerance on every emitted edge).
- Re-measure the Krammersluizen view (342 nodes today; DP@10 m suggests ~272 is reachable).
- Route-quality probe: compare a set of Zeeland routes before/after for distance and
  `min_depth` along the path. Depth must not become more optimistic anywhere.
- Largest-component connectivity must not regress — but measured **by edge length, not
  node count**. See §6.1: the node-count form of this gate is invalid for comparing
  graphs of different densities, and cost two full investigations before that was
  spotted.

### 6.1 The connectivity gate was measuring the wrong thing

The gate above originally read "largest connected component / total **nodes**". That is
invalid for judging a change whose entire purpose is to remove nodes.

Resampling strips interior vertices from long chains, and those chains live
overwhelmingly in the main component. Small disconnected fragments are short, so they
keep almost all their nodes. The main component therefore shrinks *as a fraction of
nodes* while the water it covers is unchanged — the metric penalises exactly the
intended effect.

Measured three ways on `data/zeeland_clip`:

| build | nodes | main comp | total | **% by length** | % by nodes |
|---|---|---|---|---|---|
| baseline (`--sagitta-cap 0`) | 33,057 | 4,154 km | 4,498 km | **92.35%** | 86.49% |
| cap 75 / seg 2000, no Pass 2 fix | 20,136 | 4,097 km | 4,382 km | 93.51% | 81.40% |
| cap 75 / seg 2000 + Pass 2 fix | 19,895 | **4,138 km** | 4,364 km | **94.81%** | 83.88% |

By navigable length the resampled build is **better** than baseline (94.81% vs 92.35%),
with the main component covering 4,138 km against 4,154 km — a 0.4% difference.

Reachability confirms it directly. Taking the 127 named harbour/lock/bridge POIs, snapping
each to its nearest node in both builds, and comparing all 8,001 pairs:

| | |
|---|---|
| Mutually routable in both builds | 7,626 (95.31%) |
| Routable in baseline, **lost** after the change | **0** |
| Gained | 0 |

Not one real place-pair lost routability. Mean POI-to-nearest-node snap distance rises
54 m → 68 m, the expected cost of sparser nodes.

**Use edge-length share plus the POI-pair reachability check as the gate.** The node-count
form sent two investigations chasing a 2.61pp "regression" that does not exist, and caused
a working gap-resolve improvement to be reverted because minting nodes inflated its
denominator.

### 6.2 Pass 2 candidate selection was weak independently of resampling

`_stitch_component_pieces` Pass 2 sampled per-group representatives by list-insertion
stride rather than geometry, then compared them pairwise. With the dominant component
entering at ~380-410 groups the per-group cap is 3-4, so the true nearest cross-group
connector was routinely never evaluated. Replaced with a per-node escalating-k `cKDTree`
nearest-cross-group-pair search, mirroring `_resolve_local_skeleton_gaps`' own pattern.
Pass 2 successes 14 → 53 under resampling, against a baseline of 27.

It is gated to sagitta-active builds only, because ungating it changes `--sagitta-cap 0`
output. Worth noting what that trial measured: ungated, **today's pipeline gains 1.11pp
connectivity (86.49% → 87.60%)**. So this weakness is costing connectivity in every
database currently shipped, not only under resampling. Ungating it is a candidate
follow-up, deliberately not taken here because it changes shipping output.

### 6.3 Axis-dedup deletes topology instead of replacing it — reconnect to the axis line

§4.3 shipped (PR #14, three CodeRabbit review rounds, merged) and is deployed. But two
things it left unresolved both trace to the same root cause, discovered while trying to
close the first:

**The symptom.** Two nodes from the original motivating screenshot — one at a seam near
marks KR11–14A, one on the Krammersluis approach (not inside the lock polygon; 117 m
away) — remain unsuppressed after everything shipped in §4.3/§7. Both sit at an *exact*
tie: `axis_dist_m == tol_m` (10.00 m and 12.25 m respectively), because both quantities
are computed from the same pixel-quantized `distance_transform_edt` grid, so exact
equality is systematic at these positions, not a rare coincidence. The comparison is
strict `<`, so a tie means "not suppressed."

**Why the obvious fix (`<=`) was rejected.** Measured on a real rebuild, not assumed:
switching to `<=` did flip both tie nodes to suppressed, but it also raised suppression
5.2%→5.7% dataset-wide and disconnected an unrelated 5-node stub near Hansweert — 178 m
from *any* axis line, previously held onto the main component by a 3 m link — costing
**138 POI-pairs**. Confirmed the causality by reverting and rebuilding again. A narrow
cosmetic fix that costs 138 real pairs is the wrong trade, so `<=` was backed out and the
tie left as a documented, accepted limitation (`tests/test_axis_dedup.py`).

**The actual cause, and why it generalizes.** Axis-dedup carves the raster mask (or,
after §7's navmesh extension, the piece polygon) *before* any topology is generated in
that footprint. Nothing else happens — the pipeline just hopes the generic stitching
passes (Pass 0–2, gap-resolve) rediscover a replacement connector for whatever got cut.
Those passes search for the nearest *other graph node* within a radius, subject to a
straight-line land-crossing check; they have no idea an authoritative axis line is
sitting right there — the one thing that justified the carve in the first place. The
Krammersluizen lock-crossing regression (§7's original finding, fixed by
`_lock_protection_mask`) and the Hansweert regression above are the same failure mode
twice: carving deletes a connector and nothing deliberately replaces it. `_lock_
protection_mask` is a narrow, lock-specific patch for one instance of this; it does not
generalize to Hansweert, or to the next case that hasn't been found yet.

**The fix: connect carve-induced dead ends to the axis line, instead of leaving them to
chance.** This is not a new mechanism to invent. `_connect_waterway_crossing` (used
today only from `build_navmesh_region`'s waterway-crossing injection, `_inject_waterway_
crossings` → `_connect_waterway_crossing`) already does almost exactly this: given a
graph node, a candidate `inland_waterways` line (`line_iloc`), and a position, it finds
the *nearest existing vertex* on that line — no edge-splitting, no new mid-line node —
checks two already-tuned radii (`WATERWAY_CONNECTOR_MAX_M = 250.0`, fallback
`WATERWAY_CONNECTOR_FALLBACK_MAX_M = 500.0`, both comfortably larger than `axis_dedup_
cap_m`'s 50 m, so reuse can never be too restrictive), verifies `_crosses_land`, and
wires a real edge. What's missing is a caller: nothing currently invokes it for a carve-
induced dead end on either the skeleton or the navmesh path.

#### 6.3.1 Design

**Implemented** (Phases A–C, per §6.3.5's sequencing; D and E remain open). Two points
below were corrected during implementation against what this section originally assumed
— left visible rather than quietly fixed, since both were real gaps in the design as
written, not just wording:

1. **Identify carve-induced dead ends.** After skeleton extraction / navmesh
   triangulation completes for a piece where axis-dedup fired, find degree-1 nodes whose
   position sits close to the suppression mask's own boundary. **Correction**: "roughly
   one pixel width" was wrong — measured directly, `medial_axis`'s own end-cap
   construction routinely sets a fragment's centerline terminus back from the true carved
   edge by several pixels (confirmed: 3px in one synthetic case), not one, so the shipped
   search radius (`AXIS_DEDUP_DEADEND_SEARCH_RADIUS_PX = 4`) is wider than originally
   assumed. Also confirmed empirically: severing a channel does not reliably leave a
   *clean* degree-1 node at the cut at all — `medial_axis` often produces a small
   degree-2/3 junction knot there instead. This design's `occurrences == 1` test (reusing
   the exact signal `navmesh_seam_node_ids` already relies on, unchanged) correctly
   leaves a knot untouched, the same as it leaves a genuine dead end untouched —
   reconnecting a knot would need different, junction-aware logic, out of scope here. So
   coverage is real but partial: not every carve-induced dead end resolves to a shape
   this mechanism reconnects, only the ones that surface as an actual degree-1 node.
2. **Identify the responsible line.** **Correction**: the carve step did *not* already
   know this — every candidate line was burned into one undifferentiated raster value,
   destroying per-line identity before the distance transform ran. Implemented the
   "cheaper option" anyway, since it turned out cheap to add properly: burn each
   candidate with its own `inland_waterways` positional index instead of a shared
   constant, and add `return_indices=True` to the distance transform `_axis_dedup_
   suppression_mask` already runs — the same computation scipy already performs, no
   second pass — to get each suppressed pixel's nearest-line lookup for free.
3. **Call `_connect_waterway_crossing`** with the dead end's node id, the responsible
   `line_iloc`, and its metric-CRS position. No new connector logic — reused verbatim.
4. **Wired into both carve sites**: the skeleton path (`build_skeleton_network`, after
   `_axis_dedup_suppression_mask` carves) and the navmesh path (`_axis_dedup_carve_
   navmesh_pieces`, §7), sharing one `line_m_cache` per piece with the navmesh path's
   existing `_inject_waterway_crossings` connector where both fire on the same piece.

#### 6.3.2 Bundled: the raster-padding gap (CodeRabbit, PR #14 round 4)

Deferred out of PR #14 to land here, since it's the same suppression/carving mechanism
and the two are easiest to verify together. `_rasterize_water_polygon`'s transform is
built strictly from `poly_m.bounds` — no padding — so a candidate axis line lying outside
a piece's own bounding box cannot be rasterized onto that piece's grid at all, *regardless
of true distance*. The all-zero guard added while fixing the phantom-corner artifact
(§4.3, round 3) correctly silences the genuinely-far-away case but also silently silences
a second, different one: a real, nearby line that should suppress water right at a
piece's boundary, just because it falls outside that piece's small raster footprint.
Confirmed by CodeRabbit's own reproduction: a candidate 40 m north of a piece's edge, ~42.5 m
from the nearest water cell, well inside the 50 m cap — silently skipped.

Fix: pad the raster grid outward by `axis_dedup_cap_m` before rasterizing candidates and
computing `axis_dist_m`, crop back to the piece's own `mask` shape before returning. Keep
the all-zero guard only for the case where nothing survives even in the padded grid.

This is a coverage gap, not a safety one — under-suppression can never disconnect
anything, it only means some legitimate duplicate nodes near piece edges don't get
removed. Bundling it here rather than blocking PR #14 on it was the right call; fixing it
alongside reconnect means both raster-boundary questions (grid-edge and tie-boundary) get
verified in the same rebuild pass instead of two separate ones.

#### 6.3.3 Once reconnect is verified, two follow-on questions become answerable

- **Revisit `<=`.** Reconnect is what makes flipping the tie-break safe: the reason `<=`
  was rejected was that carving without replacement could strand a fragment, and Hansweert
  is exactly that. With reconnect in place, re-run the identical `<=` experiment that
  produced the 138-pair regression. If it now closes the tie with zero loss, take it; if
  some residual loss remains, that means reconnect itself has a gap worth chasing before
  touching the comparison operator again — do not re-flip `<=` until reconnect's own
  gates are clean on `<` first.
- **Test whether `_lock_protection_mask` is still needed.** Its entire reason to exist is
  "carving deletes something and nothing reconnects it" — exactly the general problem
  reconnect fixes. Once reconnect exists, remove the lock-specific carve-out and re-run
  the exact reachability check that caught the original Krammersluizen regression
  (§7 / PR #14). If it passes, `_lock_protection_mask` is redundant and should come out —
  one less special case, and the general mechanism gets to be the only mechanism. If it
  doesn't pass, keep the protection mask; `_add_lock_crossing_edges`'s quadrant search may
  have geometric requirements (precise entry/exit points at the lock polygon boundary)
  that a generic nearest-vertex reconnect doesn't automatically satisfy. Test, don't
  assume either way.

#### 6.3.4 Verification plan

Same five-gate discipline as §4.3.3, plus checks specific to this change:

- Gates 1–4 unchanged (inert at `axis_dedup_cap_m = 0`, `crosses_land = 0`, connectivity
  by edge length non-regressing, POI-pair reachability zero-loss — id-keyed, see §6.1's
  method note on duplicate POI names).
- **The Hansweert stub must remain connected** under both `<` (already true today) and,
  if §6.3.3's `<=` re-test is taken, under `<=` too — this is the specific case reconnect
  exists to fix; verify it directly by node/coordinate, not only via the aggregate
  reachability count.
- **Both original tie-boundary nodes** (KR11–14A seam, Krammersluis approach) checked by
  coordinate proximity post-rebuild, same as every prior round — confirm whichever of
  §6.3.3's two paths is taken (reconnect alone under `<`, or reconnect + `<=`) actually
  resolves them, not just that nothing else broke.
- **Connector edges are geometrically sane**: spot-check a sample of newly-created
  reconnect edges for length (should cluster well under `axis_dedup_cap_m` + local vertex
  spacing, not at the 500 m fallback radius routinely) and confirm none cross land
  (already enforced by reusing `_connect_waterway_crossing`, but verify the reuse didn't
  bypass that check).
- **The padding fix (§6.3.2)** closes CodeRabbit's specific reproduction case directly —
  rebuild the exact fixture geometry (candidate 40 m outside a piece's north edge, 50 m
  cap) and confirm suppression now fires there.
- If §6.3.3's `_lock_protection_mask` removal is attempted: the removal is a
  **conditional** change — ship it only if the Krammersluizen reachability check passes
  with it removed; otherwise keep the mask and document why the general mechanism wasn't
  sufficient there.

#### 6.3.5 Phased implementation

- **Phase A** ✅ — wire `_connect_waterway_crossing` into the navmesh carve path (§7),
  since that path is where the connector mechanism already lives; smallest reuse
  distance. Shipped: `_axis_dedup_suppression_mask` returns per-suppressed-pixel line
  identity, `_axis_dedup_carve_navmesh_pieces` returns carve-boundary seam coords +
  responsible line, `build_navmesh_region` reconnects them in a second pass sharing
  `line_m_cache` with the existing waterway-crossing connector. 3 new tests
  (`TestNavmeshCarveReconnect`).
- **Phase B** ✅ — wire the same mechanism into the skeleton carve path
  (`_axis_dedup_suppression_mask`'s caller in `build_skeleton_network`), which today has
  no connection to this machinery at all. Shipped: reuses the existing `occurrences==1`
  degree-1 tracking (unchanged), a new pixel-radius adjacency check
  (`AXIS_DEDUP_DEADEND_SEARCH_RADIUS_PX = 4`, corrected up from an assumed ~1px — see
  §6.3.1's correction). 5 new tests (`TestSkeletonCarveReconnect`), including one
  documenting the degree-2/3 junction-knot limitation found during implementation.
- **Phase C** ✅ — the raster-padding fix (§6.3.2), independent of A/B, landed first
  (smallest, fully isolated change, verified alone before A/B built on top of it).
- **Phase D** — re-test `<=` (§6.3.3, first bullet), only once A–C are verified clean on
  `<` against a real rebuild (unit tests alone can't exercise this — see §6.3.4). Not
  started.
- **Phase E** — test removing `_lock_protection_mask` (§6.3.3, second bullet), only once
  D is settled (removing a safety net while also changing the tie-break at the same time
  would make a regression, if one appeared, ambiguous to attribute). Not started.

#### 6.3.6 Follow-up (independent code review): per-line reconnect cap — FIXED

An independent review of Phases A–C, run specifically to check whether this section's
own reconnect mechanism could be contributing to §6.4's connector fan-out ("hub node")
problem, found that neither carve-reconnect call site had an equivalent to
`_inject_waterway_crossings`'s own `WATERWAY_CROSSING_CAP_PER_LINE = 8` sanity cap: a
carve boundary running the length of one axis line (exactly the geometry axis-dedup
produces) could attribute an unbounded number of perimeter/dead-end nodes to that same
`line_iloc` within one piece, each getting its own connector edge. §6.4's own measured
A/B (226→218 hubs, same max out-degree of 222 — see that section) shows this section's
21,832 added `_connect_waterway_crossing` calls did not, in practice, worsen fan-out on
the Zeeland pilot dataset, but the missing cap was a real, unbounded structural gap
independent of that specific measurement, and the exact mechanism §6.4 root-causes the
existing fan-out to.

Fix: extracted the capping logic both `_inject_waterway_crossings` (pre-existing) and
this section's two call sites need into one shared method,
`_cap_reconnect_candidates_per_line` (reused, not duplicated, at both the navmesh and
skeleton call sites — also resolves the review's duplicated-aggregation-block
observation), applying the same `WATERWAY_CROSSING_CAP_PER_LINE` cap per (piece, line)
to carve-reconnect candidates before connecting. 5 new tests
(`TestReconnectCandidatesAreCappedPerLine`), plus the skeleton call site's own
candidate collection was restructured into the same two-phase
(collect-then-cap-then-connect) shape the navmesh call site already used, so both
sites are now structurally identical, not just cap-equivalent. Full suite (148 tests)
verified green after the change.

#### 6.3.7 Follow-up (CodeRabbit, PR #16): carve attribution attrition through boundary
simplification — MEASURED, not yet fixed

CodeRabbit's review of PR #16 flagged that `build_navmesh_region`'s pre-existing
boundary-simplify pass (`NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0`, §6.3.1's own docstring
already notes it runs "after the caller already computed seam_coord_set... because
simplify() only ever removes vertices") runs on `poly_m` BEFORE the per-vertex match
against `carve_line_iloc_by_coord` — so any carve-attributed vertex Douglas-Peucker
removes is silently unavailable for reconnect at that node.

**Measured, not assumed**, against two synthetic fixtures (a 2km piece carved by a
through-line, and a stub-scale ~200m piece matching the real Hansweert stub's own
magnitude): simplify removes **85–94% of carve-attributed vertices** (46→3 and 20→3
survivors per fragment respectively). This is real, substantial attrition — CodeRabbit's
concern is confirmed, not a false positive.

**But not (in either tested case) total failure.** In both fixtures, exactly 3 vertices
survive per fragment regardless of scale — structurally, these are the carve boundary's
own genuine corners (where Douglas-Peucker's own guarantee — never drop a point whose
removal would deviate the simplified line beyond tolerance — keeps it), which is also
where a fragment's shape actually changes, i.e. where a dead end is most likely to sit
in the first place. Neither test produced zero survivors.

**Why this isn't a regression and isn't (yet) blocking**: the simplify-then-exact-match
tradeoff is inherited from the PRE-EXISTING `seam_coord_set`/`boundary_node_ids`
mechanism this section's own `carve_seam_coords` reuses verbatim (same docstring, same
known limitation, unmodified by this PR — see §6.3.1). A carve vertex whose match is
lost to simplify simply gets the SAME treatment every dead end got before this PR
existed: left to the generic stitching passes. This is the same class of "coverage gap,
not a safety gap" this spec section already accepts elsewhere (§6.3.1's degree-2/3 knot
limitation, §6.3.2's original padding gap) — not proven to ever reach zero, but not
proven never to, either, for a fragment shaped differently from the two tested here.

**Proper fix (deferred, matches CodeRabbit's own "Heavy lift" tag)**: derive carve
attribution from the FINALIZED (post-simplify) boundary instead of relying on exact
pre-simplify vertex matches — e.g. nearest-line lookup per surviving vertex (mirroring
the skeleton path's own `_axis_dedup_nearest_line_for_suppressed_pixel` radius search,
rather than the navmesh path's current exact-coordinate dict lookup). Out of scope for
this PR; track as a follow-on phase once a real rebuild's own reachability gates (§6.3.4)
show whether the surviving-corner behavior measured here is sufficient in practice.

### 6.4 Sparse `inland_waterways` digitization causes connector fan-out ("hub" nodes) — IMPLEMENTED

**The symptom.** Rendered over OSM, the Zeeland graph shows large fan/star-shaped bursts
of edges converging on single points, scattered across the whole province — not a local
artifact. Found while sanity-checking §6.3's rebuild: the user's own words, correcting an
initial (wrong) assumption that it was localized to one harbor entrance: *"I actually
don't believe your explanation wrt this being an artifact of just the harbour entrance as
I see it all over the map."* That correction was right — measured, not assumed, below.

**Measurements.** A node's out-degree in a well-formed skeleton/navmesh graph should be
2-4 (chain, or a real junction); anything much higher is a fan converging on one point.
Counted directly against `zeeland.sqlite` snapshots taken at different points in this
project's history:

| snapshot | hub nodes (out-degree > 30) | max out-degree |
|---|---|---|
| `zeeland_pre_round23.sqlite.bak` (older baseline) | 0 | — |
| `zeeland_pre_sagitta.sqlite.disabled` | 6 | 36 |
| `zeeland_pre_rawenc.sqlite.disabled` (before a later raw-ENC source-data switch) | 32 | 63 |
| `zeeland_pre_navmesh_dedup.sqlite.disabled` (axis-dedup skeleton-only, before the §7 navmesh extension) | 7 | 47 |
| live `zeeland.sqlite` (current, post-raw-ENC-switch, pre-§6.3) | 226 | 222 |

The 226 hubs are not clustered — grouping by 0.1°×0.1° cell, they span 23 distinct cells
covering essentially the full extent (lon 3.3-4.4°E, lat 51.4-51.8°N), confirming the
user's "all over the map" directly. For those 226 hub nodes' own edges: 16,598 are
`edge_kind_id=1` (`navmesh_boundary` — i.e. waterway-connector edges) against only 101
ordinary skeleton centerline edges. The single worst hub (`51.4335N, 3.4740E`,
out-degree 222) has `source_id=14` (`inland_waterways`) and every one of its 220
`coastal`-typed edges lands on a distinct neighbor within 500m — a textbook many-to-one
connector collapse, not a real 222-way junction.

**Root cause, traced to a specific source feature.** `_connect_waterway_crossing`
(reused by both the pre-existing Round-14 `_inject_waterway_crossings` mechanism and by
§6.3's reconnect) finds the *nearest existing vertex* on the target `inland_waterways`
line and connects to that — deliberately, per its own design (no edge-splitting, no new
mid-line node; see §6.3's own reuse of it). `_build_inland_network` is the same story on
the ingestion side: it walks `coords[i] -> coords[i+1]` for each line with no length cap,
so the line's own graph edges are exactly as sparse as its source vertices, however far
apart. Both are fine assumptions for a densely-digitized line (a vertex every few
metres); both break down for a sparsely-digitized one, where many genuinely different
crossing/reconnect points within `WATERWAY_CONNECTOR_MAX_M`/`_FALLBACK_MAX_M` (250m/500m)
of each other all resolve to the *same* one nearest vertex.

Confirmed directly against the worst hub's own source feature: `inland_waterways_lines
.geojson` index 2008, `OBJNAM="Geul van de Walvischstaart"` — an S-57/ENC-style feature
(`RCID`, `OBJL=17051`, `SCAMIN=50000`, i.e. digitized for legibility starting at 1:50,000
chart scale, not survey-grade density) — has **7 vertices over 8.8km**, with individual
segments up to **1,870m** long. Every crossing or carve-boundary point within roughly a
kilometre of that segment's midpoint has no better choice than that one distant vertex.

**This predates axis-dedup/§6.3 and is not meaningfully changed by it.** The snapshot
table above already shows the hub count climbing well before §6.3 existed (0 → 6 → 32),
tracking a raw-ENC source-data switch, not axis-dedup. Confirmed directly with a clean
A/B pair (identical input, identical flags, only `git` state differs): a control build at
axis-dedup's own merge point (no §6.3) shows 226 hubs/max 222; a build from §6.3's own
tip shows 218 hubs/max 222 — slightly *fewer*, not more, despite §6.3 adding 21,832 of
its own `_connect_waterway_crossing` calls. (That's explained in §6.3's own verification:
§6.3's reconnects mostly substitute for what the generic stitching passes used to do less
precisely, rather than adding fan-out on top of it — see §6.3.4/PR discussion.) So this is
a real, independent, pre-existing gap, not a regression introduced by §6.3, and not
something §6.3's own scope can fix — its call site controls *when* to call
`_connect_waterway_crossing`, not the sparse-vertex data the function snaps onto.

**Design options considered**

1. **Cap connectors per inland vertex.** Track a global (not per-piece, unlike the
   existing `WATERWAY_CROSSING_CAP_PER_LINE = 8`, which only caps crossings within one
   *(navmesh piece, line)* pair) counter and reject once a vertex is saturated.
   Rejected: doesn't fix the underlying imprecision (a vessel would still logically route
   to a point up to ~1km from where it actually needs to cross), and a hard reject where
   today's fan-out at least connects *something* would be a **new**, silent connectivity
   loss for whichever crossings lose the cap race — trading one problem for a worse one.
2. **True edge-splitting at connect time** — insert a genuinely new vertex into the
   `inland_waterways` line/graph at the point nearest each crossing, splitting the
   existing edge, mirroring what `_inject_waterway_crossings` already does on the
   *navmesh* side of the same connection. Most geometrically accurate. Rejected for now:
   real added complexity (order-dependent splits as multiple crossings target the same
   original edge, bookkeeping to keep `line_m_cache`/`inland_gdf` positional indices
   consistent afterward) for a fix option (3) gets far more simply.
3. **Densify sparse `inland_waterways` lines once, before anything reads them —
   CHOSEN.** Insert interpolated vertices along any segment exceeding a threshold
   (`shapely.segmentize(geom, max_segment_length)`, available since Shapely 2.0,
   confirmed present in this project's venv at 2.1.2) so no segment exceeds it.
   `_connect_waterway_crossing`'s own contract (snap to nearest *existing* vertex) stays
   exactly as simple as it is today — densifying just makes that nearest-existing-vertex
   assumption true again. Also directly fixes `_build_inland_network`'s own sparse-edge
   problem (previously, "Geul van de Walvischstaart" was 6 graph edges up to 1,870m each
   with zero intermediate routing nodes — a real routing-fidelity gap on its own,
   independent of the connector fan-out).

#### 6.4.1 Implementation

`_densify_inland_waterways` runs once in `parse_shapefiles`, immediately after
`inland_waterways` loads and before `_build_fairways_unified()`/`gdfs_metric` are built,
so every downstream consumer (`_build_inland_network`, `_connect_waterway_crossing`,
`_axis_dedup_suppression_mask`'s own candidate rasterization) sees the same
already-dense geometry with zero changes to any of them:

1. Reproject `inland_waterways` to `CRS_METRIC` (EPSG:3857, this project's existing
   metric-CRS convention for `gdfs_metric`).
2. `shapely.segmentize(geoms, inland_densify_max_segment_m)` — vectorized over the whole
   layer.
3. Reproject back to WGS84 and replace `self.gdfs["inland_waterways"]`.

Gated behind `--inland-densify-max-segment-m`, default `0.0` = off, matching
`--sagitta-cap`/`--axis-dedup-cap`'s convention — gate 1 (`--inland-densify-max-segment-m
0` reproduces today's build byte-for-byte) holds by construction: the method returns its
input GeoDataFrame unchanged (same object) whenever the cap is `<= 0.0` or the layer is
empty. `ClassificationConfig.inland_densify_max_segment_m` carries the value through, the
same pattern `max_chord_sagitta_m`/`axis_dedup_cap_m` already use.

Unit-tested in `tests/test_inland_densify.py`: disabled is a no-op (same object
identity); enabled inserts intermediate vertices and no output segment exceeds the cap
(within CRS round-trip slack); endpoints and total length are preserved (`segmentize`
only inserts vertices along existing segments — it never moves an original vertex,
including the two endpoints); an empty layer and a stray non-`LineString` geometry don't
raise; and an end-to-end check that `_build_inland_network` gains intermediate routing
nodes on a synthetic sparse line once densified, versus zero when disabled.

**Verify enabled, against a real build**: (a) the hub-node scan above drops toward 0 (not
just "fewer"), (b) `_build_inland_network`'s own edge-length distribution loses its long
tail, (c) the usual connectivity-by-edge-length and POI-pair reachability gates
(§6.1/§6.3.4) show zero loss. A reasonable threshold is comfortably under
`WATERWAY_CONNECTOR_MAX_M` (250m) — e.g. 100-150m, bounding worst-case nearest-vertex
error to half that — but should be tuned against the measured segment-length
distribution across the full `inland_waterways` layer, not just the one motivating
feature, before picking a value for a shipped build.

#### 6.4.2 Verified with a real A/B rebuild — the historical 226-hub number no longer
reproduces, but the mechanism and fix are both confirmed independently

**The live `zeeland.sqlite`'s 226 hubs (max degree 222) predate this session's fix for
invalid source geometry** (`docs/`'s own PR #15, merged the day *after* that database was
built — `_connected_water_polygons` now repairs invalid `coastal_water` polygons before
the union instead of letting GEOS's `TopologyException` corrupt/skip them). Rebuilding the
same Rijkswaterstaat source data at the same coverage bbox (`3.13334,51.21038,4.65,51.95`)
against current `main` (which already includes that fix, plus §6.3's reconnect) reproduces
the exact same worst-hub node id at (3.474, 51.434) — but at out-degree **2**, not 222: the
geometry repair alone appears to have already resolved most of the historical hub count as
a side effect, independent of this section's own fix.

**So today's real baseline has far fewer hubs (5, all out-degree ≤ 33) than the historical
measurement — and controlled experiment confirms those 5 are a *different* phenomenon**,
not this section's: their inland-side crossing targets are already densely spaced (a
same-line vertex ~35m away), so `--inland-densify-max-segment-m` correctly leaves them
untouched (identical edge counts, both directions, with the flag on vs off). Right call by
construction (nothing to densify there), just not the same bug.

**The mechanism this section targets is still real and still measurably fixed**, at a
smaller scale that matches today's already-partially-repaired baseline: of the 34
inland-vertex nodes with ≥5 `navmesh_boundary` (edge_kind_id=1) out-edges in the control
build, **31 dropped and only 1 rose** once `--inland-densify-max-segment-m 120` was
enabled, summed degree across those 34 nodes falling **206 → 95** (54%). The starting
component count going into the coastal-connectivity stitching pass was identical in both
builds (626), and gap-resolve success was essentially unchanged (238 → 243) — no
connectivity lost, matching gate (c).

#### 6.4.3 Follow-up (CodeRabbit, PR #17): bound the cap before it reaches `shapely.segmentize` — FIXED

The original guard was `if cap_m <= 0.0: return inland_gdf`, matching `--sagitta-cap`/
`--axis-dedup-cap`'s own (equally unguarded) disabled-check convention. CodeRabbit
correctly flagged that this is not enough for this specific flag: `NaN` compares `False`
against everything in Python, including `<= 0.0`, so it silently reaches
`shapely.segmentize` and violates that function's own positive-finite contract; and
unlike the suppression-tolerance/resampling caps elsewhere in this spec, a valid-but-tiny
positive cap (a stray extra zero, or metres/km confusion) has no ceiling of its own --
`shapely.segmentize` generates roughly `segment_length_m / cap_m` vertices per source
segment, which on a real multi-kilometre `inland_waterways` line risks unbounded memory
rather than a clear, fast error.

Fixed by rejecting (not silently clamping or silently disabling, either of which would
mask the mistake) any cap that is non-finite or below `INLAND_DENSIFY_MIN_SEGMENT_M`
(1.0m) once the plain `<= 0.0`/empty-layer disabled-check has passed. Regression-tested
in `tests/test_inland_densify.py` (`TestDensifyRejectsUnsafeCaps`): NaN, `+inf`, and a
`1e-9` cap all raise `ValueError` instead of reaching `shapely.segmentize`; the floor
value itself (1.0m) is still accepted; a negative cap keeps taking the pre-existing
"disabled" path unchanged, since it never reaches the new check.

### 6.5 The net effect: five rounds of locally-valid fixes compounded into a regression — connector merge/split — IMPLEMENTED

**Symptom.** A rendered screenshot at a bridge crossing over a narrow canal (Postbrug,
Yerseke) showed dense clusters of nodes still present after §4.1, §4.3, §6.3, and §6.4
all shipped — including nodes less than 5m apart with no depth difference to explain
it, and nodes right on the fairway line that should have been suppressed by axis-dedup.

**Measurement.** Every prior round in this section verified itself only against its own
immediate predecessor build, never against this spec's own original baseline. Traced
via the `.bak`/`.disabled` snapshots each round leaves behind before shipping the next:

| stage | nodes | edges |
|---|---|---|
| original baseline (§1) | 48,553 | 137,718 |
| + §4.1 sagitta resampling | 32,918 | 98,091 |
| + §4.3 axis-dedup + §6.3 carve-reconnect | 50,432 | 175,743 |
| + §6.4 inland-waterways densify | **64,717** | **203,582** |

§4.1 was a real win (48,553 → 32,918). Everything shipped after it added nodes back
faster than it removed them: the deployed database ends 33% above the original node
count and 48% above the original edge count — worse than doing nothing.

**Root cause, traced to a specific shared mechanism.** Two independent call sites each
always mint a brand-new node instead of first checking whether the pipeline already has
something equivalent nearby:

1. `_connect_waterway_crossing` always snaps to the *nearest existing vertex* of the
   target `inland_waterways` line, never the true point of contact. §6.3's
   carve-reconnect calls it for every carve-induced dead end — which, by construction,
   sits within a few metres to tens of metres of the very axis line responsible for
   carving it — and gets a permanent new node + stub edge instead of merging straight
   into that axis. §6.4's fan-out fix (densify the entire ~3,700km network to
   100-150m spacing so a nearby vertex always exists) is the same "pay the cost
   everywhere" anti-pattern §4.1 already replaced once, one section later.
2. `_add_opening_bridge_edges` runs *after* `build_network()` (all of axis-dedup
   carving and §6.3's reconnect included) and is completely unaware of any of it: for
   every movable bridge it unconditionally mints a new node at each
   fairway/inland-waterways intersection with the bridge polygon, sited essentially on
   the axis by construction, then always wires up to 4 more edges outward. A bridge
   where both a fairway line and an inland-waterways line intersect produces two
   near-coincident opening nodes, each independently fanning out its own edges — the
   fairway-adjacent clusters the screenshot showed were never in axis-dedup's path at
   all.

**Design options considered.** Following §6.4.1's own precedent for choosing between
raster-time carving and post-hoc pruning: fixing the *symptom* (mint fewer nodes near
existing ones) by adding yet another compensating pass was rejected outright — that is
exactly the pattern that produced this regression across five rounds. The fix has to
replace the root mechanism (always-mint) at both call sites, not add a sixth
compensating pass on top.

**Chosen approach.** One flag, `connector_merge_m` (`--connector-merge-m`, default
0.0/disabled, matching this spec's established convention), gating two changes:

- `_connect_waterway_crossing`/new `_get_or_split_inland_segment`: project the
  crossing/dead-end point onto the target line; reuse an existing vertex (or a
  previously-inserted split point) within tolerance, or split that segment's current
  graph edge to insert exactly one new vertex at the true point of contact. Live
  per-segment split state is tracked pipeline-wide in `self._inland_split_cuts`
  (reset once per `build_network()`, unlike the call-scoped `line_m_cache`), so a
  second candidate landing on a segment a first candidate already split — from any of
  the three call sites, across any piece, in any order — always sees the current
  sub-segment structure rather than stale original geometry. This is the true
  edge-splitting option §6.4.1 named and deferred for its own fan-out fix; doing it
  here, driven by real contact points rather than a flat network-wide cap, is expected
  to make §6.4's blanket densify unnecessary as a follow-up (not part of this change).
- `_add_opening_bridge_edges`: dedupe near-coincident `opening_pts` before planting
  any node, and search for an existing nearby node before minting a new one. A reused
  node may already carry real data, so the merge never blindly re-stamps it:
  `node_depth` only ever tightens (never relaxes a more restrictive existing
  constraint with the bridge's permissive 99.0 sentinel), and `node_kind_id`/`source`
  are left alone if the node is already typed.

Recommended tolerance once enabled: 5.0m — comfortably above `_get_or_create_node`'s
~1.1m coordinate-rounding grain, matching `axis_dedup_floor_m`'s own established 5m
"effectively coincident" floor, and two orders of magnitude below
`WATERWAY_CONNECTOR_MAX_M` (250m).

**Verification.** Same five-gate discipline as §4.3.3/§6.3.4: `connector_merge_m ==
0.0` reproduces prior output byte-for-byte (194/194 unit tests pass unchanged);
`crosses_land == 0`; connectivity measured by edge length, not node count, against
`data/zeeland_clip`; POI-pair reachability (zero pairs lost, Krammersluizen checked
explicitly given its history in this area); and — the specific gate the prior five
rounds skipped — node/edge counts measured against the *original* baseline
(48,553/137,718), not just the immediately-prior build. Also: a connector-edge-length
spot check, and a re-run of §6.4's own hub-node scan (`out-degree > 30`) with
`--connector-merge-m 5.0` and `--inland-densify-max-segment-m 0.0` to confirm splitting
alone fixes the original hub-fanout problem without needing §6.4's blanket densify —
the evidence needed before recommending §6.4 be defaulted back off.

Covered by `tests/test_waterway_connector_merge.py` and extensions to
`tests/test_axis_dedup.py`'s `TestNavmeshCarveReconnect`/`TestSkeletonCarveReconnect`.

### 6.6 Pass 2's connectivity guarantee has no per-node fan-in cap — IMPLEMENTED

**Symptom.** Verifying §6.5 with real Zeeland rebuilds (`data/BUILD_LOG.md` #2-#5)
found hub nodes (out-degree > 30) persisting at 56-231 regardless of
`connector_merge_m` or `axis_dedup_cap` — nowhere near the live database's 5. Traced
to `_ensure_coastal_connectivity`/`_stitch_component_pieces`, entirely separate from
what §6.5 touches.

**Root cause.** `_stitch_component_pieces` runs several stitching passes. Pass 0c
(navmesh perimeter) and Pass 0d (inland nodes) each explicitly cap cross-type fan-in
per node (`MAX_CROSS_CONNECTORS_PER_NAVMESH_NODE` / `MAX_LOCAL_CONNECTORS_PER_INLAND_
NODE`, both 2) — a deliberate, documented guard against exactly this class of problem.
**Pass 2** (the "connectivity guarantee, one merge round at a time" pass, run last,
up to 30 rounds) has no such cap: each round it finds every still-disconnected
group's geometrically nearest cross-group candidate and merges via union-find, with
no limit on how many different groups can pick the *same* node as their nearest
candidate. Measured directly: as few as ~58 Pass 2 successes on one real build
produced out-degree up to 42 on a single node.

**Design options considered.** Capping fan-in risks stranding a group whose only
nearby candidates are all capped-out — unlike Pass 0c/0d (which have a distance
radius and can simply give up on a specific connector, leaving the broader guarantee
to Pass 2), Pass 2 *is* the guarantee, so silently refusing a candidate needs a safe
fallback, not a dropped connection.

**Chosen approach.** `pass2_max_fanin_per_node` (`--pass2-max-fanin-per-node`,
default 0/disabled, matching this spec's established convention). When a candidate's
source or target node has already accumulated this many *Pass-2-added* edges (not
counting pre-existing structural degree — a node's ordinary ring/chain topology
never itself triggers the cap), Pass 2 skips it and tries the next-nearest candidate
instead — mirroring how it already skips a poly/land-rejected candidate. A group that
only ever discovers capped-out candidates falls through to
`_resolve_local_skeleton_gaps`, which already runs immediately after Pass 2 as the
existing fallback for whatever Pass 2 can't merge — no new fallback mechanism needed.
Applied symmetrically to both Pass 2 code paths (the geometric escalating-k search
active when `--sagitta-cap` is set, and the legacy per-group-sample path used at
`--sagitta-cap 0`), gated purely on `pass2_max_fanin_per_node > 0` so it composes
with either.

**Verification.** Synthetic hub-and-spokes test (`tests/test_pass2_fanin_cap.py`):
one hub node equidistant from N spoke nodes (each spoke's true nearest cross-group
candidate is unambiguously the hub), `snap_radius_m` set below every pairwise
distance so Pass 0/Pass 1 (both distance-gated) contribute nothing, isolating Pass 2
as the only mechanism under test. Confirms: cap=0 lets the hub accumulate a connector
to every spoke (documented pre-existing behaviour); cap=N bounds the hub's
Pass-2-added out-degree at N while every spoke still ends up in the same connected
component (via a different, uncapped node) rather than being stranded; a larger cap
allows proportionally more fan-in. Full real-build verification (five-gate
discipline, against `data/BUILD_LOG.md`'s baseline) pending.

## 7. Risks

- Long edges on straight reaches increase the chance a single edge spans a chart feature
  (a shoal, a dredged-channel boundary) that the 5-point sampler steps over. Mitigate by
  keeping the `max_segment_m` backstop and, if measured necessary, scaling sample count
  with edge length rather than fixing it at 5.
- §4.3 changes which geometry wins near fairways, so it interacts with the DRGARE depth
  override in `SPEC-FAIRWAY-HARMONIZATION.md` §3 — the axis carries no `DRVAL1`, so
  dropping a generated twin must not drop the depth attribution that came with it.
- Node ids are coordinate-derived (`_coord_to_id`), so any resampling change moves ids
  wholesale. Cross-database seam stitching matches on coordinates, so registry-backed
  builds must be rebuilt together, not mixed across the change.
- §6.4's densified `inland_waterways` vertices also feed §4.3's axis-dedup carve
  rasterization and §6.3's carve-reconnect (both read `self.gdfs["inland_waterways"]`
  after `parse_shapefiles`), so enabling `--inland-densify-max-segment-m` alongside
  `--axis-dedup-cap` changes the candidate-line rasterization at finer resolution than
  before. Not expected to change carve *decisions* (segmentize doesn't move the line,
  only adds vertices along it), but re-run §6.3.4's gates when enabling both together
  rather than assuming independence.
