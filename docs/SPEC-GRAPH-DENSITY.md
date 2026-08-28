# Spec: Graph Node Density — Over-Sampling and Fairway Duplication

Status: Draft — analysis only, no code changes
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

### 4.1 Sagitta-bounded adaptive resampling (largest win)

Replace the uniform `max_segment_m` cut in `_resample_long_skeleton_edges` with a split
rule driven by chord deviation: walk the pixel-resolution polyline and close a segment
when the perpendicular distance from any skipped vertex to the running chord would exceed
`max_chord_sagitta_m`, **or** when a hard ceiling (`max_segment_m`, retained as a
backstop) is hit.

- Straight reaches collapse to a handful of long edges; bends keep — or gain — density
  exactly where the sampler needs it. This is strictly *more* faithful than today at bends
  and only relaxes where relaxing is provably free.
- Suggested starting tolerance: 10 m, or `min(10.0, 0.25 × local channel width)` using
  the `width_profile` already carried per segment, so narrow channels stay tight.
- `min_width` / `width_profile` merge safely: both are minima over the span, and a
  minimum over a union is the min of the minima. Depth is sampled after the split, so it
  picks up the new geometry automatically.
- Expected: most of the 33.4% node reduction that DP@10 m shows as available, without DP's
  blindness to the sampler contract.

### 4.2 Simplify the raster chain before it becomes graph edges

Apply a small Douglas-Peucker (≈ ½ pixel, so 1–5 m) to each `_skeleton_raster_to_graph`
chain *before* `_resample_long_skeleton_edges` sees it. Targets the 25.1% of edges in the
10–25 m band, which are raster discretisation, not channel shape. Cheap and independent
of 4.1, though 4.1 subsumes part of it.

### 4.3 Prefer the authoritative axis over a generated twin

Where a medial-axis centerline runs within a small tolerance of an imported
`inland_waterways_lines` axis (`wtwaxs`/`RECTRC`/`NAVLNE`), keep the axis and drop the
generated twin, rather than emitting both and stitching them. Addresses the 2,396 nodes
in §2.3.

This is the same lever `SPEC-RECOMMENDED-TRACK.md` §4 weighs as Option B, and the density
argument is a second, independent reason to take it — that spec deferred Option B purely
on feature count (75 US `RECTRC` lines). In NL the axis is dense (3,689 lines / 3,711 km),
so the two specs should be decided together, not separately: **Option B is much better
motivated by NL density than by US coverage.** Suggest gating on a `--prefer-axis`
tolerance and measuring both regions before shipping.

### 4.4 Not recommended: post-hoc DP on the exported graph

Simplifying after the fact would hit the 33% but silently invalidate every already-computed
`min_depth` / `min_width` / `crosses_land` attribute on the merged edges, since those were
sampled against the pre-merge geometry. Any decimation must happen *before*
`calculate_edge_attributes`, which is why 4.1 and 4.2 are placed where they are.

## 5. Verification plan

- Rebuild Zeeland with 4.1 at 5/10/25 m sagitta. Record node/edge counts, DB size, and
  `_sanity_check_no_land_crossings` violations — the last must not regress at any tolerance.
- Confirm the 90–110 m spike flattens and that new long edges appear only on straight reaches
  (assert measured sagitta ≤ tolerance on every emitted edge).
- Re-measure the Krammersluizen view (342 nodes today; DP@10 m suggests ~272 is reachable).
- Route-quality probe: compare a set of Zeeland routes before/after for distance and
  `min_depth` along the path. Depth must not become more optimistic anywhere.
- Largest-component connectivity must hold at 87.1% (the current Zeeland figure).

## 6. Risks

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
