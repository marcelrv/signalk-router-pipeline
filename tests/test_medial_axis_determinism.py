"""Tier-1 unit tests: the medial-axis skeleton must be reproducible.

skimage.morphology.medial_axis breaks ties by processing pixels in an order drawn
from a PRNG, and defaults to a fresh unseeded generator on every call. The pipeline
called it that way, so the same input produced a different centerline on every run.
Measured on one Zeeland clip: node and edge counts moved less than 1% between two
identical builds, but only 62.3% of node ids were shared -- and since node ids are
coordinate-derived (_coord_to_id), that means 37.7% of nodes sat somewhere else.

That matters beyond tidiness: cross-database seam stitching matches seam nodes on
coordinates, so irreproducible node positions mean two independently built adjacent
regions cannot be relied on to agree about a shared seam.
See docs/SPEC-GRAPH-DENSITY.md section 5.
"""
import numpy as np
import pytest
from skimage.morphology import medial_axis

from nautical_routing_pipeline import (
    MEDIAL_AXIS_SEED,
    _MEDIAL_AXIS_RNG_KW,
    NauticalRoutingPipeline,
)

extract = NauticalRoutingPipeline._extract_medial_axis_skeleton


def _channel_mask():
    """A shape with enough tie-prone symmetry to expose the PRNG ordering."""
    mask = np.zeros((80, 80), dtype=bool)
    mask[30:50, 5:75] = True      # a long straight reach
    mask[20:60, 30:50] = True     # a wide basin crossing it
    mask[35:45, 60:78] = True     # a side branch
    return mask


class TestSeeding:
    def test_a_seed_keyword_was_found(self):
        # Spelled rng / random_state / seed across scikit-image versions; if a future
        # release renames it again this fails loudly rather than silently going
        # non-deterministic.
        assert _MEDIAL_AXIS_RNG_KW in ("rng", "random_state", "seed")

    def test_repeated_extraction_is_identical(self):
        mask = _channel_mask()
        first, first_dist = extract(mask)
        for _ in range(4):
            skel, dist = extract(mask)
            assert np.array_equal(skel, first)
            assert np.allclose(dist, first_dist)

    def test_the_unseeded_call_really_is_unstable(self):
        # Guards the premise: if upstream ever makes the default deterministic this
        # fails, and the seeding above becomes belt-and-braces rather than load-bearing.
        mask = _channel_mask()
        runs = [medial_axis(mask) for _ in range(8)]
        assert any(not np.array_equal(r, runs[0]) for r in runs[1:]), (
            "unseeded medial_axis no longer varies between calls")

    def test_seeded_matches_an_explicit_call_with_the_same_seed(self):
        mask = _channel_mask()
        skel, _ = extract(mask)
        expected = medial_axis(mask, **{_MEDIAL_AXIS_RNG_KW: MEDIAL_AXIS_SEED})
        assert np.array_equal(skel, expected)

    def test_distance_transform_still_returned(self):
        mask = _channel_mask()
        skel, dist = extract(mask)
        assert dist.shape == mask.shape
        # The width profile is read off this; it must be positive inside the shape.
        assert dist[skel].min() > 0
