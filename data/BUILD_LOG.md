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
  passing.
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

## Resolved: why the live db (#1) had only 5 hubs when #2-#6 had 56-231

Traced across #2-#7 (2026-09-04 session): `_ensure_coastal_connectivity`'s Pass 2 was
the first suspect (§6.6) but confirmed NOT the cause (#6). The real mechanism (§6.7,
confirmed by #7) is `_stitch_component_pieces`' Pass 0c/0d Direction A having no
target-side fan-in cap — Direction B already had one, Direction A didn't. #7's build
resolves this to 0 hubs, better than #1's own 5. #1's own exact flags are still
unknown/unreproduced, so it's not established that #1 used equivalent caps — #7
reaches a better result via a different, now-understood mechanism.
