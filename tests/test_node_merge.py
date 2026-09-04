"""Unit tests for _get_or_create_node's tolerance merge (SPEC-GRAPH-DENSITY.md §6.8).

`_get_or_create_node` dedupes nodes purely by `(round(lon, 5), round(lat, 5))` -- a
grid roughly 1.1m wide at Zeeland's latitude. Independent call sites (skeleton
medial-axis extraction, navmesh boundary generation, lock/bridge crossings,
gap-resolve, coastal-connectivity stitching) computing "the same" real-world
junction point via different geometric paths routinely land a metre or two apart,
on opposite sides of that rounding boundary, producing permanently distinct nodes
joined by a near-zero-length stub edge.

`node_merge_m` (default 0.0, disabled) generalizes `connector_merge_m`'s (§6.5)
tolerance-merge pattern to `_get_or_create_node` itself: reuse an existing node
within tolerance instead of always minting a near-duplicate. 0.0 (default)
disables this entirely and reproduces today's output byte-for-byte, matching
`--sagitta-cap`/`--axis-dedup-cap`/.../`--connector-merge-m`'s convention.
"""
import math
from collections import defaultdict

import pytest

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    NODE_MERGE_MAX_M,
)


def _pipeline(node_merge_m=0.0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_waterway_connector_merge.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(node_merge_m=node_merge_m)
    p.coords_to_node = {}
    p._node_merge_grid = defaultdict(list)
    return p


def _meters_offset(lon, lat, dx_m, dy_m):
    """Offset (lon, lat) by (dx_m, dy_m) metres, same approximation the pipeline
    itself uses elsewhere (111320 m/deg, cos(lat) for longitude)."""
    dlat = dy_m / 111320.0
    dlon = dx_m / (111320.0 * math.cos(math.radians(lat)))
    return lon + dlon, lat + dlat


# ---------------------------------------------------------------------------
# _get_or_create_node -- default-disabled vs. tolerance-merge-enabled.
# ---------------------------------------------------------------------------

class TestGetOrCreateNodeMerge:
    BASE_LON, BASE_LAT = 4.16234, 51.66192  # roughly Krammersluis-junction latitude

    def test_disabled_by_default_two_near_points_create_two_nodes(self):
        # Pins today's documented bug (SPEC-GRAPH-DENSITY.md §6.8) as a regression
        # baseline: two independently-computed points ~1.3m apart, on opposite
        # sides of the 5-decimal rounding grid, must NOT be merged when the
        # feature is off.
        p = _pipeline(node_merge_m=0.0)
        lon2, lat2 = _meters_offset(self.BASE_LON, self.BASE_LAT, 1.3, 0.0)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        b = p._get_or_create_node(lon2, lat2, "coastal", context="b")
        assert a != b
        assert p.graph.number_of_nodes() == 2
        assert p.node_merge_stats == {"created": 0, "merged": 0}

    def test_enabled_two_near_points_merge_into_one_node(self):
        p = _pipeline(node_merge_m=3.0)
        lon2, lat2 = _meters_offset(self.BASE_LON, self.BASE_LAT, 1.3, 0.0)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        b = p._get_or_create_node(lon2, lat2, "coastal", context="b")
        assert a == b
        assert p.graph.number_of_nodes() == 1
        assert p.node_merge_stats["merged"] == 1
        assert p.node_merge_stats["created"] == 1

    def test_enabled_a_point_beyond_tolerance_stays_separate(self):
        p = _pipeline(node_merge_m=3.0)
        lon_far, lat_far = _meters_offset(self.BASE_LON, self.BASE_LAT, 50.0, 0.0)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        c = p._get_or_create_node(lon_far, lat_far, "coastal", context="c")
        assert a != c
        assert p.graph.number_of_nodes() == 2
        assert p.node_merge_stats["merged"] == 0
        assert p.node_merge_stats["created"] == 2

    def test_enabled_close_pair_merges_far_point_stays_separate(self):
        p = _pipeline(node_merge_m=3.0)
        lon_near, lat_near = _meters_offset(self.BASE_LON, self.BASE_LAT, 1.3, 0.0)
        lon_far, lat_far = _meters_offset(self.BASE_LON, self.BASE_LAT, 50.0, 0.0)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        b = p._get_or_create_node(lon_near, lat_near, "coastal", context="b")
        c = p._get_or_create_node(lon_far, lat_far, "coastal", context="c")
        assert a == b
        assert a != c
        assert p.graph.number_of_nodes() == 2
        assert p.node_merge_stats["merged"] == 1
        assert p.node_merge_stats["created"] == 2

    def test_enabled_diagonal_neighbor_cell_candidate_is_found(self):
        # A candidate offset diagonally (both lon and lat) by just under the
        # tolerance can land in a diagonally-adjacent grid cell rather than the
        # query point's own cell -- exercise the full 3x3 neighbour scan, not
        # just same-cell/orthogonal-neighbour matches.
        p = _pipeline(node_merge_m=3.0)
        lon2, lat2 = _meters_offset(self.BASE_LON, self.BASE_LAT, 1.8, 1.8)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        b = p._get_or_create_node(lon2, lat2, "coastal", context="b")
        assert a == b
        assert p.graph.number_of_nodes() == 1

    def test_enabled_high_latitude_nonzero_longitude_points_still_merge(self):
        # CodeRabbit (PR #20): an earlier version derived the longitude grid-cell
        # size from each POINT's own raw latitude via cos(lat). At high latitude
        # AND high |longitude|, floor(lon / cell_lon_deg) divides a large lon by a
        # tiny cell -- so even the sub-cell latitude spread between two points that
        # are genuinely within tolerance can shift cos(lat) enough to move the
        # quotient by more than one grid index, landing outside the 3x3 neighbour
        # scan and minting a duplicate instead of merging. 70N/120E, offset
        # (dx=1m, dy=24m) -- 24.02m apart, inside a 25m tolerance -- lands 2 grid
        # cells apart (not 1) against the unfixed per-point cos(lat) cell-index
        # math; the fix (a canonical per-latitude-bucket cell size, recomputed per
        # scanned row) must still merge these.
        p = _pipeline(node_merge_m=25.0)
        lon2, lat2 = _meters_offset(120.0, 70.0, 1.0, 24.0)
        a = p._get_or_create_node(120.0, 70.0, "coastal", context="a")
        b = p._get_or_create_node(lon2, lat2, "coastal", context="b")
        assert a == b
        assert p.graph.number_of_nodes() == 1

    def test_stale_node_removed_from_graph_is_not_reused(self):
        p = _pipeline(node_merge_m=3.0)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        p.graph.remove_node(a)
        lon2, lat2 = _meters_offset(self.BASE_LON, self.BASE_LAT, 1.3, 0.0)
        b = p._get_or_create_node(lon2, lat2, "coastal", context="b")
        assert b != a
        assert b in p.graph
        assert p.graph.number_of_nodes() == 1
        # The stale bucket entry must have been pruned during the failed lookup,
        # not merely skipped -- confirm the merge grid no longer references `a`.
        all_ids = [nid for bucket in p._node_merge_grid.values() for (_, _, nid) in bucket]
        assert a not in all_ids

    def test_context_tagging_still_applies_to_a_merged_node(self):
        p = _pipeline(node_merge_m=3.0)
        p._node_origin_diag = True
        lon2, lat2 = _meters_offset(self.BASE_LON, self.BASE_LAT, 1.3, 0.0)
        a = p._get_or_create_node(self.BASE_LON, self.BASE_LAT, "coastal", context="a")
        b = p._get_or_create_node(lon2, lat2, "coastal", context="b")
        assert a == b
        assert p._node_contexts[a] == {"a", "b"}


# ---------------------------------------------------------------------------
# _validate_node_merge_m
# ---------------------------------------------------------------------------

class TestValidateNodeMergeM:
    def test_zero_is_always_valid(self):
        NauticalRoutingPipeline._validate_node_merge_m(0.0)  # must not raise

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, NODE_MERGE_MAX_M,
                                      NODE_MERGE_MAX_M + 1.0])
    def test_rejects_non_finite_negative_or_too_large(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_node_merge_m(bad)

    def test_accepts_a_sane_positive_value(self):
        NauticalRoutingPipeline._validate_node_merge_m(5.0)  # must not raise
