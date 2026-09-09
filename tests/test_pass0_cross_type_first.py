"""Unit tests for `pass0_cross_type_first` (follow-on to SPEC-GRAPH-DENSITY.md).

Pass 0b (cross-type k=6 NN, navmesh perimeter vs everything else) already does what
a fragment embedded near open water wants -- connect outward to the surrounding
navmesh, immune to Pass 0's same-type crowding -- but it runs SECOND, after Pass 0
has already unioned many same-type fragment-to-fragment pairs together. Setting
`pass0_cross_type_first` runs Pass 0b BEFORE Pass 0, so its outward cross-type
unions land first and many of Pass 0's same-type candidates are then rejected for
free by the existing find(u)==find(v) check instead of firing.

`pass0_cross_type_first` defaults to False (disabled): nothing here changes real
build output until a build explicitly opts in via `--pass0-cross-type-first`,
matching every other flag in this file's convention. Only the call order changes --
no change to Pass 0/0b/0c/0d/Pass 1/Pass 2's own internal logic or caps, and
connectivity must be identical either way.
"""
import math

from shapely.geometry import box

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    NODE_KIND_NAVMESH_VERTEX,
    DEFAULT_SOURCE_TIER,
)

BASE_LON, BASE_LAT = 4.0, 51.5


def _pipeline(pass0_cross_type_first=False):
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(pass0_cross_type_first=pass0_cross_type_first)
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


def _covering_component_polygon():
    return box(BASE_LON - 0.02, BASE_LAT - 0.02, BASE_LON + 0.02, BASE_LAT + 0.02)


def _two_other_clusters_and_a_shared_navmesh_node(pipeline, n_per_cluster=8,
                                                    cluster_sep_deg=0.002,
                                                    navmesh_offset_deg=0.0006):
    """Two tight clusters of `n_per_cluster` "other"-type nodes each (n_per_cluster
    > 6 so a node deep in one cluster has its own Pass 0 k=6 nearest-neighbor list
    entirely filled by its own cluster-mates -- same "crowding" Pass 0b's own
    docstring describes), plus one shared "navmesh"-kind node positioned between
    the two clusters, within snap_radius of both. Both Pass 0 (type-blind) and
    Pass 0b (cross-type, separate per-type KD-tree) are independently capable of
    finding each cluster-node-to-navmesh-node connector here -- whichever pass
    runs FIRST claims it, which is exactly what this suite measures.

    Returns (ids, cluster_a_ids, cluster_b_ids, navmesh_id).
    """
    cluster_a, cluster_b = [], []
    for i in range(n_per_cluster):
        theta = 2 * math.pi * i / n_per_cluster
        lon = BASE_LON + 0.0001 * math.cos(theta)
        lat = BASE_LAT + 0.0001 * math.sin(theta)
        cluster_a.append(pipeline._get_or_create_node(lon, lat, "coastal", context="test"))
    for i in range(n_per_cluster):
        theta = 2 * math.pi * i / n_per_cluster
        lon = BASE_LON + cluster_sep_deg + 0.0001 * math.cos(theta)
        lat = BASE_LAT + 0.0001 * math.sin(theta)
        cluster_b.append(pipeline._get_or_create_node(lon, lat, "coastal", context="test"))
    navmesh_id = pipeline._get_or_create_node(
        BASE_LON + cluster_sep_deg / 2, BASE_LAT + navmesh_offset_deg, "coastal", context="test")
    pipeline._stamp_node(navmesh_id, NODE_KIND_NAVMESH_VERTEX, DEFAULT_SOURCE_TIER, None)
    ids = cluster_a + cluster_b + [navmesh_id]
    return ids, cluster_a, cluster_b, navmesh_id


def _connected_components(pipeline, ids):
    parent = {n: n for n in ids}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for n in ids:
        for nbr in pipeline.graph.neighbors(n):
            if nbr in parent:
                union(n, nbr)
    return len({find(n) for n in ids})


class TestDisabledByDefaultKeepsPass0First:
    def test_pass0_claims_the_cross_type_connectors_when_flag_is_off(self):
        p = _pipeline(pass0_cross_type_first=False)
        ids, cluster_a, cluster_b, navmesh_id = _two_other_clusters_and_a_shared_navmesh_node(p)

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=500.0)

        # Pass 0 runs first (unchanged default order) -- it's the one that
        # successfully claims the cluster-to-navmesh connectors; Pass 0b's later
        # attempts on the same pairs are rejected as already-connected.
        assert p._stitch_diag["pass0"]["success"] > 0
        assert p._stitch_diag["pass0b"].get("success", 0) == 0
        assert _connected_components(p, ids) == 1


class TestEnabledRunsPass0bFirst:
    def test_pass0b_claims_the_cross_type_connectors_when_flag_is_on(self):
        p = _pipeline(pass0_cross_type_first=True)
        ids, cluster_a, cluster_b, navmesh_id = _two_other_clusters_and_a_shared_navmesh_node(p)

        p._stitch_component_pieces(ids, _covering_component_polygon(), snap_radius_m=500.0)

        # Pass 0b now runs first -- its outward cross-type unions land before
        # Pass 0 evaluates the same pairs, so Pass 0 finds them already-connected
        # and contributes nothing new for this candidate set.
        assert p._stitch_diag["pass0b"]["success"] > 0
        assert p._stitch_diag["pass0"].get("success", 0) == 0
        assert _connected_components(p, ids) == 1

    def test_connectivity_is_identical_either_way(self):
        # The whole point: only the call order changes, never the outcome.
        p_off = _pipeline(pass0_cross_type_first=False)
        ids_off, *_ = _two_other_clusters_and_a_shared_navmesh_node(p_off)
        p_off._stitch_component_pieces(ids_off, _covering_component_polygon(), snap_radius_m=500.0)

        p_on = _pipeline(pass0_cross_type_first=True)
        ids_on, *_ = _two_other_clusters_and_a_shared_navmesh_node(p_on)
        p_on._stitch_component_pieces(ids_on, _covering_component_polygon(), snap_radius_m=500.0)

        assert _connected_components(p_off, ids_off) == _connected_components(p_on, ids_on) == 1


class TestValidation:
    def test_false_is_accepted(self):
        p = _pipeline(pass0_cross_type_first=False)
        assert p.classification_config.pass0_cross_type_first is False

    def test_true_is_accepted(self):
        p = _pipeline(pass0_cross_type_first=True)
        assert p.classification_config.pass0_cross_type_first is True
