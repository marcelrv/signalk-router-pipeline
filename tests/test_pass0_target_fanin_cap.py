"""Unit tests for Pass 0c/0d's Direction-A target fan-in cap (SPEC-GRAPH-DENSITY.md
§6.7).

Pass 0c (navmesh perimeter) and Pass 0d (inland nodes) each cap cross-type fan-in --
but only in one direction. Direction A ("each navmesh/inland vertex -> its k nearest
cross-type neighbours") caps how many connectors THAT vertex's own search may add;
Direction B (the symmetric reverse query) caps a navmesh/inland node as a TARGET.
Direction A's own target side (the "other" node being picked) has no cap at all: many
different navmesh vertices can each independently pick the SAME nearby point, with
nothing stopping that point from accumulating dozens of edges -- confirmed directly
against a real Zeeland build (a single "point"-kind node accumulated 42 short,
44-93m edges, 40 of them to distinct navmesh-perimeter vertices; verified this was
NOT Pass 2's doing, since Pass 2 has no distance cap and every one of these edges was
short).

`pass0_target_fanin_cap` defaults to 0 (disabled): nothing here changes real build
output until a build explicitly opts in via `--pass0-target-fanin-cap`, matching
`--sagitta-cap`/`--axis-dedup-cap`/`--inland-densify-max-segment-m`/
`--connector-merge-m`/`--pass2-max-fanin-per-node`'s convention.
"""
import math

from shapely.geometry import box

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    NODE_KIND_NAVMESH_VERTEX,
    DEFAULT_SOURCE_TIER,
)

BASE_LON, BASE_LAT = 4.0, 51.5


def _pipeline(pass0_target_fanin_cap=0, pass2_max_fanin_per_node=0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_pass2_fanin_cap.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        max_chord_sagitta_m=75.0,  # forces Pass 2's geometric-search path
        pass0_target_fanin_cap=pass0_target_fanin_cap,
        pass2_max_fanin_per_node=pass2_max_fanin_per_node,
    )
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


def _target_with_navmesh_ring(pipeline, n_sources=6, radius_deg=0.001):
    """One "other"-type target node T at (BASE_LON, BASE_LAT), plus `n_sources`
    navmesh-kind nodes arranged evenly on a circle of `radius_deg` around it, wired
    into a RING (each connected to its two circle-neighbours) -- mirroring a real
    navmesh perimeter piece, which is exactly the shape Pass 0c's own docstring
    describes: already one union-find group via its own ring edges, needing its own
    mechanism (not the union-find-gated Pass 0/0b/1) to give EVERY perimeter vertex a
    real chance at connecting to nearby cross-type nodes.

    Without the ring, plain Pass 0 (uncapped, no type restriction) already connects T
    to every source before Pass 0c gets a chance to contribute anything -- confirmed
    directly while designing this fixture. With the ring, T and the source ring start
    as two separate union-find groups; Pass 0/0b/1 (union-find gated) only ever add
    ONE connection between them before treating the pair as "already connected", so
    Pass 0c's own union-find-bypassing Direction A is what actually gives T most of
    its additional edges -- the real-world scenario this cap targets.

    Returns (target_id, [navmesh_source_ids...]).
    """
    target_id = pipeline._get_or_create_node(BASE_LON, BASE_LAT, "coastal", context="test")
    source_ids = []
    for i in range(n_sources):
        theta = 2 * math.pi * i / n_sources
        lon = BASE_LON + radius_deg * math.cos(theta)
        lat = BASE_LAT + radius_deg * math.sin(theta)
        node_id = pipeline._get_or_create_node(lon, lat, "coastal", context="test")
        pipeline._stamp_node(node_id, NODE_KIND_NAVMESH_VERTEX, DEFAULT_SOURCE_TIER, None)
        source_ids.append(node_id)
    for a, b in zip(source_ids, source_ids[1:] + source_ids[:1]):
        pipeline.graph.add_edge(a, b, edge_type="coastal")
        pipeline.graph.add_edge(b, a, edge_type="coastal")
    return target_id, source_ids


def _covering_component_polygon():
    return box(BASE_LON - 0.01, BASE_LAT - 0.01, BASE_LON + 0.01, BASE_LAT + 0.01)


# Real caller (_ensure_coastal_connectivity) always passes 500.0; Pass 0/0b/0c all
# gate candidates by this same radius, so it must comfortably cover this fixture's
# ~110m target-to-source spacing for those passes to engage at all.
SNAP_RADIUS_M = 500.0


class TestDisabledByDefaultReproducesUnlimitedTargetFanIn:
    def test_target_accumulates_every_navmesh_source_when_cap_is_zero(self):
        p = _pipeline(pass0_target_fanin_cap=0)
        target_id, source_ids = _target_with_navmesh_ring(p, n_sources=6)
        ids = [target_id] + source_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        assert p.graph.out_degree(target_id) == len(source_ids)
        assert p._stitch_diag["pass0c"].get("target_fanin_capped", 0) == 0


class TestCapEnabledBoundsPass0cTargetFanIn:
    def test_pass0c_success_onto_the_target_never_exceeds_the_cap(self):
        cap = 2
        p = _pipeline(pass0_target_fanin_cap=cap)
        target_id, source_ids = _target_with_navmesh_ring(p, n_sources=6)
        ids = [target_id] + source_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        # Pass 0c's OWN contribution to the target must respect the cap.
        assert p._stitch_diag["pass0c"]["success"] <= cap
        assert p._stitch_diag["pass0c"]["target_fanin_capped"] > 0
        # Total degree is bounded too: Pass 0/0b (union-find gated, already-connected
        # after the ring's own single merge) contribute at most their own one
        # pre-cap connection on top of Pass 0c's capped contribution.
        assert p.graph.out_degree(target_id) < len(source_ids)

    def test_every_source_still_ends_up_connected(self):
        # The whole point of the cap: sources that couldn't reach the (capped-out)
        # target must still end up connected overall (here, trivially via their own
        # pre-existing ring edges to each other), rather than anything regressing.
        cap = 2
        p = _pipeline(pass0_target_fanin_cap=cap)
        target_id, source_ids = _target_with_navmesh_ring(p, n_sources=6)
        ids = [target_id] + source_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        parent = {n: n for n in ids}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        for n in ids:
            for nbr in p.graph.neighbors(n):
                if nbr in parent:
                    union(n, nbr)
        assert len({find(n) for n in ids}) == 1


class TestValidation:
    def test_zero_is_accepted(self):
        p = _pipeline(pass0_target_fanin_cap=0)
        assert p.classification_config.pass0_target_fanin_cap == 0

    def test_positive_value_is_accepted(self):
        p = _pipeline(pass0_target_fanin_cap=3)
        assert p.classification_config.pass0_target_fanin_cap == 3
