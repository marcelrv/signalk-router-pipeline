"""Unit tests for inland_waterways densification (SPEC-GRAPH-DENSITY.md §6.4).

A chart-scale `inland_waterways` line (e.g. SCAMIN=50000) can have vertices well over
1000m apart. `_connect_waterway_crossing`'s nearest-existing-vertex search and
`_build_inland_network`'s raw vertex-to-vertex ingestion both assume denser digitization
than that -- every crossing/carve-reconnect point within roughly a kilometre of a sparse
segment collapses onto the same one distant vertex, producing the fan/star-shaped "hub"
nodes measured directly against `zeeland.sqlite` (226 nodes with out-degree > 30).

`_densify_inland_waterways` inserts interpolated vertices along any segment exceeding
`inland_densify_max_segment_m`, once, at load time, so every downstream consumer sees
already-dense geometry with zero changes to itself.

`inland_densify_max_segment_m` defaults to 0.0 (disabled): nothing here changes real
build output until a build explicitly opts in via `--inland-densify-max-segment-m`,
matching `--sagitta-cap`/`--axis-dedup-cap`'s convention.
"""
import geopandas as gpd
import networkx as nx
import pytest
from shapely.geometry import LineString, MultiLineString, Point

from nautical_routing_pipeline import ClassificationConfig, NauticalRoutingPipeline


def _pipeline(inland_densify_max_segment_m=0.0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_axis_dedup.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        inland_densify_max_segment_m=inland_densify_max_segment_m,
    )
    p.coords_to_node = {}
    return p


def _gdf(geoms):
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")


# A sparse "Geul van de Walvischstaart"-shaped line: two vertices ~8.9km apart at this
# latitude (0.08 degrees of longitude at ~51.45N is roughly 5.5km; use a bigger span to
# comfortably exceed any sane cap without relying on a precise geodesic figure).
SPARSE_LINE = LineString([(3.70, 51.45), (3.90, 51.45)])


class TestDensifyDisabledByDefault:
    def test_zero_cap_is_a_noop(self):
        p = _pipeline(inland_densify_max_segment_m=0.0)
        gdf = _gdf([SPARSE_LINE])
        out = p._densify_inland_waterways(gdf)
        assert out is gdf
        assert list(out.geometry.iloc[0].coords) == list(SPARSE_LINE.coords)

    def test_empty_gdf_is_a_noop(self):
        p = _pipeline(inland_densify_max_segment_m=100.0)
        gdf = _gdf([])
        out = p._densify_inland_waterways(gdf)
        assert out is gdf


class TestDensifyEnabled:
    def test_long_segment_gets_intermediate_vertices(self):
        p = _pipeline(inland_densify_max_segment_m=150.0)
        out = p._densify_inland_waterways(_gdf([SPARSE_LINE]))
        coords = list(out.geometry.iloc[0].coords)
        assert len(coords) > 2

    def test_no_output_segment_exceeds_the_cap(self):
        cap_m = 150.0
        p = _pipeline(inland_densify_max_segment_m=cap_m)
        out = p._densify_inland_waterways(_gdf([SPARSE_LINE]))
        line_m = gpd.GeoSeries([out.geometry.iloc[0]], crs="EPSG:4326").to_crs("EPSG:32631").iloc[0]
        coords = list(line_m.coords)
        seg_lens = [Point(coords[i]).distance(Point(coords[i + 1])) for i in range(len(coords) - 1)]
        # Small slack for CRS round-trip (WGS84 -> metric -> segmentize -> WGS84 -> metric).
        assert max(seg_lens) <= cap_m * 1.01

    def test_endpoints_are_unchanged(self):
        p = _pipeline(inland_densify_max_segment_m=150.0)
        out = p._densify_inland_waterways(_gdf([SPARSE_LINE]))
        coords = list(out.geometry.iloc[0].coords)
        orig = list(SPARSE_LINE.coords)
        assert coords[0] == pytest.approx(orig[0], abs=1e-9)
        assert coords[-1] == pytest.approx(orig[-1], abs=1e-9)

    def test_length_is_preserved(self):
        p = _pipeline(inland_densify_max_segment_m=150.0)
        out = p._densify_inland_waterways(_gdf([SPARSE_LINE]))
        orig_m = gpd.GeoSeries([SPARSE_LINE], crs="EPSG:4326").to_crs("EPSG:32631").iloc[0]
        new_m = gpd.GeoSeries([out.geometry.iloc[0]], crs="EPSG:4326").to_crs("EPSG:32631").iloc[0]
        assert new_m.length == pytest.approx(orig_m.length, rel=1e-6)

    def test_short_segment_is_unaffected(self):
        short = LineString([(3.70, 51.45), (3.701, 51.45)])  # ~70m at this latitude
        p = _pipeline(inland_densify_max_segment_m=150.0)
        out = p._densify_inland_waterways(_gdf([short]))
        coords = list(out.geometry.iloc[0].coords)
        assert coords[0] == pytest.approx((3.70, 51.45), abs=1e-9)
        assert coords[-1] == pytest.approx((3.701, 51.45), abs=1e-9)

    def test_multiple_lines_all_densified_independently(self):
        p = _pipeline(inland_densify_max_segment_m=150.0)
        second = LineString([(3.70, 51.60), (3.90, 51.60)])
        out = p._densify_inland_waterways(_gdf([SPARSE_LINE, second]))
        assert len(list(out.geometry.iloc[0].coords)) > 2
        assert len(list(out.geometry.iloc[1].coords)) > 2

    def test_non_linestring_geometry_does_not_raise(self):
        # inland_waterways is documented/consumed as LineString-only elsewhere
        # (_build_inland_network's isinstance check); segmentize itself accepts other
        # geometry types, so a stray MultiLineString must not blow up loading.
        p = _pipeline(inland_densify_max_segment_m=150.0)
        multi = MultiLineString([[(3.70, 51.70), (3.90, 51.70)]])
        out = p._densify_inland_waterways(_gdf([SPARSE_LINE, multi]))
        assert len(out) == 2


class TestDensifyFixesTheConnectorFanOut:
    """End-to-end: densifying before _build_inland_network runs turns one long edge
    with zero intermediate routing nodes into several shorter ones -- the same
    sparse-vertex gap that makes _connect_waterway_crossing's nearest-vertex search
    collapse distinct crossings onto one distant point.
    """

    def test_build_inland_network_gets_intermediate_nodes_once_densified(self):
        p = _pipeline(inland_densify_max_segment_m=150.0)
        p.gdfs = {"inland_waterways": _gdf([SPARSE_LINE])}
        p.gdfs["inland_waterways"] = p._densify_inland_waterways(p.gdfs["inland_waterways"])
        p.layer_source_ids = {}
        p.graph = nx.DiGraph()
        p._build_inland_network()
        # Two endpoints plus at least one interpolated vertex.
        assert p.graph.number_of_nodes() > 2

    def test_disabled_leaves_inland_network_as_one_long_edge(self):
        p = _pipeline(inland_densify_max_segment_m=0.0)
        p.gdfs = {"inland_waterways": _gdf([SPARSE_LINE])}
        p.layer_source_ids = {}
        p.graph = nx.DiGraph()
        p._build_inland_network()
        assert p.graph.number_of_nodes() == 2
