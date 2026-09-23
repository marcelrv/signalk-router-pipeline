# Next Phases: what is still open

Status as of 2026-09-21 (HEAD `c8178cc`, PR #26). The full status table is [`docs/ROADMAP.md`](docs/ROADMAP.md).
The finished chronological hardening log (Phase 0 through Round 25 and the 2026-08 stitching entries) was moved
to [`docs/archive/NEXT_PHASES_LOG.md`](docs/archive/NEXT_PHASES_LOG.md) (`git mv`, history kept).

Live: MD = `us_east_md_channel_axes.sqlite` (build #49), Zeeland = build #50 `zeeland_deadend_stitch_v3.sqlite`,
other 18 US East Coast regions on the Zeeland tuning config (builds #13-#32). See `data/BUILD_LOG.md`.

## Step 1: finish the in-flight graph-quality track (code)

1. `--navmesh-boundary-simplify-m` is BUILT (2026-09-22, `docs/SPEC-GRAPH-DENSITY.md` §10.6 item 1;
   `docs/SPEC-GRAPH-CLEANUP.md` §4.4): default 5.0 byte-identical, and above it a guarded simplify
   (inward up to the tolerance, outward over land capped at the 5.0 m default, cross/engulf guards,
   protected seam coordinates). Measured on Zeeland and MD with
   `scripts/measure_navmesh_boundary_simplify.py`: about -30% boundary vertices at 15 m with less mesh
   over land than the default. What is left is the validation it has never had -- a regional build at a
   raised tolerance, the depth-margin re-measurement and the land-crossing gate (§10.7, ROADMAP item 9)
   -- plus the chain-contraction post-process and stitch-connector dedup (§10.6 items 2-3).
2. Make the deployed channel-axis dead-end fix reproducible: `--channel-axis-deadend-stitch-m` defaults to 0.0
   and `build_region.sh` does not set it. Bake it in or document the exact command in BUILD_LOG.
3. Make the `validate.py` `counts` gate aware of edge-adding builds (it fails on #45-#50 by design).
4. Run the byte-identical-at-default check (`docs/SPEC-GRAPH-DENSITY.md` §8.5/§9.4) and the connectivity/POI-reachability
   gates for #34/#35, never run.
5. Rebuild and redeploy MD/Zeeland with the new flag; log per the BUILD_LOG convention.

## Step 2: graph cleanup with a real AI reviewer (after Step 1)

Baseline review, sonnet execution agent, adjudication review and side-by-side render are DONE (2026-09-22, 12-tile
pilot). `graph_cleanup/backends/local_openai.py` (local llama.cpp backend) is built and twice reviewed/fixed.
`graph_cleanup/prompts/prune.md` is now the v3 prompt, tuned over 4 rounds against `Qwen-27B-Vision-98k`; see the
full writeup and numbers in `docs/ROADMAP.md` ("Pass B local-model tuning session"). Key finding: the model has
real run-to-run sampling noise (re-running the identical prompt flips ~15% of verdicts), and lowering temperature
to 0 removes the noise but not the errors (it just makes the same mistakes every time). **Recommended next step:**
build majority-vote / require-repeat-agreement in `graph_cleanup/runner.py` before any real gold-set run -- call
the backend N times per tile, only emit a `drop` op when every sample agrees, else `unsure` (already safe). Not
yet implemented. Also compared two Gemma vision presets on the user's server: `Gemma-26B-Vision` crashes on any
real tile image (its `--ctx-size 8192` preset is too small, a server-config issue); `Gemma-12B-Vision-MTP-128K`
runs but showed more hard-gate violations (7 in one run) than Qwen -- stick with Qwen.
Follow-ups: deterministic "plain shoreline stub" rule, Pass C route tracing (only `graph_cleanup/prompts/trace.md`
exists), extra candidate kinds, nodes outside the main component. See `docs/SPEC-GRAPH-CLEANUP.md` §6.

## Step 3: channel-axes extensions (independent of Steps 1-2)

`_extract_buoyage_direction` (stub, `nautical_routing_pipeline.py:3064`) and `M_NSYS.ORIENT`; spatial-chaining
fallback for unparseable buoy names; USACE IENC ingest (`docs/SPEC-USACE-IENC.md`, not started); the
`docs/SPEC-RECOMMENDED-TRACK.md` probe (or close as "Option A stays").

## Step 4: new capabilities (decide priority after Steps 1-3)

3c overrides (`override-zones-spec` branch, then `apply_overrides.py`, `find_anomalies.py`), 3a OSM/seamarks fusion,
3b bathymetry, 3d AIS, 3f supernodes/macro-edges, 4b AI-vision resolution (maybe as Pass C), 4c wait/schedule
columns. See `PHASE_3_DESIGN.md`, `PHASE_4_DESIGN.md`.

## Open decisions (need the user)

- MA to RI x5.01 detour: "clip every region to a coastal band and rebuild" (may be partly overtaken by the
  `*_reclip` builds #13-#32; unverified).
- Round 25 Chunk 3 (`is_data_boundary`): deferred fallback; close or keep.
- Full-scale (~24 MB) Zeeland regression fixture: add or drop.
- NL beyond Zeeland: is a full NL scale-out wanted?
- Pass 0c / inland land-crossings and Issue H/J route back to 3a: confirm.
- `docs/SPEC-GRAPH-DENSITY.md` §4.2 (raster chain simplify) and Phases D/E (`<=` tie-break, `_lock_protection_mask`
  removal): pursue or close.
- Provenance of the `STITCHING_DESIGN.md` §10.7 rebuilds vs the #13-#32 rollout (see the TODO in
  `data/BUILD_LOG.md`).
