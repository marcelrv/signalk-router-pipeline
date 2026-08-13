# Legacy idea backlog (moved from routeiq's old todo.md)

`routeiq/todo.md` used to mix plugin-side (TypeScript runtime) planning
with routing-database/pipeline design, and had drifted out of date — the
pipeline-related brainstorm ideas below were pulled out verbatim-in-spirit
during a 2026-07-13 cleanup so they can be evaluated here against what's
actually shipped or already properly designed in `NEXT_PHASES.md` /
`PHASE_3_DESIGN.md` / `PHASE_4_DESIGN.md`, rather than staying stuck in a
plugin-repo file nobody reads anymore.

Most of these turned out to already be done or already captured by a more
precise design elsewhere — that's noted per item. What's left genuinely
open is at the bottom.

## Already superseded or done — no action needed

- **Pareto-optimal macro-edges between supernodes** (the original idea: a
  Python "cloud pipeline" step should compute multiple alternative
  macro-edges per supernode pair — a shallow shortcut, a deep channel, a
  mast-up route — so vessel-specific constraints pick the right one at
  routing time, instead of one precomputed shortest path trapping
  deep-draft/tall vessels). **This is now the actual design in
  `PHASE_3_DESIGN.md` §3f** ("Supernode / macro-edge hierarchical
  routing"), specified more precisely than the original brainstorm
  (bottleneck-removal iteration up to ~3 alternatives, stored as
  `edge_kind_id=3` rows, consumer required to evaluate all rows for a
  supernode pair per format spec §7). Not started, but tracked correctly —
  follow §3f, not this note, when picked up. Depends on 3e per that doc's
  ordering.
- **"Edge Poisoning" bug / "4-Meter Fast-Path" depth sampling** — the
  proposal (skip detailed sampling when all candidate depth polygons are
  ≥4.0m, else 10-point sample) is implemented in `_edge_attr_worker` in
  `nautical_routing_pipeline.py`, just with different tuning: a **5.0m**
  fast-path threshold and **5-point** sampling instead of 4.0m/10-point.
  Same optimization, already shipped — no action needed unless the 5.0m/
  5-point tuning turns out to be too coarse in practice (no evidence of
  that so far).
- **Schema: types → enum tables** — `edge_type_enum`, `poi_type_enum`,
  `edge_kind_enum`, `node_kind_enum` all exist in the export schema
  (`nautical_routing_pipeline.py`, `export_to_sqlite`). Done.
- **Traffic direction / one-way flags combined into one field** — done via
  the unified `traffic_mode` column (0 = two-way, 1/2 = one-way by
  direction), populated from S-57 `TRAFIC` in `_edge_attr_worker`.
- **Direction penalty** — done, but runtime-side rather than schema-side:
  `routeiq`'s `wrongWayPenalty` config (default 5.0×) applies at routing
  time against `traffic_mode`, not as a precomputed DB column. If a
  precomputed penalty column is ever wanted (e.g. to vary penalty by
  waterway class instead of one global multiplier), that's a genuinely new
  idea, not this old one.
- **"Edge type to id"** — done, `edge_type_id` column exists on `edges`
  referencing `edge_type_enum`.

## Still worth a fresh look

- **Extra/generalized cost field** — the old note ("add a cost field, or
  use an existing field like direction more extensively") predates
  `cost_factor` (fairway 0.8× / open-water 1.2×), `traffic_mode`, and
  `wrongWayPenalty` all existing. Worth checking whether there's a real
  remaining gap — e.g. per-vessel-class cost multipliers, or a
  weather/season-dependent cost — or whether this is now fully covered and
  the note can just be dropped.
- **Position/route-aware dynamic database loading** ("only load the needed
  databases, not all of them all the time") — this is plugin-side
  (`routeiq`'s `RoutingDatabase.init()`), not pipeline-side, but is
  recorded here because the original note was pipeline-adjacent. It's
  already properly scoped as **Phase 4 §4a** in `PHASE_4_DESIGN.md` — see
  that doc, not this note, when picked up.
