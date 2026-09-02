# Spec: Graph Node Density — Over-Sampling and Fairway Duplication

Status: §4.1 implemented and verified. §4.1.2, §4.1.3, §5.1 and §6.1 fixed. Not enabled by default.
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
