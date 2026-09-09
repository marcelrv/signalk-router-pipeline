"""Unit tests for `skeleton_boundary_simplify_m` (follow-on to SPEC-GRAPH-DENSITY.md).

`build_skeleton_network` rasterizes/skeletonizes a water polygon with NO boundary
simplification at all -- straight from the source layer's own ENC/chart
digitization detail. A real complex tidal marsh/creek water body can carry
hundreds of thousands of vertices; every small boundary wiggle (a cove, a point, a
single surveyed notch) spawns its own tiny branch in the medial axis, producing a
dense tangle of short junction-to-junction edges. Confirmed directly against a
real build this is NOT a resampling artifact -- 92% of sampled short (10-50m)
edges in one such dense area had exactly 2 raw width_profile points, i.e. the
chains are already minimal; the density is topological junction count, driven by
boundary noise, not chain over-sampling `--sagitta-cap` can fix.

Measured directly on the real narrow-water piece covering that area (via the
actual `_split_wide_narrow` pipeline logic, not an artificial bbox clip): a
boundary simplify before rasterizing cuts local node count 17%/27%/35% at
5m/15m/30m tolerance, plateauing past ~30m.

`skeleton_boundary_simplify_m` defaults to 0.0 (disabled): nothing here changes
real build output until a build explicitly opts in via
`--skeleton-boundary-simplify-m`, matching `--sagitta-cap`/`--axis-dedup-cap`'s
convention.
"""
import math

import pytest
from shapely.geometry import Polygon, box
from shapely.ops import transform as shapely_transform, unary_union

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    SKELETON_BOUNDARY_SIMPLIFY_MAX_M,
)

LAT0 = 38.0


def _to_lonlat(x, y):
    """Cheap local equirectangular-ish placement near LAT0 -- good enough for a
    small synthetic polygon exercising build_skeleton_network's own local-UTM
    reprojection, not a claim of cartographic accuracy."""
    lon = -76.8 + x / (111320 * math.cos(math.radians(LAT0)))
    lat = LAT0 + y / 110540
    return (lon, lat)


def _jagged_channel_wgs84(length=1500.0, width=250.0, tooth_depth=8.0, tooth_spacing=25.0):
    """A long rectangular channel with a sawtooth-notched edge along one side --
    stands in for fine-grained chart-digitization noise (small coves/points) on an
    otherwise plain, genuinely-narrow channel. Each notch is a small triangular
    land intrusion well under the simplify tolerances this suite tests, so a
    boundary simplify should smooth them away without touching the channel's own
    real width/length.
    """
    base = box(0, 0, length, width)
    teeth = []
    x = 20.0
    while x < length - 20.0:
        teeth.append(Polygon([(x, 0), (x + tooth_spacing / 2, tooth_depth), (x + tooth_spacing, 0)]))
        x += tooth_spacing
    water = base.difference(unary_union(teeth))
    return shapely_transform(lambda x, y: _to_lonlat(x, y), water)


def _pipeline(skeleton_boundary_simplify_m=0.0):
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        skeleton_boundary_simplify_m=skeleton_boundary_simplify_m)
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


class TestDisabledByDefaultReproducesTodaysSkeleton:
    def test_zero_tolerance_leaves_boundary_untouched(self):
        polygon = _jagged_channel_wgs84()
        p = _pipeline(skeleton_boundary_simplify_m=0.0)

        p.build_skeleton_network(polygon)

        assert p.skeleton_boundary_simplify_stats == {"pieces": 0, "vertices_before": 0, "vertices_after": 0}

    def test_zero_and_omitted_tolerance_produce_identical_graphs(self):
        polygon = _jagged_channel_wgs84()
        p_explicit = _pipeline(skeleton_boundary_simplify_m=0.0)
        p_explicit.build_skeleton_network(polygon)

        p_default = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
        p_default.coords_to_node = {}
        p_default._inland_split_cuts = {}
        p_default.build_skeleton_network(polygon)

        # Equal counts alone don't prove identical graphs (two graphs could
        # coincidentally have the same node/edge totals with different
        # topology) -- compare the actual node id set and edge set. Node ids
        # are coordinate-derived, so two byte-identical builds produce the
        # exact same ids, not just the same count of them.
        assert set(p_explicit.graph.nodes) == set(p_default.graph.nodes)
        assert set(p_explicit.graph.edges) == set(p_default.graph.edges)


class TestEnabledReducesNodeCount:
    def test_simplify_substantially_reduces_node_and_edge_count(self):
        polygon = _jagged_channel_wgs84()
        p_off = _pipeline(skeleton_boundary_simplify_m=0.0)
        p_off.build_skeleton_network(polygon)

        p_on = _pipeline(skeleton_boundary_simplify_m=15.0)
        p_on.build_skeleton_network(polygon)

        assert p_on.graph.number_of_nodes() < p_off.graph.number_of_nodes()
        assert p_on.graph.number_of_edges() < p_off.graph.number_of_edges()
        # Real measured effect was a 17-35% reduction -- require at least a
        # meaningful (not fractional-percent) drop, not an exact figure, since
        # this synthetic fixture's own geometry differs from the real one measured.
        assert p_on.graph.number_of_nodes() <= 0.8 * p_off.graph.number_of_nodes()

    def test_stats_are_recorded_when_enabled(self):
        polygon = _jagged_channel_wgs84()
        p = _pipeline(skeleton_boundary_simplify_m=15.0)

        p.build_skeleton_network(polygon)

        stats = p.skeleton_boundary_simplify_stats
        assert stats["pieces"] == 1
        assert stats["vertices_before"] > stats["vertices_after"] > 0

    def test_larger_tolerance_plateaus_rather_than_regresses(self):
        # Diminishing returns past ~30m on the real measurement -- confirm a
        # larger tolerance never produces MORE nodes than a smaller one (i.e.
        # simplification is monotonic here, even if it doesn't keep shrinking).
        polygon = _jagged_channel_wgs84()
        p_15 = _pipeline(skeleton_boundary_simplify_m=15.0)
        p_15.build_skeleton_network(polygon)
        p_30 = _pipeline(skeleton_boundary_simplify_m=30.0)
        p_30.build_skeleton_network(polygon)

        assert p_30.graph.number_of_nodes() <= p_15.graph.number_of_nodes()


class TestValidation:
    def test_zero_is_accepted(self):
        NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(0.0)

    def test_positive_value_within_range_is_accepted(self):
        NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(30.0)

    def test_at_or_above_ceiling_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(
                SKELETON_BOUNDARY_SIMPLIFY_MAX_M)
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(
                SKELETON_BOUNDARY_SIMPLIFY_MAX_M + 50.0)

    def test_negative_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(-1.0)

    def test_nan_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(float("nan"))

    def test_infinity_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_skeleton_boundary_simplify_m(float("inf"))
