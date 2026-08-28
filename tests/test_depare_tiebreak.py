"""Tier-1 unit tests: DEPARE candidate selection must be deterministic.

Overlapping ENC cells at different compilation scales routinely contain the
same sample point and tie on the exact same DRVAL1 (0.0 above all -- a
standard band floor every scale reuses). The winning candidate supplies not
just the depth but the DRVAL2 band and the src_cscl, and those two decide
whether a zero reading is emitted as a trusted 0.0 or as UNKNOWN_DEPTH. So a
tie broken by iterrows()/spatial-index visit order made the classification
depend on incidental row order from the multi-cell merge (CodeRabbit, PR #11).

Every selection test here therefore asserts over BOTH candidate orders.
"""
import itertools

import pytest

from nautical_routing_pipeline import (
    _depare_candidate_beats,
    _depare_candidate_sort_key,
    COARSE_DEPTH_BAND_DRVAL2_M,
    TRUSTED_SURVEY_CSCL_MAX,
    UNKNOWN_DEPTH,
)

# A candidate is the (DRVAL1, src_cscl, DRVAL2) triple the selection loops
# carry -- which is also the whole of what they hand downstream.
HARBOR = 12000            # a fine, trusted survey scale
APPROACH = 80000          # still inside TRUSTED_SURVEY_CSCL_MAX
COASTAL = 180000          # band-3, the Lake Worth Inlet failure's source cell


def select(candidates):
    """Mirror of the identical selection loop in _edge_attr_worker and
    NauticalRoutingPipeline._compute_node_depths."""
    best = best_cscl = best_upper = None
    for drval1, cscl, drval2 in candidates:
        if _depare_candidate_beats(drval1, cscl, drval2, best, best_cscl, best_upper):
            best, best_cscl, best_upper = drval1, cscl, drval2
    return best, best_cscl, best_upper


def classify(candidates):
    """Mirror of the downstream coarse-zero check both callers apply."""
    best, best_cscl, best_upper = select(candidates)
    if best is None:
        return None
    if best == 0.0 and (
        (best_upper is not None and best_upper >= COARSE_DEPTH_BAND_DRVAL2_M)
        or (best_cscl is not None and best_cscl > TRUSTED_SURVEY_CSCL_MAX)
    ):
        return UNKNOWN_DEPTH
    return best


def select_every_order(candidates):
    """The single selection result across every visit order, or fail."""
    results = {select(list(p)) for p in itertools.permutations(candidates)}
    assert len(results) == 1, f"order-dependent selection: {results}"
    return results.pop()


class TestPrimaryDepthRule:
    def test_deepest_containing_claim_wins(self):
        # Unchanged pre-#11 rule: nested bands mean deeper == more detailed.
        assert select_every_order([(0.0, COASTAL, 5.4), (11.7, HARBOR, 18.2)]) == (
            11.7, HARBOR, 18.2)

    def test_deeper_claim_wins_even_from_the_coarser_cell(self):
        # Scale is a tie-break only; it never overrides a strictly deeper read.
        assert select_every_order([(0.0, HARBOR, 1.8), (11.7, COASTAL, 18.2)]) == (
            11.7, COASTAL, 18.2)

    def test_genuine_drying_band_is_preserved(self):
        # A real intertidal reading is survey data, not flooring material.
        assert select_every_order([(-2.0, COASTAL, 5.4), (-2.0, HARBOR, 1.8)]) == (
            -2.0, HARBOR, 1.8)

    def test_no_candidates_selects_nothing(self):
        assert select([]) == (None, None, None)


class TestScaleTieBreak:
    def test_finer_scale_wins_a_zero_tie(self):
        # The Lake Worth Inlet shape: a band-3 coastal cell and a harbour cell
        # both claim DRVAL1=0.0. The harbour reading is the real one.
        assert select_every_order([(0.0, COASTAL, 5.4), (0.0, HARBOR, 1.8)]) == (
            0.0, HARBOR, 1.8)

    def test_finer_scale_wins_even_with_the_wider_band(self):
        # CSCL outranks band width: NOAA reuses the same standard cutoffs at
        # every scale, so DRVAL2 alone cannot tell them apart.
        assert select_every_order([(0.0, COASTAL, 1.8), (0.0, HARBOR, 18.2)]) == (
            0.0, HARBOR, 18.2)

    def test_equal_scales_fall_through_to_band_width(self):
        # Same cell scale on both sides: the narrower band is the more
        # specific claim, the reasoning COARSE_DEPTH_BAND_DRVAL2_M encodes.
        assert select_every_order([(0.0, HARBOR, 18.2), (0.0, HARBOR, 5.4)]) == (
            0.0, HARBOR, 5.4)

    def test_scale_outranks_band_width_between_two_trusted_cells(self):
        # Both are inside TRUSTED_SURVEY_CSCL_MAX, so neither makes the
        # reading unknown -- but selection still has to be deterministic.
        assert select_every_order([(0.0, APPROACH, 5.4), (0.0, HARBOR, 18.2)]) == (
            0.0, HARBOR, 18.2)


class TestUnknownScaleHandling:
    def test_known_scale_outranks_unlabeled_whatever_it_says(self):
        # Not "unknown is coarser" -- evidence outranks no evidence, and it
        # cuts the same way for a fine label...
        assert select_every_order([(0.0, HARBOR, 1.8), (0.0, None, 1.8)]) == (
            0.0, HARBOR, 1.8)

    def test_known_coarse_scale_also_outranks_unlabeled(self):
        # ...as for a coarse one. This is the direction that keeps
        # TRUSTED_SURVEY_CSCL_MAX able to fire when some cells went untagged.
        assert select_every_order([(0.0, COASTAL, 1.8), (0.0, None, 1.8)]) == (
            0.0, COASTAL, 1.8)

    def test_unlabeled_candidates_fall_back_to_band_width(self):
        # An untagged build behaves exactly as it did before #11.
        assert select_every_order([(0.0, None, 18.2), (0.0, None, 1.8)]) == (
            0.0, None, 1.8)

    def test_known_band_outranks_an_absent_one_at_equal_scale(self):
        assert select_every_order([(0.0, HARBOR, None), (0.0, HARBOR, 1.8)]) == (
            0.0, HARBOR, 1.8)


class TestClassificationIsOrderIndependent:
    """What the tie-break exists for: the emitted depth must not depend on
    which overlapping cell the spatial index happened to visit first."""

    @pytest.mark.parametrize("candidates,expected", [
        # A coarse cell's zero is a band floor, not a surveyed minimum.
        ([(0.0, COASTAL, 5.4)], UNKNOWN_DEPTH),
        # ...but a harbour cell claiming the same zero is real, and wins.
        ([(0.0, COASTAL, 5.4), (0.0, HARBOR, 1.8)], 0.0),
        # Wide band alone still triggers it, unlabeled or not.
        ([(0.0, None, 18.2)], UNKNOWN_DEPTH),
        ([(0.0, None, 18.2), (0.0, None, 22.0)], UNKNOWN_DEPTH),
        # Tight band from an untagged cell stays trusted, as pre-#11.
        ([(0.0, None, 1.8)], 0.0),
        # A labeled coarse cell tying an untagged one: the label decides.
        ([(0.0, COASTAL, 5.4), (0.0, None, 1.8)], UNKNOWN_DEPTH),
        # Real depths are never touched by any of this.
        ([(11.7, COASTAL, 18.2), (11.2, HARBOR, 18.2)], 11.7),
        ([(-2.0, HARBOR, 1.8), (-2.0, COASTAL, 5.4)], -2.0),
    ])
    def test_classification_holds_in_every_visit_order(self, candidates, expected):
        for perm in itertools.permutations(candidates):
            assert classify(list(perm)) == expected


class TestSortKeyIsATotalOrder:
    """The key must separate every distinct observable triple: two candidates
    comparing equal have the same DRVAL1, src_cscl and DRVAL2, so whichever is
    kept yields an identical result -- which is why no further tie-break key
    (source cell id, feature id, geometry) has to be threaded through."""

    UNIVERSE = [
        (drval1, cscl, drval2)
        for drval1 in (0.0, -2.0, 11.7)
        for cscl in (None, HARBOR, APPROACH, COASTAL)
        for drval2 in (None, 1.8, 5.4, 18.2)
    ]

    def test_equal_keys_imply_identical_candidates(self):
        for a, b in itertools.product(self.UNIVERSE, repeat=2):
            same_key = _depare_candidate_sort_key(*a) == _depare_candidate_sort_key(*b)
            assert same_key == (a == b), f"{a} vs {b}"

    def test_beats_is_antisymmetric(self):
        for a, b in itertools.product(self.UNIVERSE, repeat=2):
            a_beats_b = _depare_candidate_beats(*a, *b)
            b_beats_a = _depare_candidate_beats(*b, *a)
            if a == b:
                assert not a_beats_b, f"{a} displaces an identical incumbent"
            else:
                assert a_beats_b != b_beats_a, f"{a} vs {b}"

    def test_an_incumbent_is_never_displaced_by_an_equal_candidate(self):
        for cand in self.UNIVERSE:
            assert not _depare_candidate_beats(*cand, *cand)

    def test_first_candidate_always_wins_against_nothing(self):
        for cand in self.UNIVERSE:
            assert _depare_candidate_beats(*cand, None, None, None)

    def test_selection_is_invariant_over_every_order_of_any_four(self):
        # Exhaustive over a slice of the universe rather than sampled: every
        # 4-subset of the zero-depth candidates, in all 24 visit orders.
        zeros = [c for c in self.UNIVERSE if c[0] == 0.0]
        for subset in itertools.combinations(zeros, 4):
            select_every_order(subset)
