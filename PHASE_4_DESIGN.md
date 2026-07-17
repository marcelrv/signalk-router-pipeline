# Phase 4 Design — Dynamic Database Loading, AI-Vision-Assisted Path Resolution, Bridge/Lock Wait Data

## Scope and how this document relates to the others

This is the forward design for open threads that came up after
`PHASE_3_DESIGN.md` was written and aren't actually covered by it, despite
one of them (4b) sounding at first like it might be. It assumes Phase
0-2, Phase 2 Hardening, and Phase 3 (3a-3f) are either done or already
designed — see `PHASE_3_DESIGN.md` for that work and `NEXT_PHASES.md` for
the tactical bug-tracking log. Nothing here is a re-litigation of Phase 3;
every sub-phase below was checked against it directly (see each section's
"Relationship to Phase 3" note) rather than assumed independent.

This was written after explicitly checking whether anything else from the
project's original roadmap (the private `local_only/BACKGROUND.md` §10
"Phased plan," predating Phase 0 implementation) was missing from
`PHASE_3_DESIGN.md`. It wasn't — that original Phase 0-4 plan (pilot,
provenance/overrides, data fusion, AIS validation, scale-out+hierarchy)
is fully represented across the now-implemented Phase 0-2 and 3a-3f. All
three sub-phases below are genuinely new — they came up in conversation
after that original plan was written, not gaps in it.

| | Sub-phase | Depends on | Repo |
|---|---|---|---|
| 4a | Position- and route-aware dynamic database loading | None (independent of Phase 3) — high value only once 3e produces ≥2 real regions to actually choose between | `routeiq` |
| 4b | AI-vision-assisted ambiguous-path resolution | 3c (reuses its override schema/PR workflow unchanged) | `signalk-router-pipeline` + `router-data` |
| 4c | Bridge/lock wait-time & schedule data | 3c (same override workflow as 4b) | `signalk-router-pipeline` + `router-data`; consumed by `routeiq` (see `feature-bridge-lock-waits.md` there) |

---

## 4a. Position- and route-aware dynamic database loading

**Purpose**: today, `RoutingDatabase.init()` (`routeiq/src/database.ts:126`)
unconditionally `readdirSync`s the routing data directory and opens
*every* `.sqlite` file it finds in one `init` worker call, regardless of
where the vessel is. A deployment with NL, US Caribbean, and a future
Mediterranean region downloaded all pays the full load cost (Round 4
measured ~70s for one full-country graph) for all three every time the
plugin starts, even though a vessel in the Netherlands will only ever
route within one of them. This gets strictly worse as 3e (scale-out)
produces more regions to have on disk at once.

**What already exists and doesn't need to change**: the catalog/download
side is already real, not a gap. `GET /signalk/v1/api/router/databases/available`
(`api.ts:884`) fetches `routing-index.json` from the configured
`catalogUrl`, and a separate endpoint downloads a chosen region's
`.sqlite.gz` (with origin validation against that same catalog URL,
`api.ts:939`, as SSRF protection). A user browsing and downloading
specific regions today is a solved, working flow. **What's missing is
purely on the load side**: once a region is downloaded and sitting in the
data directory, it gets opened into memory unconditionally, forever,
regardless of relevance.

**The enabling fact that makes lazy loading cheap**: every compiled
database already carries `metadata.bounding_box`/`boundary_geometry`
(format spec §2.1) as a single small row — reading it costs nothing like
`loadNodes`/`loadEdges` does. So a "peek" step (open the file, read one
row, keep the handle around or close it) can build a full coverage index
for every locally-present file without paying the real load cost for any
of them.

**Design**:

1. **Metadata peek, not full load, at startup.** New `db-worker.ts`
   message `peekMetadata(dbPaths)`: opens each file just long enough to
   `SELECT bounding_box, boundary_geometry FROM metadata`, returns the
   parsed coverage per file. `RoutingDatabase.init()` (behind a new
   `dynamicLoading` config flag, see below) does this instead of opening
   every handle and calling the equivalent of today's full `loadGraph()`.
   Builds an in-memory `RegionCoverageIndex: Map<filename, {bbox, boundary}>`.
   This alone turns startup from "load N regions" into "peek N regions,"
   independent of the other two triggers below — a real win even before
   anything else in this section is built.
2. **Load state machine.** Each known local file is `not_loaded` (peeked
   only) → `loading` → `loaded`. `loadGraph()`'s existing per-DB logic
   becomes invokable per-file instead of only "all files, once," and
   newly-loaded nodes/edges merge into the already-loaded graph in memory
   rather than requiring a full reload of everything (a full reload on
   every region crossing would be a worse user experience than the
   current unconditional-load-everything behavior it's replacing).
3. **Trigger 1 — position-based auto-load/evict.** The plugin does not
   currently subscribe to `navigation.position` anywhere (checked — this
   is genuinely new plumbing, not an existing hook to wire into). Add a
   subscription; on each update (throttled — e.g. only re-evaluate on
   >1nm movement, not every delta), check the `RegionCoverageIndex` for
   any `not_loaded` region whose boundary contains the position or is
   within a configurable `loadRadiusNm` buffer (load proactively before
   the vessel actually crosses in, not reactively after), and trigger a
   load. Symmetrically, evict (close the handle, drop its nodes/edges
   from memory) a `loaded` region once the vessel is more than a
   configurable `unloadAfterIdleNm` outside its boundary — off by default
   (evicting nothing is always correct, just uses more memory) since
   getting eviction wrong (dropping a region mid-route) is a worse
   failure mode than never evicting.
4. **Trigger 2 — route-request-triggered on-demand load.** If a route
   request's start/end/any waypoint falls inside a `not_loaded` region's
   boundary, that region **must** load before the search runs — this is
   the case that can never be allowed to silently fail or silently route
   around the gap. Given loads can take real time (tens of seconds at
   full-country scale), don't block the HTTP request for that long:
   return `202 Accepted` with a status handle, extending the exact
   pattern already used for "not ready yet" elsewhere in `api.ts`
   (`isReady()` → `503 Database not ready` is the existing precedent for
   this class of response; a per-region load is the same idea scoped to
   one region instead of the whole engine).
5. **Loading-status UX.** New `GET /signalk/v1/api/router/databases/loaded`
   returning `{filename, coverage, state}[]` for every locally-known file.
   Webapp polls it after receiving a `202` and shows a "Loading Baltic
   charts…" indicator until the relevant region flips to `loaded`, then
   retries the original request automatically.
6. **Config.** New options in `types.ts`'s config shape: `dynamicLoading`
   (boolean, **default `false`**) — the existing unconditional-load-
   everything behavior stays the default, same reasoning as keeping
   Phase 1's minimum-viable fallback edges permanently rather than
   removing them once Phase 2 landed: a single-region deployment (which
   is every deployment that exists today) has nothing to gain from this
   and shouldn't have its startup behavior change under it. `loadRadiusNm`,
   `unloadAfterIdleNm`, `maxLoadedRegions` (a hard cap as a second safety
   net independent of the idle-distance policy).

**Concrete tasks, in order**:
1. `peekMetadata` worker message + `RegionCoverageIndex`.
2. Per-file load/unload plumbing in `db-worker.ts` (open/close a single
   handle, merge/remove its nodes/edges from the in-memory graph) —
   refactors today's "all handles, once" `loadNodes`/`loadEdges` loops
   into something callable per-handle.
3. `navigation.position` subscription + auto-load/evict logic.
4. Route-handler on-demand load path, `202`/status-poll response shape.
5. `GET .../databases/loaded` endpoint + webapp polling/indicator.
6. Config schema additions, `dynamicLoading` default `false`.

**Explicitly out of scope for this sub-phase**: auto-*downloading* a
region the vessel approaches that isn't on disk yet at all. That's a
plausible future extension of the same position-awareness (trigger 1's
logic could, in principle, also drive the existing catalog/download
endpoints instead of just a local load), but it's a materially bigger
decision — unattended network fetches based on position raise obvious
data-usage/consent questions a satnav-style device on a boat should not
make silently. Left as a clearly separate, later, opt-in feature; this
sub-phase only ever loads/unloads files already present locally.

---

## 4b. AI-vision-assisted ambiguous-path resolution

**Relationship to Phase 3 (important — this is not a duplicate of 3c)**:
`PHASE_3_DESIGN.md`'s 3c ("Community override workflow") already has one
line covering this: *"anomaly queue → either a human or an AI agent (with
satellite imagery, chart attributes, and OSM tags for that location as
context) proposes a fix."* That line is accurate but is the *entire*
design for what's actually a distinct, non-trivial capability — this
section is that line, fully specified. It reuses 3c's schema and PR
workflow completely unchanged (no new database columns, no new trust
tier); what's missing is (1) a genuinely new *trigger* category 3c's
anomaly queue doesn't detect at all, and (2) the actual mechanics of what
"an AI agent with satellite imagery" means in practice — inputs, outputs,
and where the human sign-off gate sits.

**The gap in 3c's anomaly queue**: every trigger it lists (`NEXT_PHASES.md`-
style: missing through-edge near a lock/bridge, disconnected-but-nearby
components, a tier-3/4-only bottleneck with no corroboration) is a
**connectivity/data-quality** problem — something is structurally broken
or under-sourced. None of them catch **path-preference ambiguity**: the
graph is fully connected and every candidate is individually valid, but
it's genuinely unclear *which* is the sensible one — two channels of
similar width/depth where only local knowledge or a look at the actual
water says one is customarily preferred, or a stretch where the medial
axis itself is unstable (width profile swings wildly, suggesting the
raster skeleton is picking up real structure rather than a stable
centerline). This is the situation the user actually described: not "the
graph is broken," but "the graph can't tell, and a human or a vision
model looking at the real place probably could."

**New anomaly-trigger category — detection, concretely**:
1. **Near-tied, geometrically-distinct alternatives.** The Pareto-
   alternative precompute already speced for §7/3f macro-edges (shortest
   path → remove its bottleneck → repeat) is already producing multiple
   candidate paths between supernode pairs. Reuse that machinery's output
   directly: flag a pair where two candidates are within some cost
   tolerance (e.g. 5%) *and* materially different in geometry (Fréchet or
   Hausdorff distance between them exceeds a threshold scaled to the
   pair's own distance) — a near-tie in cost that's actually two
   different real routes, not two near-identical renderings of the same
   one.
2. **Unstable classification confidence.** A channel where
   `_extract_buoyage_direction` had no real buoy/fairway data to resolve
   direction from (falls back to the S-57 `TRAFIC`-only guess, or finds
   nothing at all), or whose medial-axis width profile has a high
   coefficient of variation along its length — both signal "the pipeline
   made a mechanical decision here with weak evidence," worth a second
   look distinct from the path-tie case above.

**Vision input construction**: a new script, `render_ambiguity_tile.py`,
for each flagged location:
- Renders the pipeline's own vector layers (water/land/depth, plus the
  specific candidate paths or the classification boundary in question,
  each in a distinct color) to a chart-style raster tile at a fixed
  real-world scale (e.g. 500m across) — a headless render of data the
  pipeline already has, not a new data source.
- **Open question, not resolved here, flag before implementing**: whether
  to also overlay real satellite/aerial imagery. This would materially
  help a vision model (and a human reviewer) judge "which channel do
  boats actually use," but every convenient imagery source (ESRI World
  Imagery, Bing, Google satellite tiles) has terms that don't clearly
  permit this kind of automated, redistribution-adjacent fetching: this
  project already treats source terms as load-bearing (see how carefully
  `LICENSE-DATA.md` handles GEBCO's safety-of-navigation disclaimer), so
  this needs a real per-source terms check before picking one — Sentinel-2/
  Copernicus is genuinely free but only ~10m resolution, which may not be
  sharp enough to actually distinguish two nearby channels. Don't
  silently pick a source when this gets implemented; resolve it
  explicitly and document the choice the way `LICENSE-DATA.md` documents
  every other source.
- A short structured text description alongside the image: each
  candidate's `distance`/`min_depth`/`min_width`/`max_air_draft`, or the
  specific classification decision in question, phrased as a direct
  question ("candidate A vs. candidate B — which is the more sensible
  route for a recreational vessel, and why").

**Model output contract**: parse the response into a choice, a `reason`
string, and a confidence indicator. **Low-confidence responses do not
auto-generate a PR** — they stay in the anomaly queue as flagged-but-
unresolved, same as any anomaly no contributor has looked at yet. This is
the load-bearing design decision in this whole sub-phase: the vision
model **narrows the human review queue, it never bypasses it**. That's
not an arbitrary caution — it's the same tier-5 human-sign-off rule 3c
already established for every other override, applied consistently
rather than carved out an exception for this one source.

**Provenance, reusing 3c's schema exactly**: `contributor: "agent:<model-name>"`
(the field already exists and already anticipates this — `PHASE_3_DESIGN.md`'s
example YAML literally has `contributor: "agent:claude"`). One small,
additive schema extension: an optional `evidence_image` field on the
override YAML (a relative path to the rendered tile, committed alongside
the override PR under `overrides/.../evidence/`) — for an AI-vision-
sourced override specifically, attaching the actual image the model saw
is what makes human review fast and trustworthy; otherwise the reviewer
has to independently regenerate the same view just to check the model's
reasoning against it. Backward compatible — existing text-only `evidence`
overrides are unaffected, the field is optional.

**Build-time/batch only — not a live per-query call, and why**: the
"map situation" framing could be read as resolving ambiguity live, at
route-computation time, per request. Deliberately **not** the design
here, for four concrete reasons: (1) determinism — the whole tier system
is built around reproducible, auditable trust, and a live vision call
isn't reproducible run to run; (2) latency — vision model calls run in
seconds, incompatible with the sub-3-second route budget Round 4 was
explicitly fought to achieve; (3) it would make routing depend on a live
external API being reachable, configured, and paid for by every
deployment, which conflicts with the format spec's own "any consumer can
implement this, no special dependency" premise (§9); (4) it would create
a new, ungoverned trust tier the spec has no slot for — every existing
tier-5 fact was human-approved before a router ever relies on it. So:
`propose_ai_overrides.py` runs as a batch step (periodic, or triggered
per region regeneration), feeding the *exact same* override-PR pipeline
3c already specifies. This sub-phase adds a new **proposer** of overrides,
not a new consumer-facing capability or trust tier.

**Concrete tasks**:
1. Extend `find_anomalies.py`'s output with the two new trigger types
   above (near-tied/geometrically-distinct path pairs; low-confidence
   classification).
2. `render_ambiguity_tile.py` — vector-to-raster chart render + candidate
   overlay; resolve (don't defer past implementation time) the
   satellite-imagery-source question above.
3. `propose_ai_overrides.py` — assembles the vision-model prompt (tile +
   structured candidate description) for each anomaly-queue entry that's
   a judgment call rather than a mechanical fix, calls a configured
   vision-capable model, and for confident results only, emits a
   candidate override YAML (still a PR, never auto-merged) with
   `contributor`/`reason`/`evidence_image` populated.
4. Optional `evidence_image` field on the override YAML schema + its
   JSON-schema validator (3c task 1).
5. A short addendum to `router-data/CONTRIBUTING.md`: AI-vision-sourced
   overrides get the identical review bar as any other override — no
   fast-path merge because the proposer was an agent.

---

## 4c. Bridge/lock wait-time & schedule data

**Purpose**: the router currently assumes every opening bridge and lock
crossing is instant. The full consumption-side design (the routing-cost/
ETA changes, phased in three tiers) lives in `routeiq`'s
`feature-bridge-lock-waits.md` — this section is only the pipeline/
`router-data` side that design depends on: where the wait-time/schedule
data itself comes from and how it's stored.

**Relationship to 3c (same pattern as 4b)**: official ENC/IENC data
essentially never carries real-world operational wait times or opening
schedules — a lock's charted geometry says nothing about how long it
takes to cycle traffic through it. This data starts life as tier-6/
community-sourced almost by definition, so it's a direct, concrete use
case for 3c's override workflow, not a new mechanism: a contributor
(human, or 4b's AI-vision agent if a location's schedule is visible on
charted/satellite imagery — signage, gate position — worth checking once
4b exists) proposes a value the same way any other override does, reviewed
and merged as tier 5.

**New first-class fields on `pois`** (`type_id` `lock`/`bridge`) — not the
free-form `properties` JSON (format spec §2.10), since these feed a cost
function, not just a display label, same reasoning as why `min_depth`/
`max_air_draft` are first-class edge columns rather than buried
attributes:
- `typical_wait_minutes REAL` — a single scalar estimate.
- `opening_schedule TEXT (JSON)`: `[{ "days": [...], "windows": [{"start","end"}], "interval_minutes": N }]`
  — covers both "fixed operating hours" and "opens on a cycle within
  those hours," the two common real patterns. Shape is a first cut, not
  final — validate against real Rijkswaterstaat/USACE lock schedule data
  (both already-ingested source authorities, per `LICENSE-DATA.md`)
  before treating it as settled.

**A real, separate, smaller gap this surfaced**: bridges already get a
precise opening-point edge (`_add_opening_bridge_edges`,
`is_opening_bridge_edge`), but locks have no equivalent marker at all —
`locks_gdf` is only ever consulted in `_edge_attr_worker` to annotate an
existing edge's attributes, so there's currently no way for `routeiq`
to identify "this edge passes through a lock" the way it already can for
a bridge. Worth fixing on its own merits (`feature-bridge-lock-waits.md`
needs it regardless of timing on the wait-data work above) — add a
`requires_lock`/`lock_id` marker analogous to `is_opening_bridge_edge`,
sourced from the same lock-polygon-intersection logic already computing
`_edge_attr_worker`'s lock-adjacent attributes today.

**Concrete tasks**:
1. `typical_wait_minutes`/`opening_schedule` columns on `pois` (additive,
   no `schema_version` bump).
2. Lock-crossing edge marker (`requires_lock` or similar), mirroring
   `is_opening_bridge_edge`'s existing pattern.
3. Extend the override YAML schema (3c) with a worked example for this
   POI-field use case (as opposed to `override_provenance`'s existing
   node/edge upsert examples) — same schema, no format change, just
   documentation showing this specific shape.
4. First real values for at least the Zeeland pilot's bridges/locks,
   sourced manually (or via 4b once it exists) as the first real test of
   the override workflow end to end.

---

## Cross-cutting notes

- **4a touches only `routeiq`** — no pipeline or `router-data` schema
  changes at all. Every locally-loaded database already carries the
  `metadata.bounding_box`/`boundary_geometry` this depends on (format
  spec §2.1, already normative since Phase 0).
- **4b touches `signalk-router-pipeline` (two new scripts) and
  `router-data` (one optional YAML field + a `CONTRIBUTING.md` addendum)**
  — no `signalk-router-pipeline/nautical_routing_pipeline.py` changes,
  since it only ever *proposes* overrides through the existing overlay
  mechanism, the same as a human contributor would.
- **4c touches `pois` (two new columns) and needs the small
  `is_opening_bridge_edge`-for-locks fix** — the only sub-phase here with
  a real `nautical_routing_pipeline.py` schema addition, though still
  additive/no version bump. Its `routeiq`-side consumption is designed
  separately, in that repo's `feature-bridge-lock-waits.md`, not here.
- None of the three sub-phases requires a `schema_version` bump — all
  additive, consistent with every other phase so far.

## Suggested order

All three sub-phases are independent of each other — different code
paths, no shared implementation — and can be built in any order or in
parallel. 4a's real payoff scales with how many regions exist at once, so
it's naturally most worth doing once 3e has actually produced a second
real region (Puerto Rico, per the Phase 2 Hardening discussion, is a
reasonable near-term second data point even before 3e's full scale-out).
4b only needs 3c to exist first (it's built entirely on top of 3c's
schema/workflow) and doesn't need to wait for 3e at all — it's just as
relevant to a single-region deployment with a handful of genuinely
ambiguous channels as to a multi-region one. 4c's tier 1 (`routeiq`'s
flat-constant ETA fix, see `feature-bridge-lock-waits.md`) needs nothing
from this repo at all and can ship immediately; 4c's pipeline/schema work
here only needs 3c to exist, same as 4b, and the two make good parallel
first real tests of the override workflow once it lands.
