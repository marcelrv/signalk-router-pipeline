"""Unit tests for `channel_axis_deadend_stitch_m`
(docs/SPEC-CHANNEL-AXES.md follow-up): a channel_axes buoy-chain axis can end as a
true graph dead end far from the rest of the network, since `derive_channel_axes.py`
only knows the buoy line itself, not where the nearest medial-axis skeleton branch
ends. Found via a live user report (a real route near the Wicomico River, MD
abandoned a marked fairway for a longer, shallower detour at exactly such a dead
end) and confirmed on the real built graph: 221 such dead ends statewide, median
gap to the nearest non-axis node 211m, p90 948m.

A second live user report was skeptical of the first version of this fix, and
correctly so: it connected to the plain nearest OTHER node regardless of that
node's own connectivity, which on real geometry usually meant another equally
isolated buoy-chain fragment in the same tangle -- both ends were already in the
same connected component the long way round, so the shortest-path cost between two
real points was measured byte-identical before and after. The fix now only connects
to an ANCHORED candidate (one touching at least one non-channel_axes edge) --
`TestPrefersAnchoredCandidate` below is this regression's dedicated coverage.

All fixtures are synthetic geometry -- no real chart data.
"""
import math

import geopandas as gpd
import pytest
from shapely.geometry import box

from nautical_routing_pipeline import (
    NauticalRoutingPipeline,
    CHANNEL_AXIS_DEADEND_STITCH_MAX_M,
    _default_data_sources,
)


def _pipeline(channel_axis_deadend_stitch_m=0.0, channel_axis_deadend_max_connections=3):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_channel_axes_ingest.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:",
                                use_channel_axes=True,
                                channel_axis_deadend_stitch_m=channel_axis_deadend_stitch_m,
                                channel_axis_deadend_max_connections=channel_axis_deadend_max_connections)
    p.layer_source_ids = {s["name"]: i + 1 for i, s in enumerate(_default_data_sources(True))}
    return p


def _offset(lon, lat, bearing_deg, distance_m):
    """A point `distance_m` from (lon, lat) at `bearing_deg` (0=east, 90=north,
    matching atan2(dy, dx) in a locally-flat east/north metre frame -- the same
    convention `_connect_channel_axis_deadends` uses for its own sector angle).
    Flat-earth approximation, fine at the few-hundred-metre scale these tests use."""
    brg = math.radians(bearing_deg)
    dlat = (distance_m * math.sin(brg)) / 111320.0
    dlon = (distance_m * math.cos(brg)) / (111320.0 * math.cos(math.radians(lat)))
    return lon + dlon, lat + dlat


def _add_node(p, nid, lon, lat):
    p.graph.add_node(nid, lon=lon, lat=lat)


def _anchor(p, nid, lon, lat, real_src):
    """Add `nid` plus two real (non-channel_axes) edges to fresh mainland nodes,
    so `nid` qualifies as an eligible candidate under BOTH rules: not itself a
    terminus (degree 2, not 1) AND anchored (touches a non-channel_axes edge) --
    a genuine through-point of the real network, not just another isolated
    channel_axes fragment (nor a real but still-terminal stub, which a single
    mainland edge would produce -- see test_skips_an_anchored_candidate_that_is_
    itself_a_terminus for why a degree-1 "anchored" node must NOT qualify)."""
    _add_node(p, nid, lon, lat)
    for suffix, dlon, dlat in (("_mainland_a", 0.001, 0.001), ("_mainland_b", -0.001, 0.001)):
        mainland = f"{nid}{suffix}"
        _add_node(p, mainland, lon + dlon, lat + dlat)
        p.graph.add_edge(nid, mainland, source_id=real_src)
        p.graph.add_edge(mainland, nid, source_id=real_src)


class TestValidation:
    @pytest.mark.parametrize("bad", [float("nan"), -0.1, float("inf"),
                                     CHANNEL_AXIS_DEADEND_STITCH_MAX_M])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_channel_axis_deadend_stitch_m(bad)

    @pytest.mark.parametrize("ok", [0.0, 500.0, 1500.0])
    def test_accepts_range(self, ok):
        NauticalRoutingPipeline._validate_channel_axis_deadend_stitch_m(ok)


class TestConnectChannelAxisDeadends:
    def test_disabled_by_default_is_a_no_op(self):
        p = _pipeline(channel_axis_deadend_stitch_m=0.0)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.700, 51.440)
        _add_node(p, "junction", 3.701, 51.440)
        _anchor(p, "skeleton", 3.70000, 51.44080, inland_src)  # ~89m away
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)
        assert list(p.graph.neighbors("tip")) == ["junction"]  # unchanged

    def test_connects_a_dead_end_to_an_anchored_node_within_radius(self):
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        # "tip" is the free end of a two-node channel_axes stub; "skeleton" is an
        # anchored (real-network) node ~89m away that "tip" should reach out to.
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        _anchor(p, "skeleton", 3.70000, 51.44080, inland_src)
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert p.graph.has_edge("tip", "skeleton")
        assert p.graph.has_edge("skeleton", "tip")
        assert set(p.graph.neighbors("tip")) == {"junction", "skeleton"}

    def test_does_not_connect_beyond_radius(self):
        p = _pipeline(channel_axis_deadend_stitch_m=50.0)  # smaller than the ~89m gap
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        _anchor(p, "skeleton", 3.70000, 51.44080, inland_src)  # ~89m, beyond the 50m radius
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert not p.graph.has_edge("tip", "skeleton")
        assert list(p.graph.neighbors("tip")) == ["junction"]

    def test_rejects_a_candidate_whose_connector_crosses_land(self):
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        _anchor(p, "skeleton", 3.70000, 51.44080, inland_src)  # ~89m north of "tip"
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        # A land strip directly between "tip" and "skeleton" blocks the only
        # straight connector -- the dead end must stay unconnected, not get a
        # straight-line shortcut across charted land.
        p.gdfs["land"] = gpd.GeoDataFrame(
            geometry=[box(3.69950, 51.44030, 3.70050, 51.44050)], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert not p.graph.has_edge("tip", "skeleton")
        assert list(p.graph.neighbors("tip")) == ["junction"]

    def test_ignores_dead_ends_not_sourced_from_channel_axes(self):
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        _anchor(p, "skeleton", 3.70000, 51.44080, inland_src)
        # Ordinary inland_waterways stub, not a channel_axes one -- must be left alone.
        p.graph.add_edge("tip", "junction", source_id=inland_src)
        p.graph.add_edge("junction", "tip", source_id=inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert not p.graph.has_edge("tip", "skeleton")
        assert list(p.graph.neighbors("tip")) == ["junction"]

    def test_no_channel_axes_nodes_is_a_no_op(self):
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "a", 3.70000, 51.44000)
        _add_node(p, "b", 3.70100, 51.44000)
        p.graph.add_edge("a", "b", source_id=inland_src)
        p.graph.add_edge("b", "a", source_id=inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        # Should not raise, and should not touch anything.
        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)
        assert list(p.graph.neighbors("a")) == ["b"]

    def test_no_anchored_nodes_at_all_is_a_no_op(self):
        # Every node in the graph is channel_axes-only -- there is nothing safe to
        # connect to (connecting dead-end to dead-end is exactly the regression
        # this fix closes), so the pass must do nothing rather than wire the
        # tangle together internally.
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        axis_src = p.layer_source_ids["channel_axes"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        _add_node(p, "other_tip", 3.70000, 51.44080)  # ~89m away, but unanchored
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert list(p.graph.neighbors("tip")) == ["junction"]


class TestPrefersAnchoredCandidate:
    """Dedicated regression coverage for the bug a live user report caught: the
    first version of this pass grabbed the plain nearest OTHER node, which on real
    geometry was usually another isolated channel_axes fragment in the same tangle
    -- a no-op for routing, since both ends were already connected the long way
    round. The fix must skip a closer unanchored candidate in favour of a farther
    (but still in-radius) anchored one.
    """

    def test_skips_a_closer_unanchored_through_node_for_a_farther_anchored_one(self):
        """The real bug, reproduced exactly: the rejected candidate is NOT itself a
        terminus (degree 2, so rule 1 alone would accept it) -- it is a through-node
        of a different, but equally isolated, channel_axes-only fragment. Only rule
        2 (anchored) catches this."""
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        # Closer (~40m) but unanchored AND not a terminus (degree 2, both edges
        # channel_axes) -- a through-node of an adjacent, equally isolated
        # buoy-chain fragment, exactly the real-world "same tangle" case.
        _add_node(p, "near_unanchored", 3.70000, 51.44036)
        _add_node(p, "near_unanchored_a", 3.70010, 51.44036)
        _add_node(p, "near_unanchored_b", 3.69990, 51.44036)
        for other in ("near_unanchored_a", "near_unanchored_b"):
            p.graph.add_edge("near_unanchored", other, source_id=axis_src)
            p.graph.add_edge(other, "near_unanchored", source_id=axis_src)
        # Farther (~89m) but anchored -- the real, useful network.
        _anchor(p, "far_anchored", 3.70000, 51.44080, inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert p.graph.has_edge("tip", "far_anchored")
        assert not p.graph.has_edge("tip", "near_unanchored")

    def test_skips_an_anchored_candidate_that_is_itself_a_terminus(self):
        """Symmetric case: a real (non-channel_axes) but degree-1 stub is
        "anchored" under rule 2's plain definition, yet is itself a terminus --
        connecting to it would just chain two dead ends together. Only rule 1
        catches this."""
        p = _pipeline(channel_axis_deadend_stitch_m=500.0)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", 3.70100, 51.44000)
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        # A real charted stub (inland_waterways-sourced) but degree 1 -- a
        # terminus itself, not a through-point into the wider network.
        _add_node(p, "real_but_terminal", 3.70000, 51.44036)
        _add_node(p, "real_stub_far_end", 3.70050, 51.44036)
        p.graph.add_edge("real_but_terminal", "real_stub_far_end", source_id=inland_src)
        p.graph.add_edge("real_stub_far_end", "real_but_terminal", source_id=inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m)

        assert not p.graph.has_edge("tip", "real_but_terminal")


class TestAngularSectorSpread:
    """A marked channel typically runs down the middle of open water, with real
    skeleton on both banks. Connecting to the N nearest-by-distance candidates
    can miss one bank entirely if the other happens to be locally denser or
    closer -- these tests cover the fix: partition the search circle into
    max_connections angular sectors and take the nearest eligible candidate from
    EACH sector, not just the closest ones overall.
    """

    def test_connects_to_multiple_candidates_spread_across_sectors(self):
        p = _pipeline(channel_axis_deadend_stitch_m=500.0, channel_axis_deadend_max_connections=3)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", *_offset(3.70000, 51.44000, 180, 100))
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        # Three anchored candidates 120 degrees apart -- one per sector.
        for name, bearing in (("east_bank", 10), ("nw_bank", 130), ("sw_bank", 250)):
            _anchor(p, name, *_offset(3.70000, 51.44000, bearing, 200), inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m,
                                         p.classification_config.channel_axis_deadend_max_connections)

        assert p.graph.has_edge("tip", "east_bank")
        assert p.graph.has_edge("tip", "nw_bank")
        assert p.graph.has_edge("tip", "sw_bank")
        assert len(list(p.graph.neighbors("tip"))) == 4  # junction + all three

    def test_only_the_nearer_candidate_in_a_shared_sector_is_used(self):
        """Two anchored candidates both fall in the SAME sector (10 degrees apart,
        well inside one 120-degree-wide sector) -- only the nearer one should be
        connected; the farther one, though in-radius and eligible, must not also
        get a connection just because a "slot" happens to be free elsewhere."""
        p = _pipeline(channel_axis_deadend_stitch_m=500.0, channel_axis_deadend_max_connections=3)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", *_offset(3.70000, 51.44000, 180, 100))
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        _anchor(p, "near_same_sector", *_offset(3.70000, 51.44000, 10, 100), inland_src)
        _anchor(p, "far_same_sector", *_offset(3.70000, 51.44000, 20, 300), inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m,
                                         p.classification_config.channel_axis_deadend_max_connections)

        assert p.graph.has_edge("tip", "near_same_sector")
        assert not p.graph.has_edge("tip", "far_same_sector")

    def test_max_connections_caps_the_number_of_new_edges(self):
        p = _pipeline(channel_axis_deadend_stitch_m=500.0, channel_axis_deadend_max_connections=2)
        axis_src = p.layer_source_ids["channel_axes"]
        inland_src = p.layer_source_ids["inland_waterways"]
        _add_node(p, "tip", 3.70000, 51.44000)
        _add_node(p, "junction", *_offset(3.70000, 51.44000, 180, 100))
        p.graph.add_edge("tip", "junction", source_id=axis_src)
        p.graph.add_edge("junction", "tip", source_id=axis_src)
        # Four well-spread anchored candidates -- with max_connections=2 there
        # are only two (180-degree-wide) sectors, so at most two connections
        # can ever be made regardless of how many eligible candidates exist.
        for name, bearing in (("n", 45), ("e", 135), ("s", 225), ("w", 315)):
            _anchor(p, name, *_offset(3.70000, 51.44000, bearing, 200), inland_src)
        p.gdfs["land"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        p._connect_channel_axis_deadends(p.classification_config.channel_axis_deadend_stitch_m,
                                         p.classification_config.channel_axis_deadend_max_connections)

        new_neighbors = set(p.graph.neighbors("tip")) - {"junction"}
        assert len(new_neighbors) == 2


class TestValidateMaxConnections:
    @pytest.mark.parametrize("bad", [0, -1, 1.5, True, False, "3"])
    def test_rejects_non_positive_or_non_int(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_channel_axis_deadend_max_connections(bad)

    @pytest.mark.parametrize("ok", [1, 2, 3, 10])
    def test_accepts_positive_ints(self, ok):
        NauticalRoutingPipeline._validate_channel_axis_deadend_max_connections(ok)
