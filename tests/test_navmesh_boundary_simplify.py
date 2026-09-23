"""Unit tests for `navmesh_boundary_simplify_m` (docs/SPEC-GRAPH-DENSITY.md §10.6 item 1).

`build_navmesh_region` has always run a coarse Douglas-Peucker pass over a navmesh
region's own boundary ring before triangulating/exporting it, at a hardcoded 5.0m
(`NAVMESH_BOUNDARY_SIMPLIFY_M`, empirically tuned in Round 9). §10.4 measured that
the resulting rings are still badly over-dense -- 83.5% of the navmesh nodes in the
surveyed Potomac stretch sit on no POI-to-POI route -- and §4.4 of
SPEC-GRAPH-CLEANUP.md established that navmesh density cannot be fixed post-build at
all (the triangulation is positionally indexed, and nearly every navmesh node is
referenced by `navmesh_regions.boundary_node_ids`). So the tolerance becomes a CLI
flag, `--navmesh-boundary-simplify-m`.

Two things this suite pins down:

1. The default is `NAVMESH_BOUNDARY_SIMPLIFY_M` itself, NOT the 0.0 "off" convention
   the sibling tolerance flags use -- this pass is on today, so a default build must
   stay byte-identical.
2. The land-safety behaviour above the default (§10.7's stated open risk).
   Douglas-Peucker never moves a retained vertex, but the ring between two retained
   vertices does move, outward over land where the water polygon is concave.
   Navmesh-boundary edges have no land-crossing safety net of their own (lenient
   bucket of `_sanity_check_no_land_crossings`, never stripped; no land-mask
   re-intersection like the skeleton's rasterizer).

   The first attempt at that guard -- re-intersecting the simplified polygon with
   the original water -- was measured on the real Zeeland `coastal_water` body and
   found counterproductive: the clip re-inserts every original vertex wherever a
   chord bulged outward, so the boundary got DENSER (5,861 vertices at the 5.0m
   default -> 27,586 at 15m), fragmented (1 part -> 37 at 15m, and
   `build_navmesh_region` keeps only the largest), and grew sub-millimetre ring
   segments. `_simplify_navmesh_boundary` now uses `_topology_guarded_simplify`
   instead: an asymmetric-tolerance Douglas-Peucker that may cut INWARD by the full
   tolerance but may bulge OUTWARD over land by at most
   `NAVMESH_BOUNDARY_SIMPLIFY_M` (i.e. never further over land than the shipped
   default already goes), and that refuses any chord which would cross or engulf
   another piece of the original boundary. The tests below pin that contract:
   fewer vertices than the default, never further outside the water than the
   default, one part, islands kept, necks open, vertices only ever REMOVED.
"""
import math

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from nautical_routing_pipeline import (
    ClassificationConfig,
    NAVMESH_BOUNDARY_SIMPLIFY_M,
    NAVMESH_BOUNDARY_SIMPLIFY_MAX_M,
    NauticalRoutingPipeline,
)

UTM_CRS = "EPSG:32631"  # same metric CRS tests/test_axis_dedup.py's navmesh tests use


def _pipeline(navmesh_boundary_simplify_m=None):
    """__init__ only assigns attributes (no file I/O) -- safe to build directly,
    same pattern as tests/test_axis_dedup.py's own `_pipeline()`. Passing None
    leaves the constructor/dataclass default in place, which is the point of the
    byte-identical-at-default tests below."""
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    if navmesh_boundary_simplify_m is not None:
        p.classification_config = ClassificationConfig(
            navmesh_boundary_simplify_m=navmesh_boundary_simplify_m)
    # Normally set at the top of build_network(); tests calling node-creating
    # methods (build_navmesh_region) directly need them initialized the same way.
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


def _wobbly_disc_utm(n=400, radius=900.0, cx=501000.0, cy=5700500.0):
    """A 400-vertex near-circular water body ~1.8km across -- a stand-in for a
    survey-grade digitized open-water boundary: far denser than the shape needs,
    with every vertex a genuine (tiny) direction change. Convex, so every
    Douglas-Peucker chord cuts INWARD (into the water), so the guarded pass's
    outward-excursion cap and cross/engulf guards are inert on it by construction --
    which is exactly what makes it the right fixture for measuring the vertex-count
    win on its own.
    """
    return Polygon([(cx + radius * math.cos(2 * math.pi * i / n),
                     cy + radius * math.sin(2 * math.pi * i / n)) for i in range(n)])


def _water_with_land_spit_utm():
    """A 2km x 1km water rectangle with a wide, shallow triangular land spit
    (400m across, 20m deep) intruding from the south edge. The spit's apex is 20m
    from the chord across its mouth, so any tolerance above 20m makes
    Douglas-Peucker drop the apex and the water polygon swallow the spit -- a
    literal navmesh-over-land leak. Returns (water, land_probe_point)."""
    spit = Polygon([(500800.0, 5700000.0), (501000.0, 5700020.0), (501200.0, 5700000.0)])
    water = box(500000.0, 5700000.0, 502000.0, 5701000.0).difference(spit)
    return water, Point(501000.0, 5700010.0)


def _water_with_island_utm(island_radius=15.0):
    """A large round water body with one small island -- an interior ring. GEOS
    does not drop the ring when it simplifies, but it does shrink it (measured:
    ~1/3 of the island's area survives at a 40m tolerance), so the mesh spreads
    over most of the island. Returns (water, island)."""
    centre = Point(501000.0, 5700500.0)
    island = centre.buffer(island_radius, quad_segs=16)
    return centre.buffer(1500.0, quad_segs=64).difference(island), island


def _boundary_vertex_count(geom):
    return sum(len(p.exterior.coords) - 1 + sum(len(r.coords) - 1 for r in p.interiors)
               for p in NauticalRoutingPipeline._explode_polygonal(geom))


class TestFlagIsPlumbed:
    def test_config_default_is_the_module_constant(self):
        assert ClassificationConfig().navmesh_boundary_simplify_m == NAVMESH_BOUNDARY_SIMPLIFY_M
        assert NAVMESH_BOUNDARY_SIMPLIFY_M == 5.0

    def test_constructor_default_is_the_module_constant(self):
        p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
        assert p.classification_config.navmesh_boundary_simplify_m == NAVMESH_BOUNDARY_SIMPLIFY_M

    def test_constructor_override_reaches_the_classification_config(self):
        p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:",
                                     navmesh_boundary_simplify_m=25.0)
        assert p.classification_config.navmesh_boundary_simplify_m == 25.0

    def test_override_reaches_build_navmesh_region(self, monkeypatch):
        p = _pipeline(navmesh_boundary_simplify_m=25.0)
        seen = []
        real = p._simplify_navmesh_boundary

        def spy(poly_m, protected_coords=None):
            seen.append(p.classification_config.navmesh_boundary_simplify_m)
            return real(poly_m, protected_coords)

        monkeypatch.setattr(p, "_simplify_navmesh_boundary", spy)
        p.build_navmesh_region(_wobbly_disc_utm(), UTM_CRS, set())

        assert seen == [25.0], "build_navmesh_region must simplify at the configured tolerance"

    def test_tiling_gate_measures_the_configured_boundary(self, monkeypatch):
        # _tile_navmesh_piece's NAVMESH_TILE_MAX_VERTICES gate counts the
        # post-simplify boundary; it must count the boundary that will actually be
        # built, not a hardcoded-5.0m one.
        p = _pipeline(navmesh_boundary_simplify_m=25.0)
        seen = []
        real = p._simplify_navmesh_boundary
        monkeypatch.setattr(p, "_simplify_navmesh_boundary",
                            lambda poly_m: (seen.append(
                                p.classification_config.navmesh_boundary_simplify_m), real(poly_m))[1])

        p._tile_navmesh_piece(_wobbly_disc_utm(), 10_000.0, 800.0)

        assert seen == [25.0]


class TestDefaultReproducesTodaysBehaviour:
    def test_default_matches_the_pre_flag_expression_exactly(self):
        # The literal expression build_navmesh_region/_tile_navmesh_piece used
        # before the flag existed. `equals_exact` with zero tolerance, not
        # `equals`: this is a byte-identical claim, not a topological one.
        poly = _wobbly_disc_utm()
        p = _pipeline()
        legacy = p._clean_polygonal(poly.buffer(0).simplify(NAVMESH_BOUNDARY_SIMPLIFY_M))

        assert p._simplify_navmesh_boundary(poly).equals_exact(legacy, 0.0)

    def test_default_matches_pre_flag_expression_on_concave_geometry_too(self):
        # The guarded pass must be genuinely skipped at the default -- on a concave
        # polygon (where its caps and guards would have something to do) the result
        # is still the plain legacy Douglas-Peucker geometry, so no default build
        # changes.
        water, _ = _water_with_land_spit_utm()
        p = _pipeline()
        legacy = p._clean_polygonal(water.buffer(0).simplify(NAVMESH_BOUNDARY_SIMPLIFY_M))

        assert p._simplify_navmesh_boundary(water).equals_exact(legacy, 0.0)

    def test_explicit_default_and_omitted_flag_build_identical_graphs(self):
        poly = _wobbly_disc_utm()
        p_default = _pipeline()
        p_default.build_navmesh_region(poly, UTM_CRS, set())
        p_explicit = _pipeline(navmesh_boundary_simplify_m=NAVMESH_BOUNDARY_SIMPLIFY_M)
        p_explicit.build_navmesh_region(poly, UTM_CRS, set())

        # Node ids are coordinate-derived, so identical builds produce the same ids,
        # not merely the same counts (same argument as the skeleton suite's).
        assert set(p_default.graph.nodes) == set(p_explicit.graph.nodes)
        assert set(p_default.graph.edges) == set(p_explicit.graph.edges)


class TestLargerToleranceThinsTheBoundary:
    def test_larger_tolerance_reduces_boundary_vertex_count(self):
        poly = _wobbly_disc_utm()
        at_default = _boundary_vertex_count(
            _pipeline()._simplify_navmesh_boundary(poly))
        at_25 = _boundary_vertex_count(
            _pipeline(navmesh_boundary_simplify_m=25.0)._simplify_navmesh_boundary(poly))

        assert at_25 < at_default < _boundary_vertex_count(poly)

    def test_larger_tolerance_reduces_registered_navmesh_nodes(self):
        # The point of the flag: fewer boundary ring nodes/edges in the graph.
        poly = _wobbly_disc_utm()
        p_default = _pipeline()
        p_default.build_navmesh_region(poly, UTM_CRS, set())
        p_coarse = _pipeline(navmesh_boundary_simplify_m=25.0)
        p_coarse.build_navmesh_region(poly, UTM_CRS, set())

        assert p_coarse.graph.number_of_nodes() < p_default.graph.number_of_nodes()
        assert p_coarse.graph.number_of_edges() < p_default.graph.number_of_edges()

    def test_zero_disables_the_pass(self):
        # 0.0 is a real value here (the Round 9 sweep's "no pass" arm), not "flag
        # off": it keeps every boundary vertex.
        poly = _wobbly_disc_utm()
        at_zero = _boundary_vertex_count(
            _pipeline(navmesh_boundary_simplify_m=0.0)._simplify_navmesh_boundary(poly))

        assert at_zero == _boundary_vertex_count(poly)


def _coastline_with_islands_and_neck_utm(jitter_m=4.0, step_m=6.0, neck_w=40.0):
    """A NON-convex, coastline-like water body: two 900m basins joined by a
    dog-leg neck `neck_w` wide, every shore vertex wobbled +/-`jitter_m` by a
    deterministic pseudo-random function (so the boundary is alternately concave
    and convex toward the water, like a digitized coastline -- unlike the convex
    disc above, where a guard has nothing to do), plus two islands (interior
    rings). Returns (water, islands, neck_cut, axis) where `neck_cut` is a line
    across the neck's narrow leg and `axis` is a route through both basins.

    The neck is deliberately left un-wobbled and given a bend: a straight neck's
    walls are collinear and cannot be pinched by any Douglas-Peucker chord, so it
    would not test anything, whereas an inner corner is exactly what a chord wants
    to cut across (and what a morphological opening at t > neck_w/2 destroys).
    """
    left = box(500000.0, 5700000.0, 500900.0, 5700800.0)
    right = box(501300.0, 5700200.0, 502200.0, 5700900.0)
    neck_a = box(500900.0, 5700400.0 - neck_w / 2, 501100.0, 5700400.0 + neck_w / 2)
    neck_b = box(501100.0 - neck_w, 5700400.0 - neck_w / 2, 501100.0, 5700620.0)
    neck_c = box(501100.0 - neck_w, 5700620.0 - neck_w, 501300.0, 5700620.0)
    water = unary_union([left, neck_a, neck_b, neck_c, right])
    pts = []
    for i, (x, y) in enumerate(shapely.segmentize(water.exterior, step_m).coords[:-1]):
        if 500880.0 <= x <= 501320.0 and 5700330.0 <= y <= 5700700.0:
            pts.append((x, y))  # the neck itself stays exact
            continue
        wobble = math.sin(i * 12.9898) * 43758.5453
        d = (wobble - math.floor(wobble) - 0.5) * 2 * jitter_m
        along_x = abs(x - 500000.0) < 1.0 or abs(x - 502200.0) < 1.0
        pts.append((x + d, y) if along_x else (x, y + d))
    islands = [Point(500300.0, 5700600.0).buffer(60.0, quad_segs=24),
               Point(501800.0, 5700450.0).buffer(25.0, quad_segs=16)]
    water = Polygon(pts).difference(unary_union(islands))
    neck_cut = LineString([(501000.0, 5700500.0), (501160.0, 5700500.0)])
    axis = LineString([(500500.0, 5700400.0), (501080.0, 5700400.0),
                       (501080.0, 5700600.0), (501900.0, 5700600.0)])
    return water, islands, neck_cut, axis


def _min_segment_m(geom):
    shortest = math.inf
    for p in NauticalRoutingPipeline._explode_polygonal(geom):
        for ring in [p.exterior, *p.interiors]:
            c = np.asarray(ring.coords)
            shortest = min(shortest, float(np.hypot(np.diff(c[:, 0]), np.diff(c[:, 1])).min()))
    return shortest


def _coords(geom):
    return {(round(x, 9), round(y, 9)) for x, y in shapely.get_coordinates(geom.boundary)}


class TestTopologyGuardedSimplifyOnACoastline:
    """The contract of the raised-tolerance path, on a fixture that is concave,
    islanded and necked -- i.e. where a plain `simplify()` and the old
    re-intersect clip both misbehave. Measured on this fixture: 169 boundary
    vertices at the 5.0m default -> 38 at 20m/40m/99m."""

    TOLERANCES = (20.0, 40.0, 99.0)

    def test_raised_tolerance_beats_the_default_vertex_count_and_is_monotonic(self):
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        at_default = _boundary_vertex_count(_pipeline()._simplify_navmesh_boundary(water))
        counts = [_boundary_vertex_count(
            _pipeline(navmesh_boundary_simplify_m=t)._simplify_navmesh_boundary(water))
            for t in self.TOLERANCES]

        # The whole point of the flag, and exactly what the old clip got backwards.
        assert all(c <= at_default for c in counts), (counts, at_default)
        assert counts[0] < at_default
        assert counts == sorted(counts, reverse=True), counts

    def test_result_stays_one_valid_part_with_its_islands(self):
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        for tol in self.TOLERANCES:
            result = _pipeline(navmesh_boundary_simplify_m=tol)._simplify_navmesh_boundary(water)

            assert result.geom_type == "Polygon", tol      # the clip gave 37-55 parts
            assert result.is_valid, tol
            assert len(result.interiors) == len(water.interiors), tol

    def test_every_vertex_comes_from_the_original_boundary(self):
        # build_navmesh_region computes seam_coord_set BEFORE this pass and matches
        # it by exact coordinate afterwards, so the pass may only ever REMOVE
        # vertices -- never move one, never invent one (which is what an
        # intersection()/buffer() based guard does).
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        original = _coords(water)
        for tol in self.TOLERANCES:
            result = _pipeline(navmesh_boundary_simplify_m=tol)._simplify_navmesh_boundary(water)

            assert _coords(result) <= original, tol

    def test_never_reaches_further_over_land_than_the_default_tolerance(self):
        # Not "is a subset of the water": the 5.0m default itself is not, and
        # demanding that is what made the old clip re-insert 5x the vertices. The
        # real contract is that no part of the mesh is further outside the water
        # than the shipped default already allows.
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        band = water.buffer(NAVMESH_BOUNDARY_SIMPLIFY_M + 1e-6)
        at_default = _pipeline()._simplify_navmesh_boundary(water).difference(water).area
        for tol in self.TOLERANCES:
            result = _pipeline(navmesh_boundary_simplify_m=tol)._simplify_navmesh_boundary(water)

            assert result.difference(band).area == pytest.approx(0.0, abs=1e-6), tol
            assert result.difference(water).area <= at_default, tol

    def test_naive_simplify_would_fail_both_of_those(self):
        # Fixture guard: without the asymmetric cap, the same tolerances put
        # 2-3x more mesh over land than the default does.
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        p = _pipeline()
        at_default = p._simplify_navmesh_boundary(water).difference(water).area
        for tol in self.TOLERANCES:
            naive = p._clean_polygonal(water.buffer(0).simplify(tol))

            assert naive.difference(water).area > at_default, tol

    def test_the_narrow_neck_stays_open(self):
        water, _, neck_cut, axis = _coastline_with_islands_and_neck_utm()
        width = water.intersection(neck_cut).length
        for tol in self.TOLERANCES:
            result = _pipeline(navmesh_boundary_simplify_m=tol)._simplify_navmesh_boundary(water)

            # Not merely "still one polygon": the passage must still be navigable
            # end to end, and not reduced to a slit.
            assert result.contains(axis), tol
            assert result.intersection(neck_cut).length >= 0.9 * width, tol

    def test_no_micro_segments(self):
        # The clip emitted 6e-5 m ring segments on real coastline -- a documented
        # `triangle -pq28` blow-up risk. A vertex-subset pass cannot: its segments
        # are unions of original ones.
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        for tol in self.TOLERANCES:
            result = _pipeline(navmesh_boundary_simplify_m=tol)._simplify_navmesh_boundary(water)

            assert _min_segment_m(result) >= 1.0, (tol, _min_segment_m(result))

    def test_a_chord_may_not_engulf_or_cross_an_island(self):
        """Two 80m-deep bays, each with an island: one wholly inside the bay, one
        straddling the bay's mouth. At 99m the chord across either mouth is within
        the inward tolerance, so without guard 2 it is taken -- leaving the first
        island's ring outside the shell and slicing through the second's. Measured:
        with the guards the pass simplifies 140 boundary vertices to 27 and keeps
        both rings; with guard 2 removed the polygon comes out invalid and the
        fail-closed path hands back all 140 unsimplified vertices."""
        main = box(500000.0, 5700000.0, 501000.0, 5700600.0)
        bays = [box(500200.0, 5699920.0, 500400.0, 5700000.0),
                box(500600.0, 5699920.0, 500800.0, 5700000.0)]
        islands = [Point(500300.0, 5699960.0).buffer(18.0, quad_segs=16),
                   Point(500700.0, 5700000.0).buffer(18.0, quad_segs=16)]
        water = unary_union([main, *bays]).difference(unary_union(islands))
        at_default = _boundary_vertex_count(_pipeline()._simplify_navmesh_boundary(water))

        result = _pipeline(navmesh_boundary_simplify_m=99.0)._simplify_navmesh_boundary(water)

        assert result.is_valid and result.geom_type == "Polygon"
        assert len(result.interiors) == 2, "both island rings must survive"
        assert _boundary_vertex_count(result) <= at_default

    def test_a_chord_may_not_engulf_an_island_it_never_crosses(self):
        """The engulf half of guard 2, isolated. A 200x40m bump of water on an
        otherwise straight shore, with a 12m island inside it: the chord across the
        bump's mouth is within the inward tolerance and crosses nothing (the island
        is 40m clear of it), so only the "does this loop contain another ring"
        check stops it. Measured: with the check, 130 boundary vertices simplify to
        12 and the island ring survives; without it the bump is swallowed, the hole
        ends up outside the shell, and the fail-closed path hands back all 130."""
        bump = box(500400.0, 5699960.0, 500600.0, 5700000.0)
        island = Point(500500.0, 5699980.0).buffer(12.0, quad_segs=16)
        water = shapely.segmentize(
            unary_union([box(500000.0, 5700000.0, 501000.0, 5700600.0), bump]),
            50.0).difference(island)
        at_default = _boundary_vertex_count(_pipeline()._simplify_navmesh_boundary(water))

        result = _pipeline(navmesh_boundary_simplify_m=99.0)._simplify_navmesh_boundary(water)

        assert result.is_valid and len(result.interiors) == 1
        assert _boundary_vertex_count(result) <= at_default < _boundary_vertex_count(water)
        # and the bump itself is still water, not cut off
        assert result.intersection(bump).area >= 0.9 * water.intersection(bump).area

    def test_seam_coordinates_are_never_dropped(self):
        """`build_navmesh_region` matches `seam_coord_set` by exact (3-decimal)
        coordinate AFTER this pass, so a dropped seam vertex is a lost cross-piece
        stitching point. Seam coordinates are therefore protected DP anchors."""
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        every = sorted({(round(x, 3), round(y, 3))
                        for x, y in shapely.get_coordinates(water.exterior)})
        seams = set(every[::37])
        for tol in (20.0, 99.0):
            p = _pipeline(navmesh_boundary_simplify_m=tol)
            kept_anyway = _coords(p._simplify_navmesh_boundary(water))
            protected = _coords(p._simplify_navmesh_boundary(water, seams))

            # fixture guard: unprotected, this tolerance really does drop seams
            assert not seams <= {(round(x, 3), round(y, 3)) for x, y in kept_anyway}, tol
            assert seams <= {(round(x, 3), round(y, 3)) for x, y in protected}, tol

    def test_result_does_not_depend_on_where_the_ring_starts(self):
        """A ring's start index is an artifact of whatever GEOS operation produced
        it. Anchoring the DP on index 0 made an unrelated upstream change that
        rotated a ring silently change the navmesh, so the anchors are chosen
        geometrically instead."""
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        coords = list(water.exterior.coords)[:-1]
        rotated = Polygon(coords[500:] + coords[:500], [list(r.coords) for r in water.interiors])
        for tol in (20.0, 99.0):
            p = _pipeline(navmesh_boundary_simplify_m=tol)

            assert _coords(p._simplify_navmesh_boundary(water)) == \
                _coords(p._simplify_navmesh_boundary(rotated)), tol

    def test_multipolygon_input_keeps_all_of_its_parts(self):
        a = _wobbly_disc_utm(cx=501000.0, cy=5700500.0)
        b = _wobbly_disc_utm(cx=511000.0, cy=5700500.0)
        both = MultiPolygon([a, b])
        result = _pipeline(navmesh_boundary_simplify_m=40.0)._simplify_navmesh_boundary(both)

        assert len(NauticalRoutingPipeline._explode_polygonal(result)) == 2
        assert _boundary_vertex_count(result) < _boundary_vertex_count(both)

    def test_fails_closed_to_the_unsimplified_polygon(self, monkeypatch):
        # If the guarded pass cannot keep the topology it must hand back the
        # PRE-simplify geometry (dense but correct), never an empty or
        # differently-shaped one. The old code's empty-clip branch returned the
        # unclipped, land-leaking polygon instead.
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        p = _pipeline(navmesh_boundary_simplify_m=40.0)
        monkeypatch.setattr(p, "_topology_guarded_simplify", lambda *a, **kw: None)

        result = p._simplify_navmesh_boundary(water)

        assert not result.is_empty
        assert result.equals(p._clean_polygonal(water.buffer(0)))

    @pytest.mark.parametrize("broken, why", [
        (lambda: Polygon([(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)]), "invalid"),
        (lambda: MultiPolygon([box(0.0, 0.0, 1.0, 1.0), box(5.0, 5.0, 6.0, 6.0)]), "fragmented"),
        (lambda: Polygon(), "empty"),
    ])
    def test_the_assembled_ring_set_is_re_checked_before_it_is_returned(
            self, monkeypatch, caplog, broken, why):
        """Guard 1 and guard 2 are supposed to make an invalid/fragmented result
        impossible -- but "supposed to" is not a check, and §10.6 lists one residual
        path (two chords on opposite banks of a neck narrower than 2*tol crossing
        each other) that no guard catches. So `_topology_guarded_simplify`
        re-validates what it assembled: a self-intersecting, multi-part or empty
        result must become `None`, and `_simplify_navmesh_boundary` must then return
        the PRE-simplify polygon and say so in the log -- never the broken geometry.
        Simulated here by making the ring assembly itself produce that geometry."""
        water, _, _, _ = _coastline_with_islands_and_neck_utm()
        p = _pipeline(navmesh_boundary_simplify_m=40.0)
        monkeypatch.setattr(p, "_assemble_simplified_rings", lambda *a, **kw: broken())

        assert p._topology_guarded_simplify(water, 40.0, 5.0) is None, why

        with caplog.at_level("WARNING"):
            result = p._simplify_navmesh_boundary(water)
        assert result.equals(p._clean_polygonal(water.buffer(0))), why
        assert any("could not keep the polygon's topology" in r.message for r in caplog.records)


class TestLandSafetyAboveTheDefault:
    """§10.7's open risk: a raised tolerance must not put more navmesh over land
    than the empirically tuned default already does."""

    def test_naive_simplify_really_does_leak_over_land(self):
        # Guards the fixture itself: with a plain simplify, a 40m tolerance swallows
        # the land spit. If this ever stops failing, the test below proves nothing.
        water, land_probe = _water_with_land_spit_utm()
        assert not water.contains(land_probe)
        naive = _pipeline()._clean_polygonal(water.buffer(0).simplify(40.0))

        assert naive.contains(land_probe)

    def test_the_land_spit_is_not_swallowed(self):
        # The spit's apex is 20m from the chord across its mouth: above the 5.0m
        # outward cap, so the chord is rejected however high the tolerance goes.
        water, land_probe = _water_with_land_spit_utm()
        result = _pipeline(navmesh_boundary_simplify_m=40.0)._simplify_navmesh_boundary(water)

        assert not result.contains(land_probe)

    def test_result_stays_inside_the_default_tolerance_band(self):
        water, _ = _water_with_land_spit_utm()
        result = _pipeline(navmesh_boundary_simplify_m=40.0)._simplify_navmesh_boundary(water)

        # Not a subset of the water (the 5.0m default is not one either), but never
        # further outside it than that default already goes.
        assert result.difference(
            water.buffer(NAVMESH_BOUNDARY_SIMPLIFY_M + 1e-6)).area == pytest.approx(0.0, abs=1e-6)

    def test_a_small_island_is_covered_no_more_than_at_the_default(self):
        # An island's interior ring is not dropped by the pass, but a chord across
        # it does cut the corners off the ring. The outward cap pins how far:
        # measured, a 15m-radius island loses exactly the same 255.7 m^2 at 40m as
        # it already does at the 5.0m default, while a naive 40m pass loses 481 m^2.
        water, island = _water_with_island_utm()
        p = _pipeline(navmesh_boundary_simplify_m=40.0)
        naive = p._clean_polygonal(water.buffer(0).simplify(40.0))
        at_default = _pipeline()._simplify_navmesh_boundary(water).intersection(island).area
        # Fixture guard: the naive pass shrinks the island's hole a lot further.
        assert naive.intersection(island).area > 1.5 * at_default

        result = p._simplify_navmesh_boundary(water)

        assert len(result.interiors) == 1, "the island's ring must survive"
        assert result.intersection(island).area <= at_default + 1e-6

    def test_raised_tolerance_still_removes_vertices(self):
        # The guards must not degenerate into "no simplification at all": the
        # inward chords (the safe ones) are kept.
        water, _ = _water_with_land_spit_utm()
        # Straddles the rectangle's west edge, so half its dense rim is genuinely
        # part of the merged boundary rather than swallowed by the rectangle.
        dense = _wobbly_disc_utm(n=200, radius=400.0, cx=500000.0, cy=5700500.0)
        poly = water.union(dense)
        at_zero = _boundary_vertex_count(
            _pipeline(navmesh_boundary_simplify_m=0.0)._simplify_navmesh_boundary(poly))
        at_40 = _boundary_vertex_count(
            _pipeline(navmesh_boundary_simplify_m=40.0)._simplify_navmesh_boundary(poly))

        assert at_40 < at_zero

    def test_the_guards_are_inert_on_convex_geometry(self):
        # Where Douglas-Peucker only ever cuts inward (a convex water body) there is
        # no land excursion and nothing to cross, so the guarded pass costs no
        # vertices at all -- the full simplification win survives.
        poly = _wobbly_disc_utm()
        guarded = _pipeline(navmesh_boundary_simplify_m=25.0)._simplify_navmesh_boundary(poly)
        unguarded = _pipeline()._clean_polygonal(poly.buffer(0).simplify(25.0))

        assert _boundary_vertex_count(guarded) == _boundary_vertex_count(unguarded)


class TestValidation:
    def test_default_is_accepted(self):
        NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(NAVMESH_BOUNDARY_SIMPLIFY_M)

    def test_zero_is_accepted(self):
        NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(0.0)

    def test_positive_value_within_range_is_accepted(self):
        NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(30.0)

    def test_at_or_above_ceiling_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(
                NAVMESH_BOUNDARY_SIMPLIFY_MAX_M)
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(
                NAVMESH_BOUNDARY_SIMPLIFY_MAX_M + 50.0)

    def test_negative_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(-1.0)

    def test_nan_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(float("nan"))

    def test_infinity_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_navmesh_boundary_simplify_m(float("inf"))

    def test_build_network_validates_the_configured_value(self):
        # The same wiring every sibling flag has: an invalid value set directly on
        # the config (not via argparse) is still rejected before anything is built.
        p = _pipeline(navmesh_boundary_simplify_m=-1.0)
        p.gdfs = {}
        with pytest.raises(ValueError, match="navmesh_boundary_simplify_m"):
            p.build_network()
