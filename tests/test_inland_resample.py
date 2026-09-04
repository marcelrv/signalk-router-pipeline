"""Unit tests for inland_waterways vertex consolidation (SPEC-GRAPH-DENSITY.md
§6.9 follow-up).

`_build_inland_network` ingests every vertex of a raw `inland_waterways` source
line directly, vertex-to-vertex, with NO consolidation -- a densely-digitized IENC
line (measured as low as ~70-100m vertex spacing on a real Zeeland build)
reproduces that exact density in the graph regardless of `--sagitta-cap`/
`--max-segment-m`, which only govern SKELETON resampling
(`_resample_long_skeleton_edges`), never this raw ingestion path.

`_resample_inland_waterways` reuses that same generator's plain cumulative-arc-
length walk to consolidate consecutive EXISTING vertices into chords up to
`inland_resample_max_segment_m` metres -- the opposite, complementary operation to
`_densify_inland_waterways` (§6.4), which INSERTS vertices instead.

`inland_resample_max_segment_m` defaults to 0.0 (disabled): nothing here changes
real build output until a build explicitly opts in via
`--inland-resample-max-segment-m`, matching `--sagitta-cap`/`--axis-dedup-cap`'s
convention.
"""
import math

import geopandas as gpd
import networkx as nx
import pytest
from shapely.geometry import LineString, MultiLineString, Point

from nautical_routing_pipeline import ClassificationConfig, NauticalRoutingPipeline


def _pipeline(inland_resample_max_segment_m=0.0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_inland_densify.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        inland_resample_max_segment_m=inland_resample_max_segment_m,
    )
    p.coords_to_node = {}
    return p


def _gdf(geoms):
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")


# A densely-digitized line: 21 vertices ~50m apart at this latitude (~1000m total),
# the real-world pattern this fix targets (measured as low as ~70-100m on a real
# Zeeland inland_waterways line).
_LON0, _LAT0 = 3.80, 51.58
_STEP_DEG = 0.0007  # ~50m of longitude at 51.58N
DENSE_LINE = LineString([(_LON0 + i * _STEP_DEG, _LAT0) for i in range(21)])


class TestResampleDisabledByDefault:
    def test_zero_cap_is_a_noop(self):
        p = _pipeline(inland_resample_max_segment_m=0.0)
        gdf = _gdf([DENSE_LINE])
        out = p._resample_inland_waterways(gdf)
        assert out is gdf
        assert list(out.geometry.iloc[0].coords) == list(DENSE_LINE.coords)

    def test_empty_gdf_is_a_noop(self):
        p = _pipeline(inland_resample_max_segment_m=250.0)
        gdf = _gdf([])
        out = p._resample_inland_waterways(gdf)
        assert out is gdf

    def test_negative_cap_is_still_a_plain_disable_not_an_error(self):
        p = _pipeline(inland_resample_max_segment_m=-5.0)
        gdf = _gdf([DENSE_LINE])
        out = p._resample_inland_waterways(gdf)
        assert out is gdf


class TestResampleRejectsUnsafeCaps:
    def test_nan_raises(self):
        p = _pipeline(inland_resample_max_segment_m=float("nan"))
        with pytest.raises(ValueError):
            p._resample_inland_waterways(_gdf([DENSE_LINE]))

    def test_positive_infinity_raises(self):
        p = _pipeline(inland_resample_max_segment_m=math.inf)
        with pytest.raises(ValueError):
            p._resample_inland_waterways(_gdf([DENSE_LINE]))


class TestResampleEnabled:
    def test_dense_line_loses_intermediate_vertices(self):
        p = _pipeline(inland_resample_max_segment_m=250.0)
        out = p._resample_inland_waterways(_gdf([DENSE_LINE]))
        coords = list(out.geometry.iloc[0].coords)
        assert len(coords) < len(list(DENSE_LINE.coords))

    def test_no_output_segment_exceeds_the_cap_by_more_than_one_source_step(self):
        # The walk can only ever close a segment AT an existing source vertex (it
        # never inserts new points), so a segment can overshoot the cap by up to
        # one source step's length -- e.g. 5 vertices totalling 242m is still under
        # a 250m cap, so a 6th ~48m vertex gets folded in too, landing at ~291m.
        # That's correct "close at the first vertex where cumulative length >=
        # cap" behaviour, not a bug -- the real invariant is cap <= length <
        # cap + one source step, not a strict cap ceiling.
        cap_m = 250.0
        source_step_m = 55.0  # ~48.5m at this latitude/spacing, with slack
        p = _pipeline(inland_resample_max_segment_m=cap_m)
        out = p._resample_inland_waterways(_gdf([DENSE_LINE]))
        line_m = gpd.GeoSeries([out.geometry.iloc[0]], crs="EPSG:4326").to_crs("EPSG:32631").iloc[0]
        coords = list(line_m.coords)
        seg_lens = [Point(coords[i]).distance(Point(coords[i + 1])) for i in range(len(coords) - 1)]
        # Every segment except possibly the last (the walk's final, possibly-short
        # remainder) must be at least the cap.
        assert all(s >= cap_m or s == seg_lens[-1] for s in seg_lens)
        assert max(seg_lens) <= cap_m + source_step_m

    def test_endpoints_are_unchanged(self):
        p = _pipeline(inland_resample_max_segment_m=250.0)
        out = p._resample_inland_waterways(_gdf([DENSE_LINE]))
        coords = list(out.geometry.iloc[0].coords)
        orig = list(DENSE_LINE.coords)
        assert coords[0] == pytest.approx(orig[0], abs=1e-9)
        assert coords[-1] == pytest.approx(orig[-1], abs=1e-9)

    def test_no_new_vertices_are_ever_inserted(self):
        # Consolidation only ever REMOVES vertices -- every output coordinate must
        # be one of the original ones (unlike densify, which interpolates new
        # points along the line).
        p = _pipeline(inland_resample_max_segment_m=250.0)
        out = p._resample_inland_waterways(_gdf([DENSE_LINE]))
        orig = set(DENSE_LINE.coords)
        for c in out.geometry.iloc[0].coords:
            assert c in orig

    def test_a_cap_larger_than_the_whole_line_collapses_to_two_points(self):
        p = _pipeline(inland_resample_max_segment_m=100_000.0)
        out = p._resample_inland_waterways(_gdf([DENSE_LINE]))
        coords = list(out.geometry.iloc[0].coords)
        assert coords == [DENSE_LINE.coords[0], DENSE_LINE.coords[-1]]

    def test_short_two_point_line_is_unaffected(self):
        short = LineString([(3.70, 51.45), (3.701, 51.45)])  # ~70m at this latitude
        p = _pipeline(inland_resample_max_segment_m=10.0)
        out = p._resample_inland_waterways(_gdf([short]))
        coords = list(out.geometry.iloc[0].coords)
        assert coords[0] == pytest.approx((3.70, 51.45), abs=1e-9)
        assert coords[-1] == pytest.approx((3.701, 51.45), abs=1e-9)

    def test_multiple_lines_all_resampled_independently(self):
        p = _pipeline(inland_resample_max_segment_m=250.0)
        second = LineString([(_LON0 + i * _STEP_DEG, 51.60) for i in range(21)])
        out = p._resample_inland_waterways(_gdf([DENSE_LINE, second]))
        assert len(list(out.geometry.iloc[0].coords)) < 21
        assert len(list(out.geometry.iloc[1].coords)) < 21

    def test_non_linestring_geometry_does_not_raise(self):
        p = _pipeline(inland_resample_max_segment_m=250.0)
        multi = MultiLineString([[(3.70, 51.70), (3.90, 51.70)]])
        out = p._resample_inland_waterways(_gdf([DENSE_LINE, multi]))
        assert len(out) == 2


class TestResampleFixesInlandNetworkDensity:
    """End-to-end: resampling before _build_inland_network runs turns many short
    edges into fewer, longer ones -- the vertex-spacing symptom the bug report
    traced (a real fairway showing ~71m node spacing regardless of --sagitta-cap/
    --max-segment-m, which never touched this raw ingestion path)."""

    def test_build_inland_network_gets_fewer_nodes_once_resampled(self):
        p = _pipeline(inland_resample_max_segment_m=250.0)
        p.gdfs = {"inland_waterways": _gdf([DENSE_LINE])}
        p.gdfs["inland_waterways"] = p._resample_inland_waterways(p.gdfs["inland_waterways"])
        p.layer_source_ids = {}
        p.graph = nx.DiGraph()
        p._build_inland_network()
        assert p.graph.number_of_nodes() < 21

    def test_disabled_leaves_inland_network_at_full_source_density(self):
        p = _pipeline(inland_resample_max_segment_m=0.0)
        p.gdfs = {"inland_waterways": _gdf([DENSE_LINE])}
        p.layer_source_ids = {}
        p.graph = nx.DiGraph()
        p._build_inland_network()
        assert p.graph.number_of_nodes() == 21
