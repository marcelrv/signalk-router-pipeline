"""Unit tests for `_reclassify_scattered_narrow_fragments` (follow-on to
SPEC-GRAPH-DENSITY.md).

`_split_wide_narrow`'s erosion has no size/isolation-aware fold-back, unlike its
siblings `_split_deep_shallow` and `_tile_navmesh_piece`'s `tile_reclassified`
re-filter: a small, isolated sliver that erodes away purely because of small-scale
local detail near it (a rock, a jetty, a digitization artifact) -- not because the
water itself is narrow -- becomes "narrow" and is routed to skeleton/medial-axis
treatment even when it sits embedded in otherwise wide, deep, easily-navigable
water. This is what stops an intricate-but-deep area from fragmenting into many
scattered skeleton slivers that `_stitch_component_pieces`' Pass 0 then crisscrosses
with straight connectors, instead of the plain triangulated mesh the surrounding
water gets.

Two fixtures below exercise the two halves of the fold-back test:
- A cluster of tiny islands, well inside otherwise wide water, produces small
  "narrow" fragments hugging them -- morphological closing (at
  NARROW_FRAGMENT_RECLASS_CLOSING_M) swallows a cluster this small entirely, so the
  fold-back's eligibility re-test recovers ~100% of each fragment's area, and they
  get folded into `wide`.
- A genuine narrow channel attached to the same water body must NEVER be folded,
  regardless of how generous the fraction is: real channel width doesn't shrink
  under a modest closing radius, so the eligibility re-test correctly keeps failing
  for it, same as the original erosion did.
- The classic corner-rounding artifact of eroding a plain right-angle corner (an
  inherent property of morphological opening, not "noise" this mechanism should
  touch) is also confirmed to stay unfolded.

`narrow_fragment_reclass_max_fraction` defaults to 0.0 (disabled): nothing here
changes real build output until a build explicitly opts in via
`--narrow-fragment-reclass-max-fraction`, matching `--sagitta-cap`/
`--axis-dedup-cap`/`--connector-merge-m`'s convention.
"""
import math
from unittest import mock

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from nautical_routing_pipeline import ClassificationConfig, NauticalRoutingPipeline

RADIUS_M = 100.0


def _pipeline(narrow_fragment_reclass_max_fraction=0.0):
    return NauticalRoutingPipeline(
        data_paths={}, db_path=":memory:",
        min_navmesh_radius_m=RADIUS_M,
    ), ClassificationConfig(narrow_fragment_reclass_max_fraction=narrow_fragment_reclass_max_fraction)


def _island_cluster_water():
    """A 3000x3000 open square with 4 tiny (15x15) islands, 25m apart, clustered
    well inside the square (>1000m from every outer edge -- far more than
    RADIUS_M -- so the ONLY source of local narrowness near the cluster is the
    cluster itself, not proximity to the square's own outer boundary). The whole
    4-island cluster's footprint (bounds ~120m wide) is well under
    2 * NARROW_FRAGMENT_RECLASS_CLOSING_M (100m closing radius), so closing
    should swallow it entirely.
    """
    big = box(0, 0, 3000, 3000)
    islands = []
    x = 1450
    for _ in range(4):
        islands.append(box(x, 1500, x + 15, 1515))
        x += 40
    return big.difference(unary_union(islands))


def _island_cluster_water_with_channel():
    """Same island cluster, plus a genuine narrow channel (60m wide, 1500m long)
    attached to the square at a different edge, far from both the cluster and the
    corners this test also checks.
    """
    water = _island_cluster_water()
    channel = box(3000, 1400, 4500, 1460)  # 60m wide, attached at the x=3000 edge
    return unary_union([water, channel])


def _island_cluster_water_with_short_channel():
    """Same island cluster, plus a SHORT genuine narrow channel (60m wide, 400m
    long, area ~23,816 m^2) -- unlike `_island_cluster_water_with_channel`'s 1500m
    channel (area ~89,816 m^2, which exceeds max_area even at fraction=1.0 and so
    is excluded by the size test alone), this one's area sits below
    `1.0 * pi * RADIUS_M**2` (~31,416 m^2) at the validator's maximum fraction --
    so it actually reaches the geometric closing test, and must be rejected BY
    THAT test (not merely by size) to prove the mechanism doesn't just get lucky
    on channels too big to ever be size-eligible in the first place.
    """
    water = _island_cluster_water()
    channel = box(3000, 1400, 3400, 1460)  # 60m wide, 400m long
    return unary_union([water, channel])


class TestDisabledByDefaultReproducesTodaysSplit:
    def test_no_fragments_folded_and_stats_stay_zero(self):
        p, cfg = _pipeline(narrow_fragment_reclass_max_fraction=0.0)
        p.classification_config = cfg
        water = _island_cluster_water_with_channel()

        wide, narrow, seam = p._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        assert p.narrow_fragment_reclass_stats == {"fragments_checked": 0, "fragments_folded": 0}
        frags = p._explode_polygonal(narrow)
        # 4 corners + 3 island-cluster fragments + 1 channel = 8, all still narrow.
        assert len(frags) == 8


class TestEnabledFoldsIsolatedIslandClusterFragments:
    def test_island_cluster_fragments_are_folded_into_wide(self):
        p, cfg = _pipeline(narrow_fragment_reclass_max_fraction=0.5)
        p.classification_config = cfg
        water = _island_cluster_water_with_channel()

        wide, narrow, seam = p._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        assert p.narrow_fragment_reclass_stats["fragments_checked"] == 8
        assert p.narrow_fragment_reclass_stats["fragments_folded"] == 3
        frags = p._explode_polygonal(narrow)
        # 4 corners + channel remain; the 3 island-cluster fragments are gone.
        assert len(frags) == 5
        cluster_bounds = [f.bounds for f in frags if 1300 < f.bounds[0] < 1700]
        assert cluster_bounds == []

    def test_wide_area_increases_by_the_folded_fragments_own_area(self):
        p_off, cfg_off = _pipeline(narrow_fragment_reclass_max_fraction=0.0)
        p_off.classification_config = cfg_off
        water = _island_cluster_water_with_channel()
        wide_off, narrow_off, _ = p_off._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        p_on, cfg_on = _pipeline(narrow_fragment_reclass_max_fraction=0.5)
        p_on.classification_config = cfg_on
        wide_on, narrow_on, _ = p_on._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        # Total water area is conserved -- folding only moves area between wide
        # and narrow, it never creates or destroys any.
        assert wide_on.area + narrow_on.area == pytest.approx(wide_off.area + narrow_off.area, rel=1e-9)
        assert wide_on.area > wide_off.area
        assert narrow_on.area < narrow_off.area


class TestGenuineNarrowChannelAndCornersNeverFolded:
    def test_long_channel_stays_narrow_even_at_a_generous_fraction(self):
        # fraction=1.0 is the maximum the validator allows. The channel's own
        # area (~90,000 m^2, see the fixture) still exceeds max_area
        # (1.0 * pi * 100^2 ~= 31,416) even here, so it's excluded by the SIZE
        # test alone -- this test only proves that case. The geometric closing
        # test is separately exercised by test_short_channel_rejected_by_
        # geometric_closing_test_not_just_size below, using a smaller channel
        # that actually reaches that test.
        p, cfg = _pipeline(narrow_fragment_reclass_max_fraction=1.0)
        p.classification_config = cfg
        water = _island_cluster_water_with_channel()

        wide, narrow, seam = p._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        frags = p._explode_polygonal(narrow)
        channel_frags = [f for f in frags if f.bounds[0] >= 3000.0]
        assert len(channel_frags) == 1
        assert channel_frags[0].area == pytest.approx(89816.27982783053, rel=1e-6)

    def test_short_channel_rejected_by_geometric_closing_test_not_just_size(self):
        # This channel's area (~23,816 m^2) sits BELOW max_area at the
        # validator's maximum fraction (1.0 * pi * 100^2 ~= 31,416 m^2) -- so
        # unlike the long-channel test above, it passes the size gate and must
        # be rejected by the geometric closing test itself to stay narrow.
        p, cfg = _pipeline(narrow_fragment_reclass_max_fraction=1.0)
        p.classification_config = cfg
        water = _island_cluster_water_with_short_channel()

        _, narrow, _ = p._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        # Reached the geometric test (i.e. wasn't excluded by size alone) --
        # confirms this fixture actually exercises what it claims to.
        assert p.narrow_fragment_reclass_stats["fragments_checked"] == 8
        frags = p._explode_polygonal(narrow)
        channel_frags = [f for f in frags if f.bounds[0] >= 3000.0]
        assert len(channel_frags) == 1
        assert channel_frags[0].area == pytest.approx(23816.279827830534, rel=1e-6)

    def test_corner_rounding_artifacts_stay_narrow(self):
        p, cfg = _pipeline(narrow_fragment_reclass_max_fraction=0.5)
        p.classification_config = cfg
        water = _island_cluster_water_with_channel()

        wide, narrow, seam = p._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        frags = p._explode_polygonal(narrow)
        corner_frags = [f for f in frags if f.bounds[2] - f.bounds[0] < 200 and f.bounds[0] in (0.0, 2900.0)]
        assert len(corner_frags) == 4


class TestDefensiveFragmentCountCap:
    def test_skipped_entirely_above_the_max_fragment_count(self):
        p, cfg = _pipeline(narrow_fragment_reclass_max_fraction=0.5)
        p.classification_config = cfg
        water = _island_cluster_water_with_channel()

        with mock.patch("nautical_routing_pipeline.NARROW_FRAGMENT_RECLASS_MAX_COUNT", 2):
            wide, narrow, seam = p._split_wide_narrow(water, RADIUS_M, simplify_tol_m=1.0)

        # 8 fragments > the patched cap of 2 -- reclassification must be skipped
        # entirely (degrade gracefully), leaving narrow untouched.
        assert p.narrow_fragment_reclass_stats == {"fragments_checked": 0, "fragments_folded": 0}
        assert len(p._explode_polygonal(narrow)) == 8


class TestValidation:
    def test_zero_is_accepted(self):
        NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(0.0)

    def test_positive_value_within_range_is_accepted(self):
        NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(0.5)
        NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(1.0)

    def test_above_one_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(1.5)

    def test_negative_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(-0.1)

    def test_nan_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(float("nan"))

    def test_infinity_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_narrow_fragment_reclass_max_fraction(float("inf"))
