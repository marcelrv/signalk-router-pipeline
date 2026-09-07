# Build Log

Every `.sqlite` build produced from this repo (test builds, A/B experiments, and
anything installed to `signalk-routeiq/data`) gets an entry here **before** it's
considered done. This exists specifically to stop re-guessing/re-running builds that
were already tried — check this table first.

**Every build (yours included) must append a row + a Details block.** Do not skip
this because a build "was just a quick test" — quick tests are exactly what this log
is for.

Baseline reference (SPEC-GRAPH-DENSITY.md §1, pre-any-density-fix): **48,553 nodes /
137,718 edges** on `data/zeeland_full.sqlite`.

Deployed live db as of 2026-09-04 (`signalk-routeiq/data/zeeland.sqlite`, exact build
command unknown/unreproduced — flagged in #1 below): **64,717 nodes / 203,582 edges /
5 hub nodes (out-degree>30, max 33) / 0 crosses_land**.

## Table

**Node/edge counts are only comparable across rows with the same Input dir.** Two
rows built from different source clips are not an A/B pair no matter how similar
their flags look — check the Input dir column before drawing any conclusion from a
Nodes/Edges delta.

| # | Date | Commit | Input dir (clip) | Flags (non-default only) | Purpose | Nodes | Edges | Hubs (od>30) | Max out-deg | crosses_land | Installed live? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | unknown (~2026-09-03) | unknown | **UNKNOWN — not reconstructed** | **UNKNOWN** | n/a (found already deployed) | 64,717 | 203,582 | 5 | 33 | 0 | **YES (currently live)** |
| 2 | 2026-09-04 | `38a09ed` | `data/zeeland_fresh_clip` | `--sagitta-cap 75 --max-segment-m 2000 --axis-dedup-cap 50 --connector-merge-m 5.0 --inland-densify-max-segment-m 0.0` | Does §6.5 alone (no §6.4 densify) fix the hub problem? | 45,765 | 162,004 | 231 | 222 | 0 | no |
| 3 | 2026-09-04 | `38a09ed` | `data/zeeland_fresh_clip` | `--sagitta-cap 75 --max-segment-m 2000 --axis-dedup-cap 50 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0` | "Everything on" recommended config | 58,215 | 224,553 | 56 | 42 | 0 | no |
| 4 | 2026-09-04 | `38a09ed` | `data/zeeland_fresh_clip` | same as #3 but `--connector-merge-m 0.0` | Clean control for #3 — isolate §6.5's own effect | 56,433 | 217,787 | 65 | 42 | 0 | no |
| 5 | 2026-09-04 | `38a09ed` | `data/zeeland_fresh_clip` | same as #3 but `--axis-dedup-cap 25.0` | Does a tighter axis-dedup cap reduce stitch-pass hubs? | 59,544 | 229,379 | 57 | 42 | 0 | no |
| 6 | 2026-09-04 | `f560cbf` | `data/zeeland_fresh_clip` | same as #3 but `--pass2-max-fanin-per-node 6` | Does §6.6's Pass 2 fan-in cap fix the hub problem? | 58,215 | 224,553 | 56 | 42 | 0 | no |
| 7 | 2026-09-04 | `6165615` | `data/zeeland_fresh_clip` | same as #6 but `--pass0-target-fanin-cap 4` | §6.7 fix — Pass 0c/0d Direction A target cap | 58,215 | **187,551** | **0** | **12** | 0 | **YES** |
| 8 | 2026-09-04 | this commit (`FAIRWAY_MATCH_BUFFER_M` fix, on top of `d9eb3c5`) | `data/zeeland_fresh_clip` | same as #7 (unchanged flags — the fix is in `calculate_edge_attributes`, not a CLI flag) | Fix the fairway cost_factor coverage regression traced from the Krammersluis routing bug report | 58,215 | 187,551 | 0 | 12 | 0 | **YES** |
| 9 | 2026-09-04 | `ba07297` | `data/zeeland_fresh_clip` | same as #8 plus `--node-merge-m 5.0` | §6.8 fix — generalize `connector_merge_m`'s tolerance-merge to `_get_or_create_node` itself | 57,264 | 185,155 | 0 | 11 | 0 | superseded by #10 |
| 10 | 2026-09-04 | this commit (§6.9 follow-up, on top of `8e9f507`/`4ae9cd9`) | `data/zeeland_fresh_clip` | same as #9 plus `--sagitta-cap 250.0 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0` | §6.9 follow-up — flat 100m axis-dedup floor to fix real crisscross at the Vossemeersebrug/Tholen narrows | 42,092 | 124,689 | 0 | 14 | 0 | **YES (currently live)** |
| 11 | 2026-09-04 | this commit (`--inland-resample-max-segment-m`, on top of `c2259f2`) | `data/zeeland_fresh_clip` | same as #10 plus `--inland-resample-max-segment-m 250.0` | Try to close the "still ~71-100m spacing on fairways" gap -- REGRESSED, not deployed (21 named POIs lost from main component, incl. Krammersluizen) | 29,006 | 80,064 | 0 | 14 | 0 | no -- regressed |
| 12 | 2026-09-04 | this commit, same as #11 but a smaller cap | `data/zeeland_fresh_clip` | same as #10 plus `--inland-resample-max-segment-m 100.0` | Same idea, more conservative cap -- STILL regressed (9 named POIs lost, incl. Middelburg harbours), not deployed | 31,457 | 68,884 | 0 | 14 | 0 | no -- regressed |
| 13 | 2026-09-06 | `8652bda` | `data/geojson/ct_reclip` (re-derived via `data/raw/us-east-coast/CT`) | Zeeland build #10's tuning config (`--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0`) applied to US East Coast region `us_east_ct_stitched` | Roll out Zeeland's verified density-tuning config to US East Coast regions | 20,359 | 47,902 | 0 | 19 | 0 | **YES** |
| 14 | 2026-09-06 | `708de40` | `data/geojson/de_reclip` (re-derived via `data/raw/us-east-coast/DE`) | same tuning config as #13, applied to `us_east_de_stitched` | Roll out Zeeland's tuning config, region 2/19 | 19,420 | 45,378 | 0 | 16 | 0 | **YES** |
| 15 | 2026-09-07 | `8f60b9e` | `data/geojson/fl_atl_n1a_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_atl_n1a_stitched` | Roll out Zeeland's tuning config, region 3/19 | n/a | n/a | n/a | n/a | n/a | **FAILED — OOM-killed twice, not deployed** |
| 16 | 2026-09-07 | `f5994a5` | `data/geojson/fl_atl_n1b_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_atl_n1b_stitched` | Roll out Zeeland's tuning config, region 4/19 | 9,358 | 17,441 | 0 | 15 | 0 | **YES** |
| 17 | 2026-09-07 | `3f0d5c6` | `data/geojson/fl_atl_n2_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_atl_n2_stitched` | Roll out Zeeland's tuning config, region 5/19 | 29,242 | 67,761 | 0 | 16 | 0 | **YES** |
| 18 | 2026-09-07 | `fd316bf` | `data/geojson/fl_atl_s_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_atl_s_stitched` | Roll out Zeeland's tuning config, region 6/19 | 56,301 | 135,182 | **4** | **138** | 0 | **YES (see hub-count caveat in Details)** |
| 19 | 2026-09-07 | `19df1ef` | `data/geojson/fl_gulf_mid_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_gulf_mid_stitched` | Roll out Zeeland's tuning config, region 7/19 | 36,755 | 87,270 | 0 | 14 | 0 | **YES** |
| 20 | 2026-09-07 | `ad5f094` | `data/geojson/fl_gulf_pan_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_gulf_pan_stitched` | Roll out Zeeland's tuning config, region 8/19 | 21,617 | 45,942 | 0 | 25 | 0 | **YES** |
| 21 | 2026-09-07 | `1e75c67` | `data/geojson/fl_gulf_sw_reclip` (re-derived via `data/raw/us-east-coast/FL`) | same tuning config as #13, applied to `us_east_fl_gulf_sw_stitched` | Roll out Zeeland's tuning config, region 9/19 | 30,739 | 66,692 | **3** | **134** | 0 | **YES (same Key West hub caveat as #18)** |
| 22 | 2026-09-07 | `bae4e88` | `data/geojson/ma_reclip` (re-derived via `data/raw/us-east-coast/MA`) | same tuning config as #13, applied to `us_east_ma_stitched` | Roll out Zeeland's tuning config, region 10/19 | 36,898 | 87,637 | 0 | 22 | 0 | **YES** |
| 23 | 2026-09-07 | `809e0b1` | `data/geojson/md_reclip` (re-derived via `data/raw/us-east-coast/MD`) | same tuning config as #13, applied to `us_east_md_stitched` | Roll out Zeeland's tuning config, region 11/19 | 54,766 | 129,606 | 0 | 16 | 0 | **YES** |
| 24 | 2026-09-07 | `034892c` | `data/geojson/me_reclip` (re-derived via `data/raw/us-east-coast/ME`) | same tuning config as #13, applied to `us_east_me_stitched` | Roll out Zeeland's tuning config, region 12/19 | 52,994 | 141,005 | 0 | 17 | 0 | **YES** |
| 25 | 2026-09-07 | `b8372ae` | `data/geojson/nc_reclip` (re-derived via `data/raw/us-east-coast/NC`) | same tuning config as #13, applied to `us_east_nc_stitched` | Roll out Zeeland's tuning config, region 13/19 | 46,839 | 112,311 | 0 | 16 | 0 | **YES** |
| 26 | 2026-09-07 | `7d5dbbb` | `data/geojson/nh_reclip` (re-derived via `data/raw/us-east-coast/NH`) | same tuning config as #13, applied to `us_east_nh_stitched` | Roll out Zeeland's tuning config, region 14/19 | 8,524 | 15,333 | 0 | 14 | 0 | **YES** |
| 27 | 2026-09-07 | `39dfeac` | `data/geojson/nj_reclip` (re-derived via `data/raw/us-east-coast/NJ`) | same tuning config as #13, applied to `us_east_nj_stitched` | Roll out Zeeland's tuning config, region 15/19 | 25,973 | 62,027 | 0 | 17 | 0 | **YES** |

**Row #1 is not a valid comparison baseline** — its input clip/flags are unknown, so
its counts cannot be attributed to any specific configuration. It's recorded because
it's what's currently live, not because it's a controlled data point. Rows #2-#5 are
the only mutually-comparable set so far (identical `data/zeeland_fresh_clip` input).

## Details

### #1 — live deployed db (provenance unknown)

- **Command**: not reconstructed. This repo checkout was found 11 commits behind
  `origin/main` when this was investigated (2026-09-03/04 session); the live db must
  have been built from a different checkout/session. No build log survived in either
  this repo's `data/` or `signalk-routeiq/data/`.
- **Metadata found in the db itself**: `country=NL`, `name=Zeeland`,
  `architecture=navmesh-hybrid-phase1`, coverage bbox
  `min_lat=51.21042 min_lon=3.13334 max_lat=51.95 max_lon=4.65002`,
  metadata timestamp `2026-09-03T20:43:43Z`.
- **Why it matters**: this is the only build so far with a low hub count (5). None of
  builds #2-#5 (same source clip, various flag combos) have reproduced that — see
  "Open question" below. Do not assume flag values for this build; they are unknown.

### #2 — `zeeland_connectormerge.sqlite` — sagitta+axis-dedup+connector-merge, densify OFF

```
.venv/bin/python3 nautical_routing_pipeline.py \
  --input-dir data/zeeland_fresh_clip \
  --output data/zeeland_connectormerge.sqlite \
  --country NL --name "Zeeland" \
  --description "Zeeland province and approaches (Westerschelde, Oosterschelde, Veerse Meer, Grevelingen, Haringvliet, North Sea approach), based on Rijkswaterstaat IENC / ENC data" \
  --tags '["ienc","rws","coastal","inland"]' \
  --url "https://github.com/marcelrv/signalk-router-data" \
  --license "Public Domain (Rijkswaterstaat)" --copyright "Rijkswaterstaat" \
  --depth-ceiling 6.0 \
  --sagitta-cap 75.0 --max-segment-m 2000 \
  --axis-dedup-cap 50.0 \
  --connector-merge-m 5.0 \
  --inland-densify-max-segment-m 0.0
```

- **Purpose**: test whether §6.5 (connector-merge) alone, without §6.4's blanket
  densify, could fix the original hub-fanout problem §6.4 was built for.
- **Result**: lowest node count of any build here (45,765, actually *below* the
  original 48,553 baseline) — but hub count exploded to 231 (max out-degree 222),
  essentially back to the pre-§6.4 historical severity. **Conclusion: §6.5 does NOT
  make §6.4 unnecessary** — they fix different call sites (§6.5:
  `_connect_waterway_crossing`/`_add_opening_bridge_edges`; the hub problem here
  traces to `_ensure_coastal_connectivity`/`_stitch_component_pieces`, untouched by
  §6.5). Do not deploy this config.
- **Log**: `data/zeeland_connectormerge_build.log`

### #3 — `zeeland_connectormerge2.sqlite` — sagitta+axis-dedup+densify(120)+connector-merge(5)

```
.venv/bin/python3 nautical_routing_pipeline.py \
  --input-dir data/zeeland_fresh_clip \
  --output data/zeeland_connectormerge2.sqlite \
  --country NL --name "Zeeland" \
  --description "Zeeland province and approaches (Westerschelde, Oosterschelde, Veerse Meer, Grevelingen, Haringvliet, North Sea approach), based on Rijkswaterstaat IENC / ENC data" \
  --tags '["ienc","rws","coastal","inland"]' \
  --url "https://github.com/marcelrv/signalk-router-data" \
  --license "Public Domain (Rijkswaterstaat)" --copyright "Rijkswaterstaat" \
  --depth-ceiling 6.0 \
  --sagitta-cap 75.0 --max-segment-m 2000 \
  --axis-dedup-cap 50.0 \
  --connector-merge-m 5.0 \
  --inland-densify-max-segment-m 120.0
```

- **Purpose**: "everything on" build — the intended real-world recommended config.
- **Result**: 58,215 nodes / 224,553 edges / 56 hubs (max 42). Build log confirms
  §6.5 firing for real: *"2,887 candidates, 1,066 merged into an existing/
  previously-split vertex, 1,821 split a new vertex at the true point of contact."*
  Best hub count among #2-#5, but still far above live db's 5.
- **Log**: `data/zeeland_connectormerge2_build.log`

### #4 — `zeeland_control_nomerge.sqlite` — same as #3 but `--connector-merge-m 0.0`

- **Purpose**: clean A/B control for #3, isolating §6.5's own effect with everything
  else held constant (same input clip, same sagitta/axis-dedup/densify values).
- **Result**: 56,433 nodes / 217,787 edges / 65 hubs (max 42).
- **§6.5's isolated effect (± #3 vs #4, same data/flags otherwise)**: +1,782 nodes,
  +6,766 edges, **-9 hubs**. Node/edge count going UP with the fix on (in this
  specific densify=120 config) is real and understood: a 5m merge tolerance is much
  tighter than densify's ~120m vertex spacing, so most axis-dedup carve-reconnect
  candidates don't find an existing vertex within tolerance and split a fresh one
  instead of reusing the (now-plentiful but still >5m away) nearby vertices. §6.5's
  duplicate-avoidance is real (1,066 genuine merges in #3) but small relative to
  densify's own vertex count in this config.
- **Log**: `data/zeeland_control_nomerge_build.log`

### #5 — `zeeland_dedup25.sqlite` — same as #3 but `--axis-dedup-cap 25.0`

- **Purpose**: test whether a tighter axis-dedup cap (less carving/fragmentation)
  reduces the stitching-pass hub count.
- **Result**: 59,544 nodes / 229,379 edges / 57 hubs (max 42) — essentially
  unchanged from #3 (56 hubs). **Conclusion: axis-dedup-cap is NOT the lever for the
  stitching-pass hub problem** — component count going into
  `_ensure_coastal_connectivity` is identical (402) regardless of axis-dedup-cap,
  since that pass iterates over top-level water-body components determined before
  axis-dedup carving ever runs. Do not re-try tuning this flag for the hub issue.
- **Log**: `data/zeeland_dedup25_build.log`

### #6 — `zeeland_pass2cap.sqlite` — same as #3 but `--pass2-max-fanin-per-node 6`

```
... same as #3's command, plus:
  --pass2-max-fanin-per-node 6
```

- **Purpose**: test SPEC-GRAPH-DENSITY.md §6.6 — does capping Pass 2's per-node
  stitching fan-in fix the residual hub problem?
- **Result**: 58,215 nodes / 224,553 edges / 56 hubs (max 42) — **identical** to #3
  in every count. `fanin_capped` never fired once (confirmed by grepping the build
  log). **Conclusion: Pass 2 was NOT the dominant real-world hub source on this
  dataset.** Diagnosed directly: queried the actual max-out-degree node (42 edges) —
  every edge 44-93m long. Pass 2 has no distance cap by design, so a hub built
  entirely of short edges cannot be Pass 2's doing.
- **Log**: `data/zeeland_pass2cap_build.log`

### #7 — `zeeland_pass0targetcap.sqlite` — same as #6 but `--pass0-target-fanin-cap 4`

```
... same as #6's command, plus:
  --pass0-target-fanin-cap 4
```

- **Purpose**: test SPEC-GRAPH-DENSITY.md §6.7 — the actual mechanism traced from
  #6's diagnosis (Pass 0c's Direction A has no target-side fan-in cap, unlike
  Direction B).
- **Result**: 58,215 nodes / **187,551 edges** (−37,002 vs #6) / **0 hubs** (max
  out-degree **12**, down from 42) / `crosses_land=0`. **This is now better than the
  live db (#1) on every measured axis except raw node count**: fewer edges
  (187,551 vs 203,582), zero hubs (vs 5), lower max out-degree (12 vs 33).
- **Additional verification done** (not yet a scripted/repeatable check — ad hoc
  this session):
  - Largest-component-by-edge-length: 85.62%. Isolating `--pass0-target-fanin-cap`'s
    own effect requires comparing against **#6** (86.63%), not #4 — #6 and #7 share
    `--connector-merge-m 5.0`, so only the fan-in cap differs between them (#4 is a
    full-configuration comparator, `--connector-merge-m 0.0` as well, so a #7-vs-#4
    delta would conflate both flags' effects). #6 vs #7: **-1.01pp**, a real but
    small dip. (#4's 86.78% is noted for completeness; not the isolating comparison.)
  - **POI-pair reachability** (767 named POIs common to both #4 and #7, matched by
    name, 293,761 pairs checked): **0 lost, 0 gained** — the edge-length dip above
    does not correspond to any real place-pair losing routability. This project's
    own history (§6.1) already flagged raw edge-length-% as a metric that can look
    like a regression while POI-pair reachability shows none — confirmed again here.
    (This particular check used #4 as the control since that's what was on hand;
    the conclusion — zero reachability loss — doesn't depend on isolating #6 vs #7
    specifically, unlike the edge-length-% comparison above.)
- **Installed live** 2026-09-04 (see deploy notes below).
- **Log**: `data/zeeland_pass0targetcap_build.log`

### #8 — `zeeland_fairwaybufferfix.sqlite` — FAIRWAY_MATCH_BUFFER_M fix, same graph shape as #7

```bash
... identical command to #7 (node/edge/hub/max-out-deg counts match exactly,
confirming the fix changes edge attributes only, not topology):
.venv/bin/python3 nautical_routing_pipeline.py \
  --input-dir data/zeeland_fresh_clip \
  --output data/zeeland_fairwaybufferfix.sqlite \
  --country NL --name "Zeeland" \
  --description "Zeeland province and approaches (Westerschelde, Oosterschelde, Veerse Meer, Grevelingen, Haringvliet, North Sea approach), based on Rijkswaterstaat IENC / ENC data" \
  --tags '["ienc","rws","coastal","inland"]' \
  --url "https://github.com/marcelrv/signalk-router-data" \
  --license "Public Domain (Rijkswaterstaat)" --copyright "Rijkswaterstaat" \
  --depth-ceiling 6.0 \
  --sagitta-cap 75.0 --max-segment-m 2000 \
  --axis-dedup-cap 50.0 \
  --connector-merge-m 5.0 \
  --inland-densify-max-segment-m 120.0 \
  --pass2-max-fanin-per-node 6 \
  --pass0-target-fanin-cap 4
```

- **Bug report**: routeiq's UI showed a route through the Aanloop Westelijke
  Voorhaven Krammersluizen <-> Aanloop Krammersluis approach taking a
  longer-looking path through the main commercial lock instead of what looked
  like a shorter, fairway-marked route.
- **Root cause traced**: NOT a connector-merge-split attribute-copy bug (that
  code copies edge attrs before `cost_factor` is ever computed, so it's a
  non-issue). The actual cause: `calculate_edge_attributes`'s fairway
  cost_factor test is a bare `intersects()` between each final edge's straight
  chord and the fairway/inland-waterways reference layer (a zero-width
  centerline for inland_waterways entries). As PRs #14-#18 increased edge
  density (this corridor: ~98k -> ~187k edges pipeline-wide), each chord got
  shorter and more sensitive to a few metres of skeleton/medial-axis drift off
  the reference line -- silently downgrading `cost_factor` from 0.8 to the 1.2
  default on a growing share of a real fairway's length.
- **Measured on this exact corridor** (same start/end anchors, Dijkstra
  distance*cost_factor, no lock-wait modeled):

  | build | real dist | fairway-tagged (cf=0.8) | weighted cost |
  |---|---|---|---|
  | pre-#18 (`zeeland_pre_main_merge.sqlite.bak`) | 3992 m | 3307 m (83%) | 3467 |
  | live (#7, post #14-#18) | 4036 m | 2363 m (59%) | 3898 (+12%) |
  | **#8 (this fix)** | **3891 m** | **3891 m (100%)** | **3113 (−10% vs pre-#18, −20% vs live)** |

- **Fix**: `FAIRWAY_MATCH_BUFFER_M = 5.0` (nautical_routing_pipeline.py) —
  `calculate_edge_attributes` now buffers the fairway/inland-waterways layer by
  5m in metric CRS before handing it to `_edge_attr_worker`'s per-edge
  `intersects()` test, absorbing routine splitting/skeleton drift without
  needing every chord to land exactly on the reference line.
- **Regression coverage**: `tests/test_fairway_match_buffer.py` (a chord 3m off
  a synthetic inland-waterways line now tags cf=0.8; one 500m off still doesn't).
  Full suite: 211/211 passing, node/edge/hub counts unchanged vs #7 (confirms
  the fix only changes edge attributes, not topology).
- **Installed live** 2026-09-04 (`signalk-routeiq/data/zeeland.sqlite`, previous
  live db backed up to `zeeland_pre_fairwaybufferfix.sqlite.bak`; `signalk-server`
  container restarted).
- **Log**: `data/zeeland_fairwaybufferfix_build.log`

### #9 — `zeeland_nodemerge.sqlite` — `--node-merge-m` fix (SPEC-GRAPH-DENSITY.md §6.8)

```bash
... identical command to #8 plus one new flag:
.venv/bin/python3 nautical_routing_pipeline.py \
  --input-dir data/zeeland_fresh_clip \
  --output data/zeeland_nodemerge.sqlite \
  --country NL --name "Zeeland" \
  --description "Zeeland province and approaches (Westerschelde, Oosterschelde, Veerse Meer, Grevelingen, Haringvliet, North Sea approach), based on Rijkswaterstaat IENC / ENC data" \
  --tags '["ienc","rws","coastal","inland"]' \
  --url "https://github.com/marcelrv/signalk-router-data" \
  --license "Public Domain (Rijkswaterstaat)" --copyright "Rijkswaterstaat" \
  --depth-ceiling 6.0 \
  --sagitta-cap 75.0 --max-segment-m 2000 \
  --axis-dedup-cap 50.0 \
  --connector-merge-m 5.0 \
  --inland-densify-max-segment-m 120.0 \
  --pass2-max-fanin-per-node 6 \
  --pass0-target-fanin-cap 4 \
  --node-merge-m 5.0
```

- **Bug fixed**: SPEC-GRAPH-DENSITY.md §6.8 — `_get_or_create_node` dedupes purely by
  `(round(lon, 5), round(lat, 5))`, a ~1.1m grid at this latitude. Independent
  node-creation call sites computing "the same" real-world junction point via
  different geometric paths routinely land a metre or two apart, producing
  permanently distinct nodes joined by a near-zero-length stub edge — confirmed live
  on #8 (this build's exact `--node-merge-m 0.0` control): 1,047 edges under 3m
  network-wide, 12 of them at the Krammersluis Noord/Zuid junction alone.
- **Fix**: `--node-merge-m` generalizes `connector_merge_m`'s (§6.5) tolerance-merge
  pattern to `_get_or_create_node` itself via a grid-bucket spatial index
  (`_node_merge_grid`/`_find_nearby_node`/`_register_node_in_merge_grid`) — every
  call site now reuses an existing node within tolerance instead of relying purely on
  exact-rounding coincidence. `0.0` (default) is unchanged/byte-identical; this build
  uses `5.0m`, matching `connector_merge_m`'s own recommended value.
- **Measured against #8** (same input clip, same flags otherwise):

  | build | nodes | edges | edges <3m (network-wide) | edges <3m in Krammersluis junction bbox | hubs (od>30) | max out-deg |
  |---|---|---|---|---|---|---|
  | #8 (`--node-merge-m 0.0`) | 58,215 | 187,551 | 1,047 (0.56%) | 12 | 0 | 12 |
  | **#9 (`--node-merge-m 5.0`)** | **57,264** | **185,155** | **221 (0.12%)** | **0** | **0** | **11** |

  `_get_or_create_node` calls: 105,208 total, 49,451 (47%) reused an existing node
  within 5m instead of minting a new one (most of these are ordinary exact-coincident
  shared-vertex reuse the old rounding dict already handled fine, not all newly-
  deduped duplicates — the meaningful signal is the topology delta above, not this
  raw count). At the specific Krammersluis Noord/Zuid junction bbox (`lat
  51.65942-51.66442, lon 4.15834-4.16634`) the sub-3m edge count that motivated §6.8
  goes from 12 to **0** — every stub edge at the exact junction the bug report traced
  to is gone. Network-wide sub-3m count drops 79% (1,047 -> 221); the 221 remaining
  are all also under 1.5m, consistent with these being genuine short skeleton
  segments rather than rounding-grain duplicates. Hub count and max out-degree are
  unaffected (still 0 hubs; max out-degree improves slightly, 12 -> 11) and
  `crosses_land` stays 0 in both builds — confirms the fix removes near-duplicate
  topology without introducing new connectivity problems.
- **Regression coverage**: `tests/test_node_merge.py` (14 tests: default-disabled
  parity, tolerance merge/no-merge, diagonal-neighbor-cell lookup, stale-node
  pruning, context tagging, `_validate_node_merge_m` bounds). Full suite: 226/226
  passing (227/227 once the CodeRabbit grid-index fix below adds its own test).
- **Follow-up (CodeRabbit, PR #20): grid-index correctness fix, re-verified
  byte-identical.** `_register_node_in_merge_grid`/`_find_nearby_node` originally
  derived the longitude grid-cell size from each POINT's own raw latitude via
  `cos(lat)`. At high latitude AND high `|longitude|` this is unsafe: dividing a
  large `lon` by a tiny cell size means even the sub-cell latitude spread between
  two points genuinely within tolerance can shift `cos(lat)` enough to move the
  cell index by more than one, outside the 3x3 neighbour scan (confirmed: two
  points ~24m apart at 70N/120E, well inside a 25m tolerance, landed 2 grid cells
  apart). Fixed by keying the longitude cell size off each latitude BUCKET's
  canonical (centre) latitude instead of each point's raw latitude, and
  recomputing the query's longitude index once per scanned latitude row. Added
  `tests/test_node_merge.py::test_enabled_high_latitude_nonzero_longitude_points_
  still_merge` (fails against the pre-fix code, passes after). Not a real-world
  Zeeland bug -- rebuilt `zeeland_nodemerge.sqlite` with the fixed code and
  confirmed byte-identical nodes/edges tables (same SHA-256 hash) to the build
  logged above, exactly as expected: Zeeland's lon 3-7/lat 51-53 range is nowhere
  near where the old per-point `cos(lat)` math actually diverges. No new build
  number or redeploy needed.
- **Installed live** 2026-09-04 (`signalk-routeiq/data/zeeland.sqlite`, previous
  live db backed up to `zeeland_pre_nodemergefix.sqlite.bak`; `signalk-server`
  container restarted). Superseded same-day by #10 below.
- **Log**: `data/zeeland_nodemerge_build.log`

### #10 — `zeeland_axisdedup_wide.sqlite` — §6.9 follow-up: fix a real crisscross at Vossemeersebrug/Tholen narrows

```bash
... identical command to #9 plus four new flags:
.venv/bin/python3 nautical_routing_pipeline.py \
  --input-dir data/zeeland_fresh_clip \
  --output data/zeeland_axisdedup_wide.sqlite \
  --country NL --name "Zeeland" \
  --description "Zeeland province and approaches (Westerschelde, Oosterschelde, Veerse Meer, Grevelingen, Haringvliet, North Sea approach), based on Rijkswaterstaat IENC / ENC data" \
  --tags '["ienc","rws","coastal","inland"]' \
  --url "https://github.com/marcelrv/signalk-router-data" \
  --license "Public Domain (Rijkswaterstaat)" --copyright "Rijkswaterstaat" \
  --depth-ceiling 6.0 \
  --sagitta-cap 250.0 --max-segment-m 2000 \
  --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 \
  --min-navmesh-radius-m 1200.0 \
  --connector-merge-m 5.0 \
  --inland-densify-max-segment-m 120.0 \
  --pass2-max-fanin-per-node 6 \
  --pass0-target-fanin-cap 4 \
  --node-merge-m 5.0
```

- **Bug report**: a screenshot at the Vossemeersebrug bridge (Nieuw-Vossemeer/Tholen
  narrows, ~51.584N 4.201E) showed a dense crisscross/triangulated web of nodes and
  edges paralleling a real `inland_waterways` axis line through a genuinely narrow
  fairway — the same visual symptom as §6.8's Krammersluis case, but this junction
  has no lock and every node/edge involved was already confirmed correct topology
  (0 hubs, 0 crosses_land) -- so it traces to §6.9's documented mechanism, not a new
  bug: axis-dedup's suppression tolerance is `clip(0.5 * local_width, 5, cap)`,
  which for a 22-79m-wide channel (measured directly against #9) only reaches
  11-40m -- not enough to suppress skeleton branches near the banks, so they stay
  unsuppressed alongside the real axis line.
- **New CLI flags added** (this commit, `nautical_routing_pipeline.py`):
  `--axis-dedup-fraction`, `--axis-dedup-floor-m`, `--min-navmesh-radius-m` --
  `ClassificationConfig` fields that already existed but had no CLI override before
  now (only `--axis-dedup-cap`/`--sagitta-cap` were exposed). All three default to
  `None` (= keep the dataclass default), so omitting them reproduces prior builds
  byte-for-byte.
- **Root cause confirmed, then ruled OUT one hypothesis**: queried the live db (#9)
  directly at the Vossemeersebrug bbox before building anything -- every node there
  is `node_kind=point` (skeleton-derived), not `navmesh_vertex`, and
  `min_navmesh_radius_m=800` already correctly excludes this channel from navmesh
  treatment. So `--min-navmesh-radius-m` (raised to 1200 here anyway, for general
  robustness) is NOT what fixes this specific symptom -- `--axis-dedup-floor-m` is:
  flooring the suppression tolerance at a flat 100m regardless of local width
  directly closes the 11-40m gap measured above.
- **Measured against #9** (same input clip, `--sagitta-cap`/`--axis-dedup-cap`/
  `--axis-dedup-floor-m`/`--min-navmesh-radius-m` raised, everything else unchanged):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | #9 | 57,264 | 185,155 | 0 | 11 | 0 |
  | **#10** | **42,092** | **124,689** | **0** | **14** | **0** |

  Vossemeersebrug bbox (`lat 51.578-51.590, lon 4.192-4.211`): **52 -> 29 nodes,
  182 -> 74 edges**, and the remaining topology is mostly a clean out-degree-2
  chain (a simple line) instead of a fan -- the few remaining degree 3-6 nodes are
  legitimate bridge/waterway-crossing junctions, not rounding artifacts. Krammersluis
  junction (§6.8's own case, `lat 51.657-51.667, lon 4.158-4.166`) also improved
  further: 62 -> 44 nodes, 211 -> 164 outgoing edges, still 0 sub-3m stub edges.
  **POI-pair reachability** (767 named POIs common to #9 and #10, matched by name):
  1 lost from the main component (a minor buoy/cycle-path marker), 3 gained (three
  real marinas) -- net neutral to positive, no real regression. Largest-component
  share: 84.18% (#9) -> 82.26% (#10), the expected small dip from removing ~26% of
  total nodes, not a connectivity problem (confirmed by the reachability check
  above).
- **Operational note**: a first attempt at `--axis-dedup-cap 150.0` was OOM-killed
  by the host (a shared machine also running signalk-server, openhab, grafana, and
  other live services) partway through the wide/narrow polygon split step -- dialed
  back to `--axis-dedup-cap 100.0` (matching the bug report's own "100m or so"
  estimate) and it completed cleanly, peaking around 3.4GB RSS with 7+GB still
  available system-wide.
- **Installed live** 2026-09-04 (`signalk-routeiq/data/zeeland.sqlite`, previous
  live db backed up to `zeeland_pre_axisdedupwide.sqlite.bak`; `signalk-server`
  container restarted).
- **Log**: `data/zeeland_axisdedup_wide_build.log`

### #11/#12 — `--inland-resample-max-segment-m` — REGRESSED, NOT deployed (kept live on #10)

**Bug report**: even after #10, a live screenshot showed node spacing along a plain
open-water fairway still ~71m, not the requested ~250m. Traced directly (queried
#10's own db before building anything): that edge's nodes have `source_id=14`
(`inland_waterways`), `edge_kind_id=0` (`centerline`) -- it's a RAW
`_build_inland_network` vertex-to-vertex ingestion edge, not a skeleton edge.
`--sagitta-cap`/`--max-segment-m` only govern `_resample_long_skeleton_edges`
(skeleton resampling) -- `_build_inland_network` has NO consolidation mechanism at
all, so it reproduces the source IENC line's own raw digitization density (~70-100m
here) regardless of either flag. This is a real gap, not user error.

**Fix implemented**: new `--inland-resample-max-segment-m` flag (default 0.0,
disabled) plus `_resample_inland_waterways`, reusing
`_resample_long_skeleton_edges`'s plain cumulative-arc-length walk to consolidate
consecutive EXISTING vertices of each `inland_waterways` line down to this many
metres, run once at parse time (same pattern as `_densify_inland_waterways`, the
complementary opposite operation). Unit-tested in isolation
(`tests/test_inland_resample.py`, 15 tests, all passing) -- the function correctly
does what it says on a `GeoDataFrame` in isolation.

**Regression found before deploying either build** (POI-pair reachability check,
same methodology as #7-#10, run against #10 as the control BEFORE any deploy):

  | build | cap | nodes | edges | POIs lost from main component |
  |---|---|---|---|---|
  | #11 | 250m | 29,006 | 80,064 | **21**, incl. Krammersluizen, Jachtensluis Krammersluizen, 4 Middelburg harbour POIs, Bruinisse/Ellewoutsdijk marinas, 3 bridges |
  | #12 | 100m | 31,457 | 68,884 | **9** (Krammersluis Noord/Zuid actually recovered at this cap; Middelburg harbours and 2 bridges still lost) |

**Root cause of the regression**: traced one lost POI (Krammersluizen) directly --
its nearest node in #11 sits in an isolated 8-node island, disconnected from the
34,623-node main component. This is the SAME class of problem §6.1/§6.2 already
fought for skeleton/navmesh density: `_stitch_component_pieces`' Pass 0c/0d/Pass 2
and `_connect_waterway_crossing`'s own candidate search sample from the
`inland_waterways` vertex list -- reducing that list's density (exactly what
`_resample_inland_waterways` does) starves those passes of candidates near real
harbours/locks/bridges, the same way sparser skeleton/navmesh sampling did before.
Unlike `_get_or_split_inland_segment`'s TRUE segment-projection (unaffected by
vertex density when `connector_merge_m > 0`), the STITCHING passes' own sampling
apparently is not immune -- confirmed empirically (9 losses even at 100m, a cap
well inside `connector_merge_m`'s own established "safe" range), not fully
explained/fixed here.

**Decision: do not ship at any cap tried so far.** The flag/function stay in the
codebase (default-off, unit-tested, don't change any existing build) since the root
cause diagnosis (raw inland ingestion has no consolidation mechanism) is correct and
real -- but actually closing the user's "~250m spacing" request safely needs either
a much smaller cap with a full zero-regression re-verification, or a real fix to the
stitching passes' candidate sampling (mirroring the depth of work §6.1-§6.7 already
put into the equivalent skeleton/navmesh problem), not just a quick line-
simplification pass. Left as an open follow-up. **Live db is still #10** --
`zeeland_pre_axisdedupwide.sqlite.bak`'s replacement was never touched by this
investigation.
- **Logs**: `data/zeeland_inlandresample_build.log` (#11, 250m),
  `data/zeeland_inlandresample100_build.log` (#12, 100m)

### #13 — `us_east_ct_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/CT

```
./build_region.sh us-east-ct-stitched-v2 --states CT --source-region us-east-coast \
  --clip-bbox "-73.81,40.84,-71.83999999999999,41.41" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: this is the first of 19 US East Coast regional rebuilds applying
  Zeeland build #10's exact verified-safe tuning config (this file, builds #7-#12)
  to the already-live US East Coast region set, via the new `build_region.sh
  --extra-pipeline-args` passthrough (this commit's parent, `8652bda`).
  `--inland-resample-max-segment-m` is deliberately NOT included (builds #11/#12
  above document a real connectivity regression from it; never shipped).
- **Input**: `data/geojson/ct_reclip` already existed from a prior session, but
  `build_region.sh --states CT --clip-bbox ... --overlap-deg 0.01` re-derives its
  own `data/geojson/us-east-ct-stitched-v2_clipped` from `data/raw/us-east-coast/CT`
  (already local, no NOAA download) rather than reusing `ct_reclip` directly — cheap
  and expected per this task's brief. Clip bbox and 0.01deg overlap read from this
  region's original `data/us_east_ct_clip.log`.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_ct_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced — same "unknown baseline"
  situation as Zeeland row #1 above):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 31,278 | 74,235 | n/a (not measured) | n/a | n/a |
  | **v2 (this build, tuning applied)** | **20,359** | **47,902** | **0** | **19** | **0** |

  Node/edge counts are NOT a controlled A/B (different, unreproduced source
  recipe/flags) — recorded for context only, same caveat as Zeeland's row #1
  comparisons. Zero hubs and zero crosses_land confirm the tuning config produces
  the same clean topology on this dataset as it did on Zeeland.
- **Installed live** 2026-09-06 (deployed in a batch with the other successful
  regions at the end of this rollout — see the batch deploy note below).
- **Logs**: `data/us_east_ct_stitched_v2_run.log` (full script output),
  `data/us_east_ct_stitched_v2_build.log` (pipeline step only), `data/us_east_ct_stitched_v2_clip.log`.

### #14 — `us_east_de_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/DE

```
./build_region.sh us-east-de-stitched-v2 --states DE --source-region us-east-coast \
  --clip-bbox "-76.01,38.39,-74.83999999999999,39.809999999999995" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 2/19 of the US East Coast tuning rollout (see #13).
- **Result vs currently-live** (`signalk-routeiq/data/us_east_de_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 28,672 | 67,192 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **19,420** | **45,378** | **0** | **16** | **0** |

  No errors/tracebacks in the build log; 0 hubs, 0 crosses_land.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_de_stitched_v2_run.log`, `data/us_east_de_stitched_v2_build.log`.

### #15 — `us_east_fl_atl_n1a_stitched_v2.sqlite` — FAILED, OOM-killed twice, NOT deployed

```
./build_region.sh us-east-fl-atl-n1a-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-81.91000000000001,29.79,-79.39,30.71" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 3/19 of the US East Coast tuning rollout (see #13).
- **Attempt 1**: `free -h` showed ~11GB available before launch. Killed (SIGKILL,
  exit 137) ~2m18s in, during "Building base network topology" (the
  `_build_inland_network`/densify/skeleton stage). No output `.sqlite` produced.
- **Attempt 2 (the one retry allowed per this task's brief)**: `free -h` showed
  ~11-12GB available before relaunch. Killed again (exit 137), this time even
  earlier (~1m20s in), same "Building base network topology" stage. No output
  `.sqlite` produced.
- **Decision**: two failures — per this task's operational constraint, do not
  retry a third time. Logged as failed, moving on to the next region. The
  currently-live `signalk-routeiq/data/us_east_fl_atl_n1a_stitched.sqlite` is left
  untouched (not backed up, not replaced).
- **Not yet root-caused**: unlike Zeeland's own single OOM incident (#10's
  operational note — resolved by dialing `--axis-dedup-cap` back from 150 to 100),
  this region hit the SAME exact flag values that worked fine on CT (#13) and DE
  (#14) and on all of Zeeland's builds. FL's raw per-state ENC data is
  substantially larger than CT/DE's (live `us_east_fl_atl_n1a_stitched.sqlite` is
  comparable in size to CT/DE's, but the clip covers a busier stretch of Florida
  coast with more `land`/`coastal_water`/`obstacles` features feeding the
  topology-build stage) — plausibly just a bigger peak-RSS working set for this
  particular clip's raster/skeleton step colliding with other live services'
  memory usage on this shared host at the time. Left as an open follow-up if this
  region needs the tuning applied later (e.g. retry at a quieter time, or
  investigate whether `--min-navmesh-radius-m`/tiling behaves worse on this
  clip's geometry).
- **Logs**: `data/us_east_fl_atl_n1a_stitched_v2_run.log` (both attempts
  overwrite the same file; only the second/final attempt's content survives).

### #16 — `us_east_fl_atl_n1b_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/FL (n1b)

```
./build_region.sh us-east-fl-atl-n1b-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-81.91000000000001,28.79,-79.39,29.91" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 4/19 of the US East Coast tuning rollout (see #13).
  Succeeded cleanly this time despite being the same FL raw data as #15's
  OOM-killed n1a clip — a different (adjacent, further south) bbox from the same
  state, so the working-set size clearly varies a lot by clip, not just by state.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_fl_atl_n1b_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 11,394 | 27,732 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **9,358** | **17,441** | **0** | **15** | **0** |

  No errors/tracebacks in the build log; 0 hubs, 0 crosses_land.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_fl_atl_n1b_stitched_v2_run.log`, `data/us_east_fl_atl_n1b_stitched_v2_build.log`.

### #17 — `us_east_fl_atl_n2_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/FL (n2)

```
./build_region.sh us-east-fl-atl-n2-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-81.11,26.889999999999997,-79.39,28.91" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 5/19 of the US East Coast tuning rollout (see #13).
- **Result vs currently-live** (`signalk-routeiq/data/us_east_fl_atl_n2_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 42,829 | 103,952 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **29,242** | **67,761** | **0** | **16** | **0** |

  No errors/tracebacks in the build log; 0 hubs, 0 crosses_land.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_fl_atl_n2_stitched_v2_run.log`, `data/us_east_fl_atl_n2_stitched_v2_build.log`.

### #18 — `us_east_fl_atl_s_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/FL (s) — HUB-COUNT CAVEAT

```
./build_region.sh us-east-fl-atl-s-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-81.91000000000001,24.09,-79.08999999999999,27.01" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 6/19 of the US East Coast tuning rollout (see #13). Build
  itself completed cleanly (exit 0, no errors/tracebacks, 10m16s), `crosses_land=0`
  (the hard safety invariant this project tracks is unaffected).
- **Anomaly found**: unlike every other region in this rollout so far (#13,#14,#16,
  #17 all 0 hubs), this build has **4 hub nodes** (out-degree>30), max out-degree
  **138** — closer to Zeeland's pre-`--pass0-target-fanin-cap`-fix era (builds
  #2-#6, this file) than to the fixed builds this rollout is meant to replicate.
  Diagnosed the top hub directly: node `412686405841089` at
  `lat 24.63511, lon -81.58911` (Florida Keys, near Key West — this region's clip
  covers open Gulf-of-Mexico/Atlantic approach water). 135/138 of its edges are
  `edge_kind_id=1` (`navmesh_boundary`), each 240-495m long, `node_kind_id=0`
  (`point`, not `navmesh_vertex`). The other 3 hubs (94/57/51 out-degree,
  lat/lon 24.628-24.631/-81.589 to -81.592, ~150-350m from the first) show the
  same pattern (majority `navmesh_boundary` edges from a plain `point`-kind node).
  This looks like the same class of fan-out stitching problem
  `--pass0-target-fanin-cap` (SPEC-GRAPH-DENSITY.md §6.7) was built to fix on
  Zeeland, but recurring here specifically at a `navmesh_boundary`
  (large-open-water navmesh perimeter) contact point rather than the plain
  skeleton/inland stitching passes §6.7 targeted — i.e. `--min-navmesh-radius-m
  1200.0` classifying a large chunk of this clip's open water as navmesh (likely a
  large reef/channel/open-Gulf polygon near the Keys) may be exposing a fan-in
  path into navmesh boundary stitching that Zeeland's own dataset (smaller,
  mostly-inland Dutch coastal waters) never exercised at this scale. NOT
  root-caused further here — out of scope for this rollout task, which is
  applying Zeeland's already-verified config as-is, not re-tuning per region.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_fl_atl_s_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 120,436 | 274,938 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **56,301** | **135,182** | **4** | **138** | **0** |

  Edge count still drops by more than half vs the live baseline (same pattern as
  every other region in this rollout), and `crosses_land=0` — this is still a
  valid, land-safe routable graph, just with a worse hub-count/max-out-degree
  profile than the rest of this rollout (and than the live db's own unknown
  baseline, whose hub count was never measured — see table).
- **Decision**: deployed anyway, per this task's brief (deploy every region whose
  build *succeeds*; a soft quality regression on 4 nodes out of 56,301 is not a
  build failure or a land-crossing safety issue). Flagged here as an open
  follow-up: worth a dedicated investigation (mirroring #6/#7's Pass-0
  diagnosis work) into navmesh-boundary fan-in specifically, if this recurs on
  other large-open-water FL/Gulf regions.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_fl_atl_s_stitched_v2_run.log`, `data/us_east_fl_atl_s_stitched_v2_build.log`.

### #19 — `us_east_fl_gulf_mid_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/FL (gulf_mid)

```
./build_region.sh us-east-fl-gulf-mid-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-83.81,27.189999999999998,-82.19,29.21" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 7/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 14), unlike #18.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_fl_gulf_mid_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 68,876 | 170,296 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **36,755** | **87,270** | **0** | **14** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_fl_gulf_mid_stitched_v2_run.log`, `data/us_east_fl_gulf_mid_stitched_v2_build.log`.

### #20 — `us_east_fl_gulf_pan_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/FL (gulf_pan)

```
./build_region.sh us-east-fl-gulf-pan-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-88.16000000000001,28.99,-83.78999999999999,30.91" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 8/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 25).
- **Result vs currently-live** (`signalk-routeiq/data/us_east_fl_gulf_pan_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 34,709 | 82,809 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **21,617** | **45,942** | **0** | **25** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_fl_gulf_pan_stitched_v2_run.log`, `data/us_east_fl_gulf_pan_stitched_v2_build.log`.

### #21 — `us_east_fl_gulf_sw_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/FL (gulf_sw) — SAME Key West hub caveat as #18

```
./build_region.sh us-east-fl-gulf-sw-stitched-v2 --states FL --source-region us-east-coast \
  --clip-bbox "-82.91000000000001,24.09,-81.58999999999999,27.41" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 9/19 of the US East Coast tuning rollout (see #13). Build
  succeeded cleanly (exit 0, no errors, `crosses_land=0`), but has the same
  `navmesh_boundary` fan-in hub anomaly as #18.
- **Confirms #18's diagnosis**: 3 hub nodes, all at `lat 24.628-24.636,
  lon -81.589` — **the same Key West location** as #18's 4 hubs. This region's
  clip bbox (`-82.91..-81.59, 24.09..27.41`) genuinely overlaps #18's
  (`-81.91..-79.09, 24.09..27.01`) between lon -81.91 and -81.59 (the Atlantic/
  Gulf split intentionally double-covers the Keys from both sides) — so this is
  the SAME real-world geometry producing the SAME `node_kind_id=0`
  (`point`)-with-`navmesh_boundary`-fan-out pattern, not two independent
  coincidences. Reinforces #18's hypothesis: a large open-water navmesh polygon
  near Key West (`--min-navmesh-radius-m 1200.0`) is exposing a fan-in path this
  rollout's config doesn't fully cap, specific to this location.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_fl_gulf_sw_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 54,646 | 130,532 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **30,739** | **66,692** | **3** | **134** | **0** |

- **Decision**: deployed anyway, same reasoning as #18 (build succeeded, land-safe,
  soft quality issue confined to 3 nodes at one location). Both #18 and #21's
  Key West hub findings should be revisited together in any future follow-up.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_fl_gulf_sw_stitched_v2_run.log`, `data/us_east_fl_gulf_sw_stitched_v2_build.log`.

### #22 — `us_east_ma_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/MA

```
./build_region.sh us-east-ma-stitched-v2 --states MA --source-region us-east-coast \
  --clip-bbox "-71.71000000000001,41.190000000000005,-69.78999999999999,42.91" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 10/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 22).
- **Result vs currently-live** (`signalk-routeiq/data/us_east_ma_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 54,767 | 139,185 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **36,898** | **87,637** | **0** | **22** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_ma_stitched_v2_run.log`, `data/us_east_ma_stitched_v2_build.log`.

### #23 — `us_east_md_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/MD

```
./build_region.sh us-east-md-stitched-v2 --states MD --source-region us-east-coast \
  --clip-bbox "-77.39,37.89,-74.69,39.62" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 11/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 16). Longest build so far
  (12m11s) — Chesapeake Bay's dense inland waterway network.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_md_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 92,169 | 203,448 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **54,766** | **129,606** | **0** | **16** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_md_stitched_v2_run.log`, `data/us_east_md_stitched_v2_build.log`.

### #24 — `us_east_me_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/ME

```
./build_region.sh us-east-me-stitched-v2 --states ME --source-region us-east-coast \
  --clip-bbox "-70.81,43.04,-66.89,45.059999999999995" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 12/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 17). Longest build so far
  (13m38s) — Maine's famously convoluted, island-heavy coastline.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_me_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 101,452 | 263,406 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **52,994** | **141,005** | **0** | **17** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_me_stitched_v2_run.log`, `data/us_east_me_stitched_v2_build.log`.

### #25 — `us_east_nc_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/NC

```
./build_region.sh us-east-nc-stitched-v2 --states NC --source-region us-east-coast \
  --clip-bbox "-78.81,33.59,-75.19,36.71" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 13/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 16). Longest build so far
  (15m45s).
- **File-size note**: output file is 153MB, much larger than the live db's 63MB
  despite fewer nodes/edges (46,839/112,311 here vs live's 92,874/218,245) — checked
  directly, this is NOT duplicated data or a regression: `navmesh_regions` has 520
  polygon-geometry rows (NC's Outer Banks/barrier-island coastline is unusually
  intricate), which drives file size independently of node/edge count. All other
  table row counts look normal for this region's size.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_nc_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 92,874 | 218,245 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **46,839** | **112,311** | **0** | **16** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_nc_stitched_v2_run.log`, `data/us_east_nc_stitched_v2_build.log`.

### #26 — `us_east_nh_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/NH

```
./build_region.sh us-east-nh-stitched-v2 --states NH --source-region us-east-coast \
  --clip-bbox "-70.86,42.830000000000005,-70.08999999999999,43.12" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 14/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 14). Smallest/fastest region
  (48s) — NH's tiny coastline.
- **Result vs currently-live** (`signalk-routeiq/data/us_east_nh_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 5,117 | 12,894 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **8,524** | **15,333** | **0** | **14** | **0** |

  Note: this is the first region where the v2 build's node/edge counts went UP
  vs the live baseline (not down, unlike every other region so far) — expected
  given the unknown live recipe (row #1's own caveat applies equally here: not a
  controlled comparison). No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_nh_stitched_v2_run.log`, `data/us_east_nh_stitched_v2_build.log`.

### #27 — `us_east_nj_stitched_v2.sqlite` — Zeeland's verified tuning config, rolled out to US East Coast/NJ

```
./build_region.sh us-east-nj-stitched-v2 --states NJ --source-region us-east-coast \
  --clip-bbox "-75.36,38.79,-73.30999999999999,40.559999999999995" --overlap-deg 0.01 \
  --stitch-registry data/seam_registry.sqlite \
  --extra-pipeline-args "--sagitta-cap 250.0 --max-segment-m 2000 --axis-dedup-cap 100.0 --axis-dedup-floor-m 100.0 --min-navmesh-radius-m 1200.0 --connector-merge-m 5.0 --inland-densify-max-segment-m 120.0 --pass2-max-fanin-per-node 6 --pass0-target-fanin-cap 4 --node-merge-m 5.0"
```

- **Purpose**: region 15/19 of the US East Coast tuning rollout (see #13). Clean
  build — no hub-count anomaly (0 hubs, max out-deg 17).
- **Result vs currently-live** (`signalk-routeiq/data/us_east_nj_stitched.sqlite`,
  original build recipe/commit unknown/unreproduced):

  | build | nodes | edges | hubs (od>30) | max out-deg | crosses_land |
  |---|---|---|---|---|---|
  | live (pre-tuning, unknown recipe) | 45,523 | 109,553 | n/a | n/a | n/a |
  | **v2 (this build, tuning applied)** | **25,973** | **62,027** | **0** | **17** | **0** |

  No errors/tracebacks in the build log.
- **Installed live**: deferred to the end-of-rollout batch deploy (see #13).
- **Logs**: `data/us_east_nj_stitched_v2_run.log`, `data/us_east_nj_stitched_v2_build.log`.

## Resolved: why the live db (#1) had only 5 hubs when #2-#6 had 56-231

Traced across #2-#7 (2026-09-04 session): `_ensure_coastal_connectivity`'s Pass 2 was
the first suspect (§6.6) but confirmed NOT the cause (#6). The real mechanism (§6.7,
confirmed by #7) is `_stitch_component_pieces`' Pass 0c/0d Direction A having no
target-side fan-in cap — Direction B already had one, Direction A didn't. #7's build
resolves this to 0 hubs, better than #1's own 5. #1's own exact flags are still
unknown/unreproduced, so it's not established that #1 used equivalent caps — #7
reaches a better result via a different, now-understood mechanism.
