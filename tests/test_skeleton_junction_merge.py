"""Unit tests for `skeleton_junction_merge_m` (docs/SPEC-GRAPH-DENSITY.md §11).

`skeleton_boundary_simplify_m` reduces spurious medial-axis branches caused by
boundary noise, but does nothing about short edges BETWEEN two already-real
junction nodes: a wide water body with a detailed coastline converges many
genuine branches into a dense tangle instead of one clean line. Measured on a
real build: 85% of `coastal_water` nodes statewide have degree >= 4 (43.7%
exactly degree-6), when a clean 1D skeleton should be ~85%+ degree-2. Confirmed
on the real Cuckold Creek, MD area even with `--skeleton-boundary-simplify-m
20.0` already applied: 190 nodes at ~25m spacing, degree 6-8 -- a 2D
triangulated mesh, not a skeleton.

`_prune_skeleton_spurs` only removes short DEAD-END (one side degree-1) edges;
`_merge_close_skeleton_junctions` is the sibling pass for short JUNCTION-TO-
JUNCTION (both sides degree>=3) edges, which spur pruning never touches.

`skeleton_junction_merge_m` defaults to 0.0 (disabled): nothing here changes
real build output until a build explicitly opts in via
`--skeleton-junction-merge-m`, matching `--skeleton-boundary-simplify-m`'s own
convention.
"""
import math

import networkx as nx
import pytest
from shapely.geometry import Point, Polygon, box
from shapely.ops import transform as shapely_transform, unary_union

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    SKELETON_JUNCTION_MERGE_MAX_M,
)

LAT0 = 38.0


def _to_lonlat(x, y):
    """Cheap local equirectangular-ish placement near LAT0 -- good enough for a
    small synthetic polygon exercising build_skeleton_network's own local-UTM
    reprojection, not a claim of cartographic accuracy. Mirrors
    tests/test_skeleton_boundary_simplify.py's own helper exactly."""
    lon = -76.8 + x / (111320 * math.cos(math.radians(LAT0)))
    lat = LAT0 + y / 110540
    return (lon, lat)


def _wide_notched_blob_wgs84(size=400.0, tooth_depth=12.0, tooth_spacing=30.0):
    """A wide, roughly-square water body with a sawtooth-notched boundary on
    ALL FOUR sides -- unlike `test_skeleton_boundary_simplify.py`'s
    `_jagged_channel_wgs84` (one notched edge on an otherwise-thin channel,
    which produces isolated single-branch spurs), notches on every side of a
    WIDE body send many small branches converging toward the interior,
    producing several close-together degree>=3 junction pixels in the medial
    axis -- the real "mesh fill" pattern (Cuckold Creek, MD), not a spur.
    Confirmed directly: this fixture's raw skeleton is 84 nodes with only 23 of
    them (27%) at degree-2 -- the rest degree>=4, same signature as the real
    statewide measurement.
    """
    base = box(0, 0, size, size)
    teeth = []
    x = 15.0
    while x < size - 15.0:
        teeth.append(Polygon([(x, 0), (x + tooth_spacing / 2, tooth_depth), (x + tooth_spacing, 0)]))
        teeth.append(Polygon([(x, size), (x + tooth_spacing / 2, size - tooth_depth), (x + tooth_spacing, size)]))
        x += tooth_spacing
    y = 15.0
    while y < size - 15.0:
        teeth.append(Polygon([(0, y), (tooth_depth, y + tooth_spacing / 2), (0, y + tooth_spacing)]))
        teeth.append(Polygon([(size, y), (size - tooth_depth, y + tooth_spacing / 2), (size, y + tooth_spacing)]))
        y += tooth_spacing
    water = base.difference(unary_union(teeth))
    return shapely_transform(lambda x, y: _to_lonlat(x, y), water)


def _pipeline(skeleton_junction_merge_m=0.0):
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        skeleton_junction_merge_m=skeleton_junction_merge_m)
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


class TestMergeCloseSkeletonJunctionsUnit:
    """Exercises `_merge_close_skeleton_junctions` directly on small synthetic
    pixel graphs -- precise control over degree/length/dist_val that a raster
    fixture can't guarantee."""

    def test_disabled_returns_empty_map_regardless_of_graph(self):
        p = _pipeline()
        G = nx.Graph()
        G.add_node((0, 0), lonlat=(-76.0, 38.0), dist_val=5.0)
        G.add_node((0, 1), lonlat=(-76.001, 38.0), dist_val=8.0)
        G.add_edge((0, 0), (0, 1), length_m=1.0)
        assert p._merge_close_skeleton_junctions(G, 0.0) == {}

    @staticmethod
    def _two_junction_graph(gap_m):
        """A(deg3) --gap_m-- B(deg3), each with two more far neighbours making
        up the remaining edges, so both genuinely satisfy degree>=3."""
        G = nx.Graph()
        A, B = ("A",), ("B",)
        G.add_node(A, lonlat=(-76.100, 38.100), dist_val=5.0)
        G.add_node(B, lonlat=(-76.099, 38.100), dist_val=9.0)  # higher dist_val
        G.add_edge(A, B, length_m=gap_m)
        for tag in ("a1", "a2"):
            n = (tag,)
            G.add_node(n, lonlat=(-76.2, 38.2), dist_val=1.0)
            G.add_edge(A, n, length_m=500.0)
        for tag in ("b1", "b2"):
            n = (tag,)
            G.add_node(n, lonlat=(-76.2, 38.2), dist_val=1.0)
            G.add_edge(B, n, length_m=500.0)
        return G, A, B

    def test_two_close_junctions_merge_onto_higher_dist_val_member(self):
        p = _pipeline()
        G, A, B = self._two_junction_graph(gap_m=10.0)
        merge_map = p._merge_close_skeleton_junctions(G, 25.0)
        assert merge_map == {A: G.nodes[B]["lonlat"]}

    def test_far_junctions_are_not_merged(self):
        p = _pipeline()
        G, A, B = self._two_junction_graph(gap_m=200.0)
        assert p._merge_close_skeleton_junctions(G, 25.0) == {}

    def test_edge_length_exactly_at_tolerance_is_not_merged(self):
        # `< merge_tol_m`, strictly -- confirms the boundary is exclusive.
        p = _pipeline()
        G, A, B = self._two_junction_graph(gap_m=25.0)
        assert p._merge_close_skeleton_junctions(G, 25.0) == {}

    def test_tied_dist_val_breaks_deterministically_on_the_pixel_tuple(self):
        """`members` is built from a set (`junctions`), so its iteration order
        is not something the representative choice may depend on -- a tie on
        `dist_val` must fall back to a deterministic secondary key (the pixel
        tuple itself), or two independently-built adjacent regions could
        disagree about a shared seam node's representative."""
        p = _pipeline()
        G, A, B = self._two_junction_graph(gap_m=10.0)
        G.nodes[A]["dist_val"] = 5.0
        G.nodes[B]["dist_val"] = 5.0  # tied -- A < B by tuple order (("A",) < ("B",))
        merge_map = p._merge_close_skeleton_junctions(G, 25.0)
        assert merge_map == {B: G.nodes[A]["lonlat"]}, \
            "on a tie, the lexicographically smaller pixel tuple wins, not iteration order"

    def test_dead_end_neighbour_of_a_junction_is_never_merged(self):
        """A single degree-1 endpoint sitting close to a junction must never be
        folded in -- only genuine degree>=3 junction pixels are eligible, or a
        real dead-end stub tip would silently disappear into its junction."""
        p = _pipeline()
        G = nx.Graph()
        junction = ("J",)
        G.add_node(junction, lonlat=(-76.10, 38.10), dist_val=5.0)
        for tag in ("n1", "n2"):
            n = (tag,)
            G.add_node(n, lonlat=(-76.2, 38.2), dist_val=1.0)
            G.add_edge(junction, n, length_m=500.0)
        dead_end = ("D",)
        G.add_node(dead_end, lonlat=(-76.1001, 38.1000), dist_val=9.0)
        G.add_edge(junction, dead_end, length_m=5.0)  # short, but D stays degree 1
        assert p._merge_close_skeleton_junctions(G, 25.0) == {}

    def test_chain_of_three_close_junctions_collapses_to_one_cluster(self):
        p = _pipeline()
        G = nx.Graph()
        A, B, C = ("A",), ("B",), ("C",)
        G.add_node(A, lonlat=(-76.100, 38.100), dist_val=3.0)
        G.add_node(B, lonlat=(-76.0995, 38.100), dist_val=9.0)  # highest -- representative
        G.add_node(C, lonlat=(-76.099, 38.100), dist_val=4.0)
        G.add_edge(A, B, length_m=8.0)
        G.add_edge(B, C, length_m=8.0)
        for junc, tags in ((A, ("a1", "a2")), (B, ("b1",)), (C, ("c1", "c2"))):
            for tag in tags:
                n = (junc, tag)
                G.add_node(n, lonlat=(-76.2, 38.2), dist_val=1.0)
                G.add_edge(junc, n, length_m=500.0)
        merge_map = p._merge_close_skeleton_junctions(G, 25.0)
        assert merge_map == {A: G.nodes[B]["lonlat"], C: G.nodes[B]["lonlat"]}


class TestSpliceJunctionMergeIntoEdgePts:
    """`nx.Graph.edges()` does not guarantee `(px_u, px_v)` comes back in the same
    order the edge was originally added with -- confirmed directly on a real
    piece: ~26% of edges come back reversed relative to `pts[0]`/`pts[-1]`. An
    earlier version of `build_skeleton_network` assumed `px_u~pts[0]` always,
    which silently spliced the representative into the WRONG end on a reversed
    edge and fragmented the largest connected component on a real build (caught
    only by piece-level testing against real Cuckold Creek geometry, not by any
    synthetic fixture here). These tests pin both orderings explicitly so this
    can never regress silently again.
    """

    @staticmethod
    def _edge_graph():
        G = nx.Graph()
        G.add_node("start_px", lonlat=(-76.100, 38.100))
        G.add_node("end_px", lonlat=(-76.099, 38.100))
        return G

    def test_forward_order_splices_correct_ends(self):
        G = self._edge_graph()
        d = {"pts": [(-76.100, 38.100), (-76.0995, 38.100), (-76.099, 38.100)]}
        merge_map = {"start_px": (-76.200, 38.200), "end_px": (-76.201, 38.201)}

        NauticalRoutingPipeline._splice_junction_merge_into_edge_pts(
            G, "start_px", "end_px", d, merge_map)

        assert d["pts"][0] == (-76.200, 38.200)
        assert d["pts"][-1] == (-76.201, 38.201)
        assert d["pts"][1] == (-76.0995, 38.100)  # interior point untouched

    def test_reversed_order_still_splices_correct_ends(self):
        # Same edge, same d["pts"], but (px_u, px_v) handed in reversed --
        # exactly what nx.Graph.edges() can legitimately return.
        G = self._edge_graph()
        d = {"pts": [(-76.100, 38.100), (-76.0995, 38.100), (-76.099, 38.100)]}
        merge_map = {"start_px": (-76.200, 38.200), "end_px": (-76.201, 38.201)}

        NauticalRoutingPipeline._splice_junction_merge_into_edge_pts(
            G, "end_px", "start_px", d, merge_map)

        assert d["pts"][0] == (-76.200, 38.200)
        assert d["pts"][-1] == (-76.201, 38.201)
        assert d["pts"][1] == (-76.0995, 38.100)

    def test_only_one_end_merged_leaves_other_end_untouched(self):
        G = self._edge_graph()
        original_end = (-76.099, 38.100)
        d = {"pts": [(-76.100, 38.100), (-76.0995, 38.100), original_end]}
        merge_map = {"start_px": (-76.200, 38.200)}  # end_px not clustered

        NauticalRoutingPipeline._splice_junction_merge_into_edge_pts(
            G, "start_px", "end_px", d, merge_map)

        assert d["pts"][0] == (-76.200, 38.200)
        assert d["pts"][-1] == original_end

    def test_neither_end_merged_is_a_no_op(self):
        G = self._edge_graph()
        pts = [(-76.100, 38.100), (-76.0995, 38.100), (-76.099, 38.100)]
        d = {"pts": list(pts)}

        NauticalRoutingPipeline._splice_junction_merge_into_edge_pts(G, "start_px", "end_px", d, {})

        assert d["pts"] == pts


class TestDisabledByDefaultReproducesTodaysSkeleton:
    def test_zero_and_omitted_tolerance_produce_identical_graphs(self):
        polygon = _wide_notched_blob_wgs84()
        p_explicit = _pipeline(skeleton_junction_merge_m=0.0)
        p_explicit.build_skeleton_network(polygon)

        p_default = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
        p_default.coords_to_node = {}
        p_default._inland_split_cuts = {}
        p_default.build_skeleton_network(polygon)

        # Node ids are coordinate-derived, so a byte-identical build produces
        # the exact same ids, not just the same count of them.
        assert set(p_explicit.graph.nodes) == set(p_default.graph.nodes)
        assert set(p_explicit.graph.edges) == set(p_default.graph.edges)


class TestEnabledReducesJunctionDensity:
    def test_merge_reduces_high_degree_node_count_and_preserves_connectivity(self):
        polygon = _wide_notched_blob_wgs84()
        p_off = _pipeline(skeleton_junction_merge_m=0.0)
        p_off.build_skeleton_network(polygon)
        p_on = _pipeline(skeleton_junction_merge_m=25.0)
        p_on.build_skeleton_network(polygon)

        assert p_on.graph.number_of_nodes() < p_off.graph.number_of_nodes()

        deg_off = dict(p_off.graph.degree())
        deg_on = dict(p_on.graph.degree())
        n_ge4_off = sum(1 for d in deg_off.values() if d >= 4)
        n_ge4_on = sum(1 for d in deg_on.values() if d >= 4)
        assert n_ge4_on < n_ge4_off

        # Merging junctions must never fragment the network.
        assert nx.number_connected_components(p_off.graph.to_undirected()) == 1
        assert nx.number_connected_components(p_on.graph.to_undirected()) == 1

    def test_merged_node_positions_stay_inside_original_water_polygon(self):
        polygon = _wide_notched_blob_wgs84()
        p = _pipeline(skeleton_junction_merge_m=25.0)
        p.build_skeleton_network(polygon)

        # The representative is always an already-real, already-valid skeleton
        # pixel (max distance-transform value), never a synthetic centroid --
        # every resulting node should still sit inside the original water
        # polygon (small buffer only for float/reprojection round-trip noise).
        buffered = polygon.buffer(1e-6)
        for node_id, data in p.graph.nodes(data=True):
            pt = Point(data["lon"], data["lat"])
            assert buffered.contains(pt), (node_id, data)


class TestValidation:
    def test_zero_is_accepted(self):
        NauticalRoutingPipeline._validate_skeleton_junction_merge_m(0.0)

    def test_positive_value_within_range_is_accepted(self):
        NauticalRoutingPipeline._validate_skeleton_junction_merge_m(30.0)

    def test_at_or_above_ceiling_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_junction_merge_m(
                SKELETON_JUNCTION_MERGE_MAX_M)
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_junction_merge_m(
                SKELETON_JUNCTION_MERGE_MAX_M + 50.0)

    def test_negative_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_junction_merge_m(-1.0)

    def test_nan_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_junction_merge_m(float("nan"))

    def test_infinity_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_junction_merge_m(float("inf"))
