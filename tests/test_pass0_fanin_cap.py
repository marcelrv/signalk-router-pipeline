"""Unit tests for Pass 0's own fan-in/fan-out cap (follow-on to SPEC-GRAPH-DENSITY.md).

`_stitch_component_pieces`'s Pass 0 (the very first stitching pass, run before Pass
0b/0c/0d) is a raw, type-blind, per-node k=6 nearest-neighbor pass with NO fan-in/
fan-out cap of any kind -- unlike Pass 2 (`pass2_max_fanin_per_node`) and Pass 0c/0d's
own target side (`pass0_target_fanin_cap`, a DIFFERENT, already-shipped flag scoped
only to those two passes). Where many small fragments sit close together (e.g. an
intricate-but-deep coastline area over-fragmented by `_split_wide_narrow`'s erosion --
see `--narrow-fragment-reclass-max-fraction`), Pass 0 independently discovers and
accepts a valid connector for many distinct fragment pairs before any outward-biased
pass gets a chance to dominate, producing a dense crisscross tangle.

`pass0_fanin_cap` defaults to 0 (disabled): nothing here changes real build output
until a build explicitly opts in via `--pass0-fanin-cap`, matching
`--pass2-max-fanin-per-node`/`--pass0-target-fanin-cap`'s convention. It is a
SEPARATE budget from `pass0_target_fanin_cap` -- this suite must never observe that
flag change to confirm the two are independent.
"""
import math

from shapely.geometry import box

from nautical_routing_pipeline import ClassificationConfig, NauticalRoutingPipeline

BASE_LON, BASE_LAT = 4.0, 51.5

# Radius for _spoke_with_satellites_graph's decoy ring -- must be well above
# _get_or_create_node's ~5-decimal-place (~1.1m) coordinate-rounding grain so all
# 6 decoy positions round to distinct nodes (0.00001deg was tried first and found
# to collapse two pairs of positions onto the same rounded coordinate, silently
# leaving only 4 of the intended 6). Still tiny relative to hub_radius_deg
# (0.003deg default), so decoys remain unambiguously H's own 6 nearest neighbors.
DECOY_RADIUS_DEG = 0.0002


def _pipeline(pass0_fanin_cap=0, pass0_cross_type_first=False):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_pass2_fanin_cap.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        pass0_fanin_cap=pass0_fanin_cap,
        pass0_cross_type_first=pass0_cross_type_first,
    )
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


def _hub_and_spokes_graph(pipeline, n_spokes=5, radius_deg=0.001):
    """One hub node H at (BASE_LON, BASE_LAT) plus `n_spokes` further singleton
    "groups" (one node each, no navmesh_kind stamp -- so navmesh_idx stays empty and
    _stitch_component_pieces' fallback `else: _run_pass0()` branch is what runs Pass
    0 here, isolating it from Pass 0b/0c), arranged on a circle of `radius_deg`
    around H. n_spokes <= 5 means H's own k=min(6, N) nearest-neighbor query
    trivially includes every spoke, and each spoke's nearest OTHER node is
    unambiguously H (chord length between two adjacent spokes,
    2*r*sin(pi/n_spokes), strictly exceeds r for n_spokes <= 5 -- mirrors
    tests/test_pass2_fanin_cap.py's identical fixture and its own comment on why
    6 spokes would introduce an exact tie) -- the scenario that makes H a fan-in
    magnet for Pass 0 without the cap.

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


def _spoke_with_satellites_graph(pipeline, n_satellites=5, n_decoys=6,
                                  hub_radius_deg=0.003, sat_offset_deg=0.0001):
    """Isolates fan-in on a SPOKE (not the hub) specifically -- the
    `_hub_and_spokes_graph` fixture above only ever exercises the cap on the
    literal hub, because in a small pool (few total nodes) the hub's own k=6
    nearest-neighbor list covers the ENTIRE pool, so the hub's single Pass 0
    iteration absorbs every other node into one union-find group before any
    other node gets its own turn -- confirmed directly while designing this
    fixture: without decoys, a "busy" node placed like a second hub still only
    ever accumulates 1 real connection regardless of cap, because the true hub
    always beats it to every satellite first.

    `n_decoys` nodes sit a few metres from the hub H -- far closer than the
    busy spoke B, so H's own k=6 nearest-neighbor list is entirely decoys (H
    never even considers B or its satellites in its own iteration). They are
    still well within `snap_radius_m` (500m), so they DO receive real Pass 0
    edges from H -- that's exactly the mechanism that keeps H occupied: they
    fill every one of H's own k=6 neighbor-list slots (and, when
    `pass0_fanin_cap` is active, count toward H's own accumulated cap), not
    because they're radius-rejected. `DECOY_RADIUS_DEG` is chosen well above
    `_get_or_create_node`'s ~5-decimal-place (~1.1m) coordinate-rounding grain
    -- confirmed directly: a too-small radius here previously collapsed two
    pairs of the 6 intended decoy positions onto the same rounded coordinate,
    silently leaving only 4 distinct decoy nodes instead of 6. B sits
    `hub_radius_deg` from H, with `n_satellites` further nodes clustered
    `sat_offset_deg` around B (<< `hub_radius_deg`, so B is unambiguously each
    satellite's own nearest neighbor). This leaves B to independently
    accumulate its OWN Pass 0 connections to its satellites, exactly the
    scenario `pass0_fanin_cap` is meant to bound on any node, not just a
    designated "hub".

    Verified directly: with `pass0_fanin_cap` disabled, B accumulates all
    `n_satellites` connections (matching `_hub_and_spokes_graph`'s own
    uncapped-baseline behavior); with a cap of N, B accumulates exactly N.
    The decoys MUST be included in the caller's own `ids` list passed to
    `_stitch_component_pieces` (returned here for that reason) -- Pass 0's
    KD-tree is built only from whatever `ids` it's given, so a decoy that
    exists in the graph but isn't part of `ids` does nothing to distract H.

    Returns (hub_id, decoy_ids, busy_id, [satellite_ids...]).
    """
    hub_id = pipeline._get_or_create_node(BASE_LON, BASE_LAT, "coastal", context="test")
    decoy_ids = []
    for i in range(n_decoys):
        theta = 2 * math.pi * i / n_decoys
        lon = BASE_LON + DECOY_RADIUS_DEG * math.cos(theta)
        lat = BASE_LAT + DECOY_RADIUS_DEG * math.sin(theta)
        decoy_ids.append(pipeline._get_or_create_node(lon, lat, "coastal", context="test"))
    busy_lon, busy_lat = BASE_LON + hub_radius_deg, BASE_LAT
    busy_id = pipeline._get_or_create_node(busy_lon, busy_lat, "coastal", context="test")
    satellite_ids = []
    for i in range(n_satellites):
        theta = 2 * math.pi * i / n_satellites
        lon = busy_lon + sat_offset_deg * math.cos(theta)
        lat = busy_lat + sat_offset_deg * math.sin(theta)
        satellite_ids.append(pipeline._get_or_create_node(lon, lat, "coastal", context="test"))
    return hub_id, decoy_ids, busy_id, satellite_ids


def _covering_component_polygon():
    return box(BASE_LON - 0.01, BASE_LAT - 0.01, BASE_LON + 0.01, BASE_LAT + 0.01)


# Real caller (_ensure_coastal_connectivity) always passes 500.0; must comfortably
# cover this fixture's ~111m hub-to-spoke spacing (radius_deg=0.001) for Pass 0 to
# actually fire -- unlike tests/test_pass2_fanin_cap.py's own fixture, which
# deliberately uses a 1.0m radius to radius-reject Pass 0/0b/1 entirely so Pass 2 can
# be isolated. This suite wants the opposite: Pass 0 must be the thing that fires.
SNAP_RADIUS_M = 500.0


def _connected_components(pipeline, ids):
    parent = {n: n for n in ids}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for n in ids:
        for nbr in pipeline.graph.neighbors(n):
            if nbr in parent:
                union(n, nbr)
    return len({find(n) for n in ids})


class TestDisabledByDefaultReproducesUnlimitedPass0FanIn:
    def test_hub_accumulates_every_spoke_when_cap_is_zero(self):
        p = _pipeline(pass0_fanin_cap=0)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=5)
        ids = [hub_id] + spoke_ids

        added = p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        assert added > 0
        assert p.graph.out_degree(hub_id) == len(spoke_ids)
        assert p._stitch_diag["pass0"].get("fanin_capped", 0) == 0
        assert _connected_components(p, ids) == 1

    def test_spoke_accumulates_every_satellite_when_cap_is_zero(self):
        p = _pipeline(pass0_fanin_cap=0)
        hub_id, decoy_ids, busy_id, satellite_ids = _spoke_with_satellites_graph(p)
        ids = [hub_id] + decoy_ids + [busy_id] + satellite_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        satellite_edges = sum(1 for s in satellite_ids
                               if p.graph.has_edge(busy_id, s) or p.graph.has_edge(s, busy_id))
        assert satellite_edges == len(satellite_ids)
        assert p._stitch_diag["pass0"].get("fanin_capped", 0) == 0


class TestCapEnabledBoundsPass0HubFanIn:
    def test_hub_out_degree_never_exceeds_the_cap(self):
        cap = 2
        p = _pipeline(pass0_fanin_cap=cap)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=5)
        ids = [hub_id] + spoke_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        assert p.graph.out_degree(hub_id) <= cap
        assert p._stitch_diag["pass0"]["fanin_capped"] > 0

    def test_every_spoke_still_ends_up_connected_via_fallback_passes(self):
        # The whole point of the cap: spokes that couldn't reach the (capped-out)
        # hub via Pass 0 must still end up connected overall, via Pass 1/Pass 2/
        # gap-resolve exactly as they already are for whatever Pass 0c/0d's own
        # caps reject -- not silently stranded.
        cap = 2
        p = _pipeline(pass0_fanin_cap=cap)
        hub_id, spoke_ids = _hub_and_spokes_graph(p, n_spokes=5)
        ids = [hub_id] + spoke_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        assert _connected_components(p, ids) == 1

    def test_cap_applies_to_a_spoke_receiving_independent_fan_in_too(self):
        # Pass 0 has no source/target direction (unlike pass0_target_fanin_cap's
        # target-only asymmetry) -- a spoke that is itself the nearer neighbor for
        # several OTHER nodes must also be capped, not just a designated "hub".
        # Uses _spoke_with_satellites_graph, not _hub_and_spokes_graph: in a small
        # pool, the hub's own single Pass 0 iteration absorbs every other node
        # before a plain spoke ever gets independent fan-in of its own to test --
        # confirmed directly while designing this fixture (see its own docstring).
        cap = 2
        p = _pipeline(pass0_fanin_cap=cap)
        hub_id, decoy_ids, busy_id, satellite_ids = _spoke_with_satellites_graph(p)
        ids = [hub_id] + decoy_ids + [busy_id] + satellite_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        # Exact bound on the busy spoke's own satellite connections specifically
        # (not its total out-degree, which could also pick up an unrelated edge
        # to the hub) -- isolates Pass 0's own capped contribution from any
        # fallback-pass edge that would make a looser total-degree bound pass
        # even if the cap itself were broken.
        satellite_edges = sum(1 for s in satellite_ids
                               if p.graph.has_edge(busy_id, s) or p.graph.has_edge(s, busy_id))
        assert satellite_edges == cap
        assert p._stitch_diag["pass0"]["fanin_capped"] > 0

    def test_every_satellite_still_ends_up_connected_despite_the_spoke_cap(self):
        cap = 2
        p = _pipeline(pass0_fanin_cap=cap)
        hub_id, decoy_ids, busy_id, satellite_ids = _spoke_with_satellites_graph(p)
        ids = [hub_id] + decoy_ids + [busy_id] + satellite_ids

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=SNAP_RADIUS_M)

        assert _connected_components(p, ids) == 1


class TestPass0FaninCapIndependentOfPass0TargetFaninCap:
    def test_pass0_target_fanin_cap_stays_zero_when_only_pass0_fanin_cap_is_set(self):
        p = _pipeline(pass0_fanin_cap=2)
        assert p.classification_config.pass0_target_fanin_cap == 0


class TestValidation:
    def test_zero_is_accepted(self):
        p = _pipeline(pass0_fanin_cap=0)
        assert p.classification_config.pass0_fanin_cap == 0

    def test_positive_value_is_accepted(self):
        p = _pipeline(pass0_fanin_cap=3)
        assert p.classification_config.pass0_fanin_cap == 3
