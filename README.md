# SignalK Router Pipeline

An open-source pipeline that turns freely available nautical chart, inland
waterway, bathymetry, and vessel-traffic data into compiled routing-graph
databases for nautical route planning — covering both US (NOAA-charted)
and European waters, buildable entirely from data that is free to use and
legally redistributable.

Compiled output databases are published to
[signalk-router-data](https://github.com/marcelrv/signalk-router-data) and
consumed by [SignalK RouteIQ](https://github.com/marcelrv/signalk-routeiq)
(and any other tool — the database format is an open, documented contract,
not tied to one consumer).

## Why this exists

Turn-by-turn nautical routing needs more than "shortest path on a grid over
water." It needs to know where a vessel of a given draft/beam/air-draft can
actually go, respect one-way fairways and buoyage, route sensibly through
locks and bridges, and stay usable when the source charts are incomplete —
which, for free data, they often are. This pipeline is built around three
ideas that address that directly:

1. **Represent water the way it's actually navigated**, not as a uniform
   point grid — see [Architecture](#architecture).
2. **Be explicit about data quality** instead of silently blending a
   surveyed depth with a guess — see [Data fusion & confidence tiers](#data-fusion--confidence-tiers).
3. **Let people and AI agents fix the specific spots that are wrong**, in a
   way that's reviewable, shareable, and survives the next rebuild — see
   [Community override workflow](#community-override-workflow).

## Architecture

Instead of one grid resolution (or one "adaptive" grid) everywhere, the
pipeline classifies every water body and picks the representation that
actually fits its shape:

### Open/deep water → navmesh regions

Where a body of water is wide and deep enough that draft/beam essentially
never constrains the route (bays, sounds, large lakes, offshore legs), no
interior routing nodes are generated at all. A **constrained Delaunay
triangulation** of the free-space polygon (water minus land minus charted
obstructions) is built instead, using the polygon's own boundary as a hard
constraint the triangulation cannot cross. At query time, a path across the
region is computed with the **funnel algorithm** over the corridor of
triangles the search traverses — producing the exact taut shortest path
using only the corridor's boundary vertices, no arbitrary sampling. A
region this way needs orders of magnitude fewer graph entities than a grid
fine enough to hug the coastline would.

### Narrow channels → skeleton centerlines

Where a channel is too narrow to be "go anywhere," the pipeline extracts
its **medial axis** (the ridge line equidistant from both banks, via a
Voronoi diagram of the bank geometry) directly as the routing centerline.
No grid or point injection is used to approximate it — the exact centerline
comes straight out of the channel's geometry, carrying a width-along-edge
profile derived from the same computation.

### Buoyed/regulated channels → paired lane edges

Where a channel has a charted fairway, traffic separation scheme, or IALA
lateral buoyage, the pipeline generates two directional lane edges offset
from the centerline — one per side — instead of a single bidirectional edge
with a cost penalty standing in for "you're on the wrong side." This
degrades gracefully to a plain centerline edge wherever no buoyage data
exists for a stretch.

### Classification

One function decides which representation applies to a given water
polygon, using three geometric/attribute tests: **narrowness** (minimum
medial-axis radius), **depth headroom** (does charted/derived depth clear a
configured "universally safe" ceiling everywhere in the polygon?), and
**regulatory structure present** (does a fairway/TSS/buoyage layer overlap
it?). This is a single, named, documented decision point — not a threshold
buried inside a sampling function.

### Long-distance routing → supernodes and macro-edges

Supernodes sit at navmesh-region boundaries, skeleton junctions, and every
lock/bridge/POI. Between adjacent supernodes, the pipeline precomputes not
just the shortest path but a small set of **Pareto-optimal alternatives**
(e.g. a shallow shortcut vs. a deeper, longer channel vs. a route with no
fixed bridges) by finding the shortest path, removing its bottleneck
constraint, and repeating. At runtime, a vessel query filters these
alternatives by its own draft/beam/air-draft instead of re-searching fine
geometry for long-haul routes.

## Data fusion & confidence tiers

Survey-grade chart coverage isn't available everywhere for free, on either
continent. Rather than block a region on full coverage, every node, edge,
and POI carries a **source tier**, and both the pipeline and the consuming
router are aware of it:

| Tier | Source | Trust |
|---|---|---|
| 1 | Official hydrographic authority ENC/IENC | Ground truth |
| 2 | Other official waterway-authority data outside strict ENC | Authoritative, less standardized |
| 3 | OpenStreetMap / OpenSeaMap community tags | Good for topology, provisional for exact numbers |
| 4 | Bathymetric raster fill (GEBCO / EMODnet Bathymetry) | Statistically reasonable, not survey-grade — extra margin applied, never a sole safety authority |
| 5 | Human/AI-curated override, after human sign-off | Tier-1-equivalent for that specific location |
| 6 | AIS/vessel-density–derived candidate track | Soft preference / anomaly signal only, never a hard safety constraint on its own |

A region ships as soon as tiers 1–4 connect it, with lower-tier stretches
clearly marked so a consumer can render or weight them differently — a
graph that's honest about its gaps and improves over time, rather than one
that looks equally confident everywhere and occasionally isn't.

## Community override workflow

Some things aren't obvious from any chart — an unusual lock approach, a
bridge with an asymmetric fairway, a local shortcut everyone uses. The
pipeline treats these as a first-class, ongoing workflow rather than a
one-off fix:

1. **Automatic anomaly detection.** During ingestion, the pipeline flags
   locations that look wrong — a lock/bridge with no through-edge nearby, two
   graph components that are geographically close but topologically
   disconnected, a bottleneck constraint sourced only from tier 3/4 data
   with no corroboration, an abrupt, suspicious change in channel width.
2. **Proposal, human/AI-assisted.** Each flagged anomaly can be resolved by
   a contributor — human or an AI agent working from satellite imagery,
   chart attributes, and OSM tags — as a small, human-readable override
   file (GeoJSON/YAML) with required provenance: reason, evidence, and
   author.
3. **Review, not auto-merge.** Overrides land as pull requests against
   [signalk-router-data](https://github.com/marcelrv/signalk-router-data)'s
   `overrides/` directory — reviewable and diffable like code, the same way
   a compiled database contribution already works there. Once merged, an
   override is tier 5 for that location, permanently, and is picked up by
   every future rebuild of that region — a regeneration of the base graph
   never silently discards it.

## Real vessel-traffic signal

Two free, aggregated vessel-traffic datasets exist for the target
continents — EMODnet Human Activities' vessel-density grids (Europe,
broken out by ship type, including sailing and pleasure craft) and NOAA/
MarineCadastre's AIS products (US). The pipeline uses these two ways,
deliberately kept apart from hard safety data:

- **Validation**: after building a region, routes/edges that go strongly
  against real, aggregate traffic patterns in a fairway are flagged for
  review — a signal that the graph's preferred path may be wrong even
  though it passes the depth/width checks.
- **Gap-filling candidates**: in areas with thin official charting but a
  clear, consistent traffic ridge, that ridge is extracted as a tier-6
  candidate edge and routed into the anomaly queue above — never injected
  directly into the routable graph, since real traffic tells you *where*
  boats go, not that the charted depth is safe for a given draft.

## Free data sources

| Region | Layer | Source |
|---|---|---|
| US coastal | ENC (S-57) | NOAA Office of Coast Survey |
| US inland rivers | Inland ENC | US Army Corps of Engineers (`ienccloud.us`) |
| Europe coastal | National ENCs | National hydrographic offices (coverage/terms vary by country) |
| Europe inland | IENC | `eurisportal.eu` and national waterway authorities (harmonized Inland ECDIS standard) |
| Global/gap-fill | Bathymetry | GEBCO (global) and EMODnet Bathymetry (Europe, higher resolution) |
| Global | Coastline, waterways, seamarks | OpenStreetMap + OpenSeaMap |
| Europe | Vessel density (incl. sailing/pleasure) | EMODnet Human Activities |
| US | AIS vessel traffic | MarineCadastre.gov (AccessAIS, bulk AIS, transit counts) |

Every source above has its own license and attribution terms (several,
including OpenStreetMap and some national ENC providers, are share-alike
or otherwise conditional, and GEBCO's own terms explicitly disclaim use for
safety of navigation on its own). See
[signalk-router-data's LICENSE-DATA.md](https://github.com/marcelrv/signalk-router-data/blob/main/LICENSE-DATA.md)
for the full, per-source breakdown that applies to compiled output
databases.

## Database format

The compiled `.sqlite.gz` schema — tables, node/POI ID hashing, source-tier
columns, and how a routing engine should consume navmesh regions,
skeleton/lane edges, and macro-edges — is specified in
[signalk-router-data's `specs/routing-database-format-specification.md`](https://github.com/marcelrv/signalk-router-data/blob/main/specs/routing-database-format-specification.md),
alongside the catalog format (`specs/routing-database-catalog.md`) that
lists published regions. Any tool producing a database matching that
format is a valid producer — this pipeline is the reference implementation,
not a requirement.

## Status & roadmap

This repository is a from-scratch rebuild of the graph-generation pipeline
around the architecture above. Rollout is phased by region, starting with
a pilot area with good existing test coverage, then scaling out to full
national/regional coverage before adding the hierarchical long-distance
layer. Phase 0-2 (navmesh/skeleton generation, funnel-algorithm routing)
are implemented; see `PHASE_3_DESIGN.md` for the detailed design of what
comes next (community data fusion, override workflow, vessel-traffic
validation, scale-out, hierarchical routing), `PHASE_4_DESIGN.md` for three
further sub-phases (dynamic, position-aware database loading; AI-vision-
assisted resolution of ambiguous path choices; bridge/lock wait-time and
schedule data), and `NEXT_PHASES.md` for
the tactical, in-progress bug-tracking log.

## Contributing

- **Code** (pipeline logic, new data-source ingesters, classification
  heuristics): open a PR against this repo.
- **Data fixes for a specific location** (a wrong lock passage, a missing
  bridge clearance, a channel that should be marked differently): open a
  PR against [signalk-router-data](https://github.com/marcelrv/signalk-router-data)'s
  `overrides/` directory — no need to run the pipeline yourself.
- **A whole new region's compiled database**: also via
  [signalk-router-data](https://github.com/marcelrv/signalk-router-data),
  per its `CONTRIBUTING.md` — any tool producing a schema-compatible
  database is accepted, this pipeline is one option, not the only one.
