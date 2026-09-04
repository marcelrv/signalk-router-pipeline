"""Unit tests for connector merge/split (SPEC-GRAPH-DENSITY.md §6.5).

Five-plus prior rounds set out to cut graph node/edge density but net INCREASED it
(48,553 -> 64,717 nodes on the deployed Zeeland build). Two of the mechanisms that
compounded it shared one root cause: a "plant a node at this point" call site that
always minted something new instead of first checking whether the pipeline already
had something equivalent nearby.

`connector_merge_m` (default 0.0, disabled) governs the fix at both call sites:
  (1) `_connect_waterway_crossing` / `_get_or_split_inland_segment`: project a
      crossing/carve-reconnect point onto the target `inland_waterways` line and
      reuse an existing (or previously-split) vertex within tolerance, or split the
      specific graph edge spanning the true point of contact -- instead of always
      snapping to the line's nearest EXISTING vertex regardless of distance.
  (2) `_add_opening_bridge_edges`: dedupe near-coincident fairway/inland-waterways
      intersection points at one bridge, and reuse a nearby existing node instead of
      always minting a new one.

0.0 (default) disables both entirely and reproduces today's output byte-for-byte,
matching `--sagitta-cap`/`--axis-dedup-cap`/`--inland-densify-max-segment-m`'s
convention.
"""
import math

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    NODE_KIND_POINT,
    DEFAULT_SOURCE_TIER,
    WATERWAY_CONNECTOR_MAX_M,
)


def _pipeline(connector_merge_m=0.0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_axis_dedup.py's/tests/test_inland_densify.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(connector_merge_m=connector_merge_m)
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


class _FakeTransformer:
    """Deterministic, non-geographic stand-in for pyproj's Transformer in the pure
    _get_or_split_inland_segment tests below -- these only need distinct, stable
    lon/lat-shaped output per metric input, not real projection math."""

    @staticmethod
    def transform(x, y):
        return (x / 100000.0, y / 100000.0)


# ---------------------------------------------------------------------------
# _project_point_onto_line -- pure geometry, no pipeline needed.
# ---------------------------------------------------------------------------

class TestProjectPointOntoLine:
    def test_projects_perpendicular_onto_the_correct_segment(self):
        coords_m = _np_array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0]])
        seg_idx, t, proj_xy_m, dist_m = NauticalRoutingPipeline._project_point_onto_line(
            coords_m, (50.0, 10.0))
        assert seg_idx == 0
        assert t == pytest.approx(0.5)
        assert proj_xy_m == pytest.approx((50.0, 0.0))
        assert dist_m == pytest.approx(10.0)

    def test_picks_the_second_segment_when_closer(self):
        coords_m = _np_array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0]])
        seg_idx, t, proj_xy_m, dist_m = NauticalRoutingPipeline._project_point_onto_line(
            coords_m, (150.0, 5.0))
        assert seg_idx == 1
        assert t == pytest.approx(0.5)
        assert proj_xy_m == pytest.approx((150.0, 0.0))

    def test_clamps_t_past_an_endpoint_rather_than_extrapolating(self):
        coords_m = _np_array([[0.0, 0.0], [100.0, 0.0]])
        seg_idx, t, proj_xy_m, dist_m = NauticalRoutingPipeline._project_point_onto_line(
            coords_m, (-30.0, 4.0))
        assert seg_idx == 0
        assert t == pytest.approx(0.0)
        assert proj_xy_m == pytest.approx((0.0, 0.0))
        assert dist_m == pytest.approx(math.hypot(30.0, 4.0))


def _np_array(rows):
    import numpy as np
    return np.array(rows, dtype=float)


# ---------------------------------------------------------------------------
# _get_or_split_inland_segment -- the core merge-or-split primitive.
# ---------------------------------------------------------------------------

class TestGetOrSplitInlandSegment:
    LINE_ILOC = 0
    COORDS_WGS84 = [(0.0, 0.0), (0.001, 0.0), (0.002, 0.0)]  # 3 original vertices

    def _coords_m(self):
        return _np_array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0]])

    def _pipeline_with_seeded_edges(self, connector_merge_m=5.0):
        # In the real pipeline, _build_inland_network always creates every original
        # inland vertex/edge before any crossing/split ever runs -- replicate that
        # precondition here (just the segment-0 pair every test below exercises)
        # rather than relying on _get_or_split_inland_segment to create edges for
        # vertices it merely seeds as cut endpoints.
        p = _pipeline(connector_merge_m=connector_merge_m)
        u_lon, u_lat = self.COORDS_WGS84[0]
        v_lon, v_lat = self.COORDS_WGS84[1]
        u_id = p._get_or_create_node(u_lon, u_lat, "inland", context="test")
        v_id = p._get_or_create_node(v_lon, v_lat, "inland", context="test")
        p.graph.add_edge(u_id, v_id, edge_type="coastal")
        p.graph.add_edge(v_id, u_id, edge_type="coastal")
        return p

    def test_first_touch_seeds_both_original_endpoints_as_cuts(self):
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        node_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.5, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())

        entry = p._inland_split_cuts[self.LINE_ILOC][0]
        ts = sorted(c[0] for c in entry["cuts"])
        assert ts == [0.0, 0.5, 1.0]
        # The new split node sits strictly between the two original endpoints.
        u_id = p.coords_to_node[(0.0, 0.0)]
        v_id = p.coords_to_node[(0.001, 0.0)]
        assert node_id not in (u_id, v_id)
        assert p.graph.has_edge(u_id, node_id) and p.graph.has_edge(node_id, v_id)
        assert not p.graph.has_edge(u_id, v_id)
        assert p.waterway_connector_split_stats["split"] == 1

    def test_candidate_within_tolerance_of_an_endpoint_merges_not_splits(self):
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        # t=0.02 on a 100m segment is 2m from the left endpoint -- well inside the 5m
        # merge tolerance -- so this must reuse the endpoint, never insert a new node
        # right next to it.
        node_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.02, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        u_id = p.coords_to_node[(0.0, 0.0)]
        assert node_id == u_id
        assert p.waterway_connector_split_stats["split"] == 0
        assert p.waterway_connector_split_stats["merged"] == 1
        assert p.graph.number_of_nodes() == 2  # only the two original endpoints exist

    def test_candidate_within_tolerance_of_a_previous_split_merges_into_it(self):
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        first_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.50, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        # t=0.51 on a 100m segment is 1m from the t=0.50 split -- within tolerance.
        second_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.51, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        assert second_id == first_id
        assert p.waterway_connector_split_stats["split"] == 1
        assert p.waterway_connector_split_stats["merged"] == 1

    def test_two_splits_on_the_same_segment_ascending_order(self):
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        a_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.30, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        b_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.60, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        self._assert_three_way_split(p, a_id, b_id)

    def test_two_splits_on_the_same_segment_descending_order(self):
        # Same two candidates, opposite processing order -- must converge on the same
        # final topology (the order-independence _inland_split_cuts exists to guarantee).
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        b_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.60, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        a_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.30, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        self._assert_three_way_split(p, a_id, b_id)

    def _assert_three_way_split(self, p, a_id, b_id):
        u_id = p.coords_to_node[(0.0, 0.0)]
        v_id = p.coords_to_node[(0.001, 0.0)]
        assert len({u_id, a_id, b_id, v_id}) == 4
        assert p.graph.has_edge(u_id, a_id)
        assert p.graph.has_edge(a_id, b_id)
        assert p.graph.has_edge(b_id, v_id)
        assert not p.graph.has_edge(u_id, v_id)
        assert not p.graph.has_edge(u_id, b_id)
        assert not p.graph.has_edge(a_id, v_id)
        entry = p._inland_split_cuts[self.LINE_ILOC][0]
        assert sorted(c[0] for c in entry["cuts"]) == [0.0, 0.3, 0.6, 1.0]

    def test_cross_call_consistency_two_fresh_line_m_caches_same_pipeline(self):
        # line_m_cache is call-scoped in the real callers (a fresh dict per
        # build_navmesh_region/build_skeleton_network invocation); _inland_split_cuts
        # is pipeline-wide. Simulate two separate "outer calls" touching the same
        # original segment by simply calling the method independently twice (the
        # method itself never reads line_m_cache -- callers resolve coords_wgs84/
        # coords_m from it before calling in), confirming self._inland_split_cuts
        # alone -- not any call-scoped cache -- is what makes this safe.
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        a_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.30, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        b_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.60, self.COORDS_WGS84, coords_m, 5.0, _FakeTransformer())
        self._assert_three_way_split(p, a_id, b_id)

    def test_endpoint_collision_below_the_coordinate_rounding_grain_merges_not_self_loops(self):
        # CodeRabbit (PR #18): a merge_tol_m set BELOW _get_or_create_node's ~1.1m
        # coordinate-rounding grain can leave a real gap between what the exact
        # metric merge check (in t-space, against entry["len_m"]) accepts and what
        # 5-decimal-rounding on the REPROJECTED point collapses onto an existing
        # cut anyway. t=0.001 on this 100m segment is 0.1m from the left endpoint
        # (u) -- comfortably outside a deliberately tiny 0.05m merge_tol_m, so the
        # merge-check loop does NOT return early -- but _FakeTransformer's
        # projection of that same point rounds to the exact same (0.0, 0.0) 5dp
        # coordinate as u itself. Must reuse u, not remove the real u-v edge and
        # replace it with a u<->u self-loop plus a re-aliased u-v duplicate.
        p = self._pipeline_with_seeded_edges()
        coords_m = self._coords_m()
        u_id = p.coords_to_node[(0.0, 0.0)]
        v_id = p.coords_to_node[(0.001, 0.0)]

        node_id = p._get_or_split_inland_segment(
            self.LINE_ILOC, 0, 0.001, self.COORDS_WGS84, coords_m, 0.05, _FakeTransformer())

        assert node_id == u_id
        assert p.waterway_connector_split_stats["merged"] == 1
        assert p.waterway_connector_split_stats["split"] == 0
        assert p.graph.number_of_nodes() == 2  # no new node minted
        assert p.graph.has_edge(u_id, v_id) and p.graph.has_edge(v_id, u_id)
        assert not p.graph.has_edge(u_id, u_id)  # no self-loop


# ---------------------------------------------------------------------------
# _validate_connector_merge_m
# ---------------------------------------------------------------------------

class TestValidateConnectorMergeM:
    def test_zero_is_always_valid(self):
        NauticalRoutingPipeline._validate_connector_merge_m(0.0)  # must not raise

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, WATERWAY_CONNECTOR_MAX_M,
                                      WATERWAY_CONNECTOR_MAX_M + 1.0])
    def test_rejects_non_finite_negative_or_too_large(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_connector_merge_m(bad)

    def test_accepts_a_sane_positive_value(self):
        NauticalRoutingPipeline._validate_connector_merge_m(5.0)  # must not raise


# ---------------------------------------------------------------------------
# _add_opening_bridge_edges -- dedupe opening points, merge into a nearby node.
# ---------------------------------------------------------------------------

def _movable_bridge_gdf(polygon_wgs84):
    return gpd.GeoDataFrame({"catbrg": ["3"]}, geometry=[polygon_wgs84], crs="EPSG:4326")


class TestBridgeOpeningDedupe:
    # A small bridge polygon crossed by two nearly-coincident lines (a fairway and an
    # inland_waterways line ~2.2m apart) -- the two-source-layer case that produces a
    # near-duplicate opening point pair in practice.
    BRIDGE_BOX = box(3.8999, 51.4999, 3.9001, 51.5001)
    FAIRWAY_LINE = LineString([(3.899, 51.50000), (3.901, 51.50000)])
    INLAND_LINE = LineString([(3.899, 51.50002), (3.901, 51.50002)])  # ~2.2m north

    def _pipeline_with_bridge(self, connector_merge_m):
        p = _pipeline(connector_merge_m=connector_merge_m)
        p.gdfs["bridges"] = _movable_bridge_gdf(self.BRIDGE_BOX)
        p.gdfs["fairways_unified"] = gpd.GeoDataFrame(geometry=[self.FAIRWAY_LINE], crs="EPSG:4326")
        p.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[self.INLAND_LINE], crs="EPSG:4326")
        return p

    def test_disabled_by_default_creates_two_separate_nodes(self):
        p = self._pipeline_with_bridge(connector_merge_m=0.0)
        p._add_opening_bridge_edges()
        bridge_nodes = [n for n, d in p.graph.nodes(data=True) if d.get("node_kind_id") == NODE_KIND_POINT]
        assert len(bridge_nodes) == 2
        for n in bridge_nodes:
            assert p.graph.nodes[n]["node_depth"] == 99.0

    def test_enabled_dedupes_the_near_coincident_pair_into_one_node(self):
        p = self._pipeline_with_bridge(connector_merge_m=5.0)
        p._add_opening_bridge_edges()
        bridge_nodes = [n for n, d in p.graph.nodes(data=True) if d.get("node_kind_id") == NODE_KIND_POINT]
        assert len(bridge_nodes) == 1
        assert p.bridge_opening_merge_stats["opening_points_deduped"] == 1
        assert p.bridge_opening_merge_stats["nodes_created"] == 1
        assert p.bridge_opening_merge_stats["nodes_merged"] == 0


class TestBridgeOpeningMergesIntoExistingNode:
    BRIDGE_BOX = box(3.8999, 51.4999, 3.9001, 51.5001)
    FAIRWAY_LINE = LineString([(3.899, 51.50000), (3.901, 51.50000)])
    # ~3.3m north of the fairway intersection centroid (3.9000, 51.50000) -- within a
    # 5m merge tolerance but far enough from _get_or_create_node's ~1.1m coordinate-
    # rounding grain that only the new nearby-node search (not an accidental exact-
    # coordinate collision) can be what merges these.
    EXISTING_LON, EXISTING_LAT = 3.90000, 51.50003

    def _pipeline_with_existing_node(self, connector_merge_m):
        p = _pipeline(connector_merge_m=connector_merge_m)
        p.gdfs["bridges"] = _movable_bridge_gdf(self.BRIDGE_BOX)
        p.gdfs["fairways_unified"] = gpd.GeoDataFrame(geometry=[self.FAIRWAY_LINE], crs="EPSG:4326")
        existing_id = p._get_or_create_node(self.EXISTING_LON, self.EXISTING_LAT,
                                             node_type="coastal", context="test")
        p.graph.nodes[existing_id]["node_depth"] = 3.5  # a real, more-restrictive constraint
        p._stamp_node(existing_id, 7, DEFAULT_SOURCE_TIER, None)  # some non-bridge kind
        return p, existing_id

    def test_enabled_merges_into_the_existing_node_without_clobbering_it(self):
        p, existing_id = self._pipeline_with_existing_node(connector_merge_m=5.0)
        p._add_opening_bridge_edges()

        assert p.graph.number_of_nodes() == 1, "no new node should have been minted"
        assert p.bridge_opening_merge_stats["nodes_merged"] == 1
        assert p.bridge_opening_merge_stats["nodes_created"] == 0
        # The existing node's real depth constraint must survive -- the bridge's
        # permissive 99.0 sentinel must never relax a more-restrictive existing value.
        assert p.graph.nodes[existing_id]["node_depth"] == 3.5
        # And its existing kind/source must not be silently re-stamped as a bridge.
        assert p.graph.nodes[existing_id]["node_kind_id"] == 7

    def test_disabled_by_default_mints_a_separate_node_and_leaves_the_existing_one_alone(self):
        p, existing_id = self._pipeline_with_existing_node(connector_merge_m=0.0)
        p._add_opening_bridge_edges()

        assert p.graph.number_of_nodes() == 2
        assert p.graph.nodes[existing_id]["node_depth"] == 3.5
        assert p.graph.nodes[existing_id]["node_kind_id"] == 7
        new_nodes = [n for n, d in p.graph.nodes(data=True)
                     if n != existing_id and d.get("node_kind_id") == NODE_KIND_POINT]
        assert len(new_nodes) == 1
        assert p.graph.nodes[new_nodes[0]]["node_depth"] == 99.0
