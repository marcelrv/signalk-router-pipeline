"""Regression test for FAIRWAY_MATCH_BUFFER_M (calculate_edge_attributes).

_edge_attr_worker tags an edge cost_factor=0.8 ("fairway: preferred") via a bare
`intersects()` between the edge's straight chord and the fairway/inland-waterways
reference layer. inland_waterways entries are zero-width centerlines, so that test
only fires when a chord actually crosses the line -- a chord running a few metres
parallel to it (routine skeleton/medial-axis wobble, or drift from
SPEC-GRAPH-DENSITY.md's axis-dedup/densify/connector-merge splitting) misses
entirely, and the finer the splitting the more of a real fairway's length this
silently drops to the 1.2 default. Verified on a real Zeeland corridor: splitting
from ~98k to ~187k edges dropped fairway-tagged coverage on one route from 83% to
59% of its length with no change to the source geometry.

calculate_edge_attributes now buffers the fairway/inland-waterways layer by
FAIRWAY_MATCH_BUFFER_M metres before handing it to the workers, so a chord passing
near (not exactly through) the line still counts.
"""
import geopandas as gpd
from shapely.geometry import LineString

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    FAIRWAY_MATCH_BUFFER_M,
)

# A north-south inland-waterway centerline.
FAIRWAY_LINE = LineString([(4.1600, 51.6590), (4.1600, 51.6610)])


def _pipeline():
    # __init__ only assigns attributes (no file I/O) -- same pattern as
    # tests/test_waterway_connector_merge.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig()
    p.gdfs_metric = {}
    p.gdfs["fairways_unified"] = gpd.GeoDataFrame(geometry=[], crs=p.CRS_WGS84)
    p.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[FAIRWAY_LINE], crs=p.CRS_WGS84)
    for key in ("depth_areas", "bridges", "dredged_areas", "locks",
                "obstacles", "obstacles_soft"):
        p.gdfs.setdefault(key, gpd.GeoDataFrame(geometry=[], crs=p.CRS_WGS84))
    return p


def _add_edge(p, offset_m, node_prefix):
    # Offset the chord east of the fairway line by `offset_m` metres (~1e-5 deg/m
    # at this latitude is close enough for a small test offset).
    offset_deg = offset_m / (111320.0 * 0.622)  # cos(51.6 deg) for Zeeland's latitude
    lon = 4.1600 + offset_deg
    u, v = f"{node_prefix}_u", f"{node_prefix}_v"
    p.graph.add_node(u, lon=lon, lat=51.6595)
    p.graph.add_node(v, lon=lon, lat=51.6605)
    p.graph.add_edge(u, v, edge_type="coastal")
    return u, v


class TestFairwayMatchBuffer:
    def test_chord_a_few_metres_off_the_line_still_counts_as_fairway(self):
        p = _pipeline()
        u, v = _add_edge(p, offset_m=3.0, node_prefix="near")
        p.calculate_edge_attributes()
        assert p.graph.edges[u, v]["cost_factor"] == 0.8

    def test_chord_well_clear_of_the_line_is_not_fairway(self):
        p = _pipeline()
        u, v = _add_edge(p, offset_m=500.0, node_prefix="far")
        p.calculate_edge_attributes()
        assert p.graph.edges[u, v]["cost_factor"] == 1.2

    def test_chord_near_the_full_buffer_radius_still_counts_as_fairway(self):
        # Buffering in CRS_METRIC (EPSG:3857, Web Mercator) would only cover
        # ~3.1m on the ground at this latitude instead of the full 5.0m --
        # this offset (within the documented 4.5-5.5m tolerance) only passes
        # once the buffer is built in a local metre-based UTM CRS.
        p = _pipeline()
        u, v = _add_edge(p, offset_m=4.9, node_prefix="nearfull")
        p.calculate_edge_attributes()
        assert p.graph.edges[u, v]["cost_factor"] == 0.8

    def test_buffer_constant_is_a_few_metres_not_kilometres(self):
        # Sanity guard: this fix is meant to absorb splitting/skeleton drift, not to
        # blanket-tag distant open water as fairway.
        assert 0 < FAIRWAY_MATCH_BUFFER_M <= 20.0
