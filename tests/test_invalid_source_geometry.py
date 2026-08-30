"""Tier-1 unit tests: invalid source polygons must not kill the whole build.

GEOS raises `TopologyException: side location conflict` from unary_union when an
input ring touches itself. _connected_water_polygons is the very first topology
step in build_network, so one bad polygon anywhere in the source aborted the
entire build with no output. Measured on the real RWS source: 213 of 56,309
coastal_water polygons are invalid, and the union succeeds once repaired.
"""
import geopandas as gpd
import pytest
from shapely.geometry import Polygon, MultiPolygon

from nautical_routing_pipeline import NauticalRoutingPipeline as P


def _bowtie():
    """Self-intersecting ring -- the shape GEOS rejects."""
    return Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])


def _square(x=0.0, y=0.0, s=1.0):
    return Polygon([(x, y), (x + s, y), (x + s, y + s), (x, y + s)])


def _gdf(geoms):
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")


class TestInvalidGeometryIsRepaired:
    def test_bowtie_alone_does_not_raise(self):
        assert _bowtie().is_valid is False
        out = P._connected_water_polygons(P.__new__(P), _gdf([_bowtie()]))
        assert all(g.is_valid for g in out)

    def test_one_bad_polygon_does_not_lose_the_good_ones(self):
        # The regression: a single invalid geometry used to abort the build, so
        # every valid polygon was lost with it.
        good = [_square(10, 10), _square(20, 20)]
        out = P._connected_water_polygons(P.__new__(P), _gdf(good + [_bowtie()]))
        assert len(out) >= len(good)
        assert all(g.is_valid for g in out)

    def test_all_valid_input_is_untouched(self):
        good = [_square(0, 0), _square(5, 5)]
        out = P._connected_water_polygons(P.__new__(P), _gdf(good))
        assert len(out) == 2
        assert sum(g.area for g in out) == pytest.approx(2.0)

    def test_touching_squares_still_merge_into_one(self):
        # Repair must not defeat the union's actual job.
        out = P._connected_water_polygons(P.__new__(P), _gdf([_square(0, 0), _square(1, 0)]))
        assert len(out) == 1
        assert out[0].area == pytest.approx(2.0)

    def test_empty_and_none_are_skipped(self):
        out = P._connected_water_polygons(P.__new__(P), _gdf([_square(0, 0), Polygon()]))
        assert len(out) == 1

    def test_no_geometry_at_all_returns_empty(self):
        assert P._connected_water_polygons(P.__new__(P), _gdf([])) == []

    def test_output_is_polygonal_only(self):
        out = P._connected_water_polygons(P.__new__(P), _gdf([_bowtie(), _square(9, 9)]))
        assert all(isinstance(g, (Polygon, MultiPolygon)) for g in out)
