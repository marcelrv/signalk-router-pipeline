"""Unit tests for the constructor-level ClassificationConfig overrides
(`axis_dedup_fraction`, `axis_dedup_floor_m`, `min_navmesh_radius_m`).

CodeRabbit (PR #20): these three overrides bypassed validation entirely --
`min_navmesh_radius_m` reaches `_split_wide_narrow`'s `buffer(-radius_m)`, so a
negative value reverses erosion into dilation, silently inverting which parts of a
water polygon count as navmesh-eligible ("wide") vs. skeleton/channel ("narrow");
`NaN`/`inf` on any of the three can reach downstream geometry/raster operations.
`_validate_classification_overrides` closes that gap, called both from the
constructor (so any programmatic caller is protected) and from the CLI (for a
clean `SystemExit` instead of a raw traceback/silent misbehavior).
"""
import math

import pytest

from nautical_routing_pipeline import NauticalRoutingPipeline


class TestValidateClassificationOverrides:
    def test_all_none_is_always_valid(self):
        # None means "keep the ClassificationConfig default" -- never validated.
        NauticalRoutingPipeline._validate_classification_overrides(0.0, None, None, None)

    def test_sane_values_are_accepted(self):
        NauticalRoutingPipeline._validate_classification_overrides(50.0, 0.5, 5.0, 800.0)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
    def test_rejects_non_finite_or_negative_fraction(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_classification_overrides(50.0, bad, None, None)

    def test_zero_fraction_is_accepted(self):
        # A degenerate but well-defined choice (tol always == floor) -- not an error.
        NauticalRoutingPipeline._validate_classification_overrides(50.0, 0.0, None, None)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
    def test_rejects_non_finite_or_negative_floor(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_classification_overrides(50.0, None, bad, None)

    def test_floor_above_the_active_cap_is_rejected(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_classification_overrides(50.0, None, 100.0, None)

    def test_floor_equal_to_the_cap_is_accepted(self):
        NauticalRoutingPipeline._validate_classification_overrides(50.0, None, 50.0, None)

    def test_floor_above_a_disabled_cap_is_not_checked(self):
        # axis_dedup_cap_m == 0.0 means the whole axis-dedup mechanism is off --
        # a floor value is inert, so it isn't compared against the (disabled) cap.
        NauticalRoutingPipeline._validate_classification_overrides(0.0, None, 100.0, None)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
    def test_rejects_non_finite_zero_or_negative_navmesh_radius(self, bad):
        # Zero/negative is rejected outright (not just "disabled"): unlike the
        # other flags in this file, min_navmesh_radius_m has no 0.0-means-off
        # convention -- it's always the live disk-radius test _split_wide_narrow
        # uses, so a non-positive value would only ever be a mistake.
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_classification_overrides(50.0, None, None, bad)

    def test_a_positive_navmesh_radius_is_accepted(self):
        NauticalRoutingPipeline._validate_classification_overrides(50.0, None, None, 1200.0)


class TestConstructorValidatesOverrides:
    """The constructor calls _validate_classification_overrides itself, so a bad
    value raises at construction time rather than silently reaching
    _split_wide_narrow or a raster operation later in the pipeline."""

    def test_negative_min_navmesh_radius_raises_at_construction(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline(data_paths={}, db_path=":memory:", min_navmesh_radius_m=-1.0)

    def test_nan_axis_dedup_fraction_raises_at_construction(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline(data_paths={}, db_path=":memory:",
                                     axis_dedup_fraction=math.nan)

    def test_floor_above_cap_raises_at_construction(self):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline(data_paths={}, db_path=":memory:",
                                     axis_dedup_cap=50.0, axis_dedup_floor_m=100.0)

    def test_sane_overrides_construct_successfully(self):
        p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:",
                                     axis_dedup_cap=100.0, axis_dedup_fraction=0.75,
                                     axis_dedup_floor_m=50.0, min_navmesh_radius_m=1200.0)
        assert p.classification_config.axis_dedup_fraction == 0.75
        assert p.classification_config.axis_dedup_floor_m == 50.0
        assert p.classification_config.min_navmesh_radius_m == 1200.0


class TestConstructorKeywordOnly:
    """CodeRabbit (PR #20): inserting inland_resample_max_segment_m into the
    middle of the constructor's optional-parameter list would silently shift
    every later positional argument for any caller still passing them
    positionally. Enforcing keyword-only args after (data_paths, db_path)
    forecloses that whole class of bug for this and any future flag."""

    def test_positional_optional_argument_is_rejected(self):
        with pytest.raises(TypeError):
            NauticalRoutingPipeline({}, ":memory:", "NL")  # country positionally
