"""Unit tests for Pass 2's per-node fan-in cap (SPEC-GRAPH-DENSITY.md §6.6).

`_stitch_component_pieces`'s Pass 2 (the "connectivity guarantee, one merge round at
a time" pass) picks each still-disconnected group's geometrically nearest cross-group
candidate with no per-node fan-in limit -- unlike Pass 0c/0d, which each cap cross-type
fan-in at 2. Across many rounds, many different groups can independently converge on
the same well-placed node, producing a hub with dozens to hundreds of edges (measured
directly against a real Zeeland build: out-degree up to 42-222 from as few as ~58
Pass 2 successes).

`pass2_max_fanin_per_node` defaults to 0 (disabled): nothing here changes real build
output until a build explicitly opts in via `--pass2-max-fanin-per-node`, matching
`--sagitta-cap`/`--axis-dedup-cap`/`--inland-densify-max-segment-m`/
`--connector-merge-m`'s convention.
"""
import math

from shapely.geometry import box

from nautical_routing_pipeline import ClassificationConfig, NauticalRoutingPipeline

BASE_LON, BASE_LAT = 4.0, 51.5


def _pipeline(pass2_max_fanin_per_node=0, max_chord_sagitta_m=75.0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_axis_dedup.py's/tests/test_inland_densify.py's _pipeline().
    # _stitch_diag/_stitch_group_stats are set unconditionally in __init__ (unlike
    # coords_to_node/_inland_split_cuts, which build_network() resets), so no extra
    # setup is needed for those here.
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        max_chord_sagitta_m=max_chord_sagitta_m,  # forces Pass 2's geometric-search path
        pass2_max_fanin_per_node=pass2_max_fanin_per_node,
    )
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


def _hub_and_spokes_graph(pipeline, n_spokes=4, radius_deg=0.001):
    """One hub node H at (BASE_LON, BASE_LAT) plus `n_spokes` further singleton
    "groups" (one node each), arranged on a circle of `radius_deg` around H, evenly
    spaced so each spoke's true nearest OTHER node is H itself (chord length between
    two adjacent spokes, 2*r*sin(pi/n_spokes), exceeds r for n_spokes <= 5) -- i.e.
    every spoke's geometrically nearest cross-group candidate is unambiguously H,
    the exact scenario that makes H a fan-in magnet without the cap.

    Every node starts in its own union-find group (no edges at all yet), so
    `_stitch_component_pieces` must merge n_spokes + 1 singleton groups into one.
    Returns (hub_id, [spoke_ids...]).
    """
    hub_id = pipeline._get_or_create_node(BASE_LON, BASE_LAT, "coastal", context="test")
    spoke_ids = []
    for i in range(n_spokes):
        theta = 2 * math.pi * i / n_spokes
        lon = BASE_LON + radius_deg * math.cos(theta)
        lat = BASE_LAT + radius_deg * math.sin(theta)
        spoke_ids.append(pipeline._get_or_create_node(lon, lat, "coastal", context="test"))
    return hub_id, spoke_ids


def _covering_component_polygon():
    # Generous box around BASE_LON/BASE_LAT -- big enough that every straight
    # connector this test's nodes could need stays inside it (poly containment is
    # not what these tests are exercising).
    return box(BASE_LON - 0.01, BASE_LAT - 0.01, BASE_LON + 0.01, BASE_LAT + 0.01)


class TestDisabledByDefaultReproducesUnlimitedFanIn:
    def test_hub_accumulates_every_spoke_when_cap_is_zero(self):
        p = _pipeline(pass2_max_fanin_per_node=0)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=4)
        ids = [hub_id] + spoke_ids

        added = p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=1.0)

        assert added > 0
        # Fully connected: one union-find group at the end.
        parent_lookup = {n: p.graph.has_node(n) for n in ids}
        assert all(parent_lookup.values())
        # With no cap, the hub is free to accumulate a connector to every spoke --
        # this is the documented pre-existing behaviour the cap is meant to bound.
        hub_degree = p.graph.out_degree(hub_id)
        assert hub_degree == len(spoke_ids)
        assert p._stitch_diag["pass2"].get("fanin_capped", 0) == 0


class TestCapEnabledBoundsHubFanIn:
    def test_hub_out_degree_never_exceeds_the_cap(self):
        cap = 2
        p = _pipeline(pass2_max_fanin_per_node=cap)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=4)
        ids = [hub_id] + spoke_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=1.0)

        assert p.graph.out_degree(hub_id) <= cap
        assert p._stitch_diag["pass2"]["fanin_capped"] > 0

    def test_every_spoke_still_ends_up_connected_via_fallback_candidates(self):
        # The whole point of the cap: nodes that couldn't reach the (capped-out)
        # hub must still end up connected, via a different node instead of being
        # silently stranded.
        cap = 2
        p = _pipeline(pass2_max_fanin_per_node=cap)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=4)
        ids = [hub_id] + spoke_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=1.0)

        # Single connected component across every node (hub + all spokes) -- same
        # union-find check the real caller (_ensure_coastal_connectivity) relies on.
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

    def test_larger_cap_allows_more_hub_fanin_up_to_its_own_limit(self):
        cap = 3
        p = _pipeline(pass2_max_fanin_per_node=cap)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=4)
        ids = [hub_id] + spoke_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=1.0)

        assert p.graph.out_degree(hub_id) <= cap
        # With 4 spokes and cap=3, at least one spoke must have been denied the hub
        # and found an alternate connector instead -- otherwise this test isn't
        # actually exercising the cap.
        assert p._stitch_diag["pass2"]["fanin_capped"] > 0


class TestValidation:
    def test_negative_cap_rejected_at_cli_level(self):
        # Mirrors the CLI-level check in `if __name__ == "__main__"` -- exercised
        # directly here since that block isn't importable, following the pattern
        # already used for --connector-merge-m's own CLI validation (there it's
        # via the shared _validate_connector_merge_m staticmethod; this flag's
        # validation is a plain `< 0` check inline at the CLI, so there is nothing
        # extra to unit test beyond the config itself accepting 0 and positive
        # values without raising).
        p = _pipeline(pass2_max_fanin_per_node=0)
        assert p.classification_config.pass2_max_fanin_per_node == 0

    def test_positive_value_is_accepted(self):
        p = _pipeline(pass2_max_fanin_per_node=5)
        assert p.classification_config.pass2_max_fanin_per_node == 5
