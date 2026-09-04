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

| # | Date | Commit | Output file | Nodes | Edges | Hubs (od>30) | Max out-deg | crosses_land | Installed live? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | (unknown, ~2026-09-03) | unknown | `signalk-routeiq/data/zeeland.sqlite` | 64,717 | 203,582 | 5 | 33 | 0 | **YES (currently live)** |
| 2 | 2026-09-04 | `38a09ed` | `data/zeeland_connectormerge.sqlite` | 45,765 | 162,004 | 231 | 222 | 0 | no |
| 3 | 2026-09-04 | `38a09ed` | `data/zeeland_connectormerge2.sqlite` | 58,215 | 224,553 | 56 | 42 | 0 | no |
| 4 | 2026-09-04 | `38a09ed` | `data/zeeland_control_nomerge.sqlite` | 56,433 | 217,787 | 65 | 42 | 0 | no |
| 5 | 2026-09-04 | `38a09ed` | `data/zeeland_dedup25.sqlite` | 59,544 | 229,379 | 57 | 42 | 0 | no |

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

## Open question — not yet resolved

**Why does the live db (#1) have only 5 hubs when every reproduction attempt here
(#2-#5) has 56-231?** Root cause traced (2026-09-04 session) to
`_ensure_coastal_connectivity`/`_stitch_component_pieces` candidate selection
concentrating stitching edges onto a small set of inland nodes — confirmed NOT
sensitive to axis-dedup-cap (#3 vs #5). Not yet resolved: whether it's sensitive to
the exact source clip, to `connector_merge_m`/`inland_densify_max_segment_m`
interacting with the stitching pass's own candidate list, or something else entirely
about how #1 was actually built. This is the next thing to investigate before
trusting any new build's hub count as representative — see session picking this back
up for progress.
