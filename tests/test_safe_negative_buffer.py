"""Unit tests for `_safe_negative_buffer` (SPEC-GRAPH-DENSITY.md).

`_split_wide_narrow`'s `geom.buffer(-radius_m)` (the erosion step that decides
navmesh-eligible "wide" water from "narrow" channel) can exhaust GEOS's own
working memory on an unusually large, highly complex polygon -- confirmed
directly against a real `coastal_water` build: a ~45k-vertex connected
component (a whole region's water merged into one piece,
`us-east-fl-atl-n1a`) raised `GEOSException: std::bad_alloc` at
`--min-navmesh-radius-m 1200.0`, OOM-killing the whole build (twice) before the
exception itself was ever even reached in isolation.

`_safe_negative_buffer` falls back to progressively coarser simplification
(50m, then 100m -- smaller tolerances were empirically insufficient for this
real case) ONLY after the plain buffer call actually fails, so every
currently-working build's geometry is untouched by default. Also calls
`gc.collect()` between attempts -- measured directly, skipping this let
failures cascade even through tolerances that succeed in isolation.
"""
import pytest
from shapely.errors import GEOSException
from shapely.geometry import Point

from nautical_routing_pipeline import NauticalRoutingPipeline


def _pipeline():
    return NauticalRoutingPipeline(data_paths={}, db_path=":memory:")


class _FakeGeom:
    """Deterministic stand-in for a shapely geometry with a controllable
    `.buffer()`/`.simplify()` -- lets us test the retry/escalation logic
    without needing to reproduce a multi-second real GEOS failure per test.
    `fails_at_tolerances` is the set of simplify tolerances (0.0 = full detail,
    i.e. no simplify call yet) at which `.buffer()` still raises.
    """

    def __init__(self, label, fails_at_tolerances=frozenset(), tol=0.0):
        self.label = label
        self._fails_at = fails_at_tolerances
        self._tol = tol

    def buffer(self, radius_m, quad_segs=16):
        if self._tol in self._fails_at:
            raise GEOSException("std::bad_alloc")
        return f"buffered({self.label},{radius_m})"

    def simplify(self, tol_m):
        return _FakeGeom(f"{self.label}~simplified({tol_m})",
                          fails_at_tolerances=self._fails_at, tol=tol_m)


class TestSafeNegativeBufferHappyPath:
    def test_succeeds_on_first_try_without_any_fallback(self):
        p = _pipeline()
        geom = _FakeGeom("normal")
        result = p._safe_negative_buffer(geom, 1200.0)
        assert result == "buffered(normal,-1200.0)"


class TestSafeNegativeBufferFallback:
    # The ladder starts at 50m (not smaller tolerances): measured directly
    # against the real pathological polygon, 10m/25m were empirically
    # insufficient -- only 50m+ reliably succeeded, so smaller tolerances would
    # just waste an attempt (and, per the gc.collect() finding below, every
    # wasted attempt risks poisoning the one that would actually work).
    def test_falls_back_to_50m_simplify_when_full_detail_fails(self):
        p = _pipeline()
        geom = _FakeGeom("huge", fails_at_tolerances={0.0})  # only full-detail fails
        result = p._safe_negative_buffer(geom, 1200.0)
        assert result == "buffered(huge~simplified(50.0),-1200.0)"

    def test_escalates_to_100m_when_50m_also_fails(self):
        p = _pipeline()
        geom = _FakeGeom("huge", fails_at_tolerances={0.0, 50.0})
        result = p._safe_negative_buffer(geom, 1200.0)
        assert result == "buffered(huge~simplified(100.0),-1200.0)"

    def test_raises_if_every_tolerance_still_fails(self):
        p = _pipeline()
        geom = _FakeGeom("impossible", fails_at_tolerances={0.0, 50.0, 100.0})
        with pytest.raises(GEOSException):
            p._safe_negative_buffer(geom, 1200.0)

    def test_memory_error_is_also_caught(self):
        class _MemErrorGeom:
            def buffer(self, radius_m, quad_segs=16):
                raise MemoryError()

            def simplify(self, tol_m):
                return _FakeGeom(f"simplified({tol_m})")

        p = _pipeline()
        result = p._safe_negative_buffer(_MemErrorGeom(), 1200.0)
        assert result == "buffered(simplified(50.0),-1200.0)"

    def test_gc_collect_is_called_between_attempts(self):
        # Regression test for the real finding: chaining escalating tolerances
        # WITHOUT an explicit gc.collect() between attempts kept failing in
        # sequence even for tolerances that succeed in isolation, because
        # glibc/GEOS's allocator doesn't necessarily return freed memory to the
        # OS after a failed huge allocation. Confirms the method actually calls
        # gc.collect() at least once when the first attempt fails.
        import unittest.mock as mock
        p = _pipeline()
        geom = _FakeGeom("huge", fails_at_tolerances={0.0})
        with mock.patch("nautical_routing_pipeline.gc.collect") as mock_gc:
            p._safe_negative_buffer(geom, 1200.0)
        assert mock_gc.call_count >= 1


class TestSafeNegativeBufferOnRealGeometry:
    def test_a_normal_small_polygon_is_unaffected(self):
        # Sanity check against a real (tiny, well-behaved) shapely geometry --
        # confirms the happy path works against the actual shapely API, not
        # just the fake stand-in above.
        p = _pipeline()
        square = Point(0, 0).buffer(1000.0)  # a real, simple, small polygon
        result = p._safe_negative_buffer(square, 100.0)
        assert result.is_valid
        assert not result.is_empty
