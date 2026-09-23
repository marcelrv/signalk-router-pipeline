# Roadmap and status

Single source of truth for "is X done?" across this repo. Created 2026-09-21 from a docs audit checked
against `git log` (HEAD `c8178cc`, PR #26) and the code. Where a doc header disagrees with this table, this
table wins (and the doc header has been corrected or annotated). `routeiq` is a separate repo and was **not**
checked in that audit: its rows are marked unverified.

Status values: **done**, **open**, **superseded**, **deferred**. Owner: `pipeline` (this repo) or `routeiq`.

## Currently installed on the dev server (NOTHING is shipped)

No routing database has been released: graph quality is still too poor to ship (owner decision, 2026-09-21).
"Live" in `data/BUILD_LOG.md` means installed in `signalk-routeiq/data` for development and testing only.

- **Maryland**: `us_east_md_channel_axes.sqlite`, build #49 (channel axes plus the angular-sector dead-end stitch,
  built with `--channel-axis-deadend-stitch-m 1500.0`).
- **Zeeland**: build #50, `zeeland_deadend_stitch_v3.sqlite`.
- **Other 18 US East Coast regions**: on the Zeeland tuning config, builds #13-#32 (`*_stitched_v2.sqlite`).

Details and exact commands: `data/BUILD_LOG.md`. Note that `channel_axis_deadend_stitch_m` defaults to `0.0` and
`build_region.sh` only sets it when given the opt-in `--channel-axis-deadend-stitch-m 1500.0` (see Step 1 item 6);
the exact live commands are in `data/BUILD_LOG.md`.

## Done

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| Phases 0, 1, 2 (navmesh/skeleton, funnel routing) | `docs/archive/NEXT_PHASES_LOG.md` | done | pipeline + routeiq | Phase 2 consumer side lives in routeiq |
| Phase 2 hardening, Rounds 3-25 (incl. Zeeland bridge/lock/backlog series) | `docs/archive/NEXT_PHASES_LOG.md` | done | pipeline | "BACKLOG CLEARED" at Round 21 |
| Seam stitching by registry | `STITCHING_DESIGN.md`, `seam_registry.py` | done | pipeline | plain upsert, no freeze/retire (§3.4 annotated) |
| 19-region US East Coast rollout | `data/BUILD_LOG.md` #13-#32; PHASE_3 3e | done | pipeline | Zeeland tuning config applied to all regions |
| Graph density flags: sagitta resample, connector/node merge, fan-in caps, skeleton boundary and junction simplify | `docs/SPEC-GRAPH-DENSITY.md` §4-§9, §11 | done | pipeline | none enabled by default; deployed builds pass them explicitly |
| Graph cleanup: Pass A, Pass B, gates, renderer, `nearest_poi_m` fix | `docs/SPEC-GRAPH-CLEANUP.md` (PR #25/#26) | done | pipeline | Pass B run only via mock backend and Claude Code as reviewer |
| Channel axes and dead-end stitch v1 to v3 | `docs/SPEC-CHANNEL-AXES.md` §1-§8, §10 (PR #24/#26) | done | pipeline | live: MD #49, Zeeland #50 |
| DRGARE / FAIRWY harmonization | `docs/SPEC-FAIRWAY-HARMONIZATION.md` | done | pipeline | header said "Draft"; implemented (`enc_preprocessor.py`, `_build_fairways_unified`, `_edge_attr_worker`); `DRGARE` code is 46, not 53 |
| Lock marker (`requires_lock`, `lock_id`, `is_lock_transit_edge`) | `PHASE_4_DESIGN.md` §4c | done | pipeline | `_add_lock_crossing_edges` |
| Dynamic database loading (4a) | `PHASE_4_DESIGN.md` §4a | done | routeiq | unverified here (other repo) |
| Scale-out to US East Coast (3e) | `PHASE_3_DESIGN.md` §3e | done | pipeline | NL beyond Zeeland not done, see decisions |
| Adopted seam-node depth preservation | `docs/archive/NEXT_PHASES_LOG.md` (2026-08-14 question) | done | pipeline | commit `655ae47` |
| SPEC-FAIRWAY-DEDUP | `docs/archive/SPEC-FAIRWAY-DEDUP.md` | superseded | pipeline | superseded by `docs/SPEC-CHANNEL-AXES.md` |
| PHASE_4 §4a.1 (stitching interplay) | `PHASE_4_DESIGN.md` §4a.1 | superseded | pipeline | superseded by `STITCHING_DESIGN.md` |
| Round 25 Chunk 1 "grid-snap" | `docs/archive/NEXT_PHASES_LOG.md` | superseded | pipeline | rejected (4% coincidence) |
| LEGACY_IDEAS items | `docs/archive/LEGACY_IDEAS.md` | superseded | pipeline | done or tracked elsewhere |
| `--inland-resample-max-segment-m` (BUILD_LOG #11/#12) | `docs/SPEC-GRAPH-DENSITY.md` header note | superseded | pipeline | regressed connectivity, not deployed |

## Open actions, in recommended sequence

### Step 0: make the docs truthful

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| This file, stale banners fixed, BUILD_LOG rows #33-#38, archive moves | this file; see Moved to archive below | done (2026-09-21, uncommitted at time of writing) | pipeline | |
| Confirm provenance of `STITCHING_DESIGN.md` §10.7 rebuilds vs the #13-#32 rollout | `data/BUILD_LOG.md` header TODO | open | pipeline | needs the repo owner; deliberately not logged by guessing |

### Step 1: finish the in-flight graph-quality track (code)

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| 5. `--navmesh-boundary-simplify-m` flag (parametrize `NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0`) | `docs/SPEC-GRAPH-DENSITY.md` §10.6 item 1; `docs/SPEC-GRAPH-CLEANUP.md` §4.4 | code done (2026-09-22); MODEST measured win on the bench; NOT validated by any regional build | pipeline | The 2026-09-21 review killed the re-intersect land-safety clip (it made the boundary DENSER: 5,861 verts at 5 m -> 27,586 at 15 m, 37-55 parts, 298 -> 1,158 nodes, sub-mm segments). Replaced by `_topology_guarded_simplify` (asymmetric-tolerance Douglas-Peucker: inward up to tol, outward over land capped at the 5.0 m default, cross/engulf guards, geometric anchors, protected seam coordinates). Measured on two regions with `scripts/measure_navmesh_boundary_simplify.py`: at 15 m the boundary loses 25-34% of its vertices (Zeeland 124 km2 body 5,861 -> 3,886; MD 4,577 km2 Chesapeake body 65,539 -> 49,225) with LESS mesh over land than the default (Zeeland 24.3k -> 19.2k m2; MD 44.0M -> 40.7M m2) and one part with every island kept; end to end this is roughly a 20-35% node reduction depending on the crop (Zeeland 18.9 km2 crop 298 -> 242 at 15 m; MD 19.7 km2 crop 242 -> 167; an independent reviewer's 18.8 km2 Zeeland crop 270 -> 183). That is NOT the '83% of navmesh nodes are prunable' figure SPEC §10.4 quoted as the prize. Cost: real water shaved inward, gross 0.50 km2 of 124.2 km2 at 15 m on Zeeland. Recommended first arm `--navmesh-boundary-simplify-m 15`. Unverified: any regional build, the depth-margin re-measurement (§10.7) and the rendered-image check -- see item 9 |
| 5b. Chain-contraction post-process and stitch-connector dedup; resolve §10.7 land-crossing questions | `docs/SPEC-GRAPH-DENSITY.md` §10.6 items 2-3, §10.7 | open | pipeline | |
| 6. Make the dead-end fix reproducible (`--channel-axis-deadend-stitch-m` defaults to 0.0; `build_region.sh` does not set it) | `docs/SPEC-CHANNEL-AXES.md` §10 | done (opt-in) | pipeline | `build_region.sh` has an opt-in `--channel-axis-deadend-stitch-m <m>` (requires `--channel-axes`; validated up front, must be >= 0 and < 5000; pipeline default stays 0.0 = disabled; #49/#50 used 1500.0). Default builds unchanged. The MD/Zeeland tuning flags still go through `--extra-pipeline-args` (see BUILD_LOG #43/#44); byte-identical reproduction of #49/#50 via the script is unverified |
| 7. `validate.py` `counts` gate aware of edge-adding builds (fails on #45-#50 by design) | `graph_cleanup/validate.py` | done (opt-in) | pipeline | `validate.check(max_edge_growth=N)` (default 0 = shrink-only) supports baseline-vs-candidate comparisons of separate builds (#45-#50 were ad-hoc cross-build checks). The `apply_cleanup.py --max-edge-growth` flag is forward-looking only (no op can add an edge). Node growth is never tolerated, so drift such as #45's +34 nodes still fails (SPEC-GRAPH-CLEANUP.md §5) |
| 8. Byte-identical-at-default check; connectivity/POI-reachability gates for #34/#35 | `docs/SPEC-GRAPH-DENSITY.md` §8.5, §9.4 | open | pipeline | never run |
| 9. Rebuild and redeploy MD/Zeeland with the new flag; log per BUILD_LOG convention | `data/BUILD_LOG.md` | open | pipeline | depends on 5 (code now ready). This is the only thing that can settle what item 5's bench measurements cannot: the depth-margin cost at a raised tolerance (navmesh-boundary edges <3.0 m was 0.9%/3.9%/6.0% at no-pass/5 m/15 m for the OLD plain simplify), `crosses_land` staying 0, POI-reachability/connectivity, and the rendered-image check (§8.5/§9.4). Suggested first arm: `--navmesh-boundary-simplify-m 15` (the measured knee: -32% boundary vertices, -19% navmesh nodes, LESS mesh over land than the default, 0.39 km² net water loss from 0.50 km² gross cut over the 124.2 km² body) |

### Step 2: graph cleanup with a real AI reviewer (after Step 1)

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| 10-13. Baseline review, sonnet execution agent, separate review agent, side-by-side render | `docs/SPEC-GRAPH-CLEANUP.md` §6 | done (2026-09-22) | pipeline | 12-tile / 130-candidate pilot on `us_east_md_deadend_stitch_v3.sqlite` (build #49) after a fresh Pass A run. Baseline (mine) vs a separate sonnet-agent run: 88% effective agreement, applied-outcome verified node-for-node against both label sets by an independent adjudicator (no tooling bugs: `apply_cleanup`/`answers_to_ops` correctly map every drop, no unintended drops, no new fragments). `render.before_after`/`render_diff` used for the side-by-side |
| 14. Local-AI-engine backend and >=3 tuning rounds | `graph_cleanup/backends/local_openai.py`; `graph_cleanup/prompts/prune.md` | code done, tuning done as a pilot (not yet a real build) | pipeline | See "Pass B local-model tuning" below for the full writeup. `graph_cleanup/prompts/prune.md` is now the v3 prompt (was v1); validated at 0 hard-gate violations on 2 of 3 v3-family samples. The backend itself went through 2 independent-review + fix rounds (duplicate-JSON-key safety, manifest-digest race, destructive `--no-resume`, atomic writes, URL validation, circuit breakers) and is part of this PR |
| 15. Follow-ups: deterministic "plain shoreline stub" rule, Pass C route tracing, more candidate kinds, nodes outside the main component | `docs/SPEC-GRAPH-CLEANUP.md` §6 | open | pipeline | Pass C not built; only `graph_cleanup/prompts/trace.md` exists |
| 16. Majority-vote / require-repeat-agreement before an actual `drop` | `graph_cleanup/runner.py` (not yet built) | open, recommended next | pipeline | See noise finding below. Not implemented: `run_tile`/`run_all` would need to call the backend N times per tile and only emit a `drop` op when every sample agrees, otherwise fall back to `unsure` (already safe by the prompt's own rule) |

**Pass B local-model tuning session (2026-09-22, `Qwen-27B-Vision-98k` via the user's llama.cpp server, temperature 0.2 unless noted).** All of this ran against the same 12-tile/130-candidate pilot; scratch artifacts (verdict JSONs, prompt versions v1-v4, per-round audit) live only under `/tmp/passb` on the dev machine and were NOT committed -- this table is the durable record.

| Round | Prompt | Agreement w/ baseline | Hard-gate violations (baseline `keep`, model `drop`) | Notes |
|---|---|---|---|---|
| 1 | v1 (original `prune.md`) | 56% | 0 | 0 `unsure`, fully templated non-answers ("likely part of the local navigation mesh" on nearly every candidate) -- safe but useless |
| 2 | v2 (requires citing a `context.json` number) | 67% | 0 | groundedness fixed (82% cite a real number); a subtler templating survived: same geometric *description* reused across candidates with only the number swapped |
| 3 | v3 (bans paraphrasing the rule text; forces contrastive/neighbour comparison) | 74% | 0 | best single-run result; this is the version now deployed to `graph_cleanup/prompts/prune.md` |
| 3-repeat | v3, identical prompt, resampled | 70% | 2 | **run-to-run noise measured directly: re-running the identical prompt flips 19/130 verdicts (85% self-agreement) and produces 2 new hard-gate violations that the first v3 run didn't have** |
| 4 | v4 (v3 + explicit rule: a real, non-zero/non-missing `min_depth_m` is evidence toward `keep`, not to be overridden by shape alone) | 65% | 3 | the targeted fix worked exactly as intended on its test case (`t1_1434_-2210` #2 flipped from wrong-`drop` to correct-`keep` citing the sounding); the overall regression is not clearly attributable to v4 given the round-3-repeat noise measurement above -- inconclusive with n=1 samples per prompt version |
| temp=0 (v3, greedy decoding, 2 independent passes) | v3 | 68.5% | 3 (identical both passes) | **perfectly deterministic (130/130 identical across 2 runs) but not more accurate** -- greedy decoding locks onto the model's single most-likely answer, which is wrong on the same 3 candidates (`t1_1426_-2180` #9/10/11) every time; temp=0.2 runs got these right in 2 of 3 samples. Lowering temperature trades away the ability to use resampling as a safety check, for no accuracy gain |

**Model comparison (same 12 tiles, v3 prompt, same server):**

| Model | Works at full 1536x1536 tile res? | Agreement | Hard-gate violations |
|---|---|---|---|
| `Qwen-27B-Vision-98k` | yes | 65-74% across samples | 0-3 across samples |
| `Gemma-26B-Vision` | **no** -- crashes (raw proxy error, not a clean 400) on any real tile image; preset's `--ctx-size 8192` is too small for a 1536px chart, unrelated to my code (reproduced with raw `curl`, no backend involved) | -- | -- |
| `Gemma-12B-Vision-MTP-128K` | yes, but only after the user restarted the server (a stale swap/load state, not an image-size limit -- separately confirmed a hard ceiling of ~1024px before that, now moot) | 77% (single run) | **7** -- confidently misjudged 3 real creek-tip stubs in one tile (`t1_1414_-2225` #1/2/3, all baseline `keep`) as artifacts, with fluent, specific-sounding but factually wrong reasoning |

**Conclusion and recommendation:** stay with `Qwen-27B-Vision-98k` and the v3 prompt (now live in `prune.md`). The single biggest lever is not further prompt micro-tuning -- it's that no individual sample from either model should be trusted for an actual `drop`. Item 16 (majority vote / require repeat agreement) is the recommended next step before any real gold-set run.

### Step 3: channel-axes extensions (independent of Steps 1-2)

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| `_extract_buoyage_direction` (`nautical_routing_pipeline.py:3489`) and `M_NSYS.ORIENT` | `docs/SPEC-CHANNEL-AXES.md` §9 | done | pipeline | no live caller wires the return value in yet ("laned" classification is still a behavioral no-op); function + ORIENT cross-check implemented and tested |
| Spatial-chaining fallback for unparseable buoy names (4% US / 13% NL) | `docs/SPEC-CHANNEL-AXES.md` | open | pipeline | |
| USACE IENC ingest (phases A/B/C), then National Channel Framework polygons | `docs/SPEC-USACE-IENC.md` | open | pipeline | Draft, not started |
| Recommended-track probe (Great Lakes/NY) gating Option B, or close as "Option A stays" | `docs/SPEC-RECOMMENDED-TRACK.md` | open | pipeline | Draft |

### Step 4: new-capability roadmap (decide priority after Steps 0-3)

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| 3c community overrides: merge or finish `override-zones-spec`, then `apply_overrides.py`, `find_anomalies.py` | `PHASE_3_DESIGN.md` §3c; `docs/SPEC-OVERRIDE-ZONES.md` (on branch `override-zones-spec`, not on main) | open | pipeline | neither script exists in the repo |
| 3a OSM/OpenSeaMap tier-3 fusion | `PHASE_3_DESIGN.md` §3a | open | pipeline | also the assigned fix for ~55 inland land-crossings and Issue J |
| 3b bathymetry raster (incl. Seascape evaluation) | `PHASE_3_DESIGN.md` §3b | open | pipeline | |
| 3d AIS traffic validation | `PHASE_3_DESIGN.md` §3d | open | pipeline | |
| 3f supernodes / macro-edges | `PHASE_3_DESIGN.md` §3f | open | pipeline | README section marked PLANNED |
| 4b AI-vision ambiguity resolution: fold into GRAPH-CLEANUP as Pass C or keep separate | `PHASE_4_DESIGN.md` §4b | open | pipeline | share tile-rendering infrastructure |
| 4c `typical_wait_minutes` / `opening_schedule` columns and Zeeland manual values | `PHASE_4_DESIGN.md` §4c | open | pipeline | lock marker itself is done |

## Open decisions and loose ends (need the user)

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| MA to RI x5.01 detour: clip every region to a coastal band and rebuild | `docs/archive/NEXT_PHASES_LOG.md` (2026-08-08 entries) | open | pipeline | recorded as open in the audit plan; note BUILD_LOG #13-#32 used `*_reclip` inputs, so it may be partly overtaken. Not verified |
| Round 25 Chunk 3 (`is_data_boundary`) | `docs/archive/NEXT_PHASES_LOG.md` (Round 25) | deferred | pipeline | close as not needed, or keep |
| Full-scale (~24 MB) Zeeland regression fixture | `docs/archive/NEXT_PHASES_LOG.md` (Round 4) | open | pipeline | add or drop |
| NL beyond Zeeland | `PHASE_3_DESIGN.md` §3e | open | pipeline | only Zeeland is logged; is a full NL scale-out wanted? |
| Pass 0c / inland land-crossings and Issue H/J route back to 3a | `docs/archive/NEXT_PHASES_LOG.md` | open | pipeline | confirm that stays the plan |
| DENSITY §4.2 (raster chain simplify) and Phases D/E (`<=` tie-break, `_lock_protection_mask` removal) | `docs/SPEC-GRAPH-DENSITY.md` §4.2, §6.3 | open | pipeline | pursue or close |
| `DRGARE` OBJL 53 vs 46 typo | `docs/SPEC-FAIRWAY-HARMONIZATION.md` §2 | done | pipeline | fixed 2026-09-21 (46 is correct; 53 is `FERYRT`, DRYDOC is 47) |

## routeiq side (other repo; tracked for visibility only, unverified)

| Item | Doc / section | Status | Owner | Note |
|---|---|---|---|---|
| `aggregateSegmentEdges` dropping `path_points` (Round 11) | `docs/archive/NEXT_PHASES_LOG.md` | open | routeiq | no closure seen; unverified |
| Duplicate adjacency | `STITCHING_DESIGN.md` §8.4.2 | open | routeiq | unverified |
| "Teleport" warning | `STITCHING_DESIGN.md` §9.3 | open | routeiq | unverified |
| VA to NC A* invariant error | `STITCHING_DESIGN.md` / `docs/archive/NEXT_PHASES_LOG.md` | open | routeiq | unverified |

## Moved to archive

Moved with `git mv` (history kept); nothing was deleted.

- `LEGACY_IDEAS.md` -> `docs/archive/LEGACY_IDEAS.md`: moved, superseded by this roadmap and `PHASE_3_DESIGN.md` / `PHASE_4_DESIGN.md`.
- `docs/SPEC-FAIRWAY-DEDUP.md` -> `docs/archive/SPEC-FAIRWAY-DEDUP.md`: moved, superseded by `docs/SPEC-CHANNEL-AXES.md`.
- `NEXT_PHASES.md` (full chronological log) -> `docs/archive/NEXT_PHASES_LOG.md`: moved, superseded by this roadmap plus the short open-items file `NEXT_PHASES.md` (a new, short file now at the old path).

## Stay in place (banners fixed, not moved)

`PHASE_3_DESIGN.md`, `PHASE_4_DESIGN.md` (open items depend on them), `STITCHING_DESIGN.md`, and the live specs in `docs/SPEC-*.md`.
