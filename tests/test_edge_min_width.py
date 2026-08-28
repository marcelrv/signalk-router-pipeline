"""Tier-1 unit tests: _edge_attr_worker must not destroy the measured channel width.

build_skeleton_network measures a real medial-axis width for every centerline edge
and stores it as `min_width` (with the matching `width_profile`). The worker then
recomputes edge attributes, and calculate_edge_attributes writes every key the worker
returns back onto the edge -- so anything the worker sets unconditionally overwrites
what the skeleton measured. `min_width` used to be seeded at the 999.0 "unconstrained"
default, which wiped the measurement on every edge: a real Zeeland build carried
min_width=999.0 on all 137,718 edges while width_profile still held true values on
81,110 of them. See docs/SPEC-GRAPH-DENSITY.md section 4.1.2.
"""
import geopandas as gpd
import pytest
from pyproj import Geod
from shapely.geometry import Polygon

from nautical_routing_pipeline import _edge_attr_init, _edge_attr_worker

# A short edge in open water; endpoints chosen so the lock polygon below straddles it.
U_LON, U_LAT = 4.1600, 51.6600
V_LON, V_LAT = 4.1620, 51.6600


def _edge(min_width, edge_type="coastal"):
    return (1, 2, U_LON, U_LAT, V_LON, V_LAT, edge_type, False, False, min_width)


def _run(edge, locks=None):
    gdfs = {"locks": locks if locks is not None else gpd.GeoDataFrame(geometry=[])}
    _edge_attr_init(Geod(ellps="WGS84"), gdfs)
    return _edge_attr_worker([edge])[(1, 2)]


def _lock(horclr):
    """A lock polygon covering the whole test edge, with the given horizontal clearance."""
    box = Polygon([(4.1590, 51.6595), (4.1630, 51.6595),
                   (4.1630, 51.6605), (4.1590, 51.6605)])
    return gpd.GeoDataFrame({"HORCLR": [horclr]}, geometry=[box], crs="EPSG:4326")


class TestMeasuredWidthSurvives:
    def test_medial_axis_width_is_preserved(self):
        # The regression this fixes: 142.8m in, 142.8m out -- not 999.0.
        assert _run(_edge(142.8))["min_width"] == pytest.approx(142.8)

    def test_narrow_creek_width_is_preserved(self):
        assert _run(_edge(6.0))["min_width"] == pytest.approx(6.0)

    def test_missing_width_still_defaults_to_unconstrained(self):
        # Edges that never carried a measurement (navmesh boundary, lock transit)
        # keep the previous behaviour exactly.
        assert _run(_edge(None))["min_width"] == 999.0


class TestLockClearanceNarrowsRatherThanReplaces:
    def test_lock_gate_narrows_a_wide_basin(self):
        # A 12m gate inside a 300m basin: the gate is the binding constraint.
        assert _run(_edge(300.0), _lock(12.0))["min_width"] == pytest.approx(12.0)

    def test_creek_keeps_its_own_width_when_the_gate_is_wider(self):
        # The behavioural change: HORCLR is one more constraint along the edge, not a
        # redefinition of it. A 20m gate must not widen a 6m creek to 20m.
        assert _run(_edge(6.0), _lock(20.0))["min_width"] == pytest.approx(6.0)

    def test_lock_still_applies_when_no_width_was_measured(self):
        assert _run(_edge(None), _lock(12.0))["min_width"] == pytest.approx(12.0)

    def test_non_intersecting_lock_does_not_touch_the_width(self):
        far = gpd.GeoDataFrame(
            {"HORCLR": [12.0]},
            geometry=[Polygon([(4.30, 51.80), (4.31, 51.80), (4.31, 51.81), (4.30, 51.81)])],
            crs="EPSG:4326")
        assert _run(_edge(142.8), far)["min_width"] == pytest.approx(142.8)
