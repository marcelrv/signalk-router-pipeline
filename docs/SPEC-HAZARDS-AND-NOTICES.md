# Hazards and notices in the routing DB

Status: design accepted 2026-10-18; steps 1-5 implemented (step 5, main pipeline: 2026-10-18, not installed).

## Problem

Agent-authored navline networks (`navline_agent/`) ignore restricted areas, wrecks, obstructions and
mariculture. The agent's tiles show only a faint outline of restricted areas, `check` does not test
any of them, and `navlines_to_db.py` writes neither `crosses_obstacle` nor any hazard depth. Wrecks
are not extracted by either pipeline (`enc_preprocessor.py` has no `WRECKS` mapping), yet the raw NL
cells hold 720 of them.

Treating every charted object as a no-go would be just as wrong. Most wrecks are deep, mussel plots are
legal to sail over, and a kite-surf zone is information. Each object therefore gets a **tier** from its
structured S-57 attributes, and its full chart text is passed through **unclassified** in a notices
table. That way nothing has to be rebuilt when the router later starts showing notices.

## Tiers

Decided only from structured attributes (RESTRN, CATREA, WATLEV, VALSOU, CATWRK, EXPSOU, PERSTA/PEREND, DATEND).
Free text (INFORM/NINFOM/OBJNAM) is never parsed to pick a tier: there are 1000+ variants. The single
exception is the existing `entry prohibited` / `passage prohibited` / `toegang verboden` phrase match,
which is kept as a fallback for areas that lack RESTRN.

| tier | severity | DB effect | examples |
|---|---|---|---|
| prohibited | 3 | `crosses_obstacle = 1`, except time-limited areas (see Seasonal areas) | RESTRN 7 (entry prohibited), all year or time-limited; passage-prohibited text; hulks; wreck/obstruction/rock with WATLEV 1, 2 or 5 and no VALSOU; wreck with mast showing (CATWRK 5) |
| depth | 2 | `min_depth = min(min_depth, hazard depth)` | submerged wreck/obstruction/rock that is dangerous by the rule below |
| caution | 1 | none | RESTRN 8 (entry restricted); caution areas (CTNARE); a prohibited area downgraded on an edge (graze, charted axis) |
| info | 0 | none | mariculture without restriction; water-sport zones; anchoring/fishing prohibited (RESTRN 1-6); non-dangerous or deep wrecks; dredged areas |

### Depth rule for point hazards (S-52 isolated danger, adapted)

Draft and tide are checked at runtime against `edges.min_depth`, so the DB only needs the hazard's own
least depth on the edges near it:

- VALSOU known and real (see the UNKNOWN trap below): depth hazard with that depth, **unless** VALSOU is
  greater than or equal to the charted depth at the object. In that case the depth raster already covers it,
  and it becomes info. "Charted depth" must be the same per-point DEPARE value the DB writer uses for
  `edges.min_depth` (main pipeline: `_depare_point_depth`, i.e. scale-aware choice among overlapping cells and
  the -50 drying placeholder replaced by DRVAL2; navline path: `World.depth_at`), otherwise a hazard can be
  dropped as "covered" by a depth the edges never use.
- VALSOU unknown:
  - CATWRK 1 (non-dangerous) or EXPSOU 1 (within the surrounding depth range): info.
  - CATWRK 2 (dangerous) or EXPSOU 2 (shoaler than surroundings): depth hazard at 0.0 m. Decided
    2026-10-18 (Marcel): this stays a depth hazard with `min_depth` 0.0, not a block; the router's tide check
    may let a boat pass at high water, the same as over a drying bank.
  - Otherwise, WATLEV 3 or 4 (submerged, for example CATWRK 3 "distributed remains"): info. Following
    the S-52 convention, the unknown least depth is taken as the surrounding charted depth. The existing
    `_obstruction_depth_disposition` fallback of 0.0 m is **wrong** here: in the step 1 count it would
    have closed the Pas van Terneuzen (20 m of water) and the Pas van Rilland (10 m) for every boat,
    because of two CATWRK 3 wrecks with unknown depth.
  - WATLEV 1, 2, 5, or missing: prohibited (as now).
- A point hazard applies to an edge that passes within `HAZARD_RADIUS_M` (default 30 m; the current
  `OBSTACLE_BUFFER_METERS = 5` is too tight for an agent centre-line).

**UNKNOWN trap:** Dutch cells store `VALSOU = 'UNKNOWN'` on many wrecks and obstructions, and GDAL reads
it as `0.0` with only a warning. Extraction must treat VALSOU 0.0 with WATLEV 3 ("always under water"
at 0 m is self-contradictory) as unknown, and store `null`, never 0. Do not detect it by catching the
GDAL warning: GDAL rate-limits its warnings per dataset, and in the step 1 prototype the flag landed on
the wrong feature (a wreck with a real 19.2 m sounding).

### Prohibited areas: boundary tolerance and charted channels

Chart area boundaries are coarse (the Natura2000 polygons come from a 1:250 000-scale cell). The rule
is evaluated per **chain**: the linked edges that intersect the area, merged into connected lines,
because densified edges split one crossing into several pieces. Two edges chain only at a shared
endpoint **inside** the area (endpoints within 10 m count as shared). A fork outside the area must not
join a corner graze to another line's deep crossing. A chain is a **graze** and gets
**caution** (severity 1), not prohibited, when
(a) it goes less than `AREA_TOLERANCE_M` (default 30 m) deep into the area **and** it does not cut the
area through: every piece of the split polygon except the largest is under 10% of its area, so it is a
corner or boundary sliver. Without that second condition, a strip narrower than 60 m could never block;
**or** (b) it runs along a charted axis that itself crosses the area. The notice is still linked. Found in step 1: the charted A15 approach to Roompot Marina
(between buoys M261 and M263) runs 158 m through the corner of an entry-prohibited Natura2000 polygon;
navline wm1 #28 crosses it with at most 18 m penetration. A hard block would have cut off the marina.

### Seasonal areas

Decided 2026-10-18 (Marcel): the router becomes time-aware (signalk-routeiq, before the general release of the new
databases), so the DB does not bake a verdict into time-limited closures. Until that router change lands, the
current router treats them as open; that is accepted.

- **Time-limited** = a prohibited restriction area with a recurring period (PERSTA/PEREND) or an end of validity
  (DATEND), from the chart or from a reviewed override. A start date alone (DATSTA, "established 2022") is not a
  time limit: such an area stays blocked like a permanent one.
- A time-limited area keeps severity 3 in `notices` and `edge_notices` (reason `seasonal`), but sets no
  `crosses_obstacle` and changes no `cost_factor`. The router blocks those edges when the planned date is inside the
  window and ignores the notice outside it. The graze and charted-axis downgrades apply as to any prohibited area.
- **Time in free text only.** Tiers never parse free text, but `hazards.text_mentions_time` flags a prohibited area
  whose name or INFORM/NINFOM mentions time or conditions (months, dates, winter/zomer, season, tijdelijk,
  resting/foraging area, ...) while it has no time attributes: `notices.text_review = 1`. Such an area **stays
  blocked** (edge reason `conditions in chart text, unreviewed`) until a person accepts a reading of it.
- **Reviewed overrides** (`data/hazard_overrides.json`, keyed by notice id, which is stable across editions because it
  comes from LNAM): `scripts/review_hazard_text.py propose` asks an LLM to read the text into S-57 date fields
  (status `proposed`); a person runs `accept` / `reject` (`--by` required). Only accepted entries apply, before
  tiering, in every consumer (pipeline, `navlines_to_db`, `prepare_inputs`, the navline check/render). An entry
  records the sha1 of the text it was read from; when the chart text changes it is stale and ignored. An accepted
  entry without dates means "reviewed, no time limit": the flag clears and the area stays blocked.
- The navline agent's `check` reports a time-limited prohibited crossing as a WARNING (avoid it where a reasonable
  alternative exists) and draws it with a dashed red hatch and a `[seasonal]` label.

## Notices table (new, schema_version 3)

```sql
CREATE TABLE notices (
  id            INTEGER PRIMARY KEY,  -- deterministic: md5(source_class|lnam)[:13] as int (LNAM persists
                                      -- across editions); geometry WKB only when LNAM is missing.
                                      -- The same object in two different cells has two LNAMs; dedupe
                                      -- merges only identical copies (same LNAM, or identical attributes)
  source_class  TEXT NOT NULL,        -- S-57 class: RESARE, MARCUL, WRECKS, OBSTRN, UWTROC, CTNARE, HULKES, ...
  source_ref    TEXT,                 -- LNAM (agency+FIDN+FIDS) for tracing back to the chart
  source_cell   TEXT,                 -- cell file name, e.g. 1R5OS101.000
  severity      INTEGER NOT NULL,     -- 0 info, 1 caution, 2 depth, 3 prohibited (tiers above)
  depth_m       REAL,                 -- least depth used for tier 2, NULL otherwise or if unknown
  name          TEXT,                 -- OBJNAM / NOBJNM as charted
  text          TEXT,                 -- INFORM + NINFOM as charted, unclassified, NULL if none
  period_start  TEXT,                 -- PERSTA raw S-57 form: recurring yearly window ('--1101'), may wrap New Year
  period_end    TEXT,                 -- PEREND ('--0515')
  valid_from    TEXT,                 -- DATSTA raw ('20220425'): validity of the object
  valid_until   TEXT,                 -- DATEND raw; in the past = expired
  period_source TEXT,                 -- 'chart' | 'text-reviewed' (accepted override) | NULL (no time info)
  text_review   INTEGER,              -- 1: text mentions time/conditions, no time attributes, no accepted override
                                      -- active on a date d: inside [valid_from, valid_until] if set AND inside the
                                      -- recurring window if set
  attributes    TEXT NOT NULL,        -- JSON of every non-empty S-57 attribute (RESTRN, CATREA, CATWRK, WATLEV, VALSOU, ...)
  geometry      TEXT NOT NULL,        -- GeoJSON geometry (areas simplified to ~10 m) so a client can draw it
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL   -- bbox for spatial lookup
);
CREATE TABLE edge_notices (source INTEGER, target INTEGER, notice_id INTEGER,
                           severity INTEGER NOT NULL,  -- effective for this edge: 3 blocks, 2 depth, 1 caution, 0 info
                           reason TEXT,  -- NULL, or 'boundary graze' / 'along charted axis' / 'accepted: <reason>'
                           PRIMARY KEY (source, target, notice_id));
CREATE TABLE node_notices (node_id INTEGER, notice_id INTEGER, severity INTEGER NOT NULL,
                           PRIMARY KEY (node_id, notice_id));
CREATE INDEX idx_edge_notices_notice ON edge_notices(notice_id);
CREATE INDEX idx_node_notices_notice ON node_notices(notice_id);
CREATE INDEX idx_notices_bbox ON notices(min_lat, min_lon);
```

`notices.severity` is the object's own tier; `edge_notices.severity` is the effective verdict for that edge, which can
differ per edge (a boundary graze or a charted-axis run is downgraded to 1, an acknowledged crossing is 1 with reason
`accepted: ...`, the same notice from two navlines keeps the highest). `node_notices.severity` is the object's tier.

Linking rules:
- **edge_notices:** an area notice links to every edge that intersects it. A point notice links to every
  edge within `HAZARD_RADIUS_M`. Both directed rows of an edge are linked. Densified sub-edges are linked
  individually, so the router's sub-edge merge can union their notice sets.
- **node_notices:** a node links to an area notice when the node lies inside the area, and to a point
  notice within `HAZARD_RADIUS_M`. This covers place-based notices such as anchoring prohibited, which
  matter where a boat stops rather than where it passes.
- Every linked notice is written, **all severities including info**. The client filters by severity.
  Unlinked objects (not near any edge) are not written.
- The tier effect (`crosses_obstacle`, `min_depth`, `cost_factor`) is applied **and** the notice is
  linked. A blocked edge still names the notice that blocks it.

The router ignores unknown tables, so the schema can ship before routeiq reads it. Router-side use
(warnings, display, time-aware seasonal blocking) is a separate later stage in signalk-routeiq.

## Shared code

One module, `hazards.py` (repo root), owns extraction, tiering and linking. It is used by:
1. `navline_agent/prepare_inputs.py` writes `hazards.json` for the section (all tiers, with severity).
2. `navline_agent/render_chart.py` draws prohibited areas with a red hatch, depth hazards with a symbol and
   depth label, caution areas with an orange outline, and info items with a pale outline. Only the first
   two are labelled on overview tiles.
3. `navline_agent/nav_tools.py check`: a prohibited crossing is an error; a depth hazard is listed with its
   depth; caution and info items are listed once per line, without failing. A crossing that is really
   the route (a charted channel through an area, not already downgraded) can be acknowledged on the
   navline with the property `"accept_hazards": {"<notice id>": "<reason>"}`. That turns the error into
   a visible WARNING, never silent, for a human to judge. A stale id is also a warning.
4. `navline_agent/navlines_to_db.py` writes `crosses_obstacle` (not for time-limited areas), folds depth, and
   writes the three notice tables. Schema version becomes 3.
5. `nautical_routing_pipeline.py` replaces `_is_entry_prohibited` / `_obstruction_depth_disposition` with
   calls into `hazards.py` and writes the same notice tables. This can be a second step, but it must use
   the same module, not a copy.

## Extraction gaps to close

- `enc_preprocessor.py`: add `WRECKS` → `wrecks_points.geojson` (points and areas), `UWTROC` →
  `rocks_points.geojson`, and keep the raw `VALSOU` string. Check `obstructions_points` for the same
  UNKNOWN→0 problem.
- `navline_agent/prepare_inputs.py` reads the raw cells directly (as it does for marks), so it can get
  wrecks before the GeoJSON step is fixed.

## Bugs found in the current rules

1. **`_is_entry_prohibited` tests RESTRN == 1, which is "anchoring prohibited".** Entry prohibited is
   RESTRN 7. The main pipeline hard-blocks anchoring-prohibited areas and misses the RESTRN-7 nature
   reserves unless their text happens to match. In the Zeeland clip, 347 polygons carry RESTRN 7 (most of
   them nature reserves).
   On top of that, the data has two columns, `RESTRN` (190 rows) and `restrn` (344 rows; IENC-style
   lowercase). `_s57_col` builds a lowercase key map in which one column overwrites the other, so it
   only ever reads one of them. Read both and take the union.
2. `_obstruction_depth_disposition` takes VALSOU 0.0 at face value, so every 'UNKNOWN' becomes a 0 m
   depth constraint.

## Validation before rollout

1. Read-only count: run the tiering over the accepted ws1/ws2/wm1 navlines and report hits per tier. Every
   prohibited hit gets a zoom tile for a human verdict.
2. Rebuild the three DBs with schema 3 and verify the same POI-to-POI routes. Each change in route or cost
   must be explained by a notice.
3. Main pipeline: the RESTRN fix changes which areas are blocked, so log it in `data/BUILD_LOG.md` with
   before/after obstacle counts.

## Step 1 result (2026-10-18)

Prototype: a read-only scratch script that tiers raw-cell objects and links them to the accepted navlines.

| section | objects in bbox | linked to a navline | prohibited | depth | caution | info |
|---|---|---|---|---|---|---|
| ws1 round3 | 118 | 8 | 0 | 0 | 0 | 8 |
| ws2 round4 | 114 | 17 | 0 | 0 | 0 | 17 |
| wm1 round2 | 85 | 3 | 1 → caution by the tolerance rule | 0 | 0 | 2 |

The sections contain 58 depth-tier and 3 prohibited point hazards; none is within 30 m of a navline. The
agent kept clear of every real hazard. The linked info notices are mostly CATREA 25 (named turning basins,
"Massagoedhaven (d= 360 m)"), CATREA 19 (waiting areas) and RESTRN 1 (anchoring prohibited). Several RESTRN 1
areas lie across the Wielingen and the Kanaal Gent-Terneuzen approach, which the main pipeline's
`_is_entry_prohibited` bug would block.

## Step 5 result (2026-10-18): main pipeline

**Choice: option B.** `nautical_routing_pipeline.py --hazard-cells-dir DIR` calls `hazards.extract_hazards` on the
raw S-57 cells (`hazards.latest_cells(DIR, coverage bbox)`: latest edition per cell name, symlinked dirs followed),
exactly the path the navline DBs use. Reasons: one source of rules with no GeoJSON adapter to keep in sync; the
RESTRN/restrn union, lowercase IENC `resare`, the VALSOU 'UNKNOWN' trap, LNAM ids and cross-cell dedupe come for
free; WRECKS and UWTROC need no re-extract and re-clip of every region (the Zeeland clip predates them); NOAA regions
work the same way (`build_region.sh` passes `--hazard-cells-dir "$RAW_DIR"`, the cells it just preprocessed). Option A
would have needed a second extraction path with its own copy of the UNKNOWN/union logic.

What changed in the pipeline:
- Removed: `_is_entry_prohibited` (tested RESTRN 1, anchoring prohibited, instead of 7), `_obstruction_depth_disposition`
  (unknown VALSOU -> 0.0 m), `_build_obstacle_layer`, the `obstacles_soft` depth fold, `OBSTACLE_BUFFER_METERS`.
- Hazards are linked once, in the main process, on the final graph (after every graph pass, before node depths and
  export). Effects mirror `navlines_to_db.py`: effective severity 3 -> `crosses_obstacle = 1`; severity 2 folds into
  `min_depth` (unknown sentinel replaced, else min); seasonal closures multiplied `cost_factor` by 3 (replaced by the
  time-aware design in Seasonal areas, after #72);
  `notices` / `edge_notices` (both directions) / `node_notices` written, `schema_version` 3. Opening-bridge and lock
  edges never get `crosses_obstacle` or a depth fold from hazards (their notices are still linked); `crosses_land` is
  unchanged. The router does not gate on `schema_version` and ignores the new tables (`src/db-worker.ts`).
- The tiering depth (`depth_fn`) is the same per-point DEPARE value the edges use (`_depare_point_depth`, now shared by
  the edge worker); see the depth rule above.
- The CLI refuses to build without `--hazard-cells-dir` unless `--no-hazards` is given (schema 2, no blocking at all),
  and fails if the directory yields no cell for the bbox.
- **Charted axes, per edge.** The charted-axis downgrade uses the charted `inland_waterways_lines` (waterway axes,
  recommended tracks, navigation lines), never the derived `--channel-axes`, and in the main pipeline it is judged per
  edge (`link_edges(..., axis_per_edge=True)`; the graze test stays per chain; the navline path keeps the default).
  Reason, found in the first Zeeland build (#71): in a navmesh the edges inside an area join into one large web, so
  the per-chain "runs along an axis" test can never pass, and a 1:250 000 Natura2000 polygon (SCAMIN 250000,
  SORIND `Natura2000-2017`) over the Goessche Sas entrance channel made Goes and Yerseke unroutable.
- Decision kept as specified: a dangerous wreck of unknown depth is a depth hazard at 0.0 m, not a block.

Zeeland (BUILD_LOG #72 vs #51, same clip and flags, identical topology, all gates pass):

| | #51 (old rules) | #72 (hazards.py) |
|---|---|---|
| blocked edge pairs (`crosses_obstacle`) | 474 | 3,665 (433 in both) |
| RESTRN 1 anchoring-prohibited areas, Kanaal Gent-Terneuzen | blocked (29 pairs) | info, not blocked |
| restrn/RESTRN 7 nature reserves (CATREA 4) | 12 pairs | 2,894 pairs, 57 areas |
| restrn 7 without CATREA (lock approaches, mussel-seed installations, ...) | 0 | 585 pairs, 15 areas |
| RESTRN 7 CATREA 12 "passage prohibited, dangerous tidal streams" | 455 | 445 (12 now graze/axis) |
| depth-folded pairs | obstacles_soft fold | 36 (wrecks, obstructions, 1 rock) |
| seasonal cost x3 | none | 79 pairs |
| notices / edge_notices / node_notices | - | 324 / 19,550 / 6,472 |
| downgraded links (pairs) | - | 429 along charted axis, 192 boundary graze |

Routes (routeiq engine, draft 1.5 m): 16 probe pairs (10 Oosterschelde + Volkeraksluizen both ways, Stavenisse >
Bruinisse, Vlissingen > Terneuzen, Breskens > Vlissingen, Roompot > Colijnsplaat): 15 identical, 08 Zierikzee >
Wemeldinge +805 m around a restrn-7 strip off Schouwen. None unroutable. With #71's code (derived axes, per-chain
axis test) two were unroutable (Yerseke > Goessche Sas, Sint Annaland > Goes); that is what led to the change above.

Linking cost on Zeeland: 7.7 s for 78k edges and 662 objects.

MD (BUILD_LOG #73, optional check, paused): blocked pairs 120 -> 733, mostly RESTRN 7 restricted areas (33 CFR
334.140, Aberdeen Proving Ground); 461 depth-folded; gates pass. Two problems, not solved: 23 POIs in Washington DC
are cut off (suspect: temporary safety zone 33 CFR 165.T05-0162 across the Potomac, RESTRN 7, not confirmed), and the
DB more than doubles (34 -> 76 MB) because huge NOAA CTNARE "bathymetry omitted" coverage areas link to most edges.
NOAA needs its own look (temporary 33 CFR 165 zones, CTNARE coverage notices) before any US region uses the flag.

Time-aware notices (BUILD_LOG #74, 2026-10-19): the same 3,665 blocked pairs as #72; the x3 cost is gone; 72 pairs are
linked only to time-limited areas (reason `seasonal`, not blocked); 183 blocked pairs come from 4 areas whose
time is in the text only (reason `conditions in chart text, unreviewed`); all 16 probe routes unchanged.

Open after step 5:
- Text-only seasons ("Bollen van het Nieuwe Zand", "Winter resting area from November 1 until May 15", RESTRN 7, no
  PERSTA/PEREND): resolved by the time-aware design above (text flag + reviewed overrides); the Zeeland overrides
  still have to be proposed and accepted.
- An expired temporary notice (CTNARE "Krammersluizen: renovations", PEREND 20220425) is still in the cells and is
  linked as caution to 486 edge pairs around the Krammer locks. It has no routing effect (caution areas never raise
  cost), but a client showing notices would show a stale one; dates in the past are not filtered yet.
- `_runs_along_axis` tests "axis intersects the area" on the whole MultiPolygon, not on the part the edge is in
  (shared with the navline path; no case seen).
- The gates (`scripts/run_gates.py`) ignore `crosses_obstacle`, so a blocking regression is only visible in route
  probes. A gate that drops blocked edges before the reachability check would catch it.

## Open

- Mussel long-lines (real obstacles) cannot be told apart from bottom plots in the chart data. All MARCUL
  without restriction stays info until a source appears.
- `HAZARD_RADIUS_M` is a first guess; tune it after validation step 1. (The seasonal `cost_factor` is gone.)
