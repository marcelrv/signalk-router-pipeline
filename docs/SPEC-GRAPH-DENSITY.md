# Spec: Graph Node Density — Over-Sampling and Fairway Duplication

Status: §4.1 implemented and verified. §4.1.2, §4.1.3, §5.1 and §6.1 fixed. §6.3, §6.4,
§6.5, §6.6, and §6.7 implemented. None enabled by default. §6.5 is the fix for the net
density regression §6.3+§6.4 compounded; §6.6 (Pass 2 fan-in) and §6.7 (Pass 0c/0d
Direction-A target fan-in) are two independent fixes for residual hub-fanout §6.5
alone did not resolve — §6.7 is the one a real build confirmed as the actual dominant
cause (§6.6's own real-build verification found Pass 2 was NOT it). §8 (implemented,
real-build verification COMPLETE as of §8.6) covers two further, independent
mechanisms found while investigating a US East Coast (Potomac River) screenshot
showing a dense "bowtie" tangle in water the user identified as genuinely deep and
open: Pass 0 (the very first stitching pass) has no fan-in cap at all, unlike every
other pass in this family, and `_split_wide_narrow` has no size/isolation-aware
fold-back for scattered narrow slivers, unlike its siblings
`_split_deep_shallow`/`_tile_navmesh_piece`.
**§8.6: that real-build verification found neither §8.2 nor §8.3 actually fixes the
Potomac/Coltons Point case that motivated them** — that location's density is a
different mechanism, root-caused and fixed in §9: `build_skeleton_network` never
simplifies a water polygon's boundary before rasterizing/skeletonizing it, so fine
ENC/chart digitization noise (one real connected water body measured at 494,363
vertices) spawns spurious medial-axis junctions. §9 (implemented, real-build
verification COMPLETE — `data/BUILD_LOG.md` #34) fixes this via
`skeleton_boundary_simplify_m`, validated first against real geometry (piece-level,
17-35% node reduction) and then against a full region rebuild (72.7% boundary-vertex
reduction, 10.4%/10.9% node/edge reduction in the reported area, `crosses_land=0`).
See §10.1 for the caveat that a second, different location nearby is NOT fixed by
this mechanism. See `data/BUILD_LOG.md` for every real build's measured effect
before assuming any of these should ship enabled by default.
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

### 6.7 Pass 0c/0d's fan-in cap only covers one direction — IMPLEMENTED

**Symptom.** §6.6's `pass2_max_fanin_per_node`, verified on a real Zeeland build
(`data/BUILD_LOG.md` #6), did not reduce the hub count at all — 56 hubs before and
after, `fanin_capped` never firing once. The cap was correctly implemented but aimed
at the wrong mechanism.

**Root cause, this time confirmed empirically, not inferred.** Queried the actual
highest-degree node in the build (#6): 42 edges, every one 44-93m long, 40 of the 42
neighbors navmesh-perimeter (`node_kind_id=navmesh_vertex`) nodes. Pass 2 has no
distance cap — finding a valid connector "even when the two nearest groups are
genuinely far apart" is its entire purpose — so a hub built entirely of short edges
cannot be Pass 2's doing. It's Pass 0c. Pass 0c's Direction A ("each navmesh vertex
-> its k nearest cross-type neighbours") caps how many connectors *that vertex's own
search* may add, keyed by the navmesh vertex. Direction B (the symmetric reverse
query, "each non-navmesh node -> its k nearest navmesh vertices") caps a navmesh node
as a *target*. But Direction A's own target side — the non-navmesh point being picked
— has no cap at all: many different navmesh vertices, each individually within its
own 2-connector budget, can all independently pick the *same* nearby point. Pass 0d
(inland nodes) has the identical asymmetry in its own Direction A.

**Chosen approach.** `pass0_target_fanin_cap` (`--pass0-target-fanin-cap`, default
0/disabled, matching this spec's convention). A single shared counter, keyed by node
id, spans Direction A *and* Direction B of *both* Pass 0c and Pass 0d — a node
targeted by more than one of these four code paths shares one real budget rather than
getting a fresh allotment from each. When a node has already accumulated this many
Direction-A-or-B-sourced connectors, Direction A skips it as a target and tries the
next-nearest candidate instead — the identical pattern every other cap in this file
already uses for a poly/land-rejected or radius-rejected candidate. Safe regardless
of `pass2_max_fanin_per_node`: Pass 1/Pass 2 run afterward and remain the
connectivity guarantee no matter how tightly Pass 0c/0d are capped.

**Verification.** Synthetic test (`tests/test_pass0_target_fanin_cap.py`): one target
node plus N navmesh-kind nodes wired into a ring (mirroring a real navmesh perimeter
piece — deliberately *not* N unconnected singletons, since plain Pass 0, which is
union-find gated and runs first, would otherwise connect the target to every source
before Pass 0c gets a chance to contribute anything at all; a pre-connected ring
reproduces Pass 0c's own documented motivating scenario, where the ring is already
one union-find group and Pass 0/0b/1 only ever add a single connection to it before
treating it as "already connected"). Confirms: cap=0 lets the target accumulate every
ring member's connector; cap=N bounds Pass 0c's own contribution at N
(`_stitch_diag["pass0c"]["success"] <= N`, `target_fanin_capped` firing) while the
graph stays fully connected regardless. Real-build five-gate verification (does hub
count actually drop against `data/BUILD_LOG.md`'s baseline) pending.

### 6.8 `_get_or_create_node`'s ~1.1m rounding grain leaves near-duplicate nodes at multi-subsystem junctions

**Symptom.** A rendered screenshot at the Krammersluis Noord/Zuid lock junction, with
`--connector-merge-m 5.0` and every other §6.3-§6.7 fix already applied (live db,
0 hubs by the `out-degree > 30` metric), still showed a dense crisscross/triangulated
web of nodes and edges instead of a clean route through the junction. This is a
*different* mechanism from anything §6.3-§6.7 measured: none of the offending nodes
are hubs. Queried directly against the live build: 165 nodes packed into roughly a
0.5km² area around the junction, but the highest out-degree among them is 6 — the
`>30` hub metric every prior round in this section optimized for never fires here at
all.

**Root cause.** `_get_or_create_node` (nautical_routing_pipeline.py:2334) dedupes
nodes purely by `(round(lon, 5), round(lat, 5))` — a grid roughly **1.1m** wide at
Zeeland's latitude. Independent node-creation call sites — skeleton medial-axis
extraction, navmesh boundary generation, `_add_lock_crossing_edges`,
`_add_opening_bridge_edges`, gap-resolve, the coastal-connectivity stitch passes —
each computing "the same" real-world junction point via a different geometric path
(a different source layer, a different projection round-trip, a different sampling
resolution) routinely land a metre or two apart: close enough to be the same point in
every practical sense, but on opposite sides of that 1.1m rounding boundary, so they
become two permanently distinct nodes joined by a near-zero-length stub edge. Each
stub then fans out to its own real neighbors independently, which is exactly the
crisscrossing/triangulated look the screenshot showed instead of one clean line
through the shared junction. Confirmed directly against the live build:

```
source            target            dist_m   edge_kind
1157982930416234  509982930416234   1.11     navmesh_boundary   (51.66192,4.16234) vs (51.66193,4.16234)
509981058415791   509981058415793   1.38     centerline
509981238415979   509981238415982   2.08     centerline
```

`connector_merge_m` (§6.5) already solved exactly this class of problem — project
onto the target and reuse/split within tolerance instead of always minting a new
node — but only at the two call sites §6.5 identified
(`_connect_waterway_crossing`/`_get_or_split_inland_segment` and
`_add_opening_bridge_edges`'s own dedupe). Every other node-creation call site still
only has `_get_or_create_node`'s bare ~1.1m rounding grain, with no tolerance.

**Scope, measured** (live build, post fairway-buffer fix, `data/BUILD_LOG.md` #8):
1,047 edges under 3m and 369 under 1.5m out of 187,551 total (~0.5-0.6%) — small in
aggregate, but concentrated exactly at multi-subsystem junctions (locks, bridges,
piece-stitch boundaries), which is where a user is most likely to zoom in and notice
it.

**Design options**, following §6.5's own precedent (fix the mechanism, not add a
sixth compensating pass on top of five prior ones):

1. Generalize §6.5's spatial-tolerance-merge pattern to `_get_or_create_node` itself
   — every node-creation call site reuses (or, where a real edge already spans the
   gap, splits) an existing node within tolerance, the same principle §6.5 already
   validated for its two call sites, applied universally.
2. A dedicated post-pass late in `build_network()` (or immediately before
   `calculate_edge_attributes`) that union-find merges any two nodes within a small
   tolerance and collapses the resulting degenerate near-zero-length edges. Lower
   risk / more localized than touching the universal node-creation path, but it is
   exactly the "symptom fix via a compensating pass" pattern §6.5 rejected outright
   after five rounds of it compounding into a regression — listed for completeness,
   not as the recommended direction.

**Recommended**: option 1, gated behind a new tolerance flag (e.g. `--node-merge-m`,
default 0.0/disabled) matching this spec's established convention so a `0.0` build
stays byte-identical to today.

**Status: IMPLEMENTED.**

#### 6.8.1 Implementation

Option 1 (the recommendation above), gated behind `--node-merge-m` (default `0.0`,
disabled — a `0.0` build stays byte-identical to today's exact-rounding dedup,
matching every other flag in this section's convention). `_get_or_create_node` first
checks a new grid-bucket spatial index (`_node_merge_grid`, populated by
`_register_node_in_merge_grid`, queried by `_find_nearby_node`) for an existing node
within `node_merge_m` metres before falling back to the exact `(round(lon,5),
round(lat,5))` dedup; on a hit it reuses that node instead of minting a near-
duplicate. Unlike `connector_merge_m`'s two call sites (each merging against one
specific `inland_waterways` line, where "split the segment" is meaningful),
`_get_or_create_node` is called for arbitrary standalone points from ~15 call sites
with no line to project onto — so this is pure point-level spatial dedup ("is there
already a node within tolerance? reuse it, else create one"), not a split. A grid
bucket hash (not a KD-tree) was used because insertions are interleaved with lookups
throughout the whole build, making a periodically-rebuilt tree impractical; the cell
sizing reuses this file's existing 111320 m/deg / `cos(lat)` metre-to-degree
approximation (see `_lonlat_margin_deg`). Bounded by a new, deliberately tight
ceiling `NODE_MERGE_MAX_M = 20.0` (vs. `connector_merge_m`'s 250m
`WATERWAY_CONNECTOR_MAX_M`) since this governs every node in the graph, not one
line's own search radius — a generous value would risk collapsing genuinely distinct
nearby features (e.g. two adjacent lock chamber approach points), not just the ~1-2m
rounding-grain duplicates it targets. Not segregated by `node_type`: the existing
exact-match `coords_to_node` dict was already not `node_type`-aware, and this fix
preserves that behaviour rather than changing it (out of scope for this bug).
Regression coverage: `tests/test_node_merge.py`.

**Verified live** (`data/BUILD_LOG.md` build #9 vs. build #8, identical
`data/zeeland_fresh_clip` input and flags except `--node-merge-m 5.0`): the
Krammersluis Noord/Zuid junction's sub-3m stub-edge count — the exact symptom this
section traced — goes from 12 to **0**. Network-wide, edges under 3m drop 79% (1,047
→ 221 out of ~186k), with the remaining 221 all also under 1.5m (consistent with
genuine short skeleton segments, not rounding-grain duplicates). Hub count (0) and
`crosses_land` (0) are unaffected in both builds; max out-degree improves slightly
(12 → 11) — confirming the fix removes near-duplicate topology without introducing
new connectivity problems. Not installed live pending explicit deploy confirmation.

### 6.9 Clarification: `axis_dedup_cap_m` is a ceiling on a width-scaled tolerance, and lock polygons are exempted entirely — not a bug

Investigating the same Krammersluis screenshot also raised a real but different
question: with `--axis-dedup-cap 50.0` in effect, why do so many nodes still sit
well inside 50-75m of a genuine `inland_waterways` axis line running through that
junction (`Krammersluis Zuid`/`Krammersluis Noord`/their `Aanloop` approaches, all
present in the source data at exactly this location)? Measured directly against the
live build: of 165 nodes in the junction's bounding box, 79.4% sit within 75m of a
real axis line and 50.9% sit within the build's actual 50m cap.

This is **working as designed**, not a regression, once both mechanisms
`_axis_dedup_suppression_mask` implements are accounted for:

1. **`axis_dedup_cap_m` is a ceiling, not a guaranteed corridor width.** The actual
   per-pixel suppression tolerance is `tol = clip(axis_dedup_fraction * local_channel_
   width, axis_dedup_floor_m, axis_dedup_cap_m)` (§4.3.2) — proportional to the LOCAL
   channel width at that pixel, capped at `axis_dedup_cap_m` and floored at
   `axis_dedup_floor_m` (5m). Confirmed directly: 17 of the 165 junction nodes sit
   5-50m from the axis line (inside the nominal 50m cap) but all measure
   `min_width == 22.0m` — `tol = clip(0.5 * 22, 5, 50) = 11m` there, so a node 15-50m
   out is correctly left unsuppressed by the formula, not despite the 50m cap but
   because the cap was never the binding term at that pixel.
2. **Lock polygons are exempted from suppression entirely, regardless of distance**
   (`_lock_protection_mask`, itself added for a previously-measured Krammersluizen
   regression: axis-dedup once suppressed a node `_add_lock_crossing_edges` needed to
   hook its chamber-transit edges onto, costing 278/10,878 POI-pairs, 100% through
   Krammersluizen). The protection buffer reuses `axis_dedup_cap_m` itself (50m in
   this build). Confirmed directly: 23.6% of the junction's 165 nodes fall inside
   `lock_polygon ∪ 50m` for the two `Krammersluizen` chamber polygons present here.
   A real lock complex commonly clusters 2+ chambers (here: Krammersluis Noord,
   Krammersluis Zuid, plus the separate `Jachtensluis Krammersluizen` yacht lock,
   only the first two of which are `locks_polygons.geojson` features) within a few
   hundred metres of each other, so the union of their protection buffers can cover
   most or all of a junction a user zooms into — not a flaw in the mechanism, but its
   effective footprint is much larger at a multi-chamber junction than at an isolated
   single lock.

Between the two, essentially every node inside the build's actual 50m cap traces to a
legitimate, already-documented reason (the axis line's own ingested vertices, the
lock-protection carve-out, or a locally-narrower width-scaled tolerance); nodes in
the 50-75m band are simply outside this build's configured cap (50m, not 75m — the
75m figure is `--sagitta-cap`, a different flag governing chord-to-centerline
resampling, not axis-dedup's suppression radius) and were never candidates for
suppression in the first place. No follow-up is proposed here; recorded so the
reasoning isn't re-derived from scratch next time this junction's node density comes
up. Whether the lock-protection buffer should shrink when multiple chambers cluster
this tightly is an open tuning question, not a bug, and is left for a future
investigation if the compounded footprint proves to matter in practice.

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

## 8. Pass 0's missing fan-in cap and `_split_wide_narrow`'s missing fold-back (follow-on)

### 8.1 Symptom and root cause

A rendered screenshot of a US East Coast region (Potomac River, near Coltons
Point/St. Clements Island) showed a dense "bowtie"-shaped tangle of hundreds of
crisscrossing straight edges between two node clusters, in a small area the router
was forced to route through — instead of the sparse, regularly-triangulated navmesh
covering the surrounding open water. The user's own diagnosis, confirmed by reading
the code: this local water is genuinely deep and open; all of it could have connected
directly into the surrounding regular mesh instead of generating this structure.

Two independent, previously-undocumented gaps, traced directly in code (not this
spec's earlier Zeeland-measured mechanisms, all of which are about hub fan-in on
Pass 2/Pass 0c/0d, axis-dedup, or connector/node merge — none touch either gap below):

1. **`_split_wide_narrow` (line ~2069) has no size/isolation-aware fold-back.**
   Its erosion-based wide/narrow split has no equivalent of `_split_deep_shallow`'s
   own re-filter or `_tile_navmesh_piece`'s `tile_reclassified` handling: a small,
   isolated sliver that erodes away purely because of small-scale local detail near
   it (a rock, a jetty, a digitization artifact) — not because the water itself is
   narrow — becomes "narrow" and is routed to skeleton treatment even when embedded
   in otherwise wide, deep water.
2. **`_stitch_component_pieces`'s Pass 0 (the very first stitching pass) has no
   fan-in/fan-out cap of any kind.** Confirmed by reading its full body: the
   `pass0_target_fanin_cap` machinery (§6.7) is declared once, shared, but is only
   ever wired into Pass 0c/0d — never Pass 0 itself. Once (1) above (or any other
   mechanism) leaves a water body fragmented into many small disconnected pieces,
   Pass 0's raw, type-blind, uncapped k=6 nearest-neighbor query independently
   discovers and accepts a valid connector for many distinct fragment pairs before
   Pass 0b's outward-biased cross-type matching gets a chance to dominate — producing
   the dense crisscross tangle.

Nothing in Pass 1/Pass 2/`_resolve_local_skeleton_gaps` is touched by either fix
below — they remain the underlying connectivity *guarantee*, exactly as they already
are the fallback for whatever Pass 0c/0d's own existing caps reject.

### 8.2 Fix 1: fold isolated narrow slivers back into the wide/navmesh path — IMPLEMENTED

`_reclassify_scattered_narrow_fragments`, called from `_split_wide_narrow`, gated by
`narrow_fragment_reclass_max_fraction` (`--narrow-fragment-reclass-max-fraction`,
default `0.0` = disabled, matching this spec's established convention). Two-part test
per narrow fragment:

1. **Size**: area below `fraction * pi * min_navmesh_radius_m**2`.
2. **Geometric justification**: naively re-running `_split_wide_narrow`'s own
   erode/dilate/intersect test on `wide` unioned with just the candidate fragment
   **cannot ever recover it** — erosion is monotonic, so eroding any subset of the
   original `cleaned` polygon (which `wide ∪ frag` always is) can only ever recover a
   subset of what eroding the whole of `cleaned` already gave `wide`. That would be a
   silent no-op (caught during implementation, before shipping, by direct
   mathematical check — not assumed). Instead, a **morphological closing**
   (`NARROW_FRAGMENT_RECLASS_CLOSING_M`, 50m, mirroring `_split_deep_shallow`'s own
   established `DEPTH_SPLIT_CLOSING_RADIUS_M` pattern) is applied to `wide ∪ frag`
   before the eligibility re-test. Closing is extensive (its output always contains
   its input) and specifically smooths away small-scale boundary notches without
   widening a genuinely narrow channel, whose width is a larger-scale property a
   modest closing radius does not change. The closed shape is used only to decide
   whether to fold `frag` in — the fold itself unions the real, unmodified `frag`
   geometry into `wide`, so closing can never introduce closed-but-not-real water
   into the actual output.

Windowed to a local neighbourhood (`radius_m * 2`) per candidate fragment rather than
the whole component, for cost; skipped entirely above
`NARROW_FRAGMENT_RECLASS_MAX_COUNT` (500) fragments on one component, same
degrade-gracefully convention as `_safe_negative_buffer`.

Verified with synthetic real-geometry fixtures (`tests/test_narrow_fragment_reclass.py`):
a cluster of tiny islands well inside otherwise-wide water (small enough that closing
at 50m swallows the whole cluster) is correctly folded (~100% of each fragment's area
recovered); a genuine narrow channel attached to the same water body, and the
inherent corner-rounding artifact of eroding a plain right-angle corner, are both
correctly left unfolded even at a generous fraction. `fraction == 0.0` reproduces
`_split_wide_narrow`'s output byte-for-byte (12/12 tests pass, including this gate).

### 8.3 Fix 2: cap Pass 0's fan-out and bias outward connections first — IMPLEMENTED

Two independent, composable changes in `_stitch_component_pieces`, both gated off by
default:

**(a) `pass0_fanin_cap`** (`--pass0-fanin-cap`, default `0`) — a cap on Pass 0's own
contribution, applied **symmetrically to both sides** of a candidate pair (unlike
`pass0_target_fanin_cap`'s target-only asymmetry — Pass 0 has no source/target
direction, either side of a same-type pair can become a hub). Deliberately a
**separate** flag from `pass0_target_fanin_cap`, not a reinterpretation of it, since
that flag stays scoped to Pass 0c/0d exactly as §6.7 documents.

**(b) `pass0_cross_type_first`** (`--pass0-cross-type-first`, default `False`) — runs
Pass 0b (cross-type k=6 NN, immune to Pass 0's same-type crowding by construction)
*before* Pass 0 instead of after. Only the call order changes — no change to
Pass 0/0b/0c/0d/Pass 1/Pass 2's own internal logic or caps. Verified
(`tests/test_pass0_cross_type_first.py`) with a fixture where two tight >6-node
same-type clusters share one reachable cross-type node: with the flag off, Pass 0
claims the cluster-to-cross-type connectors (`pass0` diag shows successes,
`pass0b` shows none); with it on, Pass 0b claims them instead (reversed); final
connectivity (one component) is identical either way in both cases.

`tests/test_pass0_fanin_cap.py` verifies (a) independently with a hub-and-spokes
fixture (mirroring `tests/test_pass2_fanin_cap.py`'s own pattern): cap=0 reproduces
today's unlimited fan-in; cap=N bounds the hub's Pass-0 out-degree at N while every
spoke still ends up connected via the same union-find/Pass 1/Pass 2 fallback already
relied on elsewhere in this file; the cap applies to a spoke acting as a local hub
too, not just the geometric center. `pass0_fanin_cap == 0` and
`pass0_cross_type_first == False` reproduce today's output byte-for-byte (full
289-test suite green with both at their defaults).

### 8.4 Also landed alongside: a default memory ceiling in `build_region.sh`

Unrelated to graph density, but requested together: `data/BUILD_LOG.md` build #32
root-caused a real OOM to `_split_wide_narrow`'s own erosion step on one region's huge
single `coastal_water` component, fixed reactively via `_safe_negative_buffer`'s
retry-with-simplification ladder — but a still-unbounded process can be killed by the
Linux OOM-killer once whole-system memory runs low on a shared host, an uncatchable
SIGKILL that can take down unrelated processes too, not just this build. `build_region.sh`
now runs step 3/3 (the routing-graph build) under a default `ulimit -v` (11GB,
matching the value that build #32 confirmed converts an uncatchable host-level kill
into a clean, catchable `GEOSException`/`MemoryError` inside the pipeline's own retry
ladder), overridable via `--build-mem-limit-gb`/`SK_ROUTING_BUILD_MEM_LIMIT_GB` (`0`
disables it). Scoped to a subshell so only step 3/3 is bounded, not the whole script.

### 8.5 Verification plan (pending — not yet run against a real build)

Everything in §8.2/§8.3 is implemented and covered by synthetic unit tests (29 new
tests total, full suite 289/289 green with all new flags at their defaults), but —
per this spec's own repeatedly-learned lesson (§6.1, §6.5) — **synthetic fixtures are
not a substitute for a real rebuild.** Before enabling any of these by default:

- Rebuild `data/zeeland_clip` with every new flag at its default (`0`/`0.0`/`False`):
  node/edge counts and the exported `.sqlite` must match a pre-change baseline
  exactly (the unit suite's disabled-by-default tests are a necessary but not
  sufficient substitute for this).
- Rebuild the motivating Potomac/Coltons Point clip with the new flags enabled;
  visually confirm (same rendering method as the original screenshot) the bowtie is
  replaced by a normal triangulated mesh connecting directly to the surrounding
  navmesh, and confirm any genuinely-shallow charted depth at that location still
  gates correctly via `_split_deep_shallow` (§8.2 only reclassifies by
  width/isolation, not depth).
- Same five-gate discipline as every prior round in this file: `crosses_land` stays
  0; connectivity measured by edge length, not node count (§6.1); POI-pair
  reachability zero-loss; node/edge counts against the *original* baseline, not just
  the immediately-prior build (§6.5's own hard-won lesson); a hub-count scan
  (out-degree > 30) to confirm §8.3 actually reduces bowtie-class fan-out on a real
  dataset, not just the synthetic fixtures above.
- Report the new `narrow_fragment_reclass_stats`/Pass 0 `fanin_capped` diagnostic
  counts (both logged at the end of `build_network`/`_ensure_coastal_connectivity`,
  matching this file's established per-mechanism logging convention) in the
  `data/BUILD_LOG.md` entry for whichever build first exercises this.

### 8.6 Real-build verification (2026-09-08): §8.2/§8.3 do NOT fix the motivating case

Rebuilt both `data/zeeland_fresh_clip` (`--narrow-fragment-reclass-max-fraction 0.5
--pass0-fanin-cap 6 --pass0-cross-type-first`, on top of Zeeland's own verified
tuning config) and the MD region (`us-east-md-stitched-v3`, same additions on top
of the rollout's tuning config) with the new flags ENABLED, not at their `0`/off
defaults — these builds do not exercise, and are not a substitute for, §8.5's own
first gate (a byte-identical rebuild at the default). What they do confirm is
`crosses_land=0` and 0 hubs with the flags on — clean and safe, but node/edge counts
differ from baseline in both builds (see below), exactly as expected with the flags
enabled. Whether §8.5's own byte-identical-at-default gate holds is still verified
only by the unit test suite's disabled-by-default coverage, not by a real rebuild at
`0`. The actual question these builds were run to answer — did enabling the flags
fix the motivating Potomac/Coltons Point case — **failed**:

- Zeeland: 42,092/124,679 nodes/edges vs. baseline 42,092/124,689 — byte-similar,
  and `--narrow-fragment-reclass-max-fraction` found **zero** candidate fragments
  the entire build (no log line at all — `fragments_checked` stayed 0).
- MD: 55,074/129,976 vs. baseline 54,766/129,606 — **more** nodes/edges, not fewer.
  In the Coltons Point bounding box specifically: 20,249/48,560 vs. 19,997/48,192
  before — no improvement. `--narrow-fragment-reclass-max-fraction` found 240
  candidates but **folded 0**; Pass 0's `fanin_capped` counter never fired in
  either build.

**Root cause of the miss, confirmed by directly inspecting the live area**: of the
~20,000 nodes in that bounding box, 18,602 are skeleton points
(`node_kind_id=0`), only 1,647 are navmesh-boundary vertices, and the wider
50km-ish surrounding region has very few navmesh nodes at all (this build's
`--min-navmesh-radius-m 1200` means no nearby water qualifies as "wide" in the
first place) — so §8.2's fold-back had no adjacent wide region to fold candidates
into, regardless of tuning. The out-degree histogram in that area is overwhelmingly
2-3 (ordinary chain/junction topology, no real hub), so §8.3's Pass 0 cap had
nothing to cap either. **Both mechanisms target a fragmented-classification/
stitching-crisscross failure mode that this specific location does not have** — its
density is a different mechanism entirely, root-caused in §9 below. §8.2/§8.3
remain real, independently-useful fixes for the failure mode they DO target
(confirmed safe and inert here), just not this one — kept in the codebase,
default off, not deployed to production off the back of this investigation alone.

## 9. The actual mechanism: unsimplified boundary noise inflates medial-axis junction density

### 9.1 Symptom and root cause, confirmed on real geometry

Following §8.6's negative result, re-investigated the Coltons Point area directly
rather than continuing to guess from the screenshot. Sampled 2,000 short (10-50m)
skeleton edges in the affected bounding box: **92% carry exactly 2 raw
`width_profile` points** — i.e. they are literal, un-splittable junction-to-junction
segments, not multi-point raster chains a resampler failed to simplify. This rules
out `--sagitta-cap`/§4.1/§4.2-class fixes: there is nothing left in these edges for
a resampler to simplify away. The density is **topological junction count**, not
under-simplified chain geometry.

Traced further: `build_skeleton_network` (`nautical_routing_pipeline.py`) rasterizes
and skeletonizes the water polygon with **no boundary simplification at all** —
straight from the source `coastal_water` layer's own ENC/chart digitization detail.
The single connected water body containing Coltons Point carries **494,363
vertices** (confirmed directly, `_connected_water_polygons` against the real MD
clip). A medial axis is, by definition, sensitive to every boundary feature: any
small digitized wiggle — a cove, a point, a single surveyed notch in a tidal
marsh's edge — spawns its own tiny branch, producing exactly the dense tangle of
short junction-to-junction edges the screenshot showed. `_split_wide_narrow`
already simplifies its own input (`simplify_tol_m=1.0`) before eroding, for a
different reason (GEOS erosion cost/robustness); `build_skeleton_network` has no
equivalent step before rasterizing.

### 9.2 Validated directly against real geometry before implementing

Learning from §8.6's cost (two real builds, one deployed, before discovering
neither mechanism applied here), this was validated on real data *before*
writing the fix. Extracted the actual narrow-water piece covering Coltons Point
via the real pipeline logic (`_connected_water_polygons` → `_split_wide_narrow` at
this build's own `--min-navmesh-radius-m 1200`, windowed to a ~3km buffer around
the target area to keep the piece tractable while preserving real local shape —
not an arbitrary bbox clip, which was tried first and found to corrupt the
geometry with artificial straight-cut edges, giving a false/inverted result).
Ran `build_skeleton_network` on this real piece with a boundary simplify at
several tolerances, counting nodes landing inside the original tight
bounding box (to exclude edge effects from the buffer window's own cut):

| boundary simplify | nodes in target area | vs. raw |
|---|---|---|
| none (today's behavior) | 181 | — |
| 5m | 150 | −17% |
| 15m | 133 | −27% |
| 30m | 118 | −35% |
| 50m | 118 | −35% (plateaus) |

A real, substantial, monotonic reduction, plateauing past ~30m.

### 9.3 Fix: `skeleton_boundary_simplify_m` — IMPLEMENTED

`ClassificationConfig.skeleton_boundary_simplify_m` (`--skeleton-boundary-simplify-m`,
default `0.0` = disabled, matching this file's established convention). In
`build_skeleton_network`, immediately after the polygon is reprojected to its
local metric CRS and before pixel-size/rasterization: `poly_m =
poly_m.simplify(cfg.skeleton_boundary_simplify_m, preserve_topology=True)` when
the tolerance is `> 0.0`. `SKELETON_BOUNDARY_SIMPLIFY_MAX_M = 200.0` bounds it —
measured gains plateau at ~30m, and a much larger value risks eroding real
channel shape rather than just digitization noise.

**Land-crossing safety is structural, not dependent on this simplify being
"correct"**: `_rasterize_water_polygon` always re-intersects the rasterized water
mask against a land mask rasterized separately from the *unmodified* land layer,
after this simplify runs. A simplified water boundary that bulges slightly into
what should be land can never produce a routable pixel there — the land mask is
the actual safety gate, unaffected by this polygon's own precision. The residual
risk is purely topological (a narrow real gap simplified into an accidental merge,
or the reverse), the same class of approximation `_split_wide_narrow`'s own
pre-erosion simplify already accepts.

Verified with a synthetic real-geometry fixture (`tests/test_skeleton_boundary_simplify.py`,
11 tests): a long channel with a sawtooth-notched edge (standing in for
fine-grained chart-digitization noise) drops from 86 to 24 nodes at a 15m
tolerance in this fixture; `0.0` reproduces today's skeleton output byte-for-byte
(including against the same polygon built with the parameter entirely omitted);
validation rejects out-of-range/NaN/infinite values. Full suite: 300/300 passing.

### 9.4 Verification plan — PARTIALLY EXECUTED (real builds done; full five-gate discipline not)

Same discipline as §8.5. Status per item, updated against `data/BUILD_LOG.md` #33-35:

- **Done**: rebuilt `data/zeeland_fresh_clip` at a real value (20m, build #35) and
  spot-checked Zeeland's own dense areas (Krammersluizen, Vossemeersebrug) — both
  essentially flat, as expected (real lock/bridge topology, not chart-noise, so this
  mechanism correctly doesn't move them). **Not done**: a matched `0.0` byte-identical
  rebuild of the same clip (the unit-test suite's disabled-by-default coverage is a
  necessary but not sufficient substitute for this, per this file's own established
  discipline).
- **Done**: rebuilt the MD/Coltons Point clip at a real value (20m, build #34) and
  confirmed the real build's bounding-box node/edge counts (17,911/42,934) against
  the piece-level measurement in §9.2 — real-build effect is smaller in relative
  terms than the isolated piece-level measurement predicted (10.4%/10.9% vs.
  17-35%), consistent with other already-enabled tuning and stitching interacting
  with this piece differently in the full build than in isolation, exactly as this
  bullet anticipated. **Not done**: visual re-rendering against the original
  screenshot (§10.1's follow-up screenshot IS a real visual check, but of a
  *different* nearby location this mechanism does not fix, not a re-check of the
  original Coltons Point tangle this fix targets).
- **Partially done**: `crosses_land` confirmed 0 on both builds; node/edge/hub counts
  reported against the original baseline (not just the immediately-prior build).
  **Not done**: connectivity measured by edge length (not node count) and POI-pair
  reachability were NOT run for either build — only count/crosses_land/hub-count
  checks were. `skeleton_boundary_simplify_stats` IS reported in both BUILD_LOG.md
  entries.
- Given §8.6's lesson, do not deploy off the strength of a clean build alone —
  confirm the specific motivating location actually improved before replacing any
  live database.

## 10. Post-§9 investigation: the "bowtie" is still present nearby — a different,
navmesh-side mechanism (investigation only, no fix implemented)

### 10.1 Symptom

`--skeleton-boundary-simplify-m` (§9) shipped and was rebuilt/deployed as
`data/us_east_md_stitched_v4.sqlite` (`--skeleton-boundary-simplify-m=20.0`,
confirmed in `data/us_east_md_stitched_v4_build.log`: "Skeleton boundary simplify:
54 pieces, 459733 -> 125709 boundary vertices (72.7% reduction)"). This measurably
reduced node/edge count in the original Coltons Point bounding box (10.4%/10.9%
reported). But a follow-up screenshot at a **different, nearby** location on the
same stretch of the Potomac (START 38.1960°N -76.7836°W, DEST 38.1960°N
-76.7088°W — near Potomac River Channel Buoys 13-15, Dukeharts Channel, Heron
Island Bar, Saint Clement Bay Warning Daybeacon; roughly 7-9km south/east of the
original Coltons Point screenshot at 38.2696°N -76.8189°W / 38.2628°N -76.8716°W)
showed the same dense "bowtie" tangle, essentially unchanged.

The user rejected a "genuinely shallow/drying marsh, not worth finely routing
through" explanation for this (verbatim): "I don't agree with your analysis wrt to
the connectivity. The lack of connections is not because of little depth of the
water, it is because somehow we are not trying the right way to connect the
navmesh. we can still eliminate much of the redundant nodes navmesh basically in
the example I think we can then remove all nodes that are non-connecting to the
skeleton nodes. In very large navmeshes it may needs bit more thought on the right
solution... Write detailed findings and clear description of the real findings (not
your assumption that it is shallow, as that is plainly not right on a large portion
of the connections between the skeleton and the navmesh (the ones on the whole
south side)."

This section investigates that claim directly against the live, deployed
`data/us_east_md_stitched_v4.sqlite` (identical copy at
`/home/node/signalkdev/signalk-routeiq/data/us_east_md_stitched_v4.sqlite`) —
**investigation only, no code changed, nothing rebuilt.**

**Bottom line up front: the user's hypothesis is confirmed, not refuted.** In the
investigated area: 83.9% of navmesh (`node_kind_id=1`) nodes have zero edges to any
skeleton (`node_kind_id=0`) node; of those, 99.5% sit at a near-straight (>150°)
turn between their two ring neighbours (median sagitta 6.2m) — unnecessary
boundary-ring filler, not real shape. A pure ring-chain-contraction removes 182 of
218 navmesh nodes (83.5%) and the same number of edges. On the "south side"
specifically, **100% of the 17 navmesh-to-skeleton connector edges found there are
in ≥5.4m water** (0% shallow) — the depth explanation is flatly refuted for that
side; the only shallow connectors anywhere in the area (11 of 51, all 0.0-1.8m) are
on a distinct, localized *north*-side cluster. Separately, ≥4 pairs of skeleton
nodes 44-360m apart each independently fan out to the same navmesh targets — a
smaller, additional stitching-redundancy issue on top of the ring-density one.

### 10.2 What was already tried and ruled out (do not re-propose)

§8.2 (`--narrow-fragment-reclass-max-fraction`) and §8.3
(`--pass0-fanin-cap`/`--pass0-cross-type-first`) are already confirmed inert for
this class of location (§8.6: zero candidates found, `fanin_capped` never fired,
out-degree histogram shows no real hub). This investigation's own fan-in/fan-out
measurements (§10.4.2) independently reconfirm no meaningful same-type hub pattern
in the newly-investigated area either — don't re-propose either flag for this
problem.

**New finding: §9's fix (`--skeleton-boundary-simplify-m`) resolved the original
Coltons Point location, but that location and the new one are different
mechanisms.** Re-querying the original screenshot's bounding box (and a much wider
surrounding box, lat 38.22-38.32 / lon -76.92--76.75) in the live v4 database finds
**zero `node_kind_id=1` (navmesh) nodes at all** there now — every node is
skeleton, with a mild out-degree histogram (`{1: 28, 2: 35, 3: 49}`, nothing above
degree 3) — no hub/bowtie signature remains. §9's fix worked, fully, at that
location. The new location, by contrast, is **navmesh-dominated** (148/210 nodes in
a representative bounding box there are `node_kind_id=1`) — a region
`--skeleton-boundary-simplify-m` structurally cannot touch, because it only
`simplify()`s the *skeleton* polygon before rasterizing (`build_skeleton_network`);
`build_navmesh_region` is a separate code path with its own, separately-tuned
simplify constant (`NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0`, see §10.3.1). This fully
explains, mechanistically, why the user saw no visible improvement at the new
location despite a real, correctly-targeted fix at the original one.

### 10.3 Investigation methodology

Schema/constants confirmed directly in `nautical_routing_pipeline.py` (not
assumed): `EDGE_KIND_CENTERLINE=0`, `EDGE_KIND_NAVMESH_BOUNDARY=1`,
`EDGE_KIND_LANE=2`, `EDGE_KIND_MACRO=3`, `NODE_KIND_POINT=0` ("skeleton" below),
`NODE_KIND_NAVMESH_VERTEX=1` ("navmesh" below), `NODE_KIND_SUPERNODE=2` (0 rows in
this dataset). `region_id` is **not** a navmesh-piece discriminator in this
schema — every node in the whole MD clip has `region_id=1`; navmesh pieces have to
be found by graph structure instead (connected components of the
`node_kind_id=1`-only subgraph, §10.3.2).

#### 10.3.1 What "navmesh nodes in the graph" actually are

Reading `build_navmesh_region` (~line 3551) directly: **the interior of a
triangulated navmesh region is never added to the routable `nodes`/`edges`
tables.** It triangulates the polygon and stores the full triangle mesh
(vertices/triangles/adjacency) as a JSON blob in the separate `navmesh_regions`
table (`CREATE TABLE navmesh_regions`, ~line 7292; used by the router at query
time for point-in-triangle routing across open water) — confirmed by tracing every
use of `navmesh_region_rows`, none of which touch `self.graph`. The **only**
`node_kind_id=1` rows that land in `nodes` are the polygon's own **perimeter ring
vertices** (exterior + interior/island rings), registered one-by-one in ring order
and connected consecutively (`EDGE_KIND_NAVMESH_BOUNDARY`, added only `if not
self.graph.has_edge(u, v)`) — stated explicitly in the function's own docstring
("Registers EVERY vertex of the region's own perimeter... as a literal graph node,
connected in ring order"). Consequence: every `node_kind_id=1` node in this
investigation is a vertex on a navmesh piece's own boundary polygon, or (secondarily)
a node touched by `_stitch_component_pieces`'s cross-type passes — there is no
"navmesh interior routing node" in `nodes`/`edges` at all. The density the
screenshots show is 100% boundary-ring + stitch-connector structure.

Before this ring is registered, `build_navmesh_region` already applies
`NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0` (line 685) — the navmesh analogue of §9's
`skeleton_boundary_simplify_m`, but a fixed constant, not a CLI flag, and already
tuned once (its own comment: a prior no-pass/5.0m/15.0m sweep found "5.0m already
captures most of the vertex-count win, median vertices/region 1247 -> 125"). This
is much tighter than §9 found optimal for the analogous skeleton problem (15-30m,
plateauing ~30m) — this gap turns out to matter (§10.4.1/10.4.4).

#### 10.3.2 Distinguishing "ring" vs "stitch connector" edges from the data alone

Both a piece's own boundary-ring edges and cross-piece/cross-type stitch
connectors added later by `_stitch_component_pieces` (Pass 0/0b/0c, e.g. line 4161)
share the same `edge_kind_id=1`; the DB does not persist which pass created an edge
(`piece_ctx`/`_node_contexts` are in-memory build diagnostics only, not exported
columns). This investigation reconstructs the distinction from topology instead:
built the undirected `node_kind_id=1`–`node_kind_id=1` subgraph for the
investigation area and computed connected components. A clean, untiled, single-ring
piece shows up as one simple cycle (`edges == nodes`, uniform degree 2); a tiled
piece or genuinely-stitched multi-piece area would show multiple components or
degree >2 at shared seams. Cross-checked against `_tile_navmesh_piece` (line 2652):
tiling triggers only above `NAVMESH_TILE_MAX_EXTENT_M` (10,000m bbox extent) or
`NAVMESH_TILE_MAX_VERTICES` (1,500 boundary vertices) — the build log for this
exact build confirms tiling did fire once, but only for one 42km×131km piece
("Navmesh tiling: 42x131km piece (1874 boundary verts) -> 5x14 grid -> 23 tiles" —
clearly the main Chesapeake Bay body, not this Potomac stretch).

#### 10.3.3 Queries used

Ran directly against the live `sqlite3` file via Python's `sqlite3` module (no
ORM, no synthetic data). Investigation area: lat 38.14-38.32, lon -76.98--76.62 (a
buffered superset of the requested lat 38.15-38.30 / lon -76.95--76.65, to avoid
edge-clipping artifacts). Core query pattern:

```python
import sqlite3
db = sqlite3.connect("data/us_east_md_stitched_v4.sqlite")
cur = db.cursor()
cur.execute("""SELECT id, lat, lon, node_kind_id, node_depth FROM nodes
               WHERE lat BETWEEN 38.14 AND 38.32 AND lon BETWEEN -76.98 AND -76.62""")
nodes = {r[0]: {"lat": r[1], "lon": r[2], "kind": r[3], "depth": r[4]} for r in cur.fetchall()}
cur.execute("SELECT source, target, distance, min_depth, drval1, min_width, width_profile, edge_kind_id FROM edges")
edges = [e for e in cur.fetchall() if e[0] in nodes and e[1] in nodes]
```
followed by adjacency-list construction, degree computation, connected-component
labeling, turn-angle/sagitta geometry (local equirectangular projection at
lat0=38.2), and a chain-contraction simulation (§10.4.4) — all pure Python, no
external geometry library needed since only turn angle and point-to-line distance
are required. Scripts were session-scratch only, not preserved in-repo; every
number below is reproducible from the description given plus the exact figures
quoted.

### 10.4 Findings

#### 10.4.1 Redundant navmesh nodes

Investigation-area totals (buffered box, lat 38.14-38.32 / lon -76.98--76.62):
**1,785 nodes** (1,567 skeleton, 218 navmesh), **4,184 directed / 2,092 undirected
edges** (3,564 centerline, 620 navmesh-boundary).

Of the **218 navmesh nodes**: 35 (16.1%) have ≥1 edge to a skeleton node; **183
(83.9%) have zero**. Of those 183: **182 have degree exactly 2** (both neighbours
also navmesh — pure ring-interior vertices), 1 has degree 1 (a ring dead-end,
likely a bbox-clip artifact, not independently re-verified).

For the 182 degree-2 nodes, computed the turn angle at each node (180° = perfectly
straight) and the sagitta (perpendicular distance from the node to the straight
line joining its two neighbours):

- **Angle**: min 149.8°, median 174.2°, 90th pct 175.3°, max 178.2°. Zero nodes have
  a real corner (<120°). 95.1% exceed 170°, 99.5% exceed 150°.
- **Sagitta**: median 6.2m, only 4.9% under 5m — most of these points individually
  sit just *past* the 5.0m `NAVMESH_BOUNDARY_SIMPLIFY_M` tolerance (exactly why
  Douglas-Peucker at 5.0m keeps them), but a chain of many such barely-over-tolerance
  points in a row is still, cumulatively, a near-straight run.
- **Ring-edge length**: median 119.7m (min 105.0m, max 473.2m) — far below
  `NAVMESH_TARGET_EDGE_M` (650m, the *triangulation* target spacing), confirming
  these are raw digitized/lightly-simplified boundary points, not triangulation
  artifacts.

**Answer**: the overwhelming majority (83.9%) of navmesh nodes here have zero
skeleton connection, and of those, essentially all (99.5%) are also not needed to
preserve the navmesh piece's own boundary-ring shape in any meaningful sense — ring
filler, not genuine topology. Exact pruning count: §10.4.4.

#### 10.4.2 The "south side" depth claim

Found 51 distinct cross-type (`node_kind_id=0` ↔ `node_kind_id=1`) edges in the
investigation area — the *only* navmesh-to-skeleton connections that exist (all
`edge_kind_id=1`). Depth distribution (`min_depth`/`drval1` agree exactly):
7.3m×29, 5.4m×11, 1.8m×4, 0.0m×7 — **78.4% deep (≥5m)**, 21.6% shallow (<2m).
`min_width` is `999.0` (unconstrained sentinel) on all 51 — width is never the
limiting factor either.

Splitting by the screenshot route's own latitude (38.1960 — START and DEST share
this exact lat, so "south" = navmesh endpoint lat < 38.1960, "north" = ≥):

| | n | deep (≥5m) | shallow (<2m) |
|---|---|---|---|
| **South** | 17 | **17 (100%)** | **0 (0%)** |
| North | 34 | 23 (68%) | 11 (32%) |

All 11 shallow/drying connector edges are on the north side, all tracing to a
small cluster of skeleton hub nodes around lon -76.74 to -76.73 / lat
38.204-38.208 — a distinct, localized, genuinely-shallow feature, not
representative of the area as a whole. `node_depth` on the underlying nodes
corroborates the edge-level numbers (several south-side nodes spot-checked
directly: `node_depth = 7.3` in every case).

**Answer**: the user's claim is confirmed exactly as stated. The south side is
100% deep water at every measured connection point; depth is not a limiting factor
there. "It's shallow" only has real support on a specific, small, already-
identifiable north-side cluster (21.6% of connections area-wide).

**What IS true about the south-side connections instead**: grouping the 51
cross-type edges by skeleton source and looking for source pairs with overlapping
navmesh targets finds at least 4 pairs of skeleton nodes, 44-360m apart, **each
independently connecting to the same set of navmesh perimeter nodes**:

| skeleton source A | skeleton source B | distance A↔B | shared navmesh targets |
|---|---|---|---|
| (38.1913,-76.6215) | (38.1903,-76.6225) | 141.9m | 4 of 4 |
| (38.1987,-76.8013) | (38.1995,-76.8006) | 111.0m | 4 of 4 |
| (38.2056,-76.7417) | (38.2039,-76.7452) | 358.4m | 2 of 4 |
| (38.1995,-76.7456) | (38.1991,-76.7456) | 44.5m | 4 of 4 |

Two of the four pairs are on the south side. Fan-in per navmesh target area-wide:
16 targets have fan-in 2, 19 have fan-in 1 — **~46% of the 35 skeleton-connected
navmesh nodes are double-connected** by this mechanism, for no additional
connectivity value (the two skeleton sources are close enough that either alone
would suffice). Consistent with `_stitch_component_pieces`'s Pass 0c "LOCAL
adjacency guarantee for navmesh perimeter" (its own docstring, ~line 4099: "no
longer stops at first genuinely-nearby... candidate") running independently per
skeleton node with no check for whether a nearby skeleton node already provides
equivalent navmesh coverage — a gap distinct from what `pass0_fanin_cap`/
`pass0_target_fanin_cap` address (§6.7, §8.3 — those cap one node's own fan-out/
fan-in; this is redundancy *between two separate, individually-low-fan-out* hubs,
already confirmed not to trip either existing cap, §8.6).

#### 10.4.3 Is it multiple redundant navmesh pieces/tiles? Largely ruled out

Connected components of the `node_kind_id=1`-only subgraph in the investigation
area: **only 2 components**, sizes 148 and 70 nodes.

- 148-node component: `edges == nodes` exactly — a single simple cycle, i.e. one
  clean, untiled piece's boundary ring. Bbox ~4.4km × ~9.6km, both well under
  `NAVMESH_TILE_MAX_EXTENT_M` (10,000m); vertex count (148) far under
  `NAVMESH_TILE_MAX_VERTICES` (1,500) — `_tile_navmesh_piece` correctly never
  tiled this piece, confirmed structurally (uniform degree 2, no degree-4
  tile-seam vertices), not just inferred from the thresholds.
- 70-node component: `edges == nodes - 1` — an open chain, not a closed ring.
  Most likely an artifact of the investigation bbox clipping through part of a
  different piece's ring (a ring edge whose other endpoint falls outside the
  buffered box gets dropped) rather than a real broken ring in the live database —
  **not independently re-verified against a wider box; open question (§10.5)**.

Degree distribution across all 218 navmesh nodes: `{1: 2, 2: 216}` — essentially no
branching, confirming simple ring structure, not a web of many small pieces'
rings crisscrossing each other.

**Answer**: the "many small tiled pieces, each contributing a redundant parallel
ring" mechanism — plausible before measuring, since `NAVMESH_TILE_MAX_EXTENT_M`
tiling is real and does fire elsewhere in this exact build (the 42km×131km
Chesapeake body → 23 tiles) — does **not** apply here. This Potomac stretch is
small enough (~4-10km) to stay a single untiled piece. The real mechanism is
simpler: **one single navmesh piece's own boundary ring is far denser than it needs
to be**, because `NAVMESH_BOUNDARY_SIMPLIFY_M` (5.0m) is much tighter than the
tolerance §9 already found optimal for the structurally-analogous skeleton case
(15-30m) — see §10.4.1. This is more actionable than the tiling hypothesis would
have been: a single-parameter change to an already-proven pattern (§9's own
`skeleton_boundary_simplify_m`), not a rework of the tiling/stitching mechanism.

#### 10.4.4 Concrete pruning quantification

Simulated the user's proposed rule directly: remove every `node_kind_id=1` node
with (a) zero edges to any `node_kind_id=0` node, AND (b) degree exactly 2 within
the navmesh-only subgraph (removal splices its two neighbours together with a
direct replacement edge — standard chain contraction). Applied iteratively (a
removal can make a newly-degree-2 neighbour eligible next) until no more nodes
qualify, on the full investigation-area graph (218 navmesh nodes, 217
navmesh-navmesh edges, both components included):

- **182 of 218 navmesh nodes removed (83.5%)**
- **navmesh-navmesh edges: 217 → 35 (182 edges removed, 83.9%)**
- As a fraction of the area's entire graph (1,785 nodes / 2,092 undirected edges,
  including all skeleton nodes): **182/1,785 nodes (10.2%)**, **182/2,092 edges
  (8.7%)** — smaller in area-wide terms only because this buffered box is
  skeleton-dominated overall; restricted to the navmesh-heavy sub-area around the
  new screenshot specifically (148/210 nodes were navmesh there), the local
  reduction is much larger (roughly 70% of that sub-area's total node count).
- No node with an existing skeleton connection, and no node of degree ≠2, was
  touched — conservative by construction, can never remove a node the user's own
  stated rule says to keep.

Extrapolation, **explicitly flagged as unverified, not a claim to build on
directly**: this MD clip has 3,975 `node_kind_id=1` nodes total; if the
83.5%-prunable ratio measured here generalizes (untested), that implies on the
order of ~3,300 navmesh nodes clip-wide could be similarly prunable — order-of-
magnitude sense of scale only.

### 10.5 Hypothesis (a)/(b)/(c) status

- **(a) "Navmesh contains many nodes that never serve as a real connection point to
  the skeleton" — CONFIRMED.** 83.9% of navmesh nodes have zero skeleton
  connection; of those, 99.5% are also geometrically non-load-bearing for the
  ring's own shape (§10.4.1). Dominant mechanism by node count, by a wide margin.
- **(b) "If stitching were done correctly, most of these could be pruned" —
  PARTIALLY CONFIRMED, re-scoped.** The 182 prunable nodes (§10.4.4) are prunable
  *independent of stitching quality* — pure boundary-ring density, untouched by any
  cross-type stitch pass. Stitching quality is a real, separate, smaller issue: the
  duplicate-fan-out pattern (§10.4.2) is real and fixable but accounts for at most
  ~16 of 51 connector edges — an order of magnitude smaller than the ring-density
  issue. Stitching redundancy is real but not the primary source of the visible
  density; boundary-ring over-density is.
- **(c) "Depth is not the limiting factor for a large fraction of south-side
  connections" — CONFIRMED, unambiguously.** 100% (17/17) of south-side
  navmesh-skeleton connector edges are in ≥5.4m water. The only shallow connections
  anywhere in the area are a distinct, localized north-side cluster (11/51 edges,
  21.6% of the total) — nowhere near "a large portion" of the south side.

### 10.6 Recommended direction for the next session

In priority order, by measured impact:

1. **Primary fix — navmesh boundary ring simplification, mirroring §9's proven
   pattern.** `NAVMESH_BOUNDARY_SIMPLIFY_M` (currently a fixed constant, 5.0m, not
   a CLI flag) is the direct analogue of `skeleton_boundary_simplify_m`. Either (a)
   raise the constant, or (b) parameterize it as a new CLI flag
   (`--navmesh-boundary-simplify-m`), defaulting to today's 5.0m for byte-identical
   output, tunable upward for real builds. **Caveat, flagged explicitly**: unlike
   skeleton edges, navmesh-boundary (`EDGE_KIND_NAVMESH_BOUNDARY`) edges are in the
   *lenient* bucket of `_sanity_check_no_land_crossings` (confirmed directly,
   ~line 6322: "Navmesh fallback edges... don't set `is_placeholder`, so they fall
   into the lenient 'skeleton' bucket... never stripped") — there is **no
   automatic strip-on-land-crossing safety net** for these edges, unlike skeleton
   edges' rasterize+land-mask re-intersection. Any tolerance increase needs its own
   land-crossing validation on real extracted geometry before shipping (same
   discipline as §9.2, not synthetic fixtures alone). The original 5.0m tuning note
   (line 696-703) already found navmesh-boundary edges under 3.0m rose from 0.9%
   (no-pass) to 3.9% (5.0m) to 6.0% (15.0m) — a real, quantified, non-zero
   depth-safety-margin cost that needs re-measuring at whatever tolerance is tried.
2. **Alternative/complementary — direct chain-contraction post-process**, exactly
   as simulated in §10.4.4: after `build_navmesh_region` registers ring
   nodes/edges (and after `_stitch_component_pieces` adds any cross-type
   connectors, so a node that gains a skeleton connection is correctly excluded),
   iteratively collapse zero-skeleton-connection `node_kind_id=1` nodes of degree
   exactly 2 in the navmesh-only subgraph, splicing their two neighbours with a
   direct edge. Topology-driven rather than tolerance-driven — no "how much
   geometry am I allowed to lose" tuning question the way `simplify()` has. §10.4.1's
   sagitta/angle numbers suggest this is very safe by construction (median 6.2m
   sagitta, no real corners removed), but the *replacement* chord is a new straight
   edge that didn't exist before and should still be checked for land-crossing.
3. **Secondary fix — dedup redundant cross-type stitch connectors.** In
   `_stitch_component_pieces`'s Pass 0c (or a post-pass), when two skeleton nodes
   within some small radius (measured redundant pairs: 44-360m) both connect to
   the same navmesh target(s), keep only the nearer one. Smaller impact than #1/#2
   (§10.4.2: at most ~16 of 51 connector edges), but directly addresses the
   mechanism most likely to visually resemble a literal "bowtie" (two nearby
   sources fanning out to overlapping distant targets) rather than just "too many
   nodes." Scope as its own change from #1/#2 — genuinely different mechanism
   (stitching redundancy vs. ring density), don't conflate in one PR.

**Do not re-attempt** §8.2/§8.3 for this problem — already confirmed inert (§8.6),
independently reconfirmed by this investigation's own fan-in/fan-out measurements.

### 10.7 Open questions / what would need a real rebuild to verify

- **Does the 83.5% local prunable-fraction (§10.4.4) generalize** to other
  navmesh-heavy areas in this clip, or elsewhere on the coast? Only directly
  measured for this one Potomac stretch; the 3,300-node clip-wide extrapolation is
  explicitly flagged as unverified.
- **Land-crossing risk of any navmesh ring simplification/contraction** — not
  checked against the `land`/`depth_areas` (drying) layers directly in this
  investigation (needs `_crosses_land`/`_drying_gdf`, which need the loaded
  GeoDataFrames from a real pipeline run, not just the exported sqlite). The single
  most important gate before shipping either §10.6 item 1 or 2 — mirrors §9.2's own
  discipline.
- **The 70-node open-chain component in §10.4.3** — not confirmed whether this is
  a genuine broken ring in the live database or a bounding-box-clip artifact. Not
  load-bearing for any finding above, but worth a quick re-check with a wider box.
- **Whether raising `NAVMESH_BOUNDARY_SIMPLIFY_M`/adding chain-contraction actually
  eliminates the new screenshot's visual bowtie** — this investigation is
  graph-theoretic (node/edge counts, degree, angle, depth), not a rendered-image
  comparison. A future session should rebuild with whichever fix is chosen and do
  the same visual confirmation §8.5/§9.4 establish as this repo's standard (same
  rendering method as the original screenshots, at both locations, plus the
  five-gate discipline: `crosses_land` stays 0; connectivity by edge length not
  node count; POI-pair reachability zero-loss; counts against the *original*
  pre-any-fix baseline; report new diagnostic counters in `data/BUILD_LOG.md`).
- **Whether the duplicate-fan-out pattern (§10.4.2) recurs densely enough elsewhere
  to be worth a dedicated fix**, or whether fixing #1/#2 alone (ring density)
  already resolves the visible symptom without needing #3 — worth measuring again
  after a #1/#2 rebuild before investing in #3.
