import os
import math
import inspect
import json
import sqlite3
import hashlib
import logging
import argparse
import multiprocessing as mp
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple, Optional, Literal

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
import shapely
from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, mapping, box
from shapely.ops import triangulate, unary_union, polygonize
from shapely.validation import make_valid
from pyproj import Geod

# Phase 0 navmesh-hybrid skeleton extraction (Step C). Hard deps per requirements.txt.
from skimage.morphology import medial_axis
# medial_axis breaks ties by processing pixels in an order drawn from a PRNG, and
# defaults to a FRESH UNSEEDED generator on every call -- so two runs of the same
# build produced different centerlines. Measured on one clip: node/edge counts moved
# under 1%, but only 62.3% of coordinate-derived node ids were shared between runs,
# i.e. 37.7% of nodes sat somewhere else. See docs/SPEC-GRAPH-DENSITY.md section 5.
# The keyword has been spelled rng / random_state / seed across the versions
# requirements.txt allows (scikit-image>=0.22), so bind whichever this one has.
MEDIAL_AXIS_SEED = 0
_MEDIAL_AXIS_RNG_KW = next(
    (kw for kw in ("rng", "random_state", "seed")
     if kw in inspect.signature(medial_axis).parameters), None)
from rasterio.features import rasterize as _rio_rasterize
from rasterio.transform import from_origin as _rio_from_origin
# Phase 1 navmesh-region triangulation (Step B2). Hard dep per requirements.txt.
import triangle as _triangle

# Round 25 cross-database seam stitching (STITCHING_DESIGN.md Section 3):
# pipeline-side-only shared global-node registry.
from seam_registry import SeamRegistry

def _is_valid(val):
    if val is None:
        return False
    if isinstance(val, np.ndarray):
        return val.size > 0
    try:
        return not pd.isna(val)
    except (ValueError, TypeError):
        return True

def _s57_col(attrs, *candidates):
    if isinstance(attrs, dict):
        keys = attrs.keys()
    else:
        keys = attrs.index if hasattr(attrs, 'index') else attrs
    lower_map = {str(k).lower(): k for k in keys}
    for c in candidates:
        match = lower_map.get(c.lower())
        if match is not None:
            return attrs[match]
    return None

def _parse_catbrg(catbrg):
    if isinstance(catbrg, (list, tuple, np.ndarray)):
        return [str(v) for v in catbrg]
    if isinstance(catbrg, str):
        import re
        vals = re.findall(r"(\d+)", catbrg)
        if vals:
            return vals
        return [catbrg]
    return [str(catbrg)]

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Module-level workers for edge attribute multiprocessing ---
_EDGE_ATTR_GEOD = None
_EDGE_ATTR_GDFS = {}

def _edge_attr_init(geod, gdfs):
    global _EDGE_ATTR_GEOD, _EDGE_ATTR_GDFS
    _EDGE_ATTR_GEOD = geod
    _EDGE_ATTR_GDFS = gdfs

def _candidates_by_bounds_static(gdf, geom, margin=0.0, margin_lon=None):
    """Bounding-box prefilter, expanded by `margin` degrees on the lat axis and
    `margin_lon` degrees on the lon axis (defaults to `margin` when omitted, so
    every pre-existing single-margin call site is unaffected).

    A caller converting a METRE margin into degrees must not reuse the same
    converted value for both axes: 111320 m/deg is (approximately) constant for
    latitude everywhere, but a degree of longitude is only 111320*cos(latitude)
    metres -- at 51.5N (Zeeland) that is already a ~0.62 factor, worse further
    north. See `_lonlat_margin_deg`, which callers needing a metre margin should
    use to get both axes right instead of computing `metres / 111320.0` once and
    passing it as `margin` alone.
    """
    bounds = geom.bounds
    m_lon = margin_lon if margin_lon is not None else margin
    if margin or m_lon:
        bounds = (bounds[0] - m_lon, bounds[1] - margin, bounds[2] + m_lon, bounds[3] + margin)
    candidates = list(gdf.sindex.intersection(bounds))
    if candidates:
        return gdf.iloc[candidates]
    return gpd.GeoDataFrame()


def _lonlat_margin_deg(polygon_wgs84, metres):
    """Convert a metre distance into (margin_lon_deg, margin_lat_deg) for a WGS84
    bbox prefilter margin, safe across polygon_wgs84's own latitude extent.

    Latitude: 111320 m/deg is the (near-)constant conversion, so a flat margin
    there is always at least as generous as needed, never too tight.

    Longitude: a degree of longitude is 111320*cos(latitude) metres, which
    SHRINKS at higher latitude -- reusing the latitude constant for longitude
    (as this file did before CodeRabbit's PR #14 review caught it) silently
    under-covers the east-west search radius, worse the further from the
    equator: ~0.62x at Zeeland's 51.5N, and the planned US East Coast rebuild
    reaches Maine at ~45N (~0.71x) off a Florida start at ~25N (~0.90x) -- a
    wider and generally worse range than Zeeland alone. A candidate line or
    lock polygon offset mainly east-west from a piece's bbox could be silently
    excluded before reprojection ever ran, purely because of which direction it
    happened to be offset in.

    Uses whichever of the polygon's own min/max latitude has the larger
    abs(lat) (closer to a pole, smaller cos(lat)) as the reference, so the
    resulting margin_lon is conservative -- at least as generous as needed --
    for every point in the piece, not just its centroid.
    """
    minx, miny, maxx, maxy = polygon_wgs84.bounds
    ref_lat = max(abs(miny), abs(maxy))
    margin_lat_deg = metres / 111320.0
    margin_lon_deg = metres / (111320.0 * max(math.cos(math.radians(ref_lat)), 1e-6))
    return margin_lon_deg, margin_lat_deg


def _depare_candidate_sort_key(drval1, cscl, drval2):
    """Total order over a DEPARE candidate's (DRVAL1, src_cscl, DRVAL2);
    the winning candidate is the maximum. See _depare_candidate_beats.

    Ranked highest-priority first:

    1. Deepest DRVAL1. Unchanged from before any of this: overlapping bands
       nest, so the deepest containing claim is the most detailed one.
    2. A KNOWN src_cscl outranks an unknown one. Not a claim that an
       unlabeled cell is finer or coarser -- it is a claim that a candidate
       carrying evidence about its own chart scale is worth more than one
       carrying none, applied uniformly whatever that evidence says. It
       cuts the same way for a fine and a coarse label, and it is the
       conservative direction: preferring the candidate that CAN reveal a
       coarse source is what lets TRUSTED_SURVEY_CSCL_MAX fire at all
       (a build where _read_cell_cscl failed on some cells would otherwise
       silently fall back to pre-PR-#11 behaviour on exactly those ties).
    3. Among two known scales, the finer (smaller) one.
    4. A known DRVAL2 outranks an unknown one, for the same reason as (2):
       COARSE_DEPTH_BAND_DRVAL2_M needs an upper bound to judge.
    5. Among two known bands, the narrower (smaller DRVAL2) one -- a
       0-1.8m claim is far more specific than a 0-18.2m one, the same
       reasoning COARSE_DEPTH_BAND_DRVAL2_M already encodes.

    Two candidates comparing equal under this key have the same DRVAL1, the
    same src_cscl and the same DRVAL2 -- which is the WHOLE of what the
    selection loops carry out (best / best_cscl / best_upper). So a
    remaining tie is observationally empty: either candidate yields a
    byte-identical result downstream, and no further tie-break key (source
    cell id, feature id, geometry) is needed to make the outcome
    deterministic. That is why none is threaded through: the order is total
    where it is observable, and the unobservable residue cannot leak.
    """
    return (
        drval1,
        cscl is not None,
        -cscl if cscl is not None else 0.0,
        drval2 is not None,
        -drval2 if drval2 is not None else 0.0,
    )


def _depare_candidate_beats(val, candidate_cscl, candidate_upper,
                            best, best_cscl, best_upper) -> bool:
    """Shared tie-break policy for DEPARE candidate selection, used by both
    _edge_attr_worker and NauticalRoutingPipeline._compute_node_depths.

    The primary rule (max DRVAL1 wins) is unchanged. But two overlapping
    cells (different chart scales, both containing the same sample point)
    routinely tie on the EXACT same DRVAL1, most commonly at 0.0 -- a
    standard band floor every scale reuses. A tie used to keep whichever
    candidate iterrows()/the spatial index happened to visit first:
    incidental row/insertion order from the multi-cell merge, not anything
    tied to chart quality. Since the winner also supplies DRVAL2 and
    src_cscl, that order decided whether a zero reading came out trusted or
    UNKNOWN_DEPTH -- exactly the ambiguity TRUSTED_SURVEY_CSCL_MAX exists to
    resolve, undermined by unstable tie-breaking.

    _depare_candidate_sort_key documents the full ordering and why it is
    total over everything the caller observes.
    """
    if best is None:
        return True
    return (_depare_candidate_sort_key(val, candidate_cscl, candidate_upper)
            > _depare_candidate_sort_key(best, best_cscl, best_upper))

def _edge_attr_worker(edge_chunk):
    geod = _EDGE_ATTR_GEOD
    gdfs = _EDGE_ATTR_GDFS
    CRS_WGS84 = "EPSG:4326"
    CRS_METRIC = "EPSG:3857"
    land_metric = gdfs.get('land_metric', gpd.GeoDataFrame())
    depare_gdf = gdfs.get('depth_areas', gpd.GeoDataFrame())
    dredged_gdf = gdfs.get('dredged_areas', gpd.GeoDataFrame())
    bridges_gdf = gdfs.get('bridges', gpd.GeoDataFrame())
    fairways_gdf = gdfs.get('fairways', gpd.GeoDataFrame())
    locks_gdf = gdfs.get('locks', gpd.GeoDataFrame())
    obstacles_gdf = gdfs.get('obstacles', gpd.GeoDataFrame())
    obstacles_soft_gdf = gdfs.get('obstacles_soft', gpd.GeoDataFrame())

    results = {}
    for (u, v, u_lon, u_lat, v_lon, v_lat, edge_type, is_opening_bridge_edge,
         requires_lock, existing_min_width) in edge_chunk:
        attrs = {}
        _, _, distance = geod.inv(u_lon, u_lat, v_lon, v_lat)
        attrs['distance'] = round(distance, 2)
        edge_geom = LineString([(u_lon, u_lat), (v_lon, v_lat)])

        # Depth — Fast Precision (5-point sample)
        attrs['min_depth'] = 99.0
        attrs['drval1'] = None
        if not depare_gdf.empty and 'DRVAL1' in depare_gdf.columns:
            candidates = _candidates_by_bounds_static(depare_gdf, edge_geom)
            if not candidates.empty:
                vals = candidates['DRVAL1'].dropna()
                if not vals.empty:
                    if float(vals.min()) >= 5.0:
                        min_val = float(vals.min())
                        attrs['min_depth'] = min_val
                        attrs['drval1'] = min_val
                    else:
                        # NOAA ships a multi-scale cell pyramid (harbor/approach/coastal/
                        # overview covering the SAME water); the preprocessor merges every
                        # cell's DEPARE into one layer, so a sample point routinely falls
                        # inside several containing candidate polygons at once -- one per
                        # scale. Taking the FIRST containing polygon in iteration order was
                        # effectively random cell selection (whichever scale's polygon
                        # happened to sort first), which is how 65.7% of PR edges ended up
                        # min_depth=0: a coarse overview cell's shallow/unsurveyed 0-X band
                        # frequently "won" over a finer harbor cell's real 18.2m band for the
                        # same point. Fix: among ALL containing candidates, take the MAXIMUM
                        # DRVAL1 -- the finest cell subdivides a coarse band into deeper
                        # sub-bands, so the deepest containing claim is the most detailed one.
                        # Genuine drying stays negative only where every containing band is
                        # drying (there's no deeper containing claim to override it with).
                        sampled = []
                        for i in range(5):
                            f = i / 4.0
                            pt = Point(u_lon + f*(v_lon-u_lon), u_lat + f*(v_lat-u_lat))
                            best = None
                            best_upper = None
                            best_cscl = None
                            for _, row in candidates.iterrows():
                                geom = row.geometry
                                if geom is not None and geom.contains(pt):
                                    val = row['DRVAL1']
                                    if pd.notna(val):
                                        val = float(val)
                                        cscl = row['src_cscl'] if 'src_cscl' in row else None
                                        row_cscl = int(cscl) if pd.notna(cscl) else None
                                        upper = row['DRVAL2'] if 'DRVAL2' in row else None
                                        row_upper = float(upper) if pd.notna(upper) else None
                                        if _depare_candidate_beats(val, row_cscl, row_upper,
                                                                   best, best_cscl, best_upper):
                                            best = val
                                            best_upper = row_upper
                                            best_cscl = row_cscl
                            # See DRYING_BAND_IMPLAUSIBLE_DRVAL1_M: an implausibly
                            # extreme DRVAL1 (e.g. -50) with a plausible DRVAL2 is a
                            # coarse-band placeholder on the drying side, not a real
                            # reading -- the upper bound is the trustworthy one.
                            if (best is not None and best_upper is not None
                                    and best < DRYING_BAND_IMPLAUSIBLE_DRVAL1_M
                                    and best_upper >= DRYING_BAND_IMPLAUSIBLE_DRVAL1_M):
                                best = best_upper
                            sampled.append((best, best_upper, best_cscl) if best is not None
                                           else (99.0, None, None))
                        min_val = min(d1 for d1, _, _ in sampled)
                        # A DRVAL1 of 0 is only meaningful when the band is tight.
                        # Offshore, the sole containing band is routinely
                        # "DRVAL1=0, DRVAL2=18.2" -- 0 is the band FLOOR, not a
                        # surveyed minimum -- and taking it literally marks the
                        # water impassable: 43.6% of New York's edges, 57.3% of
                        # Maryland's, and 63.7% of NY's zero-depth edges sit in
                        # such coarse bands (only 12.4% in a genuine 0-2m band).
                        # routeiq then treats every route through them as
                        # constraint-violating and detours arbitrarily far around
                        # them (242km for an 18km crossing). Emit UNKNOWN_DEPTH,
                        # which consumers exclude from constraint checks, but only
                        # when EVERY zero-reading sample came from a coarse band;
                        # one genuine 0-2m or drying reading keeps the real value.
                        # A zero-reading is coarse via EITHER a wide DRVAL2 band
                        # OR a coarse source-cell CSCL (see TRUSTED_SURVEY_CSCL_MAX)
                        # -- some coarse cells reuse the same tight-looking standard
                        # depth-band cutoffs a real harbor survey would, so DRVAL2
                        # width alone misses them (confirmed: Lake Worth Inlet, FL).
                        zero_samples = [(d1, d2, cs) for d1, d2, cs in sampled if d1 <= 0.0]
                        coarse_only = bool(zero_samples) and all(
                            d1 == 0.0 and (
                                (d2 is not None and d2 >= COARSE_DEPTH_BAND_DRVAL2_M)
                                or (cs is not None and cs > TRUSTED_SURVEY_CSCL_MAX)
                            )
                            for d1, d2, cs in zero_samples)
                        # Preserve the sign: a genuine drying/intertidal band (e.g.
                        # DRVAL1=-2.0, exposed 2m above chart datum at low water) is
                        # real survey data, not "unknown" -- flooring it to 0.0 here
                        # discarded that information upstream of every consumer (see
                        # UNKNOWN_DEPTH above).
                        attrs['min_depth'] = (UNKNOWN_DEPTH if coarse_only
                                              else float(min_val))
                        attrs['drval1'] = min_val if min_val < 99.0 else None

        # DRGARE (dredged/maintained-depth channel) override -- see
        # docs/SPEC-FAIRWAY-HARMONIZATION.md section 3. Inside a DRGARE
        # footprint the maintained DRVAL1 is the authoritative depth, which
        # can be either deeper or shallower than the surrounding DEPARE band
        # -- so this always needs checking, even where DEPARE alone looked
        # comfortably deep above. Sampled the same 5-point way as DEPARE
        # (same max-DRVAL1-among-containing-candidates rule) so it can tell
        # per point whether the edge is actually inside a dredged footprint;
        # DEPARE's already-computed value stands wherever it isn't. Where
        # DRGARE has no candidates at all near this edge (the common case),
        # this is a no-op and DEPARE's verdict -- including UNKNOWN_DEPTH --
        # passes through untouched.
        if not dredged_gdf.empty and 'DRVAL1' in dredged_gdf.columns:
            dr_candidates = _candidates_by_bounds_static(dredged_gdf, edge_geom)
            if not dr_candidates.empty:
                dr_samples = []
                for i in range(5):
                    f = i / 4.0
                    pt = Point(u_lon + f*(v_lon-u_lon), u_lat + f*(v_lat-u_lat))
                    best = None
                    for _, row in dr_candidates.iterrows():
                        geom = row.geometry
                        if geom is not None and geom.contains(pt):
                            val = row['DRVAL1']
                            if pd.notna(val) and (best is None or float(val) > best):
                                best = float(val)
                    dr_samples.append(best)
                # Only override once a sample point is confirmed to fall
                # INSIDE a DRGARE polygon -- a candidate that merely shares a
                # bounding box with the edge (the common near-miss) must not
                # silently replace a genuine UNKNOWN_DEPTH verdict with a
                # generic 99.0 "no data here" fallback.
                if any(v is not None for v in dr_samples):
                    base_depth = attrs['min_depth'] if attrs['min_depth'] != UNKNOWN_DEPTH else 99.0
                    combined = [dv if dv is not None else base_depth for dv in dr_samples]
                    attrs['min_depth'] = min(combined)
                    attrs['drval1'] = attrs['min_depth'] if attrs['min_depth'] < 99.0 else None

        # Bridges - Determine Air Draft limit
        attrs['max_air_draft'] = 999.0
        if not bridges_gdf.empty:
            bridge_candidates = _candidates_by_bounds_static(bridges_gdf, edge_geom)
            if not bridge_candidates.empty:
                intersecting = bridge_candidates[bridge_candidates.intersects(edge_geom)]
                if not intersecting.empty:
                    min_clearance = 999.0
                    for _, row in intersecting.iterrows():
                        is_movable = False
                        catbrg = _s57_col(row, 'catbrg', 'CATAQA', 'CatBrg')
                        if _is_valid(catbrg):
                            vals = _parse_catbrg(catbrg)
                            if any(v in ('3', '4', '5', '6', '7') for v in vals):
                                is_movable = True
                        if not is_movable:
                            vercop = _s57_col(row, 'vercop', 'VERCOP', 'VerCop')
                            if _is_valid(vercop):
                                is_movable = True

                        if is_movable:
                            clearance = 999.0
                        else:
                            verclr = _s57_col(row, 'verclr', 'VERCLR', 'VerClr')
                            # Per S-57 convention, VERCLR=0 means "vertical clearance
                            # not surveyed," not a genuine zero-clearance bridge (a real
                            # navigable fixed bridge with 0m clearance is implausible).
                            # Treat it the same as "not present" — same 999.0 fallback
                            # used by the movable-bridge branch above and the
                            # no-bridge-found case.
                            if _is_valid(verclr) and float(verclr) != 0.0:
                                clearance = float(verclr)
                            else:
                                clearance = 999.0
                        if clearance < min_clearance:
                            min_clearance = clearance
                    attrs['max_air_draft'] = min_clearance

        # Locks
        # Seed from whatever width the edge already carries -- build_skeleton_network
        # measures a real medial-axis channel width and stores it (alongside the
        # matching width_profile) when it creates the edge. This used to start at the
        # 999.0 "unconstrained" default instead, and since calculate_edge_attributes
        # writes every key this worker returns back onto the edge, that overwrote the
        # measured width on EVERY edge: a real build had min_width=999.0 on all
        # 137,718 edges, while width_profile still held the true values on 81,110 of
        # them. See docs/SPEC-GRAPH-DENSITY.md section 4.1.2.
        attrs['min_width'] = (float(existing_min_width) if existing_min_width is not None
                              else 999.0)
        if not locks_gdf.empty:
            lock_candidates = _candidates_by_bounds_static(locks_gdf, edge_geom)
            if not lock_candidates.empty:
                intersecting = lock_candidates[lock_candidates.intersects(edge_geom)]
                if not intersecting.empty:
                    # HORWID, not just HORCLR. This branch only ever looked for HORCLR,
                    # which no lock in the RWS data carries -- of 304 lock polygons,
                    # HORCLR is absent as a column entirely while HORWID holds a real
                    # value on 247 -- so the lock width constraint never once applied.
                    # S-57 uses HORCLR for a clearance between structures (the bridge
                    # sense) and HORWID for a structure's own horizontal width, which is
                    # what a lock chamber publishes; both express the navigable width
                    # here, so prefer HORCLR where it exists and fall back to HORWID.
                    # Same case-variant tolerance _s57_col gives the bridge attributes.
                    # Look HORCLR up by name rather than taking the first matching
                    # column: next() over .columns returns whichever the layer happens
                    # to order first, so a merged layer listing HORWID before HORCLR
                    # would let a wider HORWID mask a tighter HORCLR (CodeRabbit, #12).
                    cols_by_lower = {str(c).lower(): c for c in intersecting.columns}
                    width_col = cols_by_lower.get('horclr') or cols_by_lower.get('horwid')
                    gate_w = (pd.to_numeric(intersecting[width_col], errors='coerce').min()
                              if width_col is not None else None)
                    # A 0 means "not surveyed" rather than a zero-width lock, exactly as
                    # VERCLR=0 does for bridges above.
                    if _is_valid(gate_w) and float(gate_w) > 0.0:
                        # A lock gate NARROWS the channel -- it is one more constraint
                        # along the edge, not a redefinition of it. Taking the min keeps
                        # whichever is tighter, so a 12m gate still wins inside a 300m
                        # basin while a 6m creek keeps its own width where the gate is
                        # wider than the creek.
                        attrs['min_width'] = min(attrs['min_width'], float(gate_w))

        # Fairway + one-way (TRAFIC)
        attrs['cost_factor'] = 1.2  # open water default
        attrs['traffic_mode'] = 0
        if not fairways_gdf.empty:
            fw_candidates = _candidates_by_bounds_static(fairways_gdf, edge_geom)
            if not fw_candidates.empty:
                intersecting = fw_candidates[fw_candidates.intersects(edge_geom)]
                if not intersecting.empty:
                    attrs['cost_factor'] = 0.8  # fairway: preferred
                    if 'TRAFIC' in intersecting.columns:
                        trafic_vals = intersecting['TRAFIC'].dropna().unique()
                        if len(trafic_vals) == 1:
                            tv = int(trafic_vals[0])
                            if abs(tv) in (1, 3):
                                attrs['traffic_mode'] = 1 if tv in (1, 3) else 2

        # Distance to land
        attrs['distance_to_land'] = 9999.0
        if not land_metric.empty:
            edge_geom_metric = gpd.GeoSeries([edge_geom], crs=CRS_WGS84).to_crs(CRS_METRIC).iloc[0]
            possible_matches = land_metric.sindex.nearest(edge_geom_metric)
            if len(possible_matches[1]) > 0:
                closest_idx = possible_matches[1][0]
                closest_geom = land_metric.iloc[closest_idx].geometry
                attrs['distance_to_land'] = round(edge_geom_metric.distance(closest_geom), 2)

        # Obstacle crossing check — skipped for opening-bridge AND lock-crossing
        # edges (same exemption already applied to crosses_land at edge-creation
        # time in _add_opening_bridge_edges / _add_lock_crossing_edges): these are
        # precise, deliberately-computed crossings of a bridge's actual navigable
        # opening, or a lock chamber's actual gate positions, via fairway/waterway
        # centerline intersection, not generic geometry. A broad obstacle polygon
        # (e.g. a mariculture/marine-farm area) incidentally overlapping the
        # bridge's/lock's footprint must not hard-block the one crossing point
        # that exists specifically so vessels CAN pass through — that's the
        # confirmed cause of a real Zeelandbrug regression (opening never
        # explored at all, not merely deprioritized) that the lock case could
        # equally suffer from.
        attrs['crosses_obstacle'] = 0
        if not is_opening_bridge_edge and not requires_lock and not obstacles_gdf.empty:
            obs_candidates = _candidates_by_bounds_static(obstacles_gdf, edge_geom)
            if not obs_candidates.empty:
                intersecting = obs_candidates[obs_candidates.intersects(edge_geom)]
                if not intersecting.empty:
                    attrs['crosses_obstacle'] = 1

        # Soft obstruction-point depth constraint (Round 18 Fix 2) — the
        # VALSOU/WATLEV-downgraded subset of obstructions_points that
        # _build_obstacle_layer routed to "obstacles_soft" instead of the hard
        # "obstacles" layer above. These aren't dropped from routing consideration
        # entirely: fold the charted sounding (or the conservative WATLEV==3/4
        # default of 0.0m — see _obstruction_depth_disposition) into min_depth,
        # same exemption for opening-bridge/lock-crossing edges as the hard check.
        if not is_opening_bridge_edge and not requires_lock and not obstacles_soft_gdf.empty:
            soft_candidates = _candidates_by_bounds_static(obstacles_soft_gdf, edge_geom)
            if not soft_candidates.empty:
                soft_intersecting = soft_candidates[soft_candidates.intersects(edge_geom)]
                if not soft_intersecting.empty:
                    soft_min = float(soft_intersecting['_depth_constraint'].min())
                    # A charted sounding always wins over UNKNOWN_DEPTH: a plain
                    # min() would keep UNKNOWN_DEPTH and silently discard the
                    # constraint, since it sorts below any real (or drying) depth.
                    # Compare by identity, not `< 0` -- a genuine drying height is
                    # also negative and must still take part in the min() below.
                    attrs['min_depth'] = (soft_min if attrs['min_depth'] == UNKNOWN_DEPTH
                                          else min(attrs['min_depth'], soft_min))

        results[(u, v)] = attrs
    return results

COORD_SPACE = 36000000
OBSTACLE_BUFFER_METERS = 5
EDGE_TYPE_COASTAL = 0
EDGE_TYPE_INLAND = 1
POI_TYPE_HARBOUR = 0
POI_TYPE_LOCK = 1
POI_TYPE_BRIDGE = 2
POI_TYPE_FAIRWAY = 3
POI_TYPE_WATERWAY = 4
TRAFFIC_TWO_WAY = 0
TRAFFIC_ONE_WAY_FWD = 1
TRAFFIC_ONE_WAY_REV = 2

# --- Phase 0 navmesh-hybrid: edge/node kind + provenance constants (spec v1 §2.4-2.6) ---
EDGE_KIND_CENTERLINE = 0
EDGE_KIND_NAVMESH_BOUNDARY = 1
EDGE_KIND_LANE = 2
EDGE_KIND_MACRO = 3
NODE_KIND_POINT = 0
NODE_KIND_NAVMESH_VERTEX = 1
NODE_KIND_SUPERNODE = 2
DEFAULT_SOURCE_TIER = 1  # 1 = official hydrographic authority (ENC/IENC)
# Depth sentinel for "unknown, do not constrain" (ROUTEIQ_NEXT_PHASES.md,
# "Negative charted depths are read as unknown" / "Depth-unknown is declared
# by the file, not inferred"). Deliberately far outside any plausible charted
# drying height (a bank drying tens of metres does not exist) so it stays
# distinguishable from a genuine negative DRVAL1 -- a drying/intertidal bank
# exposed at low water, which the pipeline now emits as-is (e.g. -2.0)
# instead of flooring to 0.0.
#
# Consumers must read this build's own declared value from
# metadata.depth_unknown_sentinel (this constant, when present), NOT gate on
# schema_version: schema_version numbers the database FORMAT and had already
# reached 3 on legacy-convention builds for unrelated reasons before this
# column existed, so no schema_version threshold can ever separate the two
# depth conventions -- routeiq shipped exactly that gate once (PR #31) and it
# silently misread 92% of a live European build's edges as "dries 1.0 m".
# Builds that omit depth_unknown_sentinel (older than this change) still
# floor drying heights to 0.0 and use -1 for unknown; that is what an absent
# column means to a tolerant reader, and it must keep meaning that.
UNKNOWN_DEPTH = -999.0
# metadata.schema_version bumped to this the first time a build emits the
# -999 sentinel (was 1, always floored negatives to 0.0 and used -1 for
# unknown) -- a general format-version bump for the new
# depth_unknown_sentinel column, not itself load-bearing for depth semantics
# (see UNKNOWN_DEPTH above; do not let a consumer start gating on it again).
DEPTH_SENTINEL_SCHEMA_VERSION = 2
# A DEPARE band of DRVAL1=0 with an upper bound at least this deep carries no
# usable minimum -- it is a coarse "0 to X" band, not a survey saying 0m.
COARSE_DEPTH_BAND_DRVAL2_M = 10.0
# The mirror-image artifact on the drying side: RWS Zeeland's DEPARE data
# carries 1,901 polygons banded DRVAL1=-50.0/DRVAL2=-4.0, all at exactly this
# pair -- verified against the full band table (a clean 0/0.5/1/.../8/10/20/
# /30/40/50/100 S-57 scheme) and geography (the three largest, 6.36/2.39/
# 2.17 km^2, centre on Verdronken Land van Saeftinghe, a real tidal marsh).
# -50 is almost certainly the SAME catch-all placeholder the positive scale
# uses for its widest band (40-50, 50-100), reused with a minus sign where it
# does not belong -- every genuine drying reading elsewhere in this dataset
# stays inside -7.0m, and no charted intertidal height on Earth approaches
# -20m, let alone -50m. DRVAL2 (-4.0, itself an ordinary band boundary) is
# the trustworthy bound. Same fix shape as COARSE_DEPTH_BAND_DRVAL2_M above,
# mirrored onto the drying side instead of the positive-floor side.
DRYING_BAND_IMPLAUSIBLE_DRVAL1_M = -20.0
# A DEPARE polygon's source-cell compilation scale (DSPM_CSCL from S-57's
# DSID layer, stamped per-feature as `src_cscl` by enc_preprocessor.py) is a
# second, independent signal that a DRVAL1<=0 reading is a coarse
# overview/general/coastal placeholder, not a real survey minimum --
# confirmed on a real build: Lake Worth Inlet, FL (a maintained federal
# channel charted 6-24m throughout) had edges reading DRVAL1=0.0/DRVAL2=5.4,
# well under COARSE_DEPTH_BAND_DRVAL2_M, sourced from US3FL1DG, a band-3
# "coastal" cell (DSPM_CSCL=180000) whose DEPARE polygon spans nearly the
# entire region -- not a harbor/approach sounding. NOAA reuses the same
# standard depth-band cutoffs (0-1.8m, 0-5.4m, ...) at every chart scale, so
# a tight-looking DRVAL2 alone cannot tell a real harbor reading from a
# coarse cell's placeholder using the same boundary. Confirmed real per-cell
# CSCL values (raw DSID reads, FL Atlantic coast, n=20 cells/band):
# harbor/band-5 12000-40000, approach/band-4 45000-90000, coastal/band-3
# 180000-350000+ (one nominal band-3 cell measured 466940, coarser than a
# typical "general" chart -- the filename's usage-band digit alone is not a
# reliable scale proxy). 100000 sits in the clean gap between the coarsest
# approach-band cell and the finest coastal-band cell observed. Builds not
# yet reprocessed with src_cscl tagging have no such column; every candidate
# then reports cscl=None and this check is a no-op, falling back to
# COARSE_DEPTH_BAND_DRVAL2_M exactly as before -- this is a strictly
# additive, backward-compatible widening of "untrustworthy," combined with
# (never replacing) the DRVAL2-width check.
TRUSTED_SURVEY_CSCL_MAX = 100_000
# How far outside this region's own water an ADOPTED seam node may sit and still
# be connected (_connect_adopted_node). A seam node is authored from the
# NEIGHBOUR's water geometry, digitised from different ENC cells, so it routinely
# lands a few metres off this build's water edge -- 406 of Maryland's 478 adopted
# nodes did, median 11.3m out, which the previous flat 2m buffer rejected
# wholesale (392 of its 471 unconnected nodes). Sized an order of magnitude below
# stitch_radius_m and at the scale of ENC digitisation differences; _crosses_land
# remains the actual safety gate.
ADOPT_POLY_TOLERANCE_M = 50.0
NAVMESH_TARGET_EDGE_M = 650.0  # target interior triangle edge length (spec's 500-800m band)
NAVMESH_PSLG_BUDGET = 20_000    # max len(vertices)+len(segments) fed into triangle's PSLG mode
NAVMESH_MAX_TRIANGLES = 200_000 # sanity cap on triangulate() output; retry coarser above this
DEPTH_SPLIT_SAFETY_MARGIN_M = 20.0  # _split_deep_shallow: extra erosion past the depth-ceiling
                                     # contour so navmesh boundary edges clear it with margin,
                                     # not sit exactly on the transition
DEPTH_SPLIT_CLOSING_RADIUS_M = 50.0  # _split_deep_shallow: morphological closing radius to bridge
                                     # DEPARE survey-contour misalignment gaps between adjacent
                                     # deep bands before cutting (Round 7's original 5m left most
                                     # of this fragmentation in place -- see Round 8 writeup)
DEPTH_SPLIT_DRYING_REPUNCH_FRACTION = 0.5  # _split_deep_shallow: an interior hole the 50m closing
                                     # above fills in gets subtracted back out (re-punched) if more
                                     # than this fraction of its own area is covered by charted
                                     # drying/intertidal DEPARE (DRVAL1<0, see _drying_gdf) -- one of
                                     # two independent real/noise signals (see DEPTH_SPLIT_HOLE_MIN_WIDTH_M
                                     # for the other -- on real Yerseke data this one alone recovered
                                     # 0/30 real holes, none of them were actually charted drying, so it
                                     # is necessary but not sufficient by itself). >50% (not a lower bar
                                     # like "any overlap") because a hole can graze the edge of an
                                     # unrelated nearby drying polygon without actually corresponding to
                                     # it; a majority-covered hole is confidently the real thing.
DEPTH_SPLIT_HOLE_MIN_WIDTH_M = 3.0  # _split_deep_shallow: an interior hole the 50m closing fills in
                                     # also gets re-punched if it survives an erosion by this radius
                                     # (i.e. contains a disk this wide) -- the second real/noise signal,
                                     # added in Round 21 after direct measurement showed the drying
                                     # signal alone (above) misses real Yerseke separators, which are
                                     # mostly DEPARE-uncovered micro-voids or charted-but-shallow
                                     # (not negative-DRVAL1) patches, not drying. Round 8's own
                                     # documented GEOS/misalignment noise fragments were sub-1m^2 to
                                     # low-single-digit-m^2 (one measured 0.007m^2) -- nowhere near wide
                                     # enough to survive even a couple meters of erosion -- while every
                                     # real Yerseke hole but the smallest handful is hundreds to tens of
                                     # thousands of m^2 and clears this easily (measured: 25/30 survive).
                                     # Scoped to interior rings only (see call site), so this can never
                                     # affect whether separate deep pieces merge -- only whether an
                                     # already-enclosed void stays excluded -- meaning it cannot
                                     # reintroduce Round 8's region-fragmentation regression regardless
                                     # of this threshold's value.
NAVMESH_BOUNDARY_SIMPLIFY_M = 5.0   # build_navmesh_region: a separate, coarser simplify pass on
                                     # the navmesh region's own boundary specifically, applied right
                                     # before it becomes PSLG input/output. §5.2.3 item 1 (Round 6/7
                                     # writeup): _split_wide_narrow's simplify_tol_m=1.0 barely thins
                                     # survey-grade coastline vertex density, and that same
                                     # under-simplified polygon flows straight through to the exported
                                     # navmesh_regions.vertices/boundary_geometry. Deliberately much
                                     # coarser than the 1.0m tolerance used for the wide/narrow and
                                     # deep/shallow classification decisions and medial-axis centering
                                     # (where 1.0m precision still matters) -- this pass only affects
                                     # the polygon that becomes the navmesh triangulation's own
                                     # boundary. Far finer than NAVMESH_TARGET_EDGE_M (650m) so real
                                     # shape detail near bridges/inlets survives. Tuned empirically, not
                                     # guessed: a 3-way full-scale sweep (no pass / 5.0m / 15.0m) found
                                     # 5.0m already captures most of the vertex-count win (median
                                     # vertices/region 1247 -> 125, vs 15.0m's 80 -- little further gain)
                                     # while costing much less real depth-safety margin than 15.0m
                                     # (navmesh_boundary edges <3.0m: 0.9% no-pass -> 3.9% at 5.0m ->
                                     # 6.0% at 15.0m -- see NEXT_PHASES.md Round 9 writeup for the full
                                     # table). Safe to apply after seam_coord_set is computed: shapely's
                                     # simplify() only ever *removes* vertices (Douglas-Peucker), never
                                     # moves a retained
                                     # one, so exact-coordinate seam matching in build_navmesh_region
                                     # still works correctly on whatever seam vertices survive.
NAVMESH_TILE_MAX_EXTENT_M = int(os.environ.get(  # Round 25 Chunk 2 PROBE: env-override so the
    "SK_ROUTING_NAVMESH_TILE_MAX_EXTENT_M", 10_000))  # global-tile-grid probe can force a small
                                     # tile size (e.g. 2-3km) without changing the shipped 10km
                                     # default for real builds. See GLOBAL_TILE_GRID_PROBE above.
                                     # build_network: a navmesh-eligible deep piece whose bbox
                                     # extent (or post-NAVMESH_BOUNDARY_SIMPLIFY_M boundary vertex
                                     # count) exceeds this gets tiled into a grid of pieces at most
                                     # this wide/tall (in the piece's own local UTM CRS) before
                                     # build_navmesh_region ever sees it. Round 22 measured Puerto
                                     # Rico's open-ocean coastal_water component as ONE navmesh_regions
                                     # row spanning the whole dataset (lat 17.17-19.01, lon
                                     # -68.10..-64.37, ~400km wide) with 8,313 vertices / 8,118
                                     # boundary_node_ids -- 1.6x Zeeland's worst pre-Round-9 outlier
                                     # (4,999 nodes, 198s) -- and traced routeiq's loadGraph() cost
                                     # for PR alone to 76.6s (Zeeland: 1.7s), because the funnel/anchor
                                     # precompute runs corridor searches across the whole triangle mesh.
                                     # Round 23a swept 30km/15km/10km on a real PR rebuild + instrumented
                                     # routeiq loadGraph(): 30km -> 117 regions/18.1s, 15km -> 375
                                     # regions/10.3s, 10km -> 763 regions/8.8s. The dominant cost isn't
                                     # region COUNT but routeiq's addAnchorShortcutEdges: any region with
                                     # >DEFAULT_MAX_ANCHORS(40) boundary_node_ids pays a FIXED O(40^2)
                                     # pairwise-funnel cost regardless of how much over 40 it is, and each
                                     # such search's cost scales with that region's own physical extent --
                                     # so smaller tiles both raised the count of >40-boundary regions AND
                                     # shrank each one's search cost, netting a win each step down (this is
                                     # NOT a general rule -- don't assume smaller is always faster without
                                     # re-measuring). 10km was the smallest step tried and the only one
                                     # clearing the <10s target with margin; going smaller was not tried
                                     # (diminishing returns were already visible: 30->15km halved load
                                     # time, 15->10km only trimmed another ~15%, while probe_pr_timeout's
                                     # measured route distance crept up with tile count -- 30km +1.3%,
                                     # 15km +7.3%, 10km +10.6% over the 242.9km baseline, all fast/no
                                     # NO-ROUTE -- extra tile seams cost a little routing optimality, so
                                     # this constant trades that off against load time and shouldn't be
                                     # lowered further without re-checking both numbers). 10km keeps a
                                     # tile's own boundary_node_ids in the low hundreds even for open
                                     # ocean, comfortably under the ~1,500 NAVMESH_TILE_MAX_VERTICES gate
                                     # below, at the cost of ~750 small, cheap tiles for a piece PR's size
                                     # -- each triangulated and stitched exactly like any other navmesh
                                     # region.
NAVMESH_TILE_MAX_VERTICES = 1_500   # build_network: second (OR'd) tiling gate -- a piece can be
                                     # narrower than NAVMESH_TILE_MAX_EXTENT_M in bbox terms yet still
                                     # carry a very dense boundary (many islands/reefs), so also tile
                                     # whenever the post-NAVMESH_BOUNDARY_SIMPLIFY_M boundary vertex
                                     # count alone would exceed this, independent of raw extent.

# --- Round 25 Chunk 2 PROBE: absolute/global tiling grid, NOT shipped behaviour ---
# _tile_navmesh_piece normally cuts a piece on lines placed RELATIVE to that piece's
# own bbox (`minx + i*width/nx`) -- deterministic within one build (R23's single
# unary_union+polygonize noding pass), but two INDEPENDENTLY built adjacent pieces
# cut on different lines, since their bboxes differ. This flag (env-var gated so it
# can never accidentally leak into a real build) switches the grid to ABSOLUTE
# world-coordinate lines -- multiples of the tile size from a fixed metre-CRS origin
# -- so that two overlapping pieces sharing a real stretch of water and (per
# `_local_utm_crs`'s per-component `estimate_utm_crs`) the same local UTM zone will
# reference the literal same cut-line constant. Tests whether that's enough for
# cross-build seam-node ID coincidence on the shared line (see NEXT_PHASES.md,
# "Chunk 2 — global cut line probe"). Defaults off; the shipped per-piece-relative
# grid is unaffected unless SK_ROUTING_GLOBAL_TILE_GRID_PROBE=1 is set.
GLOBAL_TILE_GRID_PROBE = os.environ.get("SK_ROUTING_GLOBAL_TILE_GRID_PROBE") == "1"

# --- Round 14: inland-waterway x navmesh-region boundary crossings ---
# Fix for the confirmed bug: _ensure_coastal_connectivity's candidate node set is
# `node_type != "inland"`, so inland waterway nodes are structurally invisible to
# every stitching pass (including Round 13's Pass 0c) -- a waterway line crossing
# open navmesh water was a disconnected parallel network with zero edges to the
# mesh. See _inject_waterway_crossings / build_navmesh_region.
WATERWAY_CROSSING_DEDUPE_M = 50.0      # crossings from the same line closer than this are merged
WATERWAY_CROSSING_SNAP_M = 1.0         # snap a crossing onto an existing ring vertex / earlier insert
                                       # within this range instead of adding a new PSLG vertex --
                                       # near-coincident vertices (two lines crossing the same spot,
                                       # or a crossing millimeters from a ring vertex) produce
                                       # knife-edge/duplicate segments that segfault
                                       # _triangle.triangulate at the C level (confirmed via
                                       # faulthandler on a real full-scale build; same crash class
                                       # Round 7 hit with sliver polygons)
WATERWAY_CROSSING_CAP_PER_LINE = 8     # sanity cap per (navmesh piece, line) -- multi-crossing braids exist
WATERWAY_CONNECTOR_MAX_M = 250.0       # normal connector search radius to the nearest inland vertex
WATERWAY_CONNECTOR_FALLBACK_MAX_M = 500.0  # widened radius for sparsely-digitized lines (logged)

# SPEC-GRAPH-DENSITY.md §6.4: floor for --inland-densify-max-segment-m. shapely.segmentize
# generates roughly (segment_length / cap) vertices per source segment with no cap of its
# own -- a mistyped sub-metre value (or NaN/inf slipping past a bare `<= 0.0` check) on an
# 8km+ real inland_waterways line risks unbounded memory rather than a clear error
# (CodeRabbit PR #17 review). 1.0m is comfortably below the recommended 100-150m operating
# range while still bounding worst case to a sane vertex count.
INLAND_DENSIFY_MIN_SEGMENT_M = 1.0

# SPEC-GRAPH-DENSITY.md §6.3.1 Phase B: how far (in RASTER PIXELS, not metres --
# distinct in kind from the metric-CRS WATERWAY_CONNECTOR_* radii above, which
# instead govern _connect_waterway_crossing's own nearest-inland-vertex search)
# a skeleton degree-1 dead end may look for an adjacent axis-dedup-suppressed
# pixel before treating it as carve-induced (reconnect-eligible) rather than a
# genuine dead end left to existing stitching passes. 4, not a tighter 1-2:
# medial_axis's own end-cap construction sets a fragment's centerline terminus
# back from the true carved edge by a few pixels (confirmed directly -- a
# synthetic severed channel's dead end sat 3px from the nearest suppressed
# pixel, not 1), so a narrower radius would systematically miss real
# carve-induced dead ends, not just tighten a false-positive margin.
AXIS_DEDUP_DEADEND_SEARCH_RADIUS_PX = 4

# --- Local gap-resolve pass (_stitch_component_pieces' last resort) ---
# Fix for a confirmed bug: build_skeleton_network rasterizes an entire narrow
# water-body piece at ONE shared pixel size derived from that whole piece's
# bounding box, enlarged further whenever MAX_RASTER_PIXELS would be exceeded
# (real build log: pixel size enlarged to 35-43m for the FL Atlantic-south
# narrow piece). A real jetty-narrowed inlet throat (~150-250m) can pinch to
# 0-1 pixels wide at that resolution, leaving medial-axis endpoints retreated
# from the true gap on each bank -- confirmed on a real build (Lake Worth
# Inlet, FL): two nodes 39.7m apart ended up in separate connected
# components, well inside every pass above's snap_radius_m. Rather than
# widening/relaxing any of those passes (which risks manufacturing a chord
# through solid land), this re-rasterizes just a small window around a
# still-disconnected close pair at a fixed fine pixel size and only merges
# the result if that fresh medial axis itself proves a real water path.
LOCAL_GAP_RESOLVE_MAX_M = 300.0            # max separation between two still-disconnected nodes
                                            # to attempt a local remesh for -- kept below
                                            # snap_radius_m (500m) since this is a materially
                                            # more expensive fallback than the passes above it,
                                            # reserved for near-miss gaps, not distant water bodies
LOCAL_GAP_RESOLVE_WINDOW_MARGIN_M = 150.0  # buffer around the candidate pair's bbox for the
                                            # local re-rasterization window
LOCAL_GAP_RESOLVE_PIXEL_M = 1.0            # fixed fine pixel size for the local remesh -- the
                                            # window is always tiny (<= ~600m across), so
                                            # MAX_RASTER_PIXELS is never a constraint here
LOCAL_GAP_RESOLVE_SNAP_M = 10.0            # snap tolerance for merging local-remesh chain
                                            # endpoints onto existing nearby graph nodes
LOCAL_GAP_RESOLVE_MIN_SPUR_M = 10.0        # spur-prune threshold for the tiny local skeleton
                                            # (scaled down from ClassificationConfig's 60m default,
                                            # which is sized for whole-piece rasters, not a
                                            # <=~600m window)
MAX_LOCAL_GAP_RESOLVE_PER_COMPONENT = 2000  # cap so a pathologically fragmented component can't
                                            # blow up build time in this fallback pass -- raised
                                            # from an initial 25: a full fl_atl_s rebuild showed
                                            # "resolved" landing right at the cap both at 25
                                            # (26-28) and at 200 (202-203), meaning the cap -- not
                                            # a lack of real candidates -- was the binding
                                            # constraint. Florida's coastline apparently has
                                            # hundreds of legitimate narrow-throat gaps in one
                                            # original water-body component -- not pathological,
                                            # just a real count this cap needs comfortable
                                            # headroom above (confirmed: raising to 2000 resolved
                                            # 214, well under the new cap, with no measurable
                                            # build-time increase, since each resolution is a tiny
                                            # bounded local raster, not the whole-piece rasters
                                            # this cap was originally modeled after). Leaves an
                                            # order of magnitude of headroom above observed real
                                            # demand while still bounding the pathological case
                                            # (e.g. the 8440-union-find-group one documented
                                            # above) this cap exists for in the first place.

# --- Sagitta-resampler stitch-density pin: STATUS = NOT SUFFICIENT, KEPT INERT ---
# Investigation into the connectivity regression that blocks enabling
# SPEC-GRAPH-DENSITY.md §4.1 by default. _resample_long_skeleton_edges
# collapses a long, straight, well-known-width reach down to a couple of nodes
# kilometres apart; enabling it (data/zeeland_clip, cap 75 / seg 2000 vs the
# cap 0 / seg 100 baseline, both builds bit-for-bit reproducible) drops
# largest-component connectivity from 86.49% to 81.40% while cutting node
# count 39.1% (33,057 -> 20,136) -- the intended win, but not shippable with
# that regression.
#
# ROUND 1 (STITCH_DIAG instrumentation, per-pass attempt/outcome counters
# across every pass in _stitch_component_pieces and _resolve_local_skeleton_gaps):
# union-find fragment count going INTO the stitch passes barely moved
# (1095 -> 1054 initial groups across 344 original water-body components),
# but successful stitch connections dropped ~22% (7958 -> 6191 non-gap-resolve
# connectors; 177 -> 138 last-resort local remeshes). The last-resort pass
# never once reported a node with literally no cross-group candidate within
# its 300m search radius in EITHER build (no_candidate_in_radius=0 both
# times) -- candidates ARE being found, at similar distances (Pass 2's
# nearest-sampled-pair-at-give-up averaged ~65m in both builds), but
# poly_reject/land_reject counts on those candidates rose broadly (e.g. Pass
# 0d success -24%, poly_reject +88%).
#
# ROUND 2 (coordinator's follow-up: is baseline connectivity actually built on
# ACCIDENTAL coordinate collisions between independently-built pieces, made
# common by dense pre-resample point spacing and rare by coarse resampling?
# self._node_contexts tags every node _get_or_create_node touches with which
# skeleton/navmesh piece or subsystem created/reused it; NODE_ORIGIN_DIAG
# cross-tabs that against final degree). Measured on the SAME clip, checkpointed
# both pre-stitch (right after build_network, before any stitch pass runs) and
# post-stitch (after _ensure_coastal_connectivity):
#
#   |                      | baseline | cap75/seg2000 |
#   |----------------------|---------:|---------------:|
#   | pre_stitch junctions |    5,954 |          5,955 |
#   | post_stitch junctions|   11,097 |          8,540 |
#   | cross-context nodes  |       25 |             26 |
#
# The hypothesis is REFUTED: cross-context (genuinely different pieces/
# subsystems landing on the same rounded, ~1m coordinate) nodes are ~25 in
# EVERY build regardless of resampling -- noise, not a mechanism. But the
# checkpoint split revealed something more useful: raw/native topology
# (nodepix-derived, independent of resampling by construction) is IDENTICAL
# between builds (5954 vs 5955) confirming resampling truly never touches it,
# while ALL of the observed junction-count gap is downstream of the stitch
# passes themselves creating far fewer connector edges under sparser candidate
# density (5,143 stitching-created junctions in baseline vs only 2,585 in the
# relaxed build, a 49.7% drop) -- i.e. this is the SAME mechanism Round 1
# already found, just independently confirmed via degree distribution instead
# of raw edge counts, not a second distinct mechanism.
#
# ROUND 3 (fix attempts, all measured on cap 75 / seg 2000): four
# configurations were tried and NONE met both the >=86.49% largest-component
# gate and the >=30% node-reduction gate simultaneously:
#
#   | config                                          | nodes  | reduction | largest |
#   |--------------------------------------------------|-------:|----------:|--------:|
#   | no fix                                            | 20,136 |     39.1% |  81.40% |
#   | pin @500m, both ends unconditional                | 24,802 |     25.0% |  83.38% |
#   | pin @500m, dead-ends only (excl. junction ends)   | 20,381 |     38.3% |  81.31% |
#   | pin @250m + widened snap/inland radii + 4000 samples | 25,983 | 21.4%  |  83.55% |
#   | NO pin, widened radii + 4000 samples only (free)  | 20,080 |     39.2% |  81.05% |
#
# The dead-ends-only variant confirms Round 6's older finding still holds: a
# real stitch candidate is routinely a junction/mid-chain node, "never a dead
# end, never seam-tagged" (see _stitch_component_pieces' Pass 0 docstring) --
# restricting the pin to dead ends alone erases almost the entire recovery.
# The free (node-cost-zero) widened-radius variant shows the sharpest,
# most important result: it raised Pass 0/0b/0c/0d's raw success counts (some
# above baseline) WITHOUT improving -- and slightly worsening -- largest-
# component connectivity, because those four passes are NOT union-find gated
# (Pass 0c/0d deliberately keep adding local edges after a region is already
# globally connected, see Pass 0c's own docstring) -- their extra successes
# are mostly redundant edges inside already-merged groups, not new distinct
# merges. Only Pass 2 (global greedy nearest-pair merge) and the gap-resolve
# remesh are union-find gated, i.e. only THEIR success counts can move
# largest-component connectivity, and both stayed well below baseline in
# EVERY configuration tried (Pass 2: 27 baseline -> 7-14; gap-resolve remesh:
# 177 baseline -> 134-149), independent of pin radius, search radius, or
# Pass 2's own sample budget (raising MAX_TOTAL_SAMPLES 1500 -> 4000 did not
# move its success count at all).
#
# STATUS (superseded -- kept for the measurement history above):
# The "no change recovers connectivity" conclusion below was drawn against a
# gate that turned out to be invalid. Both of its premises have since been
# overturned INSIDE this same branch:
#   1. The regression itself was largely a metric artifact. The gate measured
#      largest-component / total NODES, which penalises removing nodes by
#      construction. Measured by edge length the resampled build BEATS
#      baseline (94.81% vs 92.35%), and a direct reachability check over 8,001
#      real POI pairs lost ZERO routable pairs. See docs/SPEC-GRAPH-DENSITY.md
#      section 6.1.
#   2. Pass 2's candidate selection WAS genuinely weak, and was fixed -- by
#      exactly the KD-tree nearest-cross-group-pair search this note proposed
#      as future work. Pass 2 successes 14 -> 53 (baseline 27). See section 6.2
#      and the fix comment at the Pass 2 block below.
# The density-pinning experiments this note describes were removed as dead
# code; the per-pass measurements are retained because they are what located
# Pass 2 and the gap-resolve remesh as the only union-find-gated passes, which
# is what made the real fix findable.
#
# ORIGINAL CONCLUSION (as written during the investigation, now superseded):
# CONCLUSION: the regression is real, diagnosed, and NOT an accidental-
# collision artifact -- it is that Pass 2 and the gap-resolve remesh
# specifically lose their ability to find genuinely NEW distinct-group merges
# under sparser candidate density, and no combination of resampler-side
# density pinning, wider stitch-pass search radii, or a bigger Pass 2 sample
# budget tried here recovers that without either giving back most of the
# node-count win (failing the reduction gate) or leaving connectivity short
# (failing the largest-component gate). Fixing this properly needs a real
# algorithmic change to Pass 2 / gap-resolve's candidate selection (e.g.
# querying the true nearest cross-group pair per group via a KD-tree instead
# of a capped random-ish sample, or not giving up a round the instant the
# first sampled candidate fails its safety check) -- not further parameter
# tuning. Per the task's gate 4, none of this may change a `--sagitta-cap 0`
# build, so the machinery below is gated on max_chord_sagitta_m > 0.0
# wherever it touches shared code, and is currently called INERT (pin_start=
# pin_end=False from build_skeleton_network, the widened search radii
# reverted to their original constants) -- kept in place, and its measured
# numbers kept in this comment, as a documented starting point for that
# follow-up rather than being deleted.


@dataclass
class ClassificationConfig:
    """Tuning knobs for water-body classification (Step B) and skeleton raster (Step C)."""
    depth_ceiling_m: float = 6.0        # navigable-depth threshold separating deep open water from shoal
    min_navmesh_radius_m: float = 800.0  # a body must contain a disk of this radius to be navmesh-eligible
                                          # (raised from 300.0, §5.2.3 item 2: 300m was triggering
                                          # navmesh/ring-boundary treatment for water that reads as
                                          # channel-like, not genuinely open, per direct user review of
                                          # real screenshots -- Round 9 Issue F)
    pixel_min_m: float = 2.0            # medial-axis raster pixel floor
    pixel_max_m: float = 10.0           # medial-axis raster pixel ceiling
    pixel_dim_divisor: float = 200.0    # adaptive px = clamp(min_dim / divisor, floor, ceiling)
    min_spur_length_m: float = 60.0     # prune skeleton dead-ends shorter than this
    max_segment_m: float = 100.0        # resample collapsed centerlines to segments this long (narrow channels need <200m so straight-chord edges stay inside bends)
    # SPEC-GRAPH-DENSITY.md §4.1: sagitta-bounded adaptive resampling. 0.0 (default)
    # disables it entirely -- _resample_long_skeleton_edges falls back to the
    # unconditional max_segment_m cut above, byte-for-byte today's behaviour.
    # Nothing ships until a build is measured against the §6 gates.
    max_chord_sagitta_m: float = 0.0    # cap on chord-to-centerline deviation (m); 0.0 = disabled
    sagitta_width_fraction: float = 0.5  # tolerance = min(cap, this * local channel width), per §4.1.1
    # SPEC-GRAPH-DENSITY.md §4.3: axis dedup. Carve water pixels near an authoritative
    # inland_waterways axis (wtwaxs/RECTRC/NAVLNE) out of the raster BEFORE skeletonizing,
    # so build_skeleton_network never generates a redundant medial-axis "twin" next to a
    # centerline _build_inland_network already ingested. 0.0 (default) disables it entirely
    # -- build_skeleton_network's mask is then byte-identical to today's, matching
    # --sagitta-cap's convention. Nothing ships until a build is measured against §4.3.3's
    # gates. Constants below are §4.3.1's measured values, not guesses: tol = clip(
    # axis_dedup_fraction * local_channel_width, axis_dedup_floor_m, axis_dedup_cap_m).
    axis_dedup_cap_m: float = 0.0       # cap (m) on suppression distance from the axis; 0.0 = disabled
    axis_dedup_fraction: float = 0.5    # tolerance = clip(this * local channel width, floor, cap)
    axis_dedup_floor_m: float = 5.0     # never suppress below this -- sits under pixel_min_m's 2.0m raster floor
    # SPEC-GRAPH-DENSITY.md §6.4: sparse inland_waterways digitization causes connector
    # fan-out. A chart-scale ENC line (e.g. SCAMIN=50000) can have vertices 1000m+ apart;
    # every crossing/carve-reconnect point within a ~1km radius of a sparse segment then
    # snaps to that same one distant vertex (both _connect_waterway_crossing's nearest-
    # existing-vertex search and _build_inland_network's raw vertex-to-vertex ingestion
    # assume denser digitization than that). 0.0 (default) disables this entirely --
    # parse_shapefiles leaves inland_waterways geometry byte-identical to today's,
    # matching --sagitta-cap/--axis-dedup-cap's convention. Densifying makes the
    # nearest-existing-vertex assumption true again instead of changing either consumer.
    inland_densify_max_segment_m: float = 0.0  # max vertex spacing (m) after densifying; 0.0 = disabled

    def pixel_size_for(self, min_dimension_m: float) -> float:
        return float(np.clip(min_dimension_m / self.pixel_dim_divisor,
                             self.pixel_min_m, self.pixel_max_m))


# SPEC-GRAPH-DENSITY.md §4.1: the sentinel `_edge_attr_worker` falls back to when
# a channel's width was never measured (see line ~369, "min_width = 999.0" default).
# Per §4.1's width-exclusion rule, a width equal to this (or missing/non-positive)
# must never be fed into the sagitta tolerance -- that would produce the LARGEST
# tolerance exactly where the channel is least known.
WIDTH_UNKNOWN_M = 999.0


def _width_is_known(w) -> bool:
    """True if w is a real measured channel width, not a missing/sentinel value."""
    return (w is not None and np.isfinite(w) and w > 0.0
            and abs(float(w) - WIDTH_UNKNOWN_M) > 1e-9)


def _chain_has_known_width(widths) -> bool:
    """True only if every vertex along the (pre-split) chain carries a real width.

    Sagitta relaxation is an all-or-nothing decision per §4.1: a chain with any
    unmeasured vertex keeps today's uniform max_segment_m behaviour in full,
    rather than mixing relaxed and unrelaxed sub-segments.
    """
    return bool(widths) and all(_width_is_known(w) for w in widths)


def _sagitta_tolerance_m(seg_widths, cap_m: float, width_fraction: float) -> float:
    """tolerance = min(cap, width_fraction * local channel width), per §4.1.1.

    `seg_widths` is assumed already known-good (caller gates on
    `_chain_has_known_width` before this is ever used for relaxation).
    """
    return min(cap_m, width_fraction * min(seg_widths))


def _max_sagitta_m(seg_pts) -> float:
    """Max perpendicular deviation (metres) of seg_pts' interior vertices from the
    straight chord seg_pts[0] -> seg_pts[-1].

    seg_pts are (lon, lat) degrees; per §4.1 constraint 4 these are projected to
    local equirectangular metres about seg_pts[0] (longitude scaled by cos(lat))
    before measuring -- degrees are not a valid distance space for this comparison,
    and the local-tangent-plane error over a <=max_segment_m span is negligible.
    Vectorised over all interior points at once (no per-point Python loop) so a
    long, nearly-straight chain does not turn quadratic in practice (§4.1
    complexity note) -- callers additionally keep candidate segments short by
    retaining max_segment_m as a hard backstop.
    """
    if len(seg_pts) < 3:
        return 0.0
    lon0, lat0 = seg_pts[0][0], seg_pts[0][1]
    coslat = math.cos(math.radians(lat0))
    arr = np.asarray(seg_pts, dtype=float)
    x = (arr[:, 0] - lon0) * 111320.0 * coslat
    y = (arr[:, 1] - lat0) * 111320.0
    ax, ay = x[0], y[0]
    bx, by = x[-1], y[-1]
    abx, aby = bx - ax, by - ay
    chord_len = math.hypot(abx, aby)
    px = x[1:-1] - ax
    py = y[1:-1] - ay
    if chord_len < 1e-9:
        # Degenerate chord (start == end): sagitta is just distance from the start.
        return float(np.max(np.hypot(px, py)))
    cross = np.abs(abx * py - aby * px)
    return float(np.max(cross) / chord_len)


def _iter_boundary_lines(geom):
    """Yield the LineString components of a polygon boundary or a (multi)line geometry."""
    if geom is None:
        return
    if isinstance(geom, (Polygon, MultiPolygon)):
        boundary = geom.boundary
        yield from (boundary.geoms if isinstance(boundary, MultiLineString) else [boundary])
    elif isinstance(geom, (LineString, MultiLineString)):
        yield from (geom.geoms if isinstance(geom, MultiLineString) else [geom])


def _default_data_sources() -> List[dict]:
    """One provenance row per input layer for the data_sources table (spec §2.2).

    All current pilot layers are ENC/IENC-derived, so default_tier=1. In particular
    inland_waterways was verified during Session 0 recon to be RWS IENC (S-57
    object-catalogue fields + Dutch official SORIND), NOT OSM-derived, so it is
    tier 1 with source_type 'ienc' rather than the earlier tier-3 guess.
    Layer names use the real CLI dict keys (depth_areas / obstacles), not the
    spec-draft names (depare / obstructions).
    """
    enc_layers = [
        "land", "coastal_water", "depth_areas", "bridges", "locks", "fairways",
        # DRGARE (dredged/maintained-depth channel areas) -- see
        # docs/SPEC-FAIRWAY-HARMONIZATION.md. Same tier/source_type as
        # "fairways"; kept as its own provenance row (not folded into
        # "fairways") since it's a distinct S-57 object class read from its
        # own dredged_areas_polygons.geojson.
        "dredged_areas",
        "restricted_areas", "obstacles", "hulks", "mariculture", "caution_areas",
        "pois",
    ]
    rows = [
        {
            "name": name,
            "source_type": "enc",
            "url": None,
            "license": None,
            "attribution_text": f"{name}: ENC-derived source layer",
            "accessed_date": None,
            "default_tier": DEFAULT_SOURCE_TIER,
        }
        for name in enc_layers
    ]
    rows.append({
        "name": "inland_waterways",
        "source_type": "ienc",
        "url": None,
        "license": None,
        "attribution_text": "inland_waterways: RWS IENC (inland ENC) source layer",
        "accessed_date": None,
        "default_tier": DEFAULT_SOURCE_TIER,
    })
    return rows


def _s57_get_val(attrs, *candidates):
    val = _s57_col(attrs, *candidates)
    if val is None:
        return None
    if isinstance(val, (list, tuple, np.ndarray)):
        return val[0] if len(val) > 0 else None
    return val

def _is_entry_prohibited(row):
    restrn = _s57_col(row, 'restrn')
    if _is_valid(restrn):
        vals = restrn if isinstance(restrn, (list, tuple, np.ndarray)) else [restrn]
        for v in vals:
            try:
                if int(v) == 1:
                    return True
            except (ValueError, TypeError):
                pass
    for col in ('OBJNAM', 'NOBJNM', 'INFORM'):
        val = _s57_col(row, col)
        if val is not None and isinstance(val, str):
            lower = val.lower()
            if any(phrase in lower for phrase in ('entry prohibited', 'toegang verboden', 'passage prohibited')):
                return True
    return False

def _obstruction_depth_disposition(row):
    """S-57-grounded classification of a single feature from the raw
    `obstructions_points` layer (OBJL 86) into hard-block vs depth-constraint.

    NOAA-scale charts carry thousands of these (2,835 for the Puerto Rico
    build vs 3 genuine obstructions in the Zeeland pilot) -- hard-blocking
    every one of them (the pre-Round-18 behavior) shreds coastal
    connectivity (1,161 obstacle-blocked edges, San Juan<->Fajardo
    unroutable) for features that a shallow-draft vessel can often safely
    cross.

    - `VALSOU` (charted least depth over the obstruction) present, any
      `WATLEV`: not a hard block -- fold the sounding into the edge's
      `min_depth` (`min(min_depth, VALSOU)`) so a deep-draft ship is
      stopped but a shallow-draft one is not. This is the normal case for
      a swept/surveyed wreck or obstruction and takes priority over WATLEV
      below (a feature with a real sounding is by definition not "always
      dry" -- soundings aren't taken on land).
    - No `VALSOU` and `WATLEV == 3` (always under water): still a real
      hazard -- this is the dominant case in the PR data (1,928 of 2,835
      features, unswept rocks/wrecks with no sounding) -- but hard-
      blocking every single one is exactly the connectivity-shredding
      defect this function exists to fix. Route around it via a
      conservative default depth constraint instead (0.0m) rather than a
      hard block: deep-draft routing avoids the edge (min_depth=0 fails
      any real draft check) while the edge stays usable for the warning
      system instead of vanishing from the graph outright.
    - No `VALSOU` and `WATLEV` in {1 (partly submerged), 2 (always dry),
      5 (awash)}, or `WATLEV` missing/unrecognized (e.g. 7="floating" —
      not covered by the brief's dry/awash/underwater/covers-uncovers
      set, so treated with the same conservative fallback as unknown):
      keep the existing hard block -- there is no depth number to route
      around, and the feature is a genuine surface/near-surface hazard
      that isn't well modeled as "min_depth=0 but otherwise passable".
    - No `VALSOU` and `WATLEV == 4` (covers/uncovers): same conservative
      depth-constraint treatment as WATLEV==3, not a hard block. This is
      not explicitly called out in the WATLEV list above, but it matches
      this codebase's own established precedent for exactly this
      situation -- charted DEPARE drying/intertidal bands (DRVAL1 < 0.0)
      are handled elsewhere as a depth constraint (clamped to 0.0 in
      min_depth), never a hard block (see the Round 9 "DEPARE-drying gap"
      fix referenced in NEXT_PHASES.md) -- and "covers and uncovers" is
      the same tidal-varying category as a drying flat, not a permanent
      surface obstruction like "always dry"/"awash". 887 of PR's 2,835
      obstruction points (31%) carry WATLEV==4, so this reading matters
      materially; verified against the route probes, not just asserted.

    Returns (is_hard_block: bool, depth_constraint_m: float | None).
    """
    valsou = _s57_get_val(row, 'VALSOU')
    if _is_valid(valsou):
        try:
            return False, float(valsou)
        except (TypeError, ValueError):
            pass  # not a real number -- fall through to WATLEV handling

    watlev = _s57_get_val(row, 'WATLEV')
    try:
        watlev_i = int(float(watlev)) if _is_valid(watlev) else None
    except (TypeError, ValueError):
        watlev_i = None

    if watlev_i in (3, 4):
        return False, 0.0
    # 1 (partly submerged), 2 (always dry), 5 (awash), or missing/unrecognized
    # (including 7="floating"): conservative default, unchanged hard block.
    return True, None

class NauticalRoutingPipeline:
    def __init__(self, data_paths: Dict[str, str], db_path: str,
                 country: str = "", region_name: str = "", description: str = "",
                 tags: Optional[str] = None, contributor: str = "", url: str = "",
                 license: str = "", copyright: str = "",
                 architecture: str = "navmesh-hybrid-phase1",
                 dataset_version: str = "", depth_ceiling: float = 6.0,
                 stitch_registry_path: str = "",
                 coverage_bbox: Optional[Tuple[float, float, float, float]] = None,
                 stitch_band_m: float = 300.0, stitch_radius_m: float = 500.0,
                 navmesh_edge_m: float = NAVMESH_TARGET_EDGE_M,
                 sagitta_cap: float = 0.0,
                 max_segment_m: Optional[float] = None,
                 axis_dedup_cap: float = 0.0,
                 inland_densify_max_segment_m: float = 0.0):
        self.data_paths = data_paths
        self.db_path = db_path
        self.country = country
        self.region_name = region_name or country
        self.description = description
        self.tags = tags or "[]"
        self.contributor = contributor
        self.url = url
        self.license = license
        self.copyright = copyright
        self.architecture = architecture
        self.dataset_version = dataset_version
        self.classification_config = ClassificationConfig(depth_ceiling_m=depth_ceiling,
                                                           max_chord_sagitta_m=sagitta_cap,
                                                           axis_dedup_cap_m=axis_dedup_cap,
                                                           inland_densify_max_segment_m=inland_densify_max_segment_m)
        if max_segment_m is not None:
            self.classification_config.max_segment_m = float(max_segment_m)
        self.geod = Geod(ellps="WGS84")
        self.CRS_WGS84 = "EPSG:4326"
        self.CRS_METRIC = "EPSG:3857"
        self.gdfs = {}
        self.graph = nx.DiGraph()
        self.navmesh_region_rows = []
        # Node ids build_navmesh_region flagged as touching a seam with a
        # bordering piece (see build_navmesh_region's boundary_node_ids).
        # Round 4 found these weren't actually prioritized by
        # _stitch_component_pieces's sampling despite the intent documented
        # there -- see that method's docstring for why this matters.
        self.navmesh_seam_node_ids: set = set()
        # Round 14 summary counters, logged once at the end of build_network.
        self.waterway_crossing_stats = {"nodes": 0, "regions": 0, "edges": 0}
        # Local gap-resolve summary counters, logged once at the end of
        # _ensure_coastal_connectivity (see LOCAL_GAP_RESOLVE_* above).
        self.local_gap_resolve_stats = {"gaps_resolved": 0, "edges_added": 0}
        # SPEC-GRAPH-DENSITY.md §4.3 axis-dedup summary counters, logged once at
        # the end of build_network. All stay 0 when axis_dedup_cap_m == 0.0 (the
        # code path that computes them is only entered when the feature is on).
        self.axis_dedup_stats = {"pieces_processed": 0, "pieces_with_suppression": 0,
                                  "suppressed_px": 0, "total_water_px": 0}
        # SPEC-OVERRIDE-ZONES.md §7 follow-up: same idea, for the navmesh/PSLG carve
        # path (_axis_dedup_carve_navmesh_pieces). "checked" counts every navmesh piece
        # this ran against (gated on axis_dedup_cap_m > 0.0); "rasterized" is the subset
        # that actually paid the bbox-prefilter-passed rasterize cost -- the gap between
        # the two IS the no-op fast path's hit rate. "rasterize_seconds" is wall-clock
        # time inside _rasterize_water_polygon + _axis_dedup_suppression_mask, logged so
        # the real per-build cost is measured, not estimated.
        self.axis_dedup_navmesh_stats = {"pieces_checked": 0, "pieces_rasterized": 0,
                                          "pieces_carved": 0, "pieces_fragmented": 0,
                                          "pieces_fully_consumed": 0, "rasterize_seconds": 0.0}
        # SPEC-GRAPH-DENSITY.md §6.3: reconnect counters for carve-induced dead ends --
        # a degree-1 skeleton node or navmesh-fragment perimeter node created BY an
        # axis-dedup carve, wired back to the specific axis line responsible for it via
        # _connect_waterway_crossing. "_candidates" counts dead ends found adjacent to a
        # suppressed pixel; "_edges" counts edges _connect_waterway_crossing actually
        # added (a candidate can still be rejected by that function's own land-crossing/
        # distance guards).
        self.axis_dedup_reconnect_stats = {"skeleton_candidates": 0, "skeleton_edges": 0,
                                            "navmesh_candidates": 0, "navmesh_edges": 0}
        # DIAGNOSTIC (connectivity-regression investigation, not shipped as a
        # feature): per-pass attempt/outcome counters for _stitch_component_pieces
        # and _resolve_local_skeleton_gaps, plus aggregate union-find group counts
        # before/after stitching. Logged once at the end of _ensure_coastal_connectivity.
        self._stitch_diag = defaultdict(lambda: defaultdict(int))
        self._stitch_group_stats = {"initial_groups": 0, "final_groups": 0,
                                     "components_with_residual": 0, "residual_groups_total": 0}
        # DIAGNOSTIC (coordinator's follow-up on the connectivity regression):
        # per-node set of distinct "contexts" (skeleton/navmesh piece, or other
        # node-creating subsystem) that ever requested this node's coordinate via
        # _get_or_create_node. A node touched by >1 context is a genuine
        # cross-piece/cross-subsystem coordinate merge, not just a chain re-using
        # its own endpoint. Logged once at the end of build_network.
        # Diagnostic only (see _log_node_origin_diag). Off unless
        # SK_ROUTING_NODE_ORIGIN_DIAG is set: this keeps one set PER NODE for the
        # whole build, which at full-country node counts is a permanent allocation
        # with no production consumer (CodeRabbit, #13).
        self._node_origin_diag = bool(os.environ.get('SK_ROUTING_NODE_ORIGIN_DIAG'))
        self._node_contexts = defaultdict(set)
        self._piece_counter = 0

        # Round 25 cross-database seam stitching (STITCHING_DESIGN.md Section 3).
        # Empty stitch_registry_path (the default) means stitching is OFF and
        # every method below is a strict no-op -- single-region behavior is
        # unchanged.
        self.stitch_registry_path = stitch_registry_path
        self.coverage_bbox = coverage_bbox
        self.stitch_band_m = stitch_band_m
        self.stitch_radius_m = stitch_radius_m
        self._coverage_bbox_cache = None
        # Navmesh resolution. 650m is right for pilotage waters; an offshore
        # passage layer wants kilometres, not hundreds of metres -- meshing open
        # ocean at 650m is what made the overview-chart extents unbuildable in
        # the first place, and the detail is worthless out there.
        self.navmesh_edge_m = navmesh_edge_m

    def run_pipeline(self):
        self.parse_shapefiles()
        self.build_network()
        self._log_node_origin_diag(label="pre_stitch")
        self._add_opening_bridge_edges()
        self._add_lock_crossing_edges()
        self._mark_edges_inside_locks()
        self._sanity_check_no_land_crossings()
        self._ensure_coastal_connectivity()
        self._log_node_origin_diag(label="post_stitch")
        # Adopt pass runs BEFORE calculate_edge_attributes so the new
        # adopted-node connector edges go through normal edge-attribute
        # computation like every other edge (STITCHING_DESIGN.md Section 3.3).
        adopted_ids = self._adopt_seam_nodes() if self.stitch_registry_path else set()
        self.calculate_edge_attributes()
        for u, v, data in self.graph.edges(data=True):
            if data.get("is_opening_bridge_edge"):
                data["max_air_draft"] = 999.0
        self._compute_node_depths(adopted_ids)
        # Publish pass runs after node depths are final, right before export,
        # so the registry gets each boundary node's real computed depth
        # (STITCHING_DESIGN.md Section 3.2).
        if self.stitch_registry_path:
            self._publish_seam_nodes(adopted_ids)
        self.export_to_sqlite()
        logger.info("Pipeline execution completed successfully.")

    def parse_shapefiles(self):
        logger.info("Parsing shapefiles and GeoJSONs...")
        # Map each layer name to its data_sources row id (deterministic insertion
        # order in _default_data_sources -> id = index+1), for provenance stamping.
        self.layer_source_ids = {s["name"]: i + 1 for i, s in enumerate(_default_data_sources())}
        for layer_name, path in self.data_paths.items():
            if os.path.exists(path):
                gdf = gpd.read_file(path)
                if gdf.crs != self.CRS_WGS84:
                    gdf = gdf.to_crs(self.CRS_WGS84)
                self.gdfs[layer_name] = gdf
                logger.info(f"Loaded '{layer_name}' with {len(gdf)} features.")
            else:
                logger.warning(f"File not found for '{layer_name}': {path}. Using empty fallback.")
                self.gdfs[layer_name] = gpd.GeoDataFrame(geometry=[], crs=self.CRS_WGS84)
        if "inland_waterways" in self.gdfs:
            self.gdfs["inland_waterways"] = self._densify_inland_waterways(self.gdfs["inland_waterways"])
        self._build_fairways_unified()
        self.gdfs_metric = {
            name: gdf.to_crs(self.CRS_METRIC) for name, gdf in self.gdfs.items()
        }
        self._build_obstacle_layer()

    def _build_fairways_unified(self):
        """FAIRWY + DRGARE as one fairway signal (docs/SPEC-FAIRWAY-HARMONIZATION.md).

        NOAA sparsely charts FAIRWY (regulated traffic lanes) but densely charts
        DRGARE (maintained-depth dredged channel footprints) -- DRGARE is the
        primary US analogue of what NL RWS IENC charts as FAIRWY. Cost
        harmonization, laned/skeleton classification, and bridge/lock crossing
        detection all read this unified layer so they see the real channel
        footprint regardless of which object class charted it. Kept distinct
        from the raw "fairways" (FAIRWY-only) layer used for POI generation and
        TRAFIC one-way direction, which stays FAIRWY-only per the spec (DRGARE
        carries no traffic-direction attribute). A build without a
        dredged_areas_polygons.geojson (older data, or a country where NOAA's
        DRGARE convention doesn't apply) falls back to fairways_unified ==
        fairways -- identical to pre-harmonization behavior.
        """
        fairways = self.gdfs.get("fairways", gpd.GeoDataFrame())
        dredged = self.gdfs.get("dredged_areas", gpd.GeoDataFrame())
        parts = [gdf for gdf in (fairways, dredged) if not gdf.empty]
        self.gdfs["fairways_unified"] = (
            gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=self.CRS_WGS84)
            if parts else gpd.GeoDataFrame(geometry=[], crs=self.CRS_WGS84)
        )

    def _densify_inland_waterways(self, inland_gdf):
        """SPEC-GRAPH-DENSITY.md §6.4: insert interpolated vertices along any
        `inland_waterways` segment longer than `inland_densify_max_segment_m`, so no
        segment exceeds it. Gated on the cap being > 0.0 -- disabled (default) leaves
        `inland_gdf` untouched, byte-identical to today's build.

        Runs once, here, before anything else reads the layer: `_build_inland_network`
        (raw vertex-to-vertex ingestion) and `_connect_waterway_crossing`/axis-dedup's
        own candidate rasterization (nearest-existing-vertex search) all then see the
        same already-dense geometry with zero changes to any of them.

        `shapely.segmentize` only inserts vertices along existing segments -- it never
        moves an original vertex (including the two endpoints) and never changes the
        line's shape, so this is length- and endpoint-preserving by construction.

        Rejects a cap that is non-finite or below `INLAND_DENSIFY_MIN_SEGMENT_M`: `NaN`
        slips past a bare `cap_m <= 0.0` check (NaN comparisons are always False in
        Python), and both NaN/inf violate `shapely.segmentize`'s own positive-finite
        contract; a valid but tiny positive cap (e.g. a stray extra zero) generates
        roughly `segment_length_m / cap_m` vertices per source segment with no ceiling
        of its own, risking unbounded memory on a real multi-kilometre line instead of a
        clear error.
        """
        cap_m = self.classification_config.inland_densify_max_segment_m
        if cap_m <= 0.0 or inland_gdf.empty:
            return inland_gdf
        if not math.isfinite(cap_m) or cap_m < INLAND_DENSIFY_MIN_SEGMENT_M:
            raise ValueError(
                f"--inland-densify-max-segment-m must be finite and >= "
                f"{INLAND_DENSIFY_MIN_SEGMENT_M}m (got {cap_m!r}); a smaller/non-finite "
                f"cap risks generating an unbounded number of vertices via "
                f"shapely.segmentize.")
        metric = inland_gdf.to_crs(self.CRS_METRIC)
        densified_m = shapely.segmentize(metric.geometry.values, cap_m)
        densified = gpd.GeoSeries(densified_m, index=inland_gdf.index, crs=self.CRS_METRIC).to_crs(self.CRS_WGS84)
        out = inland_gdf.copy()
        out["geometry"] = densified
        return out

    def _build_obstacle_layer(self):
        logger.info("Building obstacle layer from restricted areas, hulks, obstructions...")
        obstacle_parts = []
        resare = self.gdfs.get("restricted_areas", gpd.GeoDataFrame())
        if not resare.empty:
            ep_mask = resare.apply(_is_entry_prohibited, axis=1)
            entry_prohibited = resare[ep_mask].copy()
            if not entry_prohibited.empty:
                entry_prohibited["_layer"] = "restricted_areas"
                obstacle_parts.append(entry_prohibited)

        hulks = self.gdfs.get("hulks", gpd.GeoDataFrame())
        if not hulks.empty:
            hulks = hulks.copy()
            hulks["_layer"] = "hulks"
            obstacle_parts.append(hulks)

        # Same entry-prohibited-only filter as restricted_areas above — a
        # mariculture/marine-farm concession (S-57 MARCUL, e.g. a mussel or
        # oyster plot) is a fishing-rights designation, not automatically a
        # navigational hard-stop. Blanket-blocking every mariculture polygon
        # regardless of RESTRN was a real, confirmed bug: an unrestricted
        # marine-farm area near Zeelandbrug happened to overlap the bridge's
        # navigable opening corridor and silently made the entire crossing
        # (and the boundary-ring edges leading to it) unreachable, with no
        # warning — a plain data area, not a genuine obstruction, was hard-
        # excluding a route that should only have been soft-penalized (if
        # even that) via cost_factor, same as any other area vessels are
        # merely asked to avoid absent an explicit restriction.
        marcult = self.gdfs.get("mariculture", gpd.GeoDataFrame())
        if not marcult.empty:
            mc_mask = marcult.apply(_is_entry_prohibited, axis=1)
            mc_prohibited = marcult[mc_mask].copy()
            if not mc_prohibited.empty:
                mc_prohibited["_layer"] = "mariculture"
                obstacle_parts.append(mc_prohibited)

        # The raw obstructions_points layer is loaded under the CLI key "obstacles"
        # (obstructions_points.geojson). Read it from there — the old code read a
        # non-existent "obstructions" key, so obstructions never entered the layer.
        # This method then overwrites self.gdfs["obstacles"] with the merged result.
        #
        # Round 18: a NOAA-scale build carries thousands of these points (2,835 for
        # Puerto Rico vs Zeeland's 3 genuine obstructions), each of which previously
        # hard-blocked (crosses_obstacle=1) every edge it touched — 1,161
        # obstacle-blocked edges, enough to make San Juan<->Fajardo unroutable.
        # Split into a hard-block subset (kept in this "obstacles" hard layer,
        # unchanged behavior) and a soft depth-constraint subset (a separate
        # "obstacles_soft" layer consumed in _edge_attr_worker to fold into
        # min_depth instead of hard-blocking) via _obstruction_depth_disposition's
        # S-57-grounded VALSOU/WATLEV rules — see that function's docstring.
        soft_obstacle_gdf = gpd.GeoDataFrame(geometry=[], crs=self.CRS_WGS84)
        obstrn = self.gdfs.get("obstacles", gpd.GeoDataFrame())
        if not obstrn.empty:
            disposition = obstrn.apply(_obstruction_depth_disposition, axis=1)
            is_hard = disposition.apply(lambda t: t[0])
            depth_constraint = disposition.apply(lambda t: t[1])

            hard_obstrn = obstrn[is_hard].copy()
            if not hard_obstrn.empty:
                buf_metric = hard_obstrn.to_crs(self.CRS_METRIC)
                buf_metric["geometry"] = buf_metric.geometry.buffer(OBSTACLE_BUFFER_METERS)
                buf_wgs84 = buf_metric.to_crs(self.CRS_WGS84)
                buf_wgs84["_layer"] = "obstructions"
                obstacle_parts.append(buf_wgs84)

            soft_obstrn = obstrn[~is_hard].copy()
            if not soft_obstrn.empty:
                soft_obstrn["_depth_constraint"] = depth_constraint[~is_hard]
                buf_metric = soft_obstrn.to_crs(self.CRS_METRIC)
                buf_metric["geometry"] = buf_metric.geometry.buffer(OBSTACLE_BUFFER_METERS)
                soft_obstacle_gdf = buf_metric.to_crs(self.CRS_WGS84)
                soft_obstacle_gdf["_layer"] = "obstructions_soft"
            logger.info(
                f"  Obstruction points: {len(obstrn)} total, "
                f"{len(hard_obstrn)} hard-block, {len(soft_obstrn)} depth-constrained"
            )
        self.gdfs["obstacles_soft"] = soft_obstacle_gdf
        self.gdfs_metric["obstacles_soft"] = soft_obstacle_gdf.to_crs(self.CRS_METRIC) if not soft_obstacle_gdf.empty else gpd.GeoDataFrame(geometry=[], crs=self.CRS_METRIC)

        if not obstacle_parts:
            self.gdfs["obstacles"] = gpd.GeoDataFrame(geometry=[], crs=self.CRS_WGS84)
            self.gdfs_metric["obstacles"] = gpd.GeoDataFrame(geometry=[], crs=self.CRS_METRIC)
            return

        merged = gpd.pd.concat(obstacle_parts, ignore_index=True)
        merged["geometry"] = merged["geometry"].make_valid()
        merged = merged[merged.geometry.notnull()]
        if not merged.empty:
            merged = merged[merged.geometry.is_valid]
        logger.info(f"  Combined obstacle layer: {len(merged)} features")
        self.gdfs["obstacles"] = merged
        self.gdfs_metric["obstacles"] = merged.to_crs(self.CRS_METRIC)

    def build_network(self):
        logger.info("Building base network topology...")
        self.coords_to_node = {}
        # Inland waterway centerlines are unchanged (already vector line topology).
        if "inland_waterways" in self.gdfs and not self.gdfs["inland_waterways"].empty:
            self._build_inland_network()

        # Coastal water: split each connected component by local width, classify each
        # sub-piece, dispatch to skeleton (narrow channels) or a real navmesh_regions
        # triangulation (open water).
        coastal_gdf = self.gdfs.get("coastal_water")
        if coastal_gdf is not None and not coastal_gdf.empty:
            polygons = self._connected_water_polygons(coastal_gdf)
            depth_gdf = self.gdfs.get("depth_areas", gpd.GeoDataFrame())
            fairway_gdf = self.gdfs.get("fairways_unified", gpd.GeoDataFrame())
            src_id = self.layer_source_ids.get("coastal_water") if hasattr(self, "layer_source_ids") else None
            cfg = self.classification_config
            counts = {"skeleton": 0, "laned": 0, "navmesh": 0}

            for polygon in polygons:
                utm = self._local_utm_crs(polygon)
                poly_m = gpd.GeoSeries([polygon], crs=self.CRS_WGS84).to_crs(utm).iloc[0]
                wide_m, narrow_m, seam_m = self._split_wide_narrow(poly_m, cfg.min_navmesh_radius_m)
                seam_coords = self._seam_coord_set(seam_m)

                skeleton_pieces = []             # WGS84 polygons for build_skeleton_network
                navmesh_pieces = []              # (poly_m, utm, seam_coord_set) for build_navmesh_region

                # Narrow channels: keep this component's WHOLE narrow part as one piece,
                # even if it's a MultiPolygon of several disjoint-looking channel
                # fragments -- build_skeleton_network rasterizes it as one mask (rasterio
                # rasterize natively handles MultiPolygon), so fragments that are
                # geometrically close/touching stay topologically connected through the
                # medial-axis pass, same as Phase 0's one-skeleton-per-whole-component.
                # Exploding this into separate per-fragment build_skeleton_network calls
                # was tried first and fragmented real channel networks into hundreds of
                # disconnected pieces, since raster-derived medial-axis endpoints don't
                # land on the exact seam coordinate the fragments were cut at.
                if not narrow_m.is_empty:
                    narrow_wgs84 = gpd.GeoSeries([narrow_m], crs=utm).to_crs(self.CRS_WGS84).iloc[0]
                    kind = self.classify_water_body(narrow_wgs84, False, depth_gdf, fairway_gdf, cfg)
                    counts[kind] += len(self._explode_polygonal(narrow_m))
                    skeleton_pieces.append(narrow_wgs84)

                # Wide (navmesh-eligible-by-width) pieces DO need to stay exploded into
                # individual simple polygons: `triangle`'s PSLG triangulation takes one
                # exterior ring (+ holes) per call, not a MultiPolygon of disjoint areas.
                for piece_m in self._explode_polygonal(wide_m):
                    piece_wgs84 = gpd.GeoSeries([piece_m], crs=utm).to_crs(self.CRS_WGS84).iloc[0]
                    kind = self.classify_water_body(piece_wgs84, True, depth_gdf, fairway_gdf, cfg)
                    if kind != "navmesh":
                        counts[kind] += 1
                        skeleton_pieces.append(piece_wgs84)
                        continue

                    # Width-eligible AND passes classify_water_body's any-overlap depth
                    # check -- but "any deep DEPARE polygon overlaps this piece" is not
                    # "this piece is deep". Carve out the confirmed-deep sub-area before
                    # triangulating; the shallow/unsurveyed remainder falls back to
                    # skeleton/laned treatment exactly like a narrow piece.
                    deep_m, shallow_m, depth_seam_m = self._split_deep_shallow(
                        piece_m, utm, depth_gdf, cfg.depth_ceiling_m, cfg.min_navmesh_radius_m)
                    # Depth-split boundary is a real seam too (see
                    # _split_deep_shallow's docstring addendum) -- merge it with
                    # the original width-based seam so build_navmesh_region tags
                    # boundary_node_ids on both, not just the width seam.
                    depth_seam_coords = self._seam_coord_set(depth_seam_m)
                    combined_seam_coords = seam_coords | depth_seam_coords
                    tile_reclassified = []
                    for deep_piece_m in self._explode_polygonal(deep_m):
                        tiles, reclassified = self._tile_navmesh_piece(
                            deep_piece_m, NAVMESH_TILE_MAX_EXTENT_M, cfg.min_navmesh_radius_m)
                        for tile_poly_m, tile_seam_coords in tiles:
                            counts["navmesh"] += 1
                            navmesh_pieces.append((tile_poly_m, utm, combined_seam_coords | tile_seam_coords))
                        tile_reclassified.extend(reclassified)
                    if tile_reclassified:
                        # A tile-grid cut produced a fragment too thin to be genuinely
                        # navmesh-eligible any more (Round 8's width-re-filter pattern,
                        # see _tile_navmesh_piece) -- fold it back into the shallow/
                        # skeleton path exactly like _split_deep_shallow's own re-filter.
                        reclass_union_m = self._clean_polygonal(unary_union(tile_reclassified))
                        reclass_wgs84 = gpd.GeoSeries([reclass_union_m], crs=utm).to_crs(self.CRS_WGS84).iloc[0]
                        reclass_kind = self.classify_water_body(reclass_wgs84, False, depth_gdf, fairway_gdf, cfg)
                        counts[reclass_kind] += len(self._explode_polygonal(reclass_union_m))
                        skeleton_pieces.append(reclass_wgs84)
                    if not shallow_m.is_empty:
                        shallow_wgs84 = gpd.GeoSeries([shallow_m], crs=utm).to_crs(self.CRS_WGS84).iloc[0]
                        shallow_kind = self.classify_water_body(shallow_wgs84, False, depth_gdf, fairway_gdf, cfg)
                        counts[shallow_kind] += len(self._explode_polygonal(shallow_m))
                        skeleton_pieces.append(shallow_wgs84)

                # Build skeleton pieces before navmesh pieces so navmesh seam nodes can
                # coordinate-snap onto already-created skeleton endpoint nodes.
                for piece_wgs84 in skeleton_pieces:
                    self.build_skeleton_network(piece_wgs84, DEFAULT_SOURCE_TIER, src_id)
                for piece_m, piece_utm, piece_seam_coords in navmesh_pieces:
                    # SPEC-OVERRIDE-ZONES.md §7: give navmesh pieces the same axis-dedup
                    # suppression build_skeleton_network already applies -- inert (single-
                    # element list, `poly_m` unchanged, empty seam/line-iloc info) unless
                    # axis_dedup_cap_m > 0.0 AND an inland_waterways line is actually
                    # within reach. May return more than one polygon if carving fragments
                    # the piece; each fragment is triangulated separately so no water is
                    # silently dropped (see that method's docstring). SPEC-GRAPH-DENSITY.md
                    # §6.3.1: carve_seam_coords/carve_line_iloc_by_coord let
                    # build_navmesh_region reconnect a carve-induced perimeter dead end to
                    # the specific axis line that caused its own creation.
                    carved_pieces, carve_seam_coords, carve_line_iloc_by_coord = \
                        self._axis_dedup_carve_navmesh_pieces(piece_m, piece_utm)
                    merged_seam_coords = piece_seam_coords | carve_seam_coords
                    for carved_piece_m in carved_pieces:
                        self.build_navmesh_region(carved_piece_m, piece_utm, merged_seam_coords,
                                                   DEFAULT_SOURCE_TIER, src_id,
                                                   carve_line_iloc_by_coord=carve_line_iloc_by_coord)

            logger.info(f"Coastal water: {len(polygons)} connected components split into "
                        f"skeleton={counts['skeleton']}, laned={counts['laned']}, "
                        f"navmesh={counts['navmesh']} pieces.")
        wcs = self.waterway_crossing_stats
        if wcs["regions"]:
            logger.info(f"Inland-waterway x navmesh crossings: {wcs['nodes']} crossing nodes added "
                        f"across {wcs['regions']} regions, {wcs['edges']} connector edges.")
        ads = self.axis_dedup_stats
        if ads["pieces_processed"]:
            pct = (100.0 * ads["suppressed_px"] / ads["total_water_px"]) if ads["total_water_px"] else 0.0
            logger.info(f"Axis-dedup (SPEC-GRAPH-DENSITY.md §4.3): suppressed {ads['suppressed_px']} of "
                        f"{ads['total_water_px']} raster water pixels ({pct:.1f}%) across "
                        f"{ads['pieces_with_suppression']}/{ads['pieces_processed']} skeleton pieces "
                        f"before skeletonizing, to avoid duplicating an authoritative inland_waterways axis.")
        ans = self.axis_dedup_navmesh_stats
        if ans["pieces_checked"]:
            logger.info(f"Axis-dedup navmesh carve (SPEC-OVERRIDE-ZONES.md §7): {ans['pieces_rasterized']}/"
                        f"{ans['pieces_checked']} navmesh pieces had an inland_waterways line within reach "
                        f"(rasterized), {ans['pieces_checked'] - ans['pieces_rasterized']} skipped rasterization "
                        f"entirely (bbox prefilter, no candidate nearby); {ans['pieces_carved']} actually carved "
                        f"({ans['pieces_fragmented']} fragmented into >1 piece, {ans['pieces_fully_consumed']} "
                        f"fully consumed); {ans['rasterize_seconds']:.2f}s total in rasterize+suppression-mask "
                        f"across all navmesh pieces.")
        ars = self.axis_dedup_reconnect_stats
        if ars["skeleton_candidates"] or ars["navmesh_candidates"]:
            logger.info(f"Axis-dedup reconnect (SPEC-GRAPH-DENSITY.md §6.3): skeleton "
                        f"{ars['skeleton_edges']}/{ars['skeleton_candidates']} carve-induced dead ends "
                        f"reconnected, navmesh {ars['navmesh_edges']}/{ars['navmesh_candidates']} -- wired "
                        f"back to the specific inland_waterways line responsible for their own carving.")
        logger.info(f"Network built with {self.graph.number_of_nodes()} nodes and {self.graph.number_of_edges()} edges.")

    # ------------------------------------------------------------------
    # Step B — water-body classification
    # ------------------------------------------------------------------
    @staticmethod
    def _local_utm_crs(geom_wgs84):
        """Pick a metre-based local UTM CRS for a WGS84 geometry (avoids Web Mercator distortion)."""
        return gpd.GeoSeries([geom_wgs84], crs="EPSG:4326").estimate_utm_crs()

    @staticmethod
    def _explode_polygonal(geom) -> List[Polygon]:
        """Explode a Polygon/MultiPolygon/GeometryCollection into single Polygons."""
        if geom is None or geom.is_empty:
            return []
        if isinstance(geom, Polygon):
            return [geom]
        if isinstance(geom, MultiPolygon):
            return list(geom.geoms)
        # GeometryCollection fallback: keep polygonal parts only
        return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]

    def _connected_water_polygons(self, coastal_gdf) -> List[Polygon]:
        """unary_union the water areas and explode into connected single polygons.

        Self-intersecting input is repaired first. GEOS raises
        `TopologyException: side location conflict` from unary_union when a ring
        touches itself, and the whole build dies at the very first topology step
        with no output. Measured on the real RWS source: 213 of 56,309
        coastal_water polygons are invalid, and the union succeeds once they are
        repaired. Only the offending geometries are rewritten -- make_valid is a
        no-op on a valid polygon, and calling it unconditionally on all 56k would
        both cost time and risk perturbing good geometry.

        make_valid can return a line or a collection where the input was a
        degenerate sliver, so the result is filtered back to polygonal parts;
        _explode_polygonal does the same downstream for the union's own output.
        """
        geoms = []
        repaired = 0
        for g in coastal_gdf.geometry:
            if g is None or g.is_empty:
                continue
            if not g.is_valid:
                g = make_valid(g)
                repaired += 1
                if g is None or g.is_empty:
                    continue
                if not isinstance(g, (Polygon, MultiPolygon)):
                    parts = [p for p in getattr(g, "geoms", [])
                             if isinstance(p, (Polygon, MultiPolygon)) and not p.is_empty]
                    if not parts:
                        continue
                    geoms.extend(parts)
                    continue
            geoms.append(g)
        if repaired:
            logger.info(f"  Repaired {repaired} invalid coastal_water polygons before union.")
        if not geoms:
            return []
        return self._explode_polygonal(unary_union(geoms))

    def _split_wide_narrow(self, poly_m, radius_m: float, simplify_tol_m: float = 1.0):
        """Split a metric-CRS polygon into a wide (navmesh-eligible) part and a
        narrow (channel) part by local width, plus the seam where they meet.

        A naive buffer(-R).buffer(+R) can bulge back out past the original
        boundary on convex banks, corrupting the subsequent difference() --
        re-intersecting with the cleaned input after re-dilation avoids that.
        The seam is computed once here so both sides get bit-identical
        coordinates (avoids precision drift if recomputed independently later).
        """
        cleaned = poly_m.buffer(0).simplify(simplify_tol_m)
        eroded = cleaned.buffer(-radius_m, quad_segs=16)
        wide = eroded.buffer(radius_m, quad_segs=16).buffer(0).intersection(cleaned)
        narrow = cleaned.difference(wide).buffer(0)
        wide, narrow = self._clean_polygonal(wide), self._clean_polygonal(narrow)
        seam = wide.boundary.intersection(narrow.boundary)
        return wide, narrow, seam

    def _clean_polygonal(self, geom) -> Polygon:
        """Keep only a geometry's polygonal parts, re-unioned into one clean
        Polygon/MultiPolygon. GEOS boundary ops (buffer/intersection/difference)
        on complex real-world coastline geometry can degrade to a
        GeometryCollection mixing points/lines/polygons -- whose `.boundary` is
        `None`, which breaks the seam computation above.
        """
        pieces = self._explode_polygonal(geom)
        if not pieces:
            return Polygon()
        return unary_union(pieces)

    @staticmethod
    def _seam_coord_set(seam_geom, precision: int = 3) -> set:
        """All vertex coordinates of a seam geometry (Point/LineString/GeometryCollection,
        in metric CRS), rounded for exact-match lookup against exploded sub-polygon rings."""
        if seam_geom is None or seam_geom.is_empty:
            return set()
        return {(round(x, precision), round(y, precision))
                for x, y in shapely.get_coordinates(seam_geom)}

    def _split_deep_shallow(self, poly_m, utm, depth_gdf, depth_ceiling_m: float,
                             min_navmesh_radius_m: float):
        """Split a width-eligible ("wide") metric-CRS polygon into a deep part
        that's actually confirmed navmesh-eligible and a shallow/unsurveyed
        remainder that isn't -- the depth-side analog of `_split_wide_narrow`.

        `_has_navigable_depth` (used by `classify_water_body`) is an ANY-overlap
        test: a wide polygon qualifies as 'navmesh' if a single deep DEPARE
        polygon intersects it anywhere, even if the polygon is mostly shallow.
        Confirmed on a real full-scale run this isn't a rounding-error edge case:
        89.6% of navmesh_boundary (edge_kind_id=1) edges came out shallower than
        the depth ceiling (avg 3.17m vs the 6.0m default ceiling), because the
        region's own PERIMETER -- which becomes real, depth-sampled graph edges,
        see build_navmesh_region -- runs through whatever shallow fringe the
        original wide polygon had, not just its confirmed-deep interior. Real
        navmesh boundary edges ended up shallower on average than skeleton
        (narrow-channel) edges, which is backwards from the architecture's own
        intent. This carves the shallow fringe OUT before triangulation instead
        of only gating the whole-polygon classification decision.

        Deliberately vector, not raster: DEPARE data is already polygon
        geometry with DRVAL1, so intersecting it directly is both simpler and
        more precise than rasterizing just to re-vectorize a threshold mask.
        No DEPARE coverage at all for this polygon falls back to the existing
        `_has_navigable_depth` behavior (treat as fully deep) rather than
        becoming stricter for data that was never partially-shallow to begin
        with -- this fix targets the "partially surveyed as shallow" case, not
        unsurveyed gaps.
        """
        if depth_gdf is None or depth_gdf.empty or "DRVAL1" not in depth_gdf.columns:
            return poly_m, Polygon(), Polygon()
        # Spatial pre-filter first (this component's own bbox only, in the
        # source CRS -- cheap, and avoids handing unary_union a global
        # depth-area layer's worth of geometry for a single small piece) and
        # make_valid() every candidate before unioning: raw S-57-derived
        # DEPARE polygons can be invalid (self-intersecting rings etc.), and
        # unary_union/intersection/difference on invalid input is a real GEOS
        # segfault risk, not just a correctness one -- confirmed by hitting
        # exactly that crash with the naive version of this function during
        # implementation (no spatial filter, only buffer(0) on the union
        # *result*, nothing on the inputs). Mirrors the make_valid() pattern
        # already used for the obstacle layer above, not a new convention.
        poly_wgs84_bounds = gpd.GeoSeries([poly_m], crs=utm).to_crs(depth_gdf.crs).total_bounds
        try:
            candidate_idx = depth_gdf.sindex.query(box(*poly_wgs84_bounds), predicate="intersects")
        except Exception:
            candidate_idx = depth_gdf.index
        candidates = depth_gdf.iloc[candidate_idx] if len(candidate_idx) else depth_gdf.iloc[[]]
        if candidates.empty:
            return Polygon(), poly_m, Polygon()
        drval = pd.to_numeric(candidates["DRVAL1"], errors="coerce")
        deep_gdf = candidates[drval >= depth_ceiling_m]
        if deep_gdf.empty:
            return Polygon(), poly_m, Polygon()
        deep_geoms = deep_gdf.geometry.make_valid()
        deep_geoms = deep_geoms[deep_geoms.notnull() & deep_geoms.is_valid]
        if deep_geoms.empty:
            return Polygon(), poly_m, Polygon()
        deep_mask_m = gpd.GeoSeries(deep_geoms, crs=depth_gdf.crs).to_crs(utm)
        # Simplify the DEPARE-derived cut boundary before it ever reaches
        # triangulation, same reasoning and same tolerance _split_wide_narrow
        # already applies to its own boundary: raw DEPARE polygons carry
        # survey-grade vertex density (confirmed elsewhere in this file's
        # history to run to hundreds of vertices for small features), and
        # feeding that complexity straight into build_navmesh_region's PSLG
        # pushed a region over NAVMESH_PSLG_BUDGET that fit comfortably
        # before this split existed, triggering build_navmesh_region's
        # simplify-retry loop on a many-holed polygon and segfaulting
        # _triangle.triangulate -- confirmed by reproducing the crash with
        # this step absent and it going away with it present.
        # Morphological closing (+buffer then -buffer) on top of simplify:
        # real DEPARE polygons from adjacent survey contours rarely align
        # exactly with each other or with the water body's own boundary, so
        # a straight union/intersection/difference chain produces thin
        # slivers and small holes that are topologically pathological (not
        # just visually messy) -- this is what was still crashing
        # _triangle.triangulate after simplify() alone. Closing radius
        # (`DEPTH_SPLIT_CLOSING_RADIUS_M`): started at 5m in the version that
        # first fixed the triangulate() crash, but that left most of the
        # fragmentation in place -- confirmed on real data that adjacent
        # DEPARE survey-contour bands routinely need tens of meters, not
        # single-digit meters, to bridge cleanly (Round 8 investigation).
        # A knife-edge cut exactly at the depth-ceiling contour puts the
        # region's own boundary -- which becomes real, sampled graph edges --
        # right at the transition, so ordinary DEPARE band-boundary/sampling
        # noise pushes a lot of it to read just *under* the ceiling rather
        # than comfortably above (confirmed: without this margin, 84% of
        # boundary edges landed in the 5.0-6.0m band specifically, not
        # spread shallower -- a real but different, much smaller problem than
        # the one this function fixes). Erode the deep mask a further margin
        # past the ceiling contour so the region boundary sits inside
        # confirmed-deep water with real clearance, not exactly on the line.
        # buffer(0) each geometry individually right before the union, not
        # just relying on the make_valid()+is_valid filter above -- GEOS can
        # still throw "TopologyException: side location conflict" out of
        # unary_union on a large batch of inputs that each independently
        # report is_valid=True (confirmed: FL Gulf's DEPARE-derived deep mask
        # crashed here with exactly that exception despite every input having
        # passed the is_valid filter). buffer(0) is a stronger/differently-
        # implemented repair than make_valid() and is the standard GEOS
        # workaround for this specific failure mode.
        pre_closing_m = (unary_union([g.buffer(0) for g in deep_mask_m.geometry]).buffer(0)
                          .simplify(1.0).buffer(0))
        closed_m = pre_closing_m.buffer(DEPTH_SPLIT_CLOSING_RADIUS_M).buffer(-DEPTH_SPLIT_CLOSING_RADIUS_M)

        # Round 21 fix (Issue G): the closing above is real and necessary (Round
        # 8 confirmed 50m is needed to bridge genuine DEPARE survey-contour
        # misalignment seams -- without it, region count balloons back toward
        # Round 7's 201), but on real Yerseke-area braided tidal-flat data it
        # was ALSO filling in genuine interior drying/shallow separators that
        # are charted, not noise (confirmed: of 30 real interior holes in the
        # deep mask there, only 1 survived 50m closing).
        #
        # Candidate set is deliberately `pre_closing_m`'s own INTERIOR RINGS
        # (voids already fully enclosed within one already-connected deep
        # piece), not `closed_m.difference(pre_closing_m)` (an earlier version
        # of this fix used that and it's wrong on two counts, both confirmed by
        # direct measurement against real Yerseke data, not assumed): (1) that
        # difference also captures ordinary buffer-rounding noise along
        # `pre_closing_m`'s OUTER boundary, unrelated to any hole -- in this
        # bbox alone the difference's total area (490,214 m^2) vastly exceeds
        # the 30 real holes' combined area (32,726 m^2), so most of it isn't
        # hole-filling at all; (2) more importantly, an interior ring can only
        # ever exist within an ALREADY-single-connected polygon/multipolygon
        # component -- filling or not filling one can never change whether two
        # previously SEPARATE deep pieces merge into one (that's a distinct
        # operation, gated entirely by the closing's effect on each piece's own
        # exterior boundary). Restricting the candidate set to interior rings
        # therefore makes this fix structurally incapable of touching Round 8's
        # region/component-count fragmentation fix, regardless of the real/noise
        # threshold below -- confirmed directly in this bbox too (the two
        # largest deep pieces here are 1,855m apart, far beyond 50m closing;
        # the whole effect here is hole-filling, see Round 9's Issue G writeup).
        #
        # Real/noise test per hole: the brief's original lead (charted drying,
        # `_drying_gdf`, DRVAL1<0) is kept as one signal -- real elsewhere in
        # the dataset -- but ALONE it recovers 0 of the 30 real Yerseke holes:
        # direct measurement found none of them are charted drying at all, they
        # are either DEPARE-uncovered micro-voids or charted-but-shallow
        # (0 <= DRVAL1 < ceiling) patches, neither of which is "drying" by the
        # strict DRVAL1<0 definition. Added a second, independently-justified
        # signal: does the hole survive a small erosion (contains a disk of
        # `DEPTH_SPLIT_HOLE_MIN_WIDTH_M`)? Round 8's own documented noise
        # fragments were sub-1m^2 to low-single-digit-m^2 GEOS artifacts (one
        # measured 0.007m^2) -- nowhere near wide enough to survive even a
        # couple meters of erosion -- while every real Yerseke hole bar the
        # smallest handful is hundreds to tens of thousands of m^2 and
        # comfortably clears it (measured: 25/30 survive at this radius,
        # 26/30 once combined with the drying signal, recovering the majority
        # target with area matching/exceeding the original 32,726 m^2).
        pre_holes = []
        for piece in self._explode_polygonal(pre_closing_m):
            for ring in piece.interiors:
                hole = Polygon(ring)
                if hole.area > 0:
                    pre_holes.append(hole)

        if pre_holes:
            drying_gdf = self._drying_gdf()
            has_drying = drying_gdf is not None and not drying_gdf.empty
            re_punch = []
            for hole in pre_holes:
                # Only act on holes the closing actually filled -- one that's
                # still (mostly) excluded from closed_m needs no correction.
                if hole.intersection(closed_m).area / hole.area < 0.5:
                    continue
                is_wide_enough = not hole.buffer(-DEPTH_SPLIT_HOLE_MIN_WIDTH_M).is_empty
                drying_fraction = 0.0
                if has_drying:
                    hole_bounds = gpd.GeoSeries([hole], crs=utm).to_crs(drying_gdf.crs).total_bounds
                    try:
                        drying_idx = drying_gdf.sindex.query(box(*hole_bounds), predicate="intersects")
                    except Exception:
                        drying_idx = drying_gdf.index
                    drying_candidates = drying_gdf.iloc[drying_idx] if len(drying_idx) else drying_gdf.iloc[[]]
                    if not drying_candidates.empty:
                        drying_m = gpd.GeoSeries(drying_candidates.geometry, crs=drying_gdf.crs).to_crs(utm)
                        drying_union_m = unary_union(list(drying_m.geometry)).buffer(0)
                        drying_fraction = hole.intersection(drying_union_m).area / hole.area
                if is_wide_enough or drying_fraction > DEPTH_SPLIT_DRYING_REPUNCH_FRACTION:
                    re_punch.append(hole)
            if re_punch:
                closed_m = self._clean_polygonal(closed_m.difference(unary_union(re_punch)))

        deep_union_m = closed_m.buffer(-DEPTH_SPLIT_SAFETY_MARGIN_M)
        deep = self._clean_polygonal(poly_m.intersection(deep_union_m))
        shallow = self._clean_polygonal(poly_m.difference(deep_union_m))

        # Even after closing, a deep sub-piece can come out too small/narrow to
        # be genuinely navmesh-eligible any more -- either real GEOS
        # intersection-boundary noise (confirmed on real data: some resulting
        # pieces were a few hundredths of a m^2, sitting meters from a
        # multi-km^2 neighbor -- clearly a cut artifact, not a water body), or
        # a real but tiny, non-navigable pocket (same class of feature flagged
        # in Round 5 SS5.4). Re-apply the same width test `_split_wide_narrow`
        # already gates initial wide/narrow classification with: a deep piece
        # that no longer contains a disk of `min_navmesh_radius_m` isn't wide
        # enough to deserve full navmesh treatment, so fold it back into the
        # shallow/skeleton path instead of emitting a degenerate navmesh_regions row.
        still_deep_pieces, reclassified_pieces = [], []
        for piece in self._explode_polygonal(deep):
            wide_piece, narrow_piece, _ = self._split_wide_narrow(piece, min_navmesh_radius_m)
            if not wide_piece.is_empty:
                still_deep_pieces.append(wide_piece)
            if not narrow_piece.is_empty:
                reclassified_pieces.append(narrow_piece)
        deep = unary_union(still_deep_pieces) if still_deep_pieces else Polygon()
        if reclassified_pieces:
            shallow = self._clean_polygonal(unary_union([shallow, *reclassified_pieces]))

        # The depth-cut boundary between the final deep/shallow pieces is a real
        # seam, exactly like _split_wide_narrow's wide/narrow seam -- but it was
        # never fed into build_navmesh_region's seam_coord_set, so navmesh_regions
        # rows produced by a depth split never got any boundary_node_ids tagged
        # for it (confirmed against a live database: 24/25 regions had a
        # completely empty boundary_node_ids, disabling routeiq's funnel-upgrade
        # and anchor-shortcut precompute for those regions -- see NEXT_PHASES.md,
        # "master root cause" writeup). Compute it here, the same way
        # _split_wide_narrow does (boundary intersection of the two final
        # pieces), so the caller can merge it into the width-based seam set.
        depth_seam = Polygon()
        if not deep.is_empty and not shallow.is_empty:
            try:
                depth_seam = deep.boundary.intersection(shallow.boundary)
            except Exception:
                depth_seam = Polygon()
        return deep, shallow, depth_seam

    def _tile_navmesh_piece(self, poly_m, max_extent_m: float, min_navmesh_radius_m: float):
        """Round 23a: cap a navmesh-eligible piece's extent by splitting it into a
        regular grid of tiles no larger than `max_extent_m` per side, when the piece
        exceeds `NAVMESH_TILE_MAX_EXTENT_M` (bbox extent) and/or
        `NAVMESH_TILE_MAX_VERTICES` (post-`NAVMESH_BOUNDARY_SIMPLIFY_M` boundary
        vertex count) -- see those constants' comments for the measured PR
        motivation (one 400km-wide, 8,118-boundary-node region -> 76.6s loadGraph()).

        Every surviving tile is meant to flow through the normal
        `build_navmesh_region` path exactly like an untiled piece; the only extra
        contract is that adjacent tiles MUST end up with bit-identical coordinates
        along their shared cut line, or `_get_or_create_node`'s 5-decimal coordinate
        dedupe won't merge them into the same graph node and the tile seam won't
        connect. That's why the grid cut is done as ONE `unary_union` of the piece's
        own boundary rings together with every grid line, followed by ONE
        `polygonize()` call, rather than N independent `poly_m.intersection(box_i)`
        calls per cell: a single noding pass computes each cut-line/coastline (or
        cut-line/cut-line) intersection point exactly once and both adjoining faces
        reuse that same computed vertex, whereas two separate GEOS calls computing
        "the same" intersection independently are not guaranteed to land on
        identical floating-point coordinates.

        Returns (tiles, reclassified):
        - tiles: [(tile_poly_m, tile_seam_coords), ...] -- tile_seam_coords is the
          subset of that tile's own boundary coordinates lying on an internal grid
          cut line (rounded to the same 3-decimal-metre precision
          `build_navmesh_region` matches `seam_coord_set` against), for the caller
          to union into the piece's existing width/depth seam set so tile-seam
          vertices get tagged `boundary_node_ids` on BOTH adjacent tiles, same as
          any other seam.
        - reclassified: [poly_m, ...] tile fragments that failed the
          `min_navmesh_radius_m` disk test (Round 8's width-re-filter pattern) --
          for the caller to fold back into the skeleton/shallow path. Deliberately
          a whole-tile pass/fail test (`buffer(-radius).is_empty`) rather than
          Round 8's full wide/narrow buffer(-r).buffer(+r) RECONSTRUCTION: that
          reconstruction is a morphological open, which subtly perturbs straight
          edges/corners with buffer-approximation noise -- fine for Round 8's
          purpose (isolating a genuinely narrow appendage), but here it would
          silently break the exact-coordinate seam match this tiling depends on
          for cross-tile connectivity. A single grid cell is small enough to treat
          as one unit: for open-ocean tiling it will essentially always wholly
          pass; the rare wholly-thin case (an edge tile that's mostly a ragged
          sliver of original coastline) is exactly what this test is for.
        """
        minx, miny, maxx, maxy = poly_m.bounds
        width, height = maxx - minx, maxy - miny

        simplified = self._clean_polygonal(poly_m.buffer(0).simplify(NAVMESH_BOUNDARY_SIMPLIFY_M))
        nverts = sum(len(p.exterior.coords) - 1 + sum(len(ring.coords) - 1 for ring in p.interiors)
                     for p in self._explode_polygonal(simplified))
        needs_tiling = (width > max_extent_m or height > max_extent_m
                        or nverts > NAVMESH_TILE_MAX_VERTICES)
        if not needs_tiling:
            return [(poly_m, set())], []

        if GLOBAL_TILE_GRID_PROBE:
            # PROBE grid: interior cut lines are multiples of max_extent_m from a
            # fixed world origin (0,0) in this piece's own projected metre CRS,
            # rather than `minx + i*width/nx` relative to the piece's own bbox.
            # Whichever global lines fall strictly inside the bbox are used --
            # count and spacing are a function of world position, not this piece.
            def _global_interior_lines(lo, hi, step):
                first_k = math.floor(lo / step) + 1
                last_k = math.ceil(hi / step) - 1
                return [k * step for k in range(int(first_k), int(last_k) + 1)]

            xs_interior = _global_interior_lines(minx, maxx, max_extent_m)
            ys_interior = _global_interior_lines(miny, maxy, max_extent_m)
            if not xs_interior and not ys_interior:
                # No global grid line actually crosses this piece -- nothing to cut.
                return [(poly_m, set())], []
            nx, ny = len(xs_interior) + 1, len(ys_interior) + 1
        else:
            nx = max(1, math.ceil(width / max_extent_m))
            ny = max(1, math.ceil(height / max_extent_m))
            if nx <= 1 and ny <= 1:
                # Vertex-count-only trigger with a bbox already under max_extent_m: a grid
                # split would just hand back the same single piece (nx=ny=1) -- nothing
                # to gain here. build_navmesh_region's own NAVMESH_PSLG_BUDGET retry/
                # simplify loop is the fallback for an over-dense boundary this small.
                return [(poly_m, set())], []
            xs = [minx + i * width / nx for i in range(nx + 1)]
            ys = [miny + j * height / ny for j in range(ny + 1)]
            xs_interior = xs[1:-1]
            ys_interior = ys[1:-1]

        pad = max(width, height) * 0.01 + 10.0  # past the bbox so a grid line fully crosses it
        grid_lines = [LineString([(x, miny - pad), (x, maxy + pad)]) for x in xs_interior]
        grid_lines += [LineString([(minx - pad, y), (maxx + pad, y)]) for y in ys_interior]

        boundary_lines = [poly_m.exterior] + list(poly_m.interiors)
        try:
            noded = unary_union(boundary_lines + grid_lines)
            raw_faces = list(polygonize(noded))
        except Exception as exc:
            logger.warning(f"  Navmesh tiling failed to node/polygonize a piece ({exc}); "
                            f"leaving it untiled.")
            return [(poly_m, set())], []

        raw_tiles = [f for f in raw_faces
                     if not f.is_empty and f.representative_point().within(poly_m)]
        if not raw_tiles:
            logger.warning("  Navmesh tiling produced no tiles inside the original piece; "
                            "leaving it untiled.")
            return [(poly_m, set())], []

        grid_x_set = {round(x, 3) for x in xs_interior}
        grid_y_set = {round(y, 3) for y in ys_interior}

        tiles, reclassified = [], []
        for face in raw_tiles:
            for sub_tile in self._explode_polygonal(self._clean_polygonal(face.buffer(0))):
                if sub_tile.is_empty:
                    continue
                if sub_tile.buffer(-min_navmesh_radius_m, quad_segs=16).is_empty:
                    reclassified.append(sub_tile)
                    continue
                tile_seam = {(round(x, 3), round(y, 3)) for x, y in shapely.get_coordinates(sub_tile)
                             if round(x, 3) in grid_x_set or round(y, 3) in grid_y_set}
                tiles.append((sub_tile, tile_seam))

        if not tiles and not reclassified:
            return [(poly_m, set())], []
        logger.info(f"  Navmesh tiling: {width/1000:.0f}x{height/1000:.0f}km piece "
                    f"({nverts} boundary verts) -> {nx}x{ny} grid -> {len(tiles)} tiles"
                    + (f", {len(reclassified)} sub-threshold fragments reclassified" if reclassified else "") + ".")
        return tiles, reclassified

    def classify_water_body(self, polygon, is_wide: bool, depth_gdf, fairway_gdf,
                            config) -> Literal["navmesh", "skeleton", "laned"]:
        """Classify one already width-split water sub-polygon.

        `is_wide` is computed once per sub-polygon by `_split_wide_narrow`, not
        by eroding this function's own whole-polygon input. Real hydrography
        merges wide bays and narrow channels into one connected polygon, so
        eroding the whole thing here (the Phase 0 approach) always finds the
        bay and misclassifies the narrow channel bundled together with it.

        A wide piece is 'navmesh' unless it's entirely shallower than the
        depth ceiling, in which case it falls back to the narrow-piece logic:
        a channel that overlaps a regulated fairway is 'laned' (directional
        treatment attempted in Step D); otherwise it's a plain 'skeleton'
        centerline.
        """
        if is_wide and self._has_navigable_depth(polygon, depth_gdf, config.depth_ceiling_m):
            return "navmesh"
        if self._has_regulatory_structure(polygon, fairway_gdf):
            return "laned"
        return "skeleton"

    @staticmethod
    def _has_navigable_depth(polygon, depth_gdf, depth_ceiling_m: float) -> bool:
        """True if any DEPARE area of depth >= ceiling overlaps the polygon (genuine open water)."""
        if depth_gdf is None or depth_gdf.empty or "DRVAL1" not in depth_gdf.columns:
            # No depth data: default to treating wide bodies as navigable open water.
            return True
        drval = pd.to_numeric(depth_gdf["DRVAL1"], errors="coerce")
        deep = depth_gdf[drval >= depth_ceiling_m]
        if deep.empty:
            return False
        try:
            hits = deep.sindex.query(polygon, predicate="intersects")
            return len(hits) > 0
        except Exception:
            return bool(deep.intersects(polygon).any())

    def _has_regulatory_structure(self, polygon, fairway_gdf) -> bool:
        """True if a fairway (regulated channel) polygon overlaps this water body (Step D feeder)."""
        if fairway_gdf is None or fairway_gdf.empty:
            return False
        try:
            hits = fairway_gdf.sindex.query(polygon, predicate="intersects")
            return len(hits) > 0
        except Exception:
            return bool(fairway_gdf.intersects(polygon).any())

    @staticmethod
    def _extract_buoyage_direction(fairway_row) -> Optional[int]:
        """Step D — lateral-buoyage direction for a fairway, if the attributes carry it.

        Session 0 recon confirmed the current fairway layer has NO structured
        direction data (TRAFIC/ORIENT null on all features; no IALA CATLAM/COLOUR),
        so this returns None for real data and lane pairs are never fabricated —
        'laned' polygons gracefully degrade to a plain skeleton centerline with the
        default two-way traffic_mode (spec-sanctioned, format spec §2.5/§2.8).
        Kept as the single seam to light up when a richer fairway source is added.
        """
        # S-57 TRAFIC (1=inbound, 2=outbound, 3=one-way, 4=two-way) is the only
        # direction-ish attribute present, and it is null on every current feature.
        # Even when populated it is a plain flag — not enough for a geometric lane
        # pair — so we never derive a lane direction here.
        return None

    @staticmethod
    def _coord_to_id(lon: float, lat: float, node_type: str = "coastal") -> int:
        lat_int = int((round(lat, 5) + 90.0) * 100000)
        lon_int = int((round(lon, 5) + 180.0) * 100000)
        type_int = 1 if node_type == "inland" else 0
        return (type_int * 648000000000000) + (lat_int * COORD_SPACE) + lon_int

    @staticmethod
    def _generate_poi_id(poi_type: str, lat: float, lon: float) -> int:
        unique_str = f"{poi_type}_{round(lat, 5)}_{round(lon, 5)}"
        return int(hashlib.md5(unique_str.encode("utf-8")).hexdigest()[:13], 16)

    def _get_or_create_node(self, lon: float, lat: float, node_type: str = "coastal", context=None) -> int:
        coord = (round(lon, 5), round(lat, 5))
        if coord in self.coords_to_node:
            existing_id = self.coords_to_node[coord]
            if existing_id not in self.graph:
                del self.coords_to_node[coord]
                node_id = self._coord_to_id(lon, lat, node_type)
                self.graph.add_node(node_id, lon=coord[0], lat=coord[1], node_type=node_type)
                self.coords_to_node[coord] = node_id
                if context is not None:
                    if self._node_origin_diag:
                        self._node_contexts[node_id].add(context)
                return node_id
            if context is not None:
                if self._node_origin_diag:
                    self._node_contexts[existing_id].add(context)
            return existing_id
        node_id = self._coord_to_id(lon, lat, node_type)
        self.graph.add_node(node_id, lon=coord[0], lat=coord[1], node_type=node_type)
        self.coords_to_node[coord] = node_id
        if context is not None:
            if self._node_origin_diag:
                self._node_contexts[node_id].add(context)
        return node_id

    def _build_inland_network(self):
        inland_gdf = self.gdfs["inland_waterways"]
        src_id = self.layer_source_ids.get("inland_waterways") if hasattr(self, "layer_source_ids") else None
        eattr = dict(edge_type="inland", source_tier=DEFAULT_SOURCE_TIER, source_id=src_id)
        for fi, (_, row) in enumerate(inland_gdf.iterrows()):
            geom = row.geometry
            if isinstance(geom, LineString):
                coords = list(geom.coords)
                for i in range(len(coords) - 1):
                    u_lon, u_lat = coords[i]
                    v_lon, v_lat = coords[i+1]
                    u = self._get_or_create_node(u_lon, u_lat, node_type="inland", context="inland")
                    v = self._get_or_create_node(v_lon, v_lat, node_type="inland", context="inland")
                    self._stamp_node(u, NODE_KIND_POINT, DEFAULT_SOURCE_TIER, src_id)
                    self._stamp_node(v, NODE_KIND_POINT, DEFAULT_SOURCE_TIER, src_id)
                    self.graph.add_edge(u, v, **eattr)
                    self.graph.add_edge(v, u, **eattr)

    def build_navmesh_placeholder(self, polygon, source_tier=DEFAULT_SOURCE_TIER, source_id=None):
        """TEMPORARY PLACEHOLDER — Phase 0 only, replaced by real navmesh_regions in Phase 1.

        The inherited priority-binned point injection + unconstrained Delaunay logic,
        now scoped to a SINGLE open-water polygon. Emits an ordinary point graph
        (node_kind_id=0, edge_kind_id=0); it is intentionally NOT a navmesh_regions
        row. Phase 1 re-derives real navmesh regions from the source polygon geometry,
        not from this output, so no forward-compat structuring is needed here.
        """
        BIN_SIZE = 0.0005
        binned_points = {}

        def add_point(lon, lat, priority):
            bx, by = int(lon / BIN_SIZE), int(lat / BIN_SIZE)
            if (bx, by) not in binned_points or priority > binned_points[(bx, by)][2]:
                binned_points[(bx, by)] = (lon, lat, priority)

        def _in_poly_coords(coords):
            """Filter (lon,lat) tuples to those inside this polygon (vectorized)."""
            if not coords:
                return []
            arr = np.asarray(coords, dtype=float)
            keep = shapely.contains_xy(polygon, arr[:, 0], arr[:, 1])
            return arr[keep]

        # Prio 3: fairway / inland-waterway centerlines that fall inside this polygon
        line_pts = []
        for key in ("fairways", "inland_waterways"):
            gdf = self.gdfs.get(key, gpd.GeoDataFrame())
            if gdf is None or gdf.empty:
                continue
            for geom in gdf.geometry:
                for line in _iter_boundary_lines(geom):
                    line_pts.extend(line.simplify(0.001).coords)
        for lon, lat in _in_poly_coords(line_pts):
            add_point(lon, lat, 3)

        # Prio 2: representative point of the polygon
        rep = polygon.representative_point()
        add_point(rep.x, rep.y, 2)

        # Prio 1: DEPARE depth-contour vertices inside this polygon
        depare_gdf = self.gdfs.get("depth_areas", gpd.GeoDataFrame())
        if depare_gdf is not None and not depare_gdf.empty and "DRVAL1" in depare_gdf.columns:
            drval = pd.to_numeric(depare_gdf["DRVAL1"], errors="coerce")
            contour_pts = []
            for geom in depare_gdf[drval >= 0.5].geometry:
                for line in _iter_boundary_lines(geom):
                    contour_pts.extend(line.simplify(0.001).coords)
            for lon, lat in _in_poly_coords(contour_pts):
                add_point(lon, lat, 1)

        # Prio 0: coarse grid over the polygon bbox, kept only where inside the polygon
        MAX_RES = 0.005
        minx, miny, maxx, maxy = polygon.bounds
        xs = np.arange(minx, maxx, MAX_RES)
        ys = np.arange(miny, maxy, MAX_RES)
        if xs.size and ys.size:
            xx, yy = np.meshgrid(xs, ys)
            gx, gy = xx.ravel(), yy.ravel()
            grid_keep = shapely.contains_xy(polygon, gx, gy)
            for lon, lat in zip(gx[grid_keep], gy[grid_keep]):
                add_point(lon, lat, 0)

        points = [(x, y) for x, y, _ in binned_points.values()]
        if len(points) < 3:
            return

        import scipy.spatial
        pts_array = np.array(points)
        try:
            tri = scipy.spatial.Delaunay(pts_array)
        except Exception as exc:  # degenerate/collinear point set
            logger.warning(f"  Placeholder Delaunay failed on a polygon ({exc}); skipping.")
            return
        simplices = tri.simplices
        edges = np.vstack((simplices[:, [0, 1]], simplices[:, [1, 2]], simplices[:, [2, 0]]))
        edges.sort(axis=1)
        unique_edges = np.unique(edges, axis=0)

        MAX_EDGE_LEN = 0.015  # ~1.5km in degrees
        pt_to_id = {}
        for pt in points:
            u = self._get_or_create_node(pt[0], pt[1], "coastal", context="navmesh_placeholder")
            self._stamp_node(u, NODE_KIND_POINT, source_tier, source_id)
            pt_to_id[(pt[0], pt[1])] = u

        added = 0
        for idx1, idx2 in unique_edges:
            p1, p2 = pts_array[idx1], pts_array[idx2]
            if math.hypot(p1[0] - p2[0], p1[1] - p2[1]) > MAX_EDGE_LEN:
                continue
            u = pt_to_id[(p1[0], p1[1])]
            v = pt_to_id[(p2[0], p2[1])]
            if not self.graph.has_edge(u, v):
                attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_CENTERLINE,
                             is_placeholder=True, source_tier=source_tier, source_id=source_id)
                self.graph.add_edge(u, v, **attrs)
                self.graph.add_edge(v, u, **attrs)
                added += 2

    # ------------------------------------------------------------------
    # Step B2 (Phase 1) — real navmesh_regions via constrained triangulation
    # ------------------------------------------------------------------
    @staticmethod
    def _polygon_to_pslg(poly_m):
        """Build a `triangle`-library PSLG dict (vertices/segments/holes) from a
        shapely polygon-with-holes, in metric CRS. Returns (pslg_dict, ring_ranges)
        where ring_ranges is [(start_index, vertex_count), ...], one entry per ring
        (exterior first, then each interior), so callers can walk a single ring's
        own vertex-adjacency without touching `triangle`'s input dict (it rejects
        unrecognized keys).

        Exterior ring vertices come first (indices 0..n-1), each interior ring
        is appended after. `triangle`'s PSLG mode preserves this input vertex
        order exactly in its output (verified: output_vertices[0:N] ==
        input_vertices, even after quality/area refinement inserts interior
        Steiner points), so seam/boundary node identity can be tracked by
        index alone -- no need for the `-Y` switch, which silently suppresses
        all refinement in this binding despite its docs.
        """
        def _ring_coords(ring):
            coords = list(ring.coords)
            if len(coords) > 1 and coords[0] == coords[-1]:
                coords = coords[:-1]
            return coords

        vertices: List[Tuple[float, float]] = []
        segments: List[Tuple[int, int]] = []
        ring_ranges: List[Tuple[int, int]] = []  # (start_index, vertex_count) per ring

        def _add_ring(ring):
            coords = _ring_coords(ring)
            start = len(vertices)
            n = len(coords)
            vertices.extend(coords)
            segments.extend((start + i, start + (i + 1) % n) for i in range(n))
            ring_ranges.append((start, n))

        _add_ring(poly_m.exterior)
        holes: List[Tuple[float, float]] = []
        for interior in poly_m.interiors:
            _add_ring(interior)
            holes.append(tuple(Polygon(list(interior.coords)).representative_point().coords[0]))

        data = {"vertices": np.array(vertices, dtype=float),
                "segments": np.array(segments, dtype=np.int32)}
        if holes:
            data["holes"] = np.array(holes, dtype=float)
        return data, ring_ranges

    def _inject_waterway_crossings(self, poly_m, utm_crs):
        """Where an `inland_waterways` line crosses this navmesh piece's FINAL
        boundary (post-`NAVMESH_BOUNDARY_SIMPLIFY_M`, post-PSLG-budget-retry --
        i.e. exactly the geometry `build_navmesh_region` is about to hand to
        `_polygon_to_pslg`), insert the crossing as a genuine ring vertex. Because
        nothing simplifies this geometry again afterward, the inserted point
        survives untouched into the PSLG and its coordinate exact-matches the
        seam set this method also returns -- unlike the width/depth seams (whose
        seam_coord_set is computed on the PRE-simplify boundary and only survives
        because simplify never *moves* a retained vertex), a freshly-interpolated
        crossing point computed against a pre-simplify boundary would generally
        NOT land on the post-simplify boundary at all, so this must run after all
        simplification is finalized, not passed down from build_network like the
        width/depth seams are.

        Returns (poly_m_with_crossings, extra_seam_coords, crossing_records).
        crossing_records is [(ring_idx, position_in_ring, inland_gdf_iloc, (x_m, y_m)), ...]
        -- position_in_ring is an index into the OPEN (no duplicate closing point)
        per-ring coordinate list, i.e. directly comparable to `_polygon_to_pslg`'s
        ring_ranges once it's called on the returned polygon. The caller uses this,
        once perimeter node ids exist, to add the waterway connector edge.
        """
        inland_gdf = self.gdfs.get("inland_waterways")
        if inland_gdf is None or inland_gdf.empty:
            return poly_m, set(), []

        poly_wgs84 = gpd.GeoSeries([poly_m], crs=utm_crs).to_crs(self.CRS_WGS84).iloc[0]
        try:
            cand_idx = list(inland_gdf.sindex.query(poly_wgs84, predicate="intersects"))
        except Exception:
            cand_idx = list(range(len(inland_gdf)))
        if not cand_idx:
            return poly_m, set(), []

        rings = [poly_m.exterior] + list(poly_m.interiors)
        ring_coords: List[List[Tuple[float, float]]] = []
        for ring in rings:
            coords = list(ring.coords)
            if len(coords) > 1 and coords[0] == coords[-1]:
                coords = coords[:-1]
            ring_coords.append(coords)

        # inland_gdf_iloc -> [(ring_idx, (x, y)), ...] raw crossing hits.
        per_line_hits = defaultdict(list)
        for line_iloc in cand_idx:
            line_wgs84 = inland_gdf.geometry.iloc[line_iloc]
            if line_wgs84 is None or line_wgs84.is_empty:
                continue
            line_m = gpd.GeoSeries([line_wgs84], crs=self.CRS_WGS84).to_crs(utm_crs).iloc[0]
            for ring_idx, ring in enumerate(rings):
                try:
                    inter = ring.intersection(line_m)
                except Exception:
                    continue
                if inter.is_empty:
                    continue
                for g in getattr(inter, "geoms", [inter]):
                    if g.geom_type == "Point":
                        per_line_hits[line_iloc].append((ring_idx, (g.x, g.y)))
                    # A LineString result (the waterway runs collinear with the
                    # boundary for a stretch) has no single clean crossing point
                    # to anchor a node on -- rare, deliberately skipped.

        if not per_line_hits:
            return poly_m, set(), []

        vertex_hits = [defaultdict(list) for _ in rings]   # ring_idx -> orig_i -> [inland_gdf_iloc]
        seg_inserts = [defaultdict(list) for _ in rings]   # ring_idx -> orig_seg_i -> [(t, (x,y), iloc)]
        extra_seam_coords = set()

        for line_iloc, hits in per_line_hits.items():
            # Dedupe crossings from the SAME line within WATERWAY_CROSSING_DEDUPE_M
            # of each other (typically 2 real crossings -- in/out -- per line per
            # piece; near-tangential digitization noise can otherwise cluster).
            kept = []
            for ring_idx, (x, y) in hits:
                if any(ri == ring_idx and math.hypot(x - kx, y - ky) < WATERWAY_CROSSING_DEDUPE_M
                       for ri, (kx, ky) in kept):
                    continue
                kept.append((ring_idx, (x, y)))
            if len(kept) > WATERWAY_CROSSING_CAP_PER_LINE:
                logger.info(f"  Waterway crossing cap: inland_waterways row {line_iloc} crosses "
                            f"one navmesh piece {len(kept)}x; capping to "
                            f"{WATERWAY_CROSSING_CAP_PER_LINE}.")
                kept = kept[:WATERWAY_CROSSING_CAP_PER_LINE]

            for ring_idx, (x, y) in kept:
                coords = ring_coords[ring_idx]
                n = len(coords)
                best_i, best_dist, best_t = None, None, 0.0
                for i in range(n):
                    ux, uy = coords[i]
                    vx, vy = coords[(i + 1) % n]
                    d = LineString([(ux, uy), (vx, vy)]).distance(Point(x, y))
                    if best_dist is None or d < best_dist:
                        seg_len2 = (vx - ux) ** 2 + (vy - uy) ** 2
                        t = 0.0 if seg_len2 == 0 else ((x - ux) * (vx - ux) + (y - uy) * (vy - uy)) / seg_len2
                        best_i, best_dist, best_t = i, d, t
                if best_i is None or best_dist > 0.5:
                    continue  # GEOS numerical edge case -- point should be on some segment.
                ux, uy = coords[best_i]
                vx, vy = coords[(best_i + 1) % n]
                if math.hypot(x - ux, y - uy) < WATERWAY_CROSSING_SNAP_M:
                    vertex_hits[ring_idx][best_i].append(line_iloc)
                    extra_seam_coords.add((round(ux, 3), round(uy, 3)))
                elif math.hypot(x - vx, y - vy) < WATERWAY_CROSSING_SNAP_M:
                    vertex_hits[ring_idx][(best_i + 1) % n].append(line_iloc)
                    extra_seam_coords.add((round(vx, 3), round(vy, 3)))
                else:
                    # Merge with any already-accepted insert on this ring (from
                    # ANY line -- the per-line dedupe above can't see other
                    # lines) within snap range: reuse the earlier point's exact
                    # coordinates instead of adding a near-duplicate vertex.
                    merged = False
                    for si, inserts in seg_inserts[ring_idx].items():
                        for t0, (px, py), _il in inserts:
                            if math.hypot(x - px, y - py) < WATERWAY_CROSSING_SNAP_M:
                                seg_inserts[ring_idx][si].append((t0, (px, py), line_iloc))
                                extra_seam_coords.add((round(px, 3), round(py, 3)))
                                merged = True
                                break
                        if merged:
                            break
                    if not merged:
                        seg_inserts[ring_idx][best_i].append((best_t, (x, y), line_iloc))
                        extra_seam_coords.add((round(x, 3), round(y, 3)))

        if not extra_seam_coords:
            return poly_m, set(), []

        new_rings_coords = []
        crossing_records = []
        for ring_idx, coords in enumerate(ring_coords):
            new_coords = []
            for i, c in enumerate(coords):
                new_coords.append(c)
                pos = len(new_coords) - 1
                for line_iloc in vertex_hits[ring_idx].get(i, []):
                    crossing_records.append((ring_idx, pos, line_iloc, c))
                for t, (x, y), line_iloc in sorted(seg_inserts[ring_idx].get(i, []), key=lambda r: r[0]):
                    if new_coords[-1] == (x, y):
                        # merged duplicate (same point, another line) -- record
                        # against the already-inserted vertex, don't re-insert
                        crossing_records.append((ring_idx, len(new_coords) - 1, line_iloc, (x, y)))
                    else:
                        crossing_records.append((ring_idx, len(new_coords), line_iloc, (x, y)))
                        new_coords.append((x, y))
            new_rings_coords.append(new_coords)

        # Last-line-of-defense guard: a degenerate (near-zero) segment anywhere
        # in the modified rings would segfault _triangle.triangulate at the C
        # level, which try/except cannot catch -- skip injection for this piece
        # entirely rather than risk the whole build.
        for coords in new_rings_coords:
            m = len(coords)
            for i in range(m):
                x1, y1 = coords[i]
                x2, y2 = coords[(i + 1) % m]
                if math.hypot(x2 - x1, y2 - y1) < 0.005:
                    logger.warning("  Waterway crossing injection produced a degenerate ring segment; "
                                   "skipping crossing injection for this navmesh piece.")
                    return poly_m, set(), []

        new_poly_m = Polygon(new_rings_coords[0], new_rings_coords[1:])
        if not new_poly_m.is_valid or new_poly_m.is_empty:
            logger.warning("  Waterway crossing insertion produced an invalid navmesh "
                            "boundary; skipping crossings for this piece.")
            return poly_m, set(), []

        return new_poly_m, extra_seam_coords, crossing_records

    def _connect_waterway_crossing(self, node_id, line_iloc, utm_crs, crossing_xy_m, line_m_cache):
        """Connect a crossing-derived boundary node to the nearest inland node of
        the SAME waterway line feature (`try_add`'s attrs, mirroring
        `_stitch_component_pieces` -- see that method). Returns the number of
        edges added (0 or 2).
        """
        inland_gdf = self.gdfs["inland_waterways"]
        if line_iloc not in line_m_cache:
            line_wgs84 = inland_gdf.geometry.iloc[line_iloc]
            coords_wgs84 = list(line_wgs84.coords)
            line_m = gpd.GeoSeries([line_wgs84], crs=self.CRS_WGS84).to_crs(utm_crs).iloc[0]
            line_m_cache[line_iloc] = (coords_wgs84, np.array(line_m.coords))
        coords_wgs84, coords_m = line_m_cache[line_iloc]

        x, y = crossing_xy_m
        d2 = (coords_m[:, 0] - x) ** 2 + (coords_m[:, 1] - y) ** 2
        nearest_idx = int(np.argmin(d2))
        nearest_dist_m = math.sqrt(d2[nearest_idx])

        if nearest_dist_m > WATERWAY_CONNECTOR_FALLBACK_MAX_M:
            logger.info(f"  Waterway crossing: nearest inland vertex of row {line_iloc} is "
                        f"{nearest_dist_m:.0f}m away (> {WATERWAY_CONNECTOR_FALLBACK_MAX_M:.0f}m); "
                        f"skipping connector.")
            return 0
        if nearest_dist_m > WATERWAY_CONNECTOR_MAX_M:
            logger.info(f"  Waterway crossing: nearest inland vertex of row {line_iloc} is "
                        f"{nearest_dist_m:.0f}m away (> {WATERWAY_CONNECTOR_MAX_M:.0f}m normal radius, "
                        f"within {WATERWAY_CONNECTOR_FALLBACK_MAX_M:.0f}m fallback); connecting anyway.")

        inland_lon, inland_lat = coords_wgs84[nearest_idx]
        inland_node_id = self._get_or_create_node(inland_lon, inland_lat, "inland", context="waterway_crossing")
        if inland_node_id == node_id:
            return 0
        crossing_lon = self.graph.nodes[node_id]["lon"]
        crossing_lat = self.graph.nodes[node_id]["lat"]
        candidate_wgs84 = LineString([(crossing_lon, crossing_lat), (inland_lon, inland_lat)])
        if self._crosses_land(candidate_wgs84):
            logger.info(f"  Waterway crossing: connector to inland row {line_iloc} crosses land; skipping.")
            return 0
        if self.graph.has_edge(node_id, inland_node_id):
            return 0
        attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                     source_tier=DEFAULT_SOURCE_TIER, source_id=None)
        self.graph.add_edge(node_id, inland_node_id, **attrs)
        self.graph.add_edge(inland_node_id, node_id, **attrs)
        return 2

    @staticmethod
    def _cap_reconnect_candidates_per_line(candidates, piece_label):
        """SPEC-GRAPH-DENSITY.md §6.3: apply the same sanity cap
        `_inject_waterway_crossings` enforces per (piece, line) (`WATERWAY_CROSSING_
        CAP_PER_LINE`) to axis-dedup's own carve-reconnect candidates -- shared by
        both the navmesh (`build_navmesh_region`) and skeleton (`build_skeleton_
        network`) carve-reconnect call sites. A carve boundary can hug one axis line
        for the width of a whole channel, attributing many perimeter/dead-end nodes
        to the same `line_iloc` within one piece; without this cap, that geometry --
        not real crossing multiplicity -- decides the connector count.

        `candidates`: iterable of `(node_id, line_iloc, xy_m)`, in the order found.
        Returns the same tuples, order preserved, truncated to at most
        `WATERWAY_CROSSING_CAP_PER_LINE` per distinct `line_iloc`.
        """
        by_line: Dict[int, list] = defaultdict(list)
        for candidate in candidates:
            by_line[candidate[1]].append(candidate)
        capped = []
        for line_iloc, group in by_line.items():
            if len(group) > WATERWAY_CROSSING_CAP_PER_LINE:
                logger.info(f"  Axis-dedup reconnect cap: inland_waterways row {line_iloc} has "
                            f"{len(group)} carve-induced dead ends on one {piece_label}; capping to "
                            f"{WATERWAY_CROSSING_CAP_PER_LINE}.")
                group = group[:WATERWAY_CROSSING_CAP_PER_LINE]
            capped.extend(group)
        return capped

    def build_navmesh_region(self, poly_m, utm_crs, seam_coord_set,
                             source_tier=DEFAULT_SOURCE_TIER, source_id=None,
                             carve_line_iloc_by_coord=None):
        """Triangulate one wide, navmesh-eligible water sub-polygon (already in
        metric CRS) and stage a `navmesh_regions` row for export. Registers EVERY
        vertex of the region's own perimeter (exterior + interior/island rings) as
        a literal graph node, connected in ring order (format spec §6's
        minimum-viable fallback) -- this guarantees the region's whole perimeter is
        one connected cycle, correctly, with no land-crossing risk, since it's
        tracing the polygon's own boundary rather than guessing straight-line
        shortcuts between boundary points. (A first version tried k-NN shortcuts
        plus radius-based stitching between just the seam vertices; that failed to
        fully connect large, non-convex regions, since a straight line between two
        boundary points on opposite sides of a headland or peninsula exits the
        polygon -- exactly the case the land-crossing containment check is
        supposed to catch, so it correctly rejected those chords, leaving the
        region internally fragmented no matter how large the search radius.)
        The subset of perimeter vertices that lie on the seam with a bordering
        skeleton piece is tracked separately as `boundary_node_ids`, used by
        `build_network`'s cross-piece `_stitch_component_pieces` call to connect
        this region into the rest of its original connected water body.

        `carve_line_iloc_by_coord` (SPEC-GRAPH-DENSITY.md §6.3.1, optional --
        `_axis_dedup_carve_navmesh_pieces`'s own return value, `None` for every other
        caller/test): `{(x, y): line_iloc}` for perimeter coordinates that sit on an
        axis-dedup carve boundary. Any registered perimeter node at one of those exact
        coordinates gets reconnected, via `_connect_waterway_crossing`, to the specific
        inland_waterways line responsible for carving that boundary -- otherwise a
        carve-induced dead end has no deliberate path back into the graph.
        """
        # DIAGNOSTIC (connectivity-regression investigation): tag every node this
        # call creates/touches with a unique per-region context so
        # _get_or_create_node can tell a genuine cross-piece coordinate merge
        # apart from this region's own perimeter nodes.
        self._piece_counter += 1
        piece_ctx = f"navmesh:{self._piece_counter}"
        # Coarse boundary-output simplify pass (§5.2.3 item 1, NAVMESH_BOUNDARY_SIMPLIFY_M's
        # docstring above has the full rationale) -- applied here, after the caller already
        # computed seam_coord_set from the un-simplified wide/narrow and deep/shallow
        # boundaries, because simplify() only ever removes vertices, never moves a
        # retained one, so exact-coordinate seam matching below still works on whatever
        # seam vertices survive this pass.
        simplified_poly_m = self._clean_polygonal(poly_m.buffer(0).simplify(NAVMESH_BOUNDARY_SIMPLIFY_M))
        simplified_pieces = self._explode_polygonal(simplified_poly_m)
        if not simplified_pieces:
            logger.warning("  Navmesh region boundary simplify collapsed the polygon; skipping region.")
            return
        poly_m = max(simplified_pieces, key=lambda p: p.area)

        # PSLG segment count is unbounded (it's the region's own perimeter, including
        # every island ring) and becomes a hard constraint for triangle's quality
        # refinement -- an insufficiently-simplified real-world coastline boundary is
        # a known way to trigger a combinatorial blow-up in output triangle/Steiner
        # count. Mirror _rasterize_water_polygon's MAX_RASTER_PIXELS pattern: shrink
        # the input (via increasing simplify tolerance) until it fits a fixed budget,
        # rather than letting triangulate() run unbounded.
        simplify_tol = 1.0
        for attempt in range(6):
            pslg, ring_ranges = self._polygon_to_pslg(poly_m)
            budget_used = len(pslg["vertices"]) + len(pslg["segments"])
            if budget_used <= NAVMESH_PSLG_BUDGET or attempt == 5:
                break
            simplify_tol *= 2
            simplified = self._clean_polygonal(poly_m.buffer(0).simplify(simplify_tol))
            pieces = self._explode_polygonal(simplified)
            if not pieces:
                break
            poly_m = max(pieces, key=lambda p: p.area)
            logger.info(f"  Navmesh region PSLG too large ({budget_used} verts+segs > "
                        f"{NAVMESH_PSLG_BUDGET}); retrying with simplify tolerance "
                        f"{simplify_tol:.1f}m.")

        # Round 14: crossing points MUST be computed against this exact poly_m --
        # the geometry `pslg`/`ring_ranges` above were just derived from, and
        # nothing simplifies it again after this point -- see
        # _inject_waterway_crossings' docstring for why an earlier (pre-simplify)
        # boundary won't do.
        poly_m, extra_seam_coords, crossing_records = self._inject_waterway_crossings(poly_m, utm_crs)
        if crossing_records:
            pslg, ring_ranges = self._polygon_to_pslg(poly_m)
            seam_coord_set = seam_coord_set | extra_seam_coords

        max_area = (self.navmesh_edge_m ** 2) * 0.433
        try:
            result = _triangle.triangulate(pslg, f"pq28a{max_area:.1f}n")
        except Exception as exc:
            logger.warning(f"  Navmesh triangulation failed on a polygon ({exc}); skipping region.")
            return
        if "triangles" not in result or len(result["triangles"]) == 0:
            logger.warning("  Navmesh triangulation produced no triangles; skipping region.")
            return
        if len(result["triangles"]) > NAVMESH_MAX_TRIANGLES:
            logger.warning(f"  Navmesh triangulation produced {len(result['triangles'])} triangles "
                            f"(> {NAVMESH_MAX_TRIANGLES}); retrying once with a coarser mesh.")
            coarse_area = max_area * 4
            try:
                retry = _triangle.triangulate(pslg, f"pq20a{coarse_area:.1f}n")
            except Exception as exc:
                logger.warning(f"  Coarser navmesh retry failed ({exc}); skipping region.")
                return
            if "triangles" not in retry or len(retry["triangles"]) == 0:
                logger.warning("  Coarser navmesh retry produced no triangles; skipping region.")
                return
            result = retry

        out_vertices = result["vertices"]
        out_triangles = result["triangles"]
        out_neighbors = result["neighbors"]
        n_input = len(pslg["vertices"])

        pts_wgs84 = gpd.GeoSeries(gpd.points_from_xy(out_vertices[:, 0], out_vertices[:, 1]),
                                  crs=utm_crs).to_crs(self.CRS_WGS84)
        lons, lats = pts_wgs84.x.to_numpy(), pts_wgs84.y.to_numpy()

        perimeter_node_ids = [None] * n_input
        boundary_node_ids = []
        # SPEC-GRAPH-DENSITY.md §6.3.1: (node_id, line_iloc, (x, y)) for perimeter
        # nodes that sit on a carve boundary -- collected here (same x, y already in
        # hand, zero extra cost) but connected in a SECOND pass below, after every
        # perimeter node id exists (mirrors why the existing crossing_records connector
        # pass below already runs after this loop, not inside it).
        carve_connect_candidates = []
        for start, count in ring_ranges:
            for k in range(count):
                i = start + k
                node_id = self._get_or_create_node(float(lons[i]), float(lats[i]), "coastal", context=piece_ctx)
                self._stamp_node(node_id, NODE_KIND_NAVMESH_VERTEX, source_tier, source_id)
                perimeter_node_ids[i] = node_id
                x, y = pslg["vertices"][i]
                if (round(x, 3), round(y, 3)) in seam_coord_set:
                    boundary_node_ids.append(node_id)
                    self.navmesh_seam_node_ids.add(node_id)
                if carve_line_iloc_by_coord:
                    line_iloc = carve_line_iloc_by_coord.get((round(x, 3), round(y, 3)))
                    if line_iloc is not None:
                        carve_connect_candidates.append((node_id, line_iloc, (x, y)))
            attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                         source_tier=source_tier, source_id=source_id)
            for k in range(count):
                u = perimeter_node_ids[start + k]
                v = perimeter_node_ids[start + (k + 1) % count]
                if u != v and not self.graph.has_edge(u, v):
                    self.graph.add_edge(u, v, **attrs)
                    self.graph.add_edge(v, u, **attrs)

        # Shared across both connector mechanisms below: a line that both crosses this
        # piece's boundary (_inject_waterway_crossings) AND caused a carve on this same
        # piece then only pays reprojection cost once. Declared unconditionally (was
        # only inside `if crossing_records:`) since carve reconnect can fire on its own.
        line_m_cache: Dict[int, Tuple[list, np.ndarray]] = {}

        if crossing_records:
            crossing_node_ids = set()
            edges_added = 0
            for ring_idx, pos_in_ring, line_iloc, xy_m in crossing_records:
                start, count = ring_ranges[ring_idx]
                global_idx = start + pos_in_ring
                cnode_id = perimeter_node_ids[global_idx]
                if cnode_id is None:
                    continue
                crossing_node_ids.add(cnode_id)
                edges_added += self._connect_waterway_crossing(cnode_id, line_iloc, utm_crs, xy_m, line_m_cache)
            if crossing_node_ids:
                self.waterway_crossing_stats["regions"] += 1
                self.waterway_crossing_stats["nodes"] += len(crossing_node_ids)
                self.waterway_crossing_stats["edges"] += edges_added

        if carve_connect_candidates:
            connected_node_ids = set()
            edges_added = 0
            capped = self._cap_reconnect_candidates_per_line(carve_connect_candidates, "navmesh piece")
            for node_id, line_iloc, xy_m in capped:
                connected_node_ids.add(node_id)
                edges_added += self._connect_waterway_crossing(node_id, line_iloc, utm_crs, xy_m, line_m_cache)
            if connected_node_ids:
                self.axis_dedup_reconnect_stats["navmesh_candidates"] += len(connected_node_ids)
                self.axis_dedup_reconnect_stats["navmesh_edges"] += edges_added

        boundary_geom_wgs84 = gpd.GeoSeries([poly_m], crs=utm_crs).to_crs(self.CRS_WGS84).iloc[0]
        self.navmesh_region_rows.append({
            "boundary_geometry": json.dumps(mapping(boundary_geom_wgs84)),
            "vertices": json.dumps([[float(lat), float(lon)] for lat, lon in zip(lats, lons)]),
            "triangles": json.dumps(out_triangles.tolist()),
            "triangle_adjacency": json.dumps(out_neighbors.tolist()),
            "boundary_node_ids": json.dumps(boundary_node_ids),
            "depth_ceiling_m": self.classification_config.depth_ceiling_m,
            "source_tier": source_tier,
            "source_id": source_id,
        })

    def _drying_gdf(self):
        """Depth-area polygons charted as drying/intertidal (DRVAL1 < 0.0), cached
        once per pipeline run. Used alongside the `land` layer by `_crosses_land`
        for genuine land-crossing safety checks.

        `land` and `depth_areas` (DEPARE) are digitized independently, same as
        `land`/`coastal_water` -- a stretch of charted drying tidal flat can sit
        entirely inside `coastal_water`'s own polygon footprint (nowhere near the
        `land` layer) while still being genuinely unsafe for a connectivity-
        critical straight-chord edge to cross. Confirmed on real data (Round 9
        master-finding investigation): a stored navmesh_boundary edge had
        `drval1=-2.0` (charted drying) but `crosses_land=0`, because the
        land-only check never saw it.
        """
        if getattr(self, "_drying_gdf_cache", None) is not None:
            return self._drying_gdf_cache
        depth_gdf = self.gdfs.get("depth_areas", gpd.GeoDataFrame())
        if depth_gdf is None or depth_gdf.empty or "DRVAL1" not in depth_gdf.columns:
            self._drying_gdf_cache = gpd.GeoDataFrame(geometry=[], crs=self.CRS_WGS84)
            return self._drying_gdf_cache
        drval = pd.to_numeric(depth_gdf["DRVAL1"], errors="coerce")
        drying = depth_gdf[drval < 0.0].copy()
        if not drying.empty:
            drying["geometry"] = drying.geometry.make_valid()
            drying = drying[drying.geometry.notnull() & drying.geometry.is_valid]
        self._drying_gdf_cache = drying
        return self._drying_gdf_cache

    def _crosses_land(self, line_wgs84) -> bool:
        """True if a WGS84 LineString genuinely intersects the separate `land`
        layer, OR a charted drying/intertidal DEPARE polygon (see `_drying_gdf`).

        `land` is digitized independently from `coastal_water`, so the two don't
        always agree at the vertex level -- `_sanity_check_no_land_crossings` strips
        any edge that intersects `land_gdf` regardless of what a caller's own
        containment check against a water polygon concluded, so callers that
        generate connectivity-critical edges (ring-perimeter, stitching) should
        pre-check against this same layer to avoid building an edge only to have it
        stripped later, re-fragmenting whatever it was bridging. Also checks
        against drying/intertidal terrain, not just the `land` layer -- see
        `_drying_gdf`'s docstring for why that's a separate, real gap.
        """
        land_gdf = self.gdfs.get("land", gpd.GeoDataFrame())
        if land_gdf is not None and not land_gdf.empty:
            try:
                if len(land_gdf.sindex.query(line_wgs84, predicate="intersects")) > 0:
                    return True
            except Exception:
                if bool(land_gdf.intersects(line_wgs84).any()):
                    return True
        drying_gdf = self._drying_gdf()
        if drying_gdf is not None and not drying_gdf.empty:
            try:
                return len(drying_gdf.sindex.query(line_wgs84, predicate="intersects")) > 0
            except Exception:
                return bool(drying_gdf.intersects(line_wgs84).any())
        return False

    def _stitch_component_pieces(self, node_ids, component_polygon_wgs84, snap_radius_m: float = 500.0) -> int:
        """Reconnect nodes from independently-built skeleton/navmesh pieces that were
        all exploded from the SAME original connected water body (see build_network).

        Exact-coordinate seam matching (`_seam_coord_set`) only reconnects a narrow
        piece to a WIDE piece it borders, and only via the wide piece's raw polygon
        boundary vertices. Skeleton medial-axis endpoints are derived from raster
        pruning instead, so they don't land on identical coordinates even where two
        pieces of the same original water body are genuinely adjacent (narrow-to-
        narrow, or narrow-to-wide when the raster endpoint falls short of the exact
        boundary). Left unfixed, the width-based split fragments one connected water
        body into many disconnected graph components -- this stitches nearby nodes
        back together, scoped to this one component's own node set, and only where
        the straight connector stays inside this component's own polygon (never
        bridging to a merely-nearby, unrelated water body across a spit of land).

        Default radius is set above `min_navmesh_radius_m` (300m): the wide/narrow
        seam is reconstructed from an eroded-then-dilated buffer, which rounds off
        sharp channel-mouth corners, so the seam can sit up to roughly one erosion
        radius away from where the (un-eroded) skeleton's medial axis actually
        terminates -- a smaller radius left most real seams unbridged in testing.
        Connectivity-regression investigation (see the STITCH-DENSITY INVESTIGATION note's
        comment): widening this to 700m when the sagitta resampler is active
        raised Pass 0/0b/0c's raw success counts (free in node-count terms --
        it only widens the SEARCH try_add's own poly-containment/
        _crosses_land safety gates still check), but largest-component
        connectivity did not follow, and Pass 2 -- the pass that actually is
        union-find gated, i.e. the one whose success count controls this
        metric -- got WORSE (27 baseline -> 7 with this alone, worse than the
        12-14 measured without it). Reverted to a flat 500m pending a real
        fix for Pass 2 / gap-resolve specifically.
        """
        ids = list(node_ids)
        if len(ids) < 2:
            return 0

        # Union-find seeded from these nodes' EXISTING edges (skeleton chains, navmesh
        # fallback edges already built) -- only pairs still in different groups after
        # that need stitching. Without this, a radius-based pass reconnects thousands of
        # already-connected same-chain neighbors too (any point along a winding skeleton
        # has plenty of other chain points within the snap radius), which both wastes
        # work and creates spurious chords that cut across land on a tight bend --
        # exactly what drove the land-crossing strip rate up when this was tried without
        # the union-find guard.
        parent = {n: n for n in ids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b) -> bool:
            ra, rb = find(a), find(b)
            if ra == rb:
                return False
            parent[ra] = rb
            return True

        id_set = set(ids)
        for n in ids:
            for nbr in self.graph.neighbors(n):
                if nbr in id_set:
                    union(n, nbr)

        initial_groups = len({find(n) for n in ids})
        if initial_groups <= 1:
            return 0  # already one connected piece
        self._stitch_group_stats["initial_groups"] += initial_groups

        coords_wgs84 = np.array([(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"]) for n in ids])
        utm = self._local_utm_crs(component_polygon_wgs84)
        pts_m = gpd.GeoSeries(gpd.points_from_xy(coords_wgs84[:, 0], coords_wgs84[:, 1]),
                              crs=self.CRS_WGS84).to_crs(utm)
        coords_m = np.column_stack([pts_m.x.to_numpy(), pts_m.y.to_numpy()])
        poly_m = gpd.GeoSeries([component_polygon_wgs84], crs=self.CRS_WGS84).to_crs(utm).iloc[0].buffer(2.0)
        # Prepared geometry for the containment gate: `line.within(poly_m)`
        # against a full-component water polygon (the Oosterschelde component's
        # exterior+island rings carry an enormous vertex count) re-walks that
        # entire boundary per candidate. shapely.prepared builds the polygon's
        # edge index once, making each `poly_prep.contains(line)` (exactly
        # equivalent to `line.within(poly_m)`, since a.within(b) <=>
        # b.contains(a)) cheap. Pass 0c below no longer stops at first
        # connectivity, so it evaluates orders of magnitude more candidates
        # than try_add's union-find-gated passes ever did -- without this,
        # a full Zeeland run stalled indefinitely inside component 1.
        from shapely.prepared import prep
        poly_prep = prep(poly_m)

        added = 0

        def try_add(i: int, j: int, pass_name: str = "pass0") -> bool:
            nonlocal added
            diag = self._stitch_diag[pass_name]
            diag["calls"] += 1
            u, v = ids[i], ids[j]
            if find(u) == find(v):
                diag["already_connected"] += 1
                return False
            candidate_m = LineString([coords_m[i], coords_m[j]])
            if not poly_prep.contains(candidate_m):
                diag["poly_reject"] += 1
                return False
            if self._crosses_land(LineString([coords_wgs84[i], coords_wgs84[j]])):
                diag["land_reject"] += 1
                return False
            attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                         source_tier=DEFAULT_SOURCE_TIER, source_id=None)
            self.graph.add_edge(u, v, **attrs)
            self.graph.add_edge(v, u, **attrs)
            union(u, v)
            added += 2
            diag["success"] += 1
            return True

        # Pass 0: k-nearest-neighbor pass, independent of Pass 1's
        # MAX_IDS_FOR_PASS1 gate. That gate exists because materializing every
        # coastal-node pair within snap_radius_m (tree.query_pairs()) blows up
        # combinatorially over tens of thousands of densely-spaced nodes -- but
        # a k-nearest-neighbor *query* (not an all-pairs-within-radius scan)
        # costs O(log n) per point regardless of how many nodes exist in
        # total, so it's safe to run unconditionally over every node in the
        # component, not just a pre-tagged subset -- try_add's union-find
        # check (`find(u) == find(v)`) rejects an already-connected pair
        # before any of the expensive shapely checks run, so this doesn't
        # reintroduce Pass 1's "thousands of already-connected same-chain
        # neighbors" cost: those pairs are almost always already unified via
        # their own chain/ring edges and get skipped in O(1).
        #
        # Originally this queried only navmesh_seam_node_ids (perimeter
        # vertices tagged on a real narrow/wide seam, or dead-end skeleton
        # chain nodes) -- Round 4 found that necessary for a real, ~30m gap
        # at the Zeelandbrug bridge opening (see below). Round 6 found that
        # tagged-seam-only restriction itself a gap: a skeleton chain's
        # *mid-chain* node (degree 2, never a dead end, never seam-tagged)
        # can pass within a stone's throw of a navmesh region's boundary --
        # not at the exact coordinate `_seam_coord_set` recorded for that
        # region's own true narrow/wide split -- while the two pieces are
        # still only connected, overall, via some much longer route
        # elsewhere in the component. Nothing before this pass ever
        # considered that specific pair, since neither side was tagged as a
        # seam node: the *component* was already technically fully
        # connected (satisfying every pass's actual goal), so the guarantee
        # passes below had no reason to also add this shorter, more direct
        # connector. Confirmed on a real full-scale reproduction: a skeleton
        # node 88m from a navmesh boundary node had no edge between them,
        # forcing a real route to detour ~20km round-trip through the one
        # place a connection *did* exist instead. Querying every node (not
        # just tagged seam nodes) as both source and target catches this
        # class of gap directly.
        #
        # Round 4's original finding, still true: at full-country scale a
        # single component split into thousands of initial union-find groups
        # (e.g. 8440 for the Zeeland/Oosterschelde body -- one per navmesh
        # region ring, per island ring, per skeleton chain), which floors
        # Pass 2's per-group sample cap to 1 regardless of group size. A
        # real, ~30m gap at the Zeelandbrug bridge opening sat in a 21-node
        # group alongside other seam nodes; whichever single member Pass 2
        # happened to sample every round (the same one, all 30 rounds --
        # nothing rotates the pick) was never the right one, so the
        # connector was never found despite being trivially within
        # snap_radius_m. This pass sidesteps group sampling entirely: every
        # node gets to look at its own nearest neighbors directly.
        pass0_idx_list = list(range(len(ids)))
        if len(pass0_idx_list) >= 2:
            from scipy.spatial import cKDTree
            pass0_coords = coords_m[pass0_idx_list]
            pass0_tree = cKDTree(pass0_coords)
            k = min(6, len(pass0_idx_list))
            _, neighbor_local_idxs = pass0_tree.query(pass0_coords, k=k)
            if k == 1:
                neighbor_local_idxs = neighbor_local_idxs.reshape(-1, 1)
            for local_i, neighbors in enumerate(neighbor_local_idxs):
                gi = pass0_idx_list[local_i]
                for local_j in neighbors:
                    if local_j == local_i:
                        continue
                    gj = pass0_idx_list[int(local_j)]
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > snap_radius_m:
                        self._stitch_diag["pass0"]["radius_reject"] += 1
                        continue
                    try_add(gi, gj, "pass0")

        # Pass 0b: cross-type k-nearest-neighbor pass, navmesh perimeter
        # vertices against everything else (skeleton chain nodes, etc).
        # Pass 0 above queries each node's k=6 nearest neighbors regardless
        # of type -- inside a densely triangulated navmesh region (a real
        # region in this dataset has up to 2,877 perimeter vertices packed a
        # few meters apart), a node's own top-6 nearest neighbors are almost
        # always same-type immediate neighbors, which crowds out the one
        # cross-type connector that might be 50-100m away and actually
        # needed. Confirmed on a real full-scale reproduction: a skeleton
        # node 94.8m from that region's nearest boundary node had neither
        # node's top-6 list include the other (both lists were full of
        # same-type points closer than 94.8m), so Pass 0 never tried the
        # pair despite it being well within snap_radius_m -- forcing a real
        # route to detour ~20km round-trip through the one place a
        # connection did exist. Splitting the KNN query by type (every
        # skeleton/other node looks at its k nearest navmesh vertices, and
        # vice versa, via two separate KD-trees) guarantees the true
        # nearest cross-type candidate is always considered, independent of
        # same-type local density on either side. node_kind_id is already
        # stamped NODE_KIND_NAVMESH_VERTEX for every navmesh perimeter
        # vertex (build_navmesh_region), including ones never seam-tagged,
        # so no new bookkeeping is needed to tell the two groups apart.
        navmesh_idx = [i for i, n in enumerate(ids)
                       if self.graph.nodes[n].get("node_kind_id", NODE_KIND_POINT) == NODE_KIND_NAVMESH_VERTEX]
        other_idx = [i for i in range(len(ids)) if i not in set(navmesh_idx)]
        if navmesh_idx and other_idx:
            from scipy.spatial import cKDTree
            navmesh_coords = coords_m[navmesh_idx]
            other_coords = coords_m[other_idx]
            navmesh_tree = cKDTree(navmesh_coords)
            other_tree = cKDTree(other_coords)

            k_navmesh = min(6, len(navmesh_idx))
            _, nn_idxs = navmesh_tree.query(other_coords, k=k_navmesh)
            if k_navmesh == 1:
                nn_idxs = nn_idxs.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_idxs):
                gi = other_idx[local_i]
                for local_j in neighbors:
                    gj = navmesh_idx[int(local_j)]
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > snap_radius_m:
                        self._stitch_diag["pass0b"]["radius_reject"] += 1
                        continue
                    try_add(gi, gj, "pass0b")

            k_other = min(6, len(other_idx))
            _, nn_idxs2 = other_tree.query(navmesh_coords, k=k_other)
            if k_other == 1:
                nn_idxs2 = nn_idxs2.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_idxs2):
                gi = navmesh_idx[local_i]
                for local_j in neighbors:
                    gj = other_idx[int(local_j)]
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > snap_radius_m:
                        self._stitch_diag["pass0b"]["radius_reject"] += 1
                        continue
                    try_add(gi, gj, "pass0b")

            # Pass 0c: LOCAL adjacency guarantee for navmesh perimeter
            # vertices -- unlike every pass above (and try_add's own
            # `find(u) == find(v)` guard), this one deliberately does NOT
            # stop once the region is globally connected. Confirmed on a
            # real full-scale Zeeland build: region idx 14 (85 boundary
            # nodes ringing ~5km of open Oosterschelde) ended up with
            # exactly 7 edges out to the rest of the graph, every one of
            # them on the region's west side, despite 85 skeleton nodes
            # sitting only 100-200m to its east -- well inside
            # snap_radius_m. The reason: the moment Pass 0b's first
            # west-side connector fires, union() merges the ENTIRE 85-node
            # perimeter ring (already one union-find group via its own ring
            # edges) into the main component in one step. Every other
            # genuinely-nearby eastward candidate Pass 0b considers after
            # that is then rejected by try_add as "already connected", even
            # though none of those pairs share an actual edge. Global
            # connectivity was satisfied by one pinch point, but the region
            # still routes like a bag with a single drawstring: a real
            # route crossing it has to walk the whole boundary ring out to
            # that one point and back. A pure-distance Dijkstra over the
            # resulting graph (ignoring all soft routing penalties) still
            # returned 4x the straight-line distance for a crossing through
            # this exact region -- proof this is a missing-edge defect, not
            # a routing-cost one.
            #
            # The fix: for each navmesh vertex, add cross-type connectors
            # even when already unified, bounded by a per-node cap on
            # cross-type external connectors (pre-existing ones count
            # toward the cap too, so already-well-attached nodes are
            # skipped after one O(degree) check) instead of a union-find
            # check. This keeps the pass from re-creating the "thousands of
            # spurious chords" problem Pass 0's docstring warns about,
            # while still giving every perimeter vertex a real chance at
            # its own nearest cross-type neighbour rather than only the
            # first vertex to get merged.
            MAX_CROSS_CONNECTORS_PER_NAVMESH_NODE = 2

            def _existing_cross_connector_count(node_id) -> int:
                return sum(
                    1 for nbr in self.graph.neighbors(node_id)
                    if self.graph.nodes[nbr].get("node_kind_id", NODE_KIND_POINT) != NODE_KIND_NAVMESH_VERTEX
                )

            cross_count = {ids[gi]: _existing_cross_connector_count(ids[gi]) for gi in navmesh_idx}

            def try_add_local(i: int, j: int, pass_name: str = "pass0c") -> bool:
                """Same safety gates and edge attrs as try_add, but WITHOUT
                the union-find gate -- see the Pass 0c comment above."""
                nonlocal added
                diag = self._stitch_diag[pass_name]
                diag["calls"] += 1
                u, v = ids[i], ids[j]
                if self.graph.has_edge(u, v):
                    diag["already_edge"] += 1
                    return False
                candidate_m = LineString([coords_m[i], coords_m[j]])
                if not poly_prep.contains(candidate_m):
                    diag["poly_reject"] += 1
                    return False
                if self._crosses_land(LineString([coords_wgs84[i], coords_wgs84[j]])):
                    diag["land_reject"] += 1
                    return False
                attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                             source_tier=DEFAULT_SOURCE_TIER, source_id=None)
                self.graph.add_edge(u, v, **attrs)
                self.graph.add_edge(v, u, **attrs)
                union(u, v)
                added += 2
                diag["success"] += 1
                return True

            # Direction A: each navmesh vertex -> its k nearest cross-type
            # neighbours (cKDTree.query already returns them nearest-first),
            # stopping as soon as that vertex's own cap is met. Reuses the
            # other_tree/navmesh_coords built for Pass 0b above.
            k_cross = min(6, len(other_idx))
            _, nn_local = other_tree.query(navmesh_coords, k=k_cross)
            if k_cross == 1:
                nn_local = nn_local.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_local):
                if local_i and local_i % 2000 == 0:
                    logger.info(f"  Stitch Pass 0c (local adjacency): {local_i}/{len(navmesh_idx)} "
                                f"navmesh vertices processed, {added} stitch edges added so far.")
                gi = navmesh_idx[local_i]
                u = ids[gi]
                if cross_count[u] >= MAX_CROSS_CONNECTORS_PER_NAVMESH_NODE:
                    continue
                for local_j in neighbors:
                    if cross_count[u] >= MAX_CROSS_CONNECTORS_PER_NAVMESH_NODE:
                        break
                    gj = other_idx[int(local_j)]
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > snap_radius_m:
                        self._stitch_diag["pass0c"]["radius_reject"] += 1
                        continue
                    if try_add_local(gi, gj):
                        cross_count[u] += 1

            # Direction B (symmetric): each non-navmesh node -> its k
            # nearest navmesh vertices. Direction A alone caps how many
            # external connectors each navmesh vertex GAINS, but a node's
            # own k-nearest-neighbour list is still just one node's view --
            # without also querying from the other side, an "other" node
            # that sits equidistant between two perimeter vertices might
            # only ever appear in one of their neighbour lists, silently
            # leaving one side under-attached even though a valid connector
            # exists. Querying from both directions and letting each side's
            # own cap gate it is what actually spreads attachment points
            # around the full perimeter rather than clustering them near
            # whichever "other" nodes happen to be geometrically central.
            k_navmesh_b = min(6, len(navmesh_idx))
            _, nn_local_b = navmesh_tree.query(other_coords, k=k_navmesh_b)
            if k_navmesh_b == 1:
                nn_local_b = nn_local_b.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_local_b):
                if local_i and local_i % 5000 == 0:
                    logger.info(f"  Stitch Pass 0c (local adjacency, direction B): {local_i}/{len(other_idx)} "
                                f"non-navmesh nodes processed, {added} stitch edges added so far.")
                gi = other_idx[local_i]
                for local_j in neighbors:
                    gj = navmesh_idx[int(local_j)]
                    u = ids[gj]
                    if cross_count[u] >= MAX_CROSS_CONNECTORS_PER_NAVMESH_NODE:
                        continue
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > snap_radius_m:
                        self._stitch_diag["pass0c"]["radius_reject"] += 1
                        continue
                    if try_add_local(gj, gi):
                        cross_count[u] += 1

        # Pass 0d (Round 15, NEXT_PHASES.md §5.2.2): LOCAL adjacency guarantee for
        # in-polygon inland nodes, generalizing Pass 0c to a second cross-type
        # pairing (inland vs everything else) rather than widening Pass 0c's own
        # navmesh-vs-other split to a three-way one -- keeping the two passes
        # separate lets each keep its own cap/radius tuned to its own real gap
        # (Pass 0c: 100-200m against a big navmesh perimeter's own crowding;
        # this one: §5.2.2's narrower ~300m inland/coastal physical-interface
        # radius) without the cross terms interacting.
        #
        # _ensure_coastal_connectivity only ever adds an inland node to `ids`
        # when its coordinate falls INSIDE this component's own coastal_water
        # polygon -- per §5.2.2's physically-grounded rule, an inland-typed
        # vertex sitting in what's charted as open coastal water is a
        # source-layering artifact (the inland waterway line-work happens to run
        # through/near a stretch also covered by the coastal polygon), not a
        # real barrier, so linking it locally to nearby coastal nodes is safe.
        # An inland node behind a lock or up a canal reach never satisfies that
        # containment test in the caller, so it never reaches this pass -- this
        # cannot manufacture a lock bypass. Confirmed real gap this closes: an
        # inland waterway node sitting 33m from a coastal skeleton node, with no
        # edge between them, forced a hard-constrained route into a ~100km
        # detour around a bridge whose direct opening required an air-draft
        # violation (Round 14 probe finding).
        inland_idx = [i for i, n in enumerate(ids) if self.graph.nodes[n].get("node_type") == "inland"]
        noninland_idx = [i for i in range(len(ids)) if i not in set(inland_idx)]
        if inland_idx and noninland_idx:
            from scipy.spatial import cKDTree
            inland_coords = coords_m[inland_idx]
            noninland_coords = coords_m[noninland_idx]
            inland_tree = cKDTree(inland_coords)
            noninland_tree = cKDTree(noninland_coords)

            MAX_LOCAL_CONNECTORS_PER_INLAND_NODE = 2
            # Connectivity-regression investigation (see the STITCH-DENSITY INVESTIGATION note's
            # comment): widening this to 500m when sagitta is active DID raise
            # Pass 0d's raw success count above the un-resampled baseline
            # (4835 -> 4854 on data/zeeland_clip), but largest-component
            # connectivity did not follow -- Pass 0d isn't union-find gated
            # (see Pass 0c's docstring), so most of that gain is redundant
            # edges inside already-merged groups, not new distinct merges. Left
            # at the original 300m pending a real fix for Pass 2 / gap-resolve,
            # the two passes that ARE union-find gated and that measurement
            # showed still degraded (Pass 2 success 27 -> 7-14 across every
            # combination tried) regardless of this radius.
            INLAND_LOCAL_RADIUS_M = 300.0

            def _existing_noninland_count(node_id) -> int:
                return sum(
                    1 for nbr in self.graph.neighbors(node_id)
                    if self.graph.nodes[nbr].get("node_type") != "inland"
                )

            inland_cross_count = {ids[gi]: _existing_noninland_count(ids[gi]) for gi in inland_idx}

            def try_add_inland_local(i: int, j: int) -> bool:
                """Same safety gates/attrs as try_add_local -- see Pass 0c above."""
                nonlocal added
                diag = self._stitch_diag["pass0d"]
                diag["calls"] += 1
                u, v = ids[i], ids[j]
                if self.graph.has_edge(u, v):
                    diag["already_edge"] += 1
                    return False
                candidate_m = LineString([coords_m[i], coords_m[j]])
                if not poly_prep.contains(candidate_m):
                    diag["poly_reject"] += 1
                    return False
                if self._crosses_land(LineString([coords_wgs84[i], coords_wgs84[j]])):
                    diag["land_reject"] += 1
                    return False
                attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                             source_tier=DEFAULT_SOURCE_TIER, source_id=None)
                self.graph.add_edge(u, v, **attrs)
                self.graph.add_edge(v, u, **attrs)
                union(u, v)
                added += 2
                diag["success"] += 1
                return True

            # Direction A: each inland node -> its k nearest non-inland neighbours,
            # stopping as soon as that node's own cap is met.
            k_cross = min(6, len(noninland_idx))
            _, nn_local = noninland_tree.query(inland_coords, k=k_cross)
            if k_cross == 1:
                nn_local = nn_local.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_local):
                gi = inland_idx[local_i]
                u = ids[gi]
                if inland_cross_count[u] >= MAX_LOCAL_CONNECTORS_PER_INLAND_NODE:
                    continue
                for local_j in neighbors:
                    if inland_cross_count[u] >= MAX_LOCAL_CONNECTORS_PER_INLAND_NODE:
                        break
                    gj = noninland_idx[int(local_j)]
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > INLAND_LOCAL_RADIUS_M:
                        self._stitch_diag["pass0d"]["radius_reject"] += 1
                        continue
                    if try_add_inland_local(gi, gj):
                        inland_cross_count[u] += 1

            # Direction B (symmetric): each non-inland node -> its k nearest inland
            # nodes -- same rationale as Pass 0c's Direction B: a node's own
            # k-nearest-neighbour list is only one side's view, so an inland node
            # equidistant between two coastal nodes might not appear in either
            # coastal node's own top-k unless queried from that side too.
            k_inland = min(6, len(inland_idx))
            _, nn_local_b = inland_tree.query(noninland_coords, k=k_inland)
            if k_inland == 1:
                nn_local_b = nn_local_b.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_local_b):
                gi = noninland_idx[local_i]
                for local_j in neighbors:
                    gj = inland_idx[int(local_j)]
                    u = ids[gj]
                    if inland_cross_count[u] >= MAX_LOCAL_CONNECTORS_PER_INLAND_NODE:
                        continue
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > INLAND_LOCAL_RADIUS_M:
                        self._stitch_diag["pass0d"]["radius_reject"] += 1
                        continue
                    if try_add_inland_local(gj, gi):
                        inland_cross_count[u] += 1

        # Pass 1: cheap radius-limited KD-tree pass, handles the common case of a
        # small local gap between two adjacent pieces. SKIPPED for large node counts:
        # `tree.query_pairs()` materializes EVERY pair within radius before anything
        # can be capped -- at full-dataset scale (tens of thousands of densely-spaced
        # coastal nodes in one call from `_ensure_coastal_connectivity`) that pair set
        # itself reached tens of millions of Python tuples and exhausted 15GB of RAM,
        # confirmed against the real machine, not just a slow-code guess. Pass 2 below
        # is bounded by component *count* via MAX_TOTAL_SAMPLES regardless of total
        # node count, so it alone carries the load at that scale.
        MAX_IDS_FOR_PASS1 = 4000
        if len(ids) <= MAX_IDS_FOR_PASS1:
            from scipy.spatial import cKDTree
            tree = cKDTree(coords_m)
            for i, j in tree.query_pairs(snap_radius_m):
                try_add(i, j, "pass1")

        # Pass 2: guarantee, one merge round at a time. A region's exterior ring and
        # each interior (island) ring are separate cycles after ring-adjacency edges
        # alone (build_navmesh_region) -- they don't share vertices, so a region with
        # many islands starts as that many separate components here, and a boundary
        # that loops back close to itself (e.g. around a peninsula) can *also* leave
        # locally-dense clusters more than snap_radius_m apart along the direct path
        # between them. Repeatedly merge the two components with the globally nearest
        # valid (in-polygon) connector.
        #
        # Connectivity-regression investigation and fix (see the STITCH-DENSITY INVESTIGATION note's
        # comment for the earlier rounds of this investigation): Pass 2's success
        # count roughly HALVED under sagitta resampling (27 -> 12-14 on
        # data/zeeland_clip) even though total stitching edges elsewhere recovered
        # to at-or-above baseline with other fixes -- each Pass 2 success is a real
        # distinct group merge (union-find gated, unlike Pass 0c/0d), making this
        # the single most consequential deficit measured, and the one that
        # actually controls largest-component connectivity.
        #
        # ROOT CAUSE (superseding the version of this comment that shipped
        # inert): this pass used to pick a FIXED number of representative nodes
        # per union-find group (`MAX_TOTAL_SAMPLES // len(groups)`, floored to at
        # least 1) via list-order striding (`member_idxs[::step][:cap]`), then ran
        # a full pairwise `cdist` over just those representatives. Whether two
        # groups merged depended on whether their arbitrary representatives
        # happened to be close, not on whether the groups had a close connector
        # ANYWHERE along their real boundary. Instrumented directly (logging
        # `len(groups)`/`per_group_cap` per round) on this exact clip: the
        # dominant, most-fragmented component enters this pass with ~380-410
        # groups (NOT the ~1050-1095 an earlier round of this investigation
        # assumed -- that figure was the union-find group count from BEFORE
        # Pass 0/0b/0c/0d run, most of which they already merge away; the
        # earlier per_group_cap==1 diagnosis is REFUTED). Real per_group_cap on
        # that component is 3-4, both with and without resampling, against
        # average group sizes of 45-73 nodes -- still only a handful of
        # arbitrarily-chosen points standing in for each group, which is why
        # raising MAX_TOTAL_SAMPLES 1500 -> 4000 (cap 3-4 -> roughly 9-10) never
        # moved Pass 2's success count: more arbitrary strided picks per group is
        # not the same as picking the actual nearest one.
        #
        # FIX: replace per-group sampling with a per-NODE nearest-cross-group
        # search over a single cKDTree built on EVERY node in this component (no
        # sampling at all), escalating each node's k only when its immediate
        # neighbourhood turns out to be entirely same-group -- the identical
        # discovery strategy `_resolve_local_skeleton_gaps` below already uses
        # safely at full-country scale, just without that function's fixed
        # LOCAL_GAP_RESOLVE_MAX_M distance cap, since Pass 2's whole job is to
        # guarantee connectivity regardless of how far apart two groups' nearest
        # points are. This finds the geometrically NEAREST cross-group connector
        # for every node, not the nearest pair among a handful of arbitrary
        # per-group picks, and costs O(n log n) per round rather than
        # O(samples^2) -- there is no longer a distance matrix to bound.
        #
        # NOT unconditional. Gate 4 requires a `--sagitta-cap 0` build to
        # reproduce the exact legacy node/edge counts byte-for-bit, and Pass 2
        # runs in EVERY build. Measured directly: swapping this search in
        # unconditionally changes which pairs merge even at cap 0 (32,845
        # nodes / 86,159 edges instead of the required 33,057 / 86,617) --
        # different candidate discovery order picks different, differently-
        # ordered valid connectors even though both algorithms are internally
        # deterministic, so this is a real behaviour change to the shipping
        # pipeline, not a rounding artifact. Gated on `max_chord_sagitta_m >
        # 0.0` (same condition the earlier widened-radius/sample-budget
        # experiments in this investigation used, see STITCH_PIN_RADIUS_M's
        # comment), so a cap-0 build takes the untouched legacy per-group
        # sample + `cdist` path below and a sagitta-active build takes this
        # one -- the fix applies exactly where the regression was measured
        # and nowhere else.
        use_geometric_search = self.classification_config.max_chord_sagitta_m > 0.0
        MAX_ROUNDS = 30
        # Legacy (cap-0) path only -- unchanged from the pre-fix implementation,
        # including this cap's original role: bounding the number of expensive
        # `try_add` (poly-containment + `_crosses_land`) calls walked off the
        # up-to-MAX_TOTAL_SAMPLES**2/2 sorted distance-matrix pairs below.
        # Confirmed against the real machine: an earlier *per-group-only* cap
        # (no total-sample bound at all) produced a tens-of-thousands-squared
        # distance matrix that ran for 1h45m+ before being OOM-killed at
        # full-country scale.
        MAX_EVALUATIONS_PER_ROUND = 20000
        MAX_TOTAL_SAMPLES = 1500
        # Geometric (sagitta-active) path only: bounds the same `try_add` calls,
        # but sized against a fundamentally different candidate list. There is
        # no O(candidates^2) matrix here -- `candidates` is built directly from
        # each node's own (escalating) k-nearest-neighbour query, so its size is
        # O(n_ids * avg_k), not O(n_ids^2); measured on data/zeeland_clip's most
        # fragmented component (17,516 nodes, 387 groups): ~93,000-98,000
        # candidates per round, about 5.6 per node. A first attempt reused
        # MAX_EVALUATIONS_PER_ROUND (20000) for this path too and regressed
        # exactly the way the diagnosis predicted, just one level down: capped=
        # True every round (confirmed via round-level logging), silently
        # dropping ~75,000 already-discovered, already-sorted-nearest-first
        # candidates per round before evaluating even one of them, so rounds
        # kept reporting 0 merges and giving up with genuine cross-group
        # candidates still sitting unevaluated past position 20000. This is
        # the exact "give up on a group pair after the first sampled candidate
        # fails" failure mode the task called out, just relocated from
        # candidate SELECTION (fixed above) to candidate EVALUATION -- fixing
        # one without the other reproduces the same symptom. Sized generously
        # above the largest measured count (with headroom for full-country
        # scale, where the most-fragmented single component is larger) rather
        # than removed outright, so a pathological future input still has a
        # bound instead of an open-ended per-round walk.
        PASS2_GEOMETRIC_MAX_EVALUATIONS_PER_ROUND = 500_000
        # Geometric (sagitta-active) path only: per-node escalation ceiling,
        # matching _resolve_local_skeleton_gaps' own MAX_K exactly (see that
        # function's docstring for why this specific bound: worst case
        # O(nodes x MAX_K), not O(nodes x local-density) or O(groups x nodes)
        # -- a genuine miss is only possible for a node with more than MAX_K
        # same-group neighbours all closer than its true cross-group
        # candidate).
        PASS2_MAX_K = min(256, len(ids))
        from scipy.spatial.distance import cdist
        from scipy.spatial import cKDTree
        n_ids = len(ids)
        # Built once outside the round loop (geometric path only) -- `coords_m`
        # never changes across rounds, only which group each node belongs to does.
        pass2_tree = cKDTree(coords_m) if use_geometric_search else None
        for _round in range(MAX_ROUNDS):
            groups: Dict[Any, List[int]] = {}
            for idx, node in enumerate(ids):
                groups.setdefault(find(node), []).append(idx)
            if len(groups) <= 1:
                break

            if use_geometric_search:
                # Escalating-k cross-group candidate search over every node in
                # the component (see the fix comment above). Unlike
                # _resolve_local_skeleton_gaps, there is deliberately no
                # distance cap: Pass 2 must still find a valid connector even
                # when the two nearest groups are genuinely far apart.
                own_group = np.array([find(n) for n in ids])
                k = min(8, n_ids)
                seen_pairs = set()
                candidates: List[Tuple[float, int, int]] = []
                search_remaining = np.arange(n_ids)
                while len(search_remaining):
                    dists, nns = pass2_tree.query(coords_m[search_remaining], k=k)
                    dists = np.atleast_2d(dists)
                    nns = np.atleast_2d(nns)
                    still_stuck = []
                    for local_a, a in enumerate(search_remaining):
                        found_cross = False
                        for dist, b in zip(dists[local_a], nns[local_a], strict=True):
                            b = int(b)
                            if b >= n_ids or b == a:
                                continue
                            if own_group[a] != own_group[b]:
                                found_cross = True
                                pair = (a, b) if a < b else (b, a)
                                if pair not in seen_pairs:
                                    seen_pairs.add(pair)
                                    candidates.append((float(dist), pair[0], pair[1]))
                        if not found_cross and k < PASS2_MAX_K:
                            still_stuck.append(a)
                    if k >= PASS2_MAX_K:
                        break
                    search_remaining = np.array(still_stuck, dtype=int)
                    k = min(k * 2, PASS2_MAX_K)
                candidates.sort(key=lambda t: t[0])

                merged_this_round = 0
                evaluated = 0
                nearest_cross_group_m = None
                for dist, i, j in candidates:
                    if nearest_cross_group_m is None:
                        nearest_cross_group_m = dist
                    if evaluated >= PASS2_GEOMETRIC_MAX_EVALUATIONS_PER_ROUND:
                        break
                    if try_add(i, j, "pass2"):
                        merged_this_round += 1
                    evaluated += 1
            else:
                # Legacy path -- byte-for-bit unchanged from before this fix.
                # See the fix comment above for why this must stay untouched:
                # gate 4 requires a `--sagitta-cap 0` build to reproduce the
                # exact pre-fix node/edge counts.
                per_group_cap = max(1, MAX_TOTAL_SAMPLES // len(groups))
                sample_idxs: List[int] = []
                for member_idxs in groups.values():
                    step = max(1, len(member_idxs) // per_group_cap)
                    sample_idxs.extend(member_idxs[::step][:per_group_cap])
                sample_coords = coords_m[sample_idxs]
                n_samples = len(sample_idxs)
                dmat = cdist(sample_coords, sample_coords)
                np.fill_diagonal(dmat, np.inf)
                merged_this_round = 0
                # Walk the sorted candidate list, merging every valid pair found (not
                # just the first) -- `find()` inside try_add skips pairs that have
                # already been merged earlier in this same pass, so one sort serves
                # many merges instead of recomputing groups/samples/distances per
                # merge. Capped at MAX_EVALUATIONS_PER_ROUND pairs (see above); an
                # unfinished round still leaves the remaining groups to the next
                # round (or the "could not find a connector" warning below if
                # MAX_ROUNDS runs out first).
                evaluated = 0
                nearest_cross_group_m = None
                for flat_idx in np.argsort(dmat, axis=None):
                    si, sj = divmod(int(flat_idx), n_samples)
                    if si >= sj:
                        continue
                    if find(ids[sample_idxs[si]]) != find(ids[sample_idxs[sj]]) and nearest_cross_group_m is None:
                        nearest_cross_group_m = float(dmat[si, sj])
                    if try_add(sample_idxs[si], sample_idxs[sj], "pass2"):
                        merged_this_round += 1
                    evaluated += 1
                    if evaluated >= MAX_EVALUATIONS_PER_ROUND:
                        break

            if merged_this_round == 0:
                remaining = len({find(n) for n in ids})
                if remaining > 1:
                    logger.warning(f"  Stitch guarantee pass could not find a valid in-polygon "
                                   f"connector among sampled candidates; {remaining} components "
                                   f"left unmerged. Nearest sampled cross-group pair was "
                                   f"{nearest_cross_group_m:.0f}m apart (rejected by poly/land check)."
                                   if nearest_cross_group_m is not None else
                                   f"  Stitch guarantee pass could not find a valid in-polygon "
                                   f"connector among sampled candidates; {remaining} components "
                                   f"left unmerged. No cross-group pair was even sampled this round.")
                    self._stitch_diag["pass2"]["gave_up_components"] += 1
                    if nearest_cross_group_m is not None:
                        self._stitch_diag["pass2"]["gave_up_nearest_m_sum"] += nearest_cross_group_m
                break

        if len({find(n) for n in ids}) > 1:
            # Connectivity-regression investigation (Pass 2 fix comment above):
            # once Pass 2 fully converges (evaluates every discovered
            # cross-group candidate, not just a capped subset -- see that
            # comment), any STILL-residual fragmentation in a large,
            # sagitta-resampled component is a genuine straight-line
            # geometric wall, confirmed directly on data/zeeland_clip: the
            # dominant 17,516-node component's Pass 2 round loop evaluated
            # 78,079 fully-discovered cross-group candidates in its final
            # round and merged zero of them.
            #
            # This call's own straight-line prefilter (see its docstring) was
            # twice tried as a further lever here -- once removed globally,
            # once removed only for this one large/fragmented component (via
            # an `ids`-size gate) -- and BOTH measured net NEGATIVE for
            # largest-component connectivity on data/zeeland_clip, despite
            # each raising raw `remesh_success` substantially (127 -> 429
            # global; 127 -> 269 scoped) and shrinking aggregate residual
            # fragmentation. Reason: `_remesh_local_gap` mints new nodes for
            # every successful local remesh, including the (large majority
            # of) successes that stitch two SMALL side-fragments to each
            # other rather than onto the one dominant group -- so total node
            # count (the fraction's denominator) grew faster than the
            # dominant group itself (the numerator) in both trials (global:
            # 19,895 -> 22,830 nodes, largest fraction 83.88% -> 78.69%;
            # scoped: 19,895 -> 22,083 nodes, 83.88% -> 81.18%). Left as the
            # unmodified original call below -- the Pass 2 fix above is the
            # net-positive change; this pass's own candidate-acceptance logic
            # is a separate, currently-net-negative lever, not a free one.
            added += self._resolve_local_skeleton_gaps(
                ids, coords_m, coords_wgs84, poly_m, poly_prep, utm,
                DEFAULT_SOURCE_TIER, None, parent, find, union)
        final_groups = len({find(n) for n in ids})
        self._stitch_group_stats["final_groups"] += final_groups
        if final_groups > 1:
            self._stitch_group_stats["components_with_residual"] += 1
            self._stitch_group_stats["residual_groups_total"] += final_groups
        return added

    def _snap_or_create_node(self, lon, lat, pool_ids, pool_coords_m, utm, snap_m, node_type="coastal"):
        """Return (node_id, (x_m, y_m)) -- an existing node from pool_ids/pool_coords_m
        within snap_m of (lon, lat) if one exists, else a freshly created node.
        Used by _remesh_local_gap to merge a local skeleton onto nearby existing
        graph nodes rather than always minting brand-new ones a few metres away."""
        pt_m = gpd.GeoSeries([Point(lon, lat)], crs=self.CRS_WGS84).to_crs(utm).iloc[0]
        if len(pool_ids):
            d = np.hypot(pool_coords_m[:, 0] - pt_m.x, pool_coords_m[:, 1] - pt_m.y)
            k = int(np.argmin(d))
            if d[k] <= snap_m:
                return pool_ids[k], (pt_m.x, pt_m.y)
        node_id = self._get_or_create_node(lon, lat, node_type, context="gap_resolve")
        return node_id, (pt_m.x, pt_m.y)

    def _remesh_local_gap(self, i: int, j: int, ids, coords_m, coords_wgs84, poly_m, poly_prep, utm,
                          source_tier, source_id) -> int:
        """Re-rasterize a small window around candidate nodes ids[i]/ids[j] at a
        fixed fine pixel size and, if their medial axis is genuinely connected
        there, merge the result into self.graph. Returns edges added (0 if the
        two points turn out not to be connected at this resolution either --
        e.g. a real charting gap, not a rasterization artifact).

        See LOCAL_GAP_RESOLVE_* constants' comment for why this exists: the
        whole-piece skeleton raster this component was originally built from
        can retreat well short of a narrow, jetty-flanked throat, and the
        ordinary stitch passes above can only accept or reject a straight
        connector between whatever nodes that coarse raster already produced.
        This re-derives the answer at a resolution fine enough that
        MAX_RASTER_PIXELS never forces a coarser pixel size.
        """
        cfg = self.classification_config
        margin = LOCAL_GAP_RESOLVE_WINDOW_MARGIN_M
        minx = min(coords_m[i][0], coords_m[j][0]) - margin
        maxx = max(coords_m[i][0], coords_m[j][0]) + margin
        miny = min(coords_m[i][1], coords_m[j][1]) - margin
        maxy = max(coords_m[i][1], coords_m[j][1]) + margin
        window_m = box(minx, miny, maxx, maxy)
        clipped_m = poly_m.intersection(window_m)
        if clipped_m.is_empty:
            return 0

        window_wgs84 = gpd.GeoSeries([window_m], crs=utm).to_crs(self.CRS_WGS84).iloc[0]
        land_m = self._land_union_for(window_wgs84, utm)

        mask, transform, px = self._rasterize_water_polygon(clipped_m, land_m, LOCAL_GAP_RESOLVE_PIXEL_M)
        if mask is None or int(mask.sum()) < 3:
            return 0
        skel, dist = self._extract_medial_axis_skeleton(mask)
        if int(skel.sum()) < 2:
            return 0
        G_local = self._skeleton_raster_to_graph(skel, dist, transform, utm, px)
        if G_local.number_of_nodes() < 2:
            return 0
        self._prune_skeleton_spurs(G_local, LOCAL_GAP_RESOLVE_MIN_SPUR_M)
        if G_local.number_of_edges() == 0:
            return 0

        # Correctness gate: only proceed if the two ORIGINAL candidate points
        # both land near the SAME connected component of this fresh local
        # skeleton -- i.e. the fine remesh itself proves a real water path
        # exists between them, not just that this window happens to contain
        # some water. This is what makes the whole pass safe: it can never
        # bridge two genuinely separate water bodies (clipped_m is still
        # scoped to poly_m, the original charted component) and can never
        # punch through solid land (land_m is subtracted here exactly like
        # every other raster in this pipeline).
        local_ids = list(G_local.nodes)
        local_lonlat = np.array([G_local.nodes[n]["lonlat"] for n in local_ids])
        d_i = np.hypot(local_lonlat[:, 0] - coords_wgs84[i][0], local_lonlat[:, 1] - coords_wgs84[i][1])
        d_j = np.hypot(local_lonlat[:, 0] - coords_wgs84[j][0], local_lonlat[:, 1] - coords_wgs84[j][1])
        node_near_i = local_ids[int(np.argmin(d_i))]
        node_near_j = local_ids[int(np.argmin(d_j))]
        comp = nx.node_connected_component(G_local, node_near_i)
        if node_near_j not in comp:
            return 0

        # Confirmed connected -- merge G_local into self.graph. Endpoints of each
        # collapsed chain snap onto an existing nearby graph node (this
        # component's own `ids`, extended as new nodes are created) within
        # LOCAL_GAP_RESOLVE_SNAP_M, same discipline build_skeleton_network uses
        # via _get_or_create_node's coordinate-rounding dedupe, just with an
        # explicit wider tolerance since this bridges two independently-built
        # meshes rather than deduping within one. Pool is pre-filtered to the
        # remesh window (+ snap margin) rather than the whole component's
        # `ids` -- a component can hold tens of thousands of nodes, and only
        # ones inside this ~600m window can ever be within snap_m of a local
        # skeleton node.
        in_window = ((coords_m[:, 0] >= minx - LOCAL_GAP_RESOLVE_SNAP_M) &
                     (coords_m[:, 0] <= maxx + LOCAL_GAP_RESOLVE_SNAP_M) &
                     (coords_m[:, 1] >= miny - LOCAL_GAP_RESOLVE_SNAP_M) &
                     (coords_m[:, 1] <= maxy + LOCAL_GAP_RESOLVE_SNAP_M))
        window_idxs = np.flatnonzero(in_window)
        pool_ids = [ids[k] for k in window_idxs]
        pool_coords_m = coords_m[window_idxs].copy()
        node_map = {}
        for n in local_ids:
            lon, lat = G_local.nodes[n]["lonlat"]
            node_id, pt_m = self._snap_or_create_node(lon, lat, pool_ids, pool_coords_m, utm,
                                                       LOCAL_GAP_RESOLVE_SNAP_M)
            node_map[n] = node_id
            if node_id not in pool_ids:
                pool_ids.append(node_id)
                pool_coords_m = np.vstack([pool_coords_m, [pt_m]])

        added = 0
        for u, v, d in G_local.edges(data=True):
            full_pts, full_widths = d["pts"], d["width_profile"]
            # G_local is undirected -- edge iteration does not guarantee u
            # corresponds to full_pts[0]: a node already present in G_local
            # from an EARLIER edge (in that edge's "end" role) is reported
            # first regardless of the argument order THIS edge's own
            # add_edge call used (confirmed via networkx directly). Resolve
            # each end by matching against the two nodes' own recorded
            # lonlat instead of assuming u/v order matches pts order --
            # otherwise a multi-segment chain's outer hops can bind to the
            # wrong endpoint node, producing a straight chord to the wrong
            # place instead of following the validated centerline.
            node_start = u if G_local.nodes[u]["lonlat"] == full_pts[0] else v
            node_end = v if node_start == u else u
            for sub_pts, sub_widths in self._resample_long_skeleton_edges(
                    full_pts, full_widths, cfg.max_segment_m,
                    cfg.max_chord_sagitta_m, cfg.sagitta_width_fraction):
                su = node_map[node_start] if sub_pts[0] == full_pts[0] else \
                    self._get_or_create_node(sub_pts[0][0], sub_pts[0][1], "coastal", context="gap_resolve")
                sv = node_map[node_end] if sub_pts[-1] == full_pts[-1] else \
                    self._get_or_create_node(sub_pts[-1][0], sub_pts[-1][1], "coastal", context="gap_resolve")
                if su == sv:
                    continue
                self._stamp_node(su, NODE_KIND_POINT, source_tier, source_id)
                self._stamp_node(sv, NODE_KIND_POINT, source_tier, source_id)
                wp = json.dumps({"min_m": min(sub_widths), "samples_m": sub_widths})
                attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_CENTERLINE,
                             width_profile=wp, min_width=min(sub_widths),
                             source_tier=source_tier, source_id=source_id)
                if not self.graph.has_edge(su, sv):
                    self.graph.add_edge(su, sv, **attrs)
                    self.graph.add_edge(sv, su, **attrs)
                    added += 2

        # Guarantee ids[i]/ids[j] themselves join the new structure: their own
        # coordinate can sit mid-chain along the fine remesh's longer
        # centerline (collapsed away, never a G_local node in its own right),
        # so node_map's per-chain-endpoint snap above isn't guaranteed to have
        # touched them directly. Same safety gates as the ordinary stitch
        # passes (poly_prep containment + _crosses_land) apply here too --
        # reuses the caller's already-built poly_prep (same poly_m this
        # method received) rather than re-preparing the full-component
        # polygon's edge index on every successful pair.
        for idx in (i, j):
            orig_id = ids[idx]
            touched = set(node_map.values())
            if orig_id in touched:
                continue
            olon, olat = coords_wgs84[idx]
            best_id, best_d = None, None
            for cand_id in touched:
                clon, clat = self.graph.nodes[cand_id]["lon"], self.graph.nodes[cand_id]["lat"]
                _, _, dist = self.geod.inv(olon, olat, clon, clat)
                if best_d is None or dist < best_d:
                    best_id, best_d = cand_id, dist
            if best_id is None:
                continue
            best_lon, best_lat = self.graph.nodes[best_id]["lon"], self.graph.nodes[best_id]["lat"]
            best_pt_m = gpd.GeoSeries([Point(best_lon, best_lat)], crs=self.CRS_WGS84).to_crs(utm).iloc[0]
            candidate_m = LineString([coords_m[idx], (best_pt_m.x, best_pt_m.y)])
            if not poly_prep.contains(candidate_m):
                continue
            if self._crosses_land(LineString([(olon, olat), (best_lon, best_lat)])):
                continue
            attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                         source_tier=source_tier, source_id=source_id)
            self.graph.add_edge(orig_id, best_id, **attrs)
            self.graph.add_edge(best_id, orig_id, **attrs)
            added += 2

        return added

    def _resolve_local_skeleton_gaps(self, ids, coords_m, coords_wgs84, poly_m, poly_prep, utm,
                                     source_tier, source_id, parent, find, union) -> int:
        """Last-resort local high-resolution re-mesh for node pairs still left in
        different components after every pass above (see LOCAL_GAP_RESOLVE_*
        constants' comment for the confirmed bug this fixes).

        Rather than trying to guess *why* an earlier pass missed a given pair
        (never sampled due to a per-component cap, or evaluated and rejected
        by `_crosses_land`), this re-derives the answer directly per candidate
        pair via `_remesh_local_gap`.
        """
        groups: Dict[Any, List[int]] = {}
        for idx, n in enumerate(ids):
            groups.setdefault(find(n), []).append(idx)
        if len(groups) <= 1:
            return 0

        # Nearest-first candidate pairs across DIFFERENT groups. This has
        # tried two simpler approaches first, each with a real failure mode:
        #   1. A separate cKDTree per group, cross-queried against every
        #      other group, is O(groups x nodes) -- Pass 0's own comment
        #      above records 8440 union-find groups for one real component,
        #      which would make that approach run hundreds of millions of
        #      queries right when this last-resort pass is most likely to be
        #      reached (a highly fragmented component).
        #   2. A single tree over ALL nodes queried with a small fixed k has
        #      Pass 0's own documented crowding failure: if a node has more
        #      same-group neighbours than k closer than the true cross-group
        #      candidate, that candidate never appears in its top-k and the
        #      gap silently goes unresolved.
        #   3. query_ball_point(r=LOCAL_GAP_RESOLVE_MAX_M) fixes the crowding
        #      miss (returns every node in range, not just the k nearest),
        #      but can materialize an unbounded same-group-inclusive
        #      neighbour list before any group filtering happens -- a single
        #      node sitting inside a large, densely-packed already-connected
        #      mesh can return thousands of same-group hits that get thrown
        #      away immediately after, real memory/time wasted for nothing.
        # Escalating k bounds the DISCOVERY budget instead: start small,
        # and only widen it for a node whose entire current top-k turned out
        # to be same-group AND still has room before LOCAL_GAP_RESOLVE_MAX_M
        # (i.e. genuinely crowded, not just sparse), up to a hard ceiling.
        # Worst case is O(nodes x MAX_K), not O(nodes x local-density) or
        # O(groups x nodes) -- a genuine miss is now only possible for a
        # node with more than MAX_K same-group neighbours all closer than
        # its true cross-group candidate, far rarer than the k=8 case this
        # replaced and explicitly bounded rather than open-ended.
        from scipy.spatial import cKDTree
        all_tree = cKDTree(coords_m)
        own_group = np.array([find(n) for n in ids])
        n = len(ids)
        MAX_K = min(256, n)
        k = min(8, n)
        seen_pairs = set()
        candidates = []
        remaining = np.arange(n)
        while len(remaining):
            dists, nns = all_tree.query(coords_m[remaining], k=k, distance_upper_bound=LOCAL_GAP_RESOLVE_MAX_M)
            dists = np.atleast_2d(dists)
            nns = np.atleast_2d(nns)
            still_stuck = []
            for local_a, a in enumerate(remaining):
                found_cross = False
                last_dist = -1.0
                for dist, b in zip(dists[local_a], nns[local_a], strict=True):
                    b = int(b)
                    if b >= n or not np.isfinite(dist) or b == a:
                        continue
                    last_dist = dist
                    if own_group[a] != own_group[b]:
                        found_cross = True
                        pair = (a, b) if a < b else (b, a)
                        if pair not in seen_pairs:
                            seen_pairs.add(pair)
                            candidates.append((float(dist), pair[0], pair[1]))
                if not found_cross and k < MAX_K and 0 <= last_dist < LOCAL_GAP_RESOLVE_MAX_M:
                    still_stuck.append(a)
            if k >= MAX_K:
                # These nodes found NO cross-group candidate within
                # LOCAL_GAP_RESOLVE_MAX_M even after widening k to MAX_K -- the
                # "no candidate within radius" failure mode by construction.
                self._stitch_diag["gap_resolve"]["no_candidate_in_radius"] += len(still_stuck)
                break
            remaining = np.array(still_stuck, dtype=int)
            k = min(k * 2, MAX_K)
        candidates.sort(key=lambda t: t[0])
        self._stitch_diag["gap_resolve"]["candidate_pairs_found"] += len(candidates)

        added = 0
        resolved = 0
        attempted = set()
        for dist, i, j in candidates:
            if resolved >= MAX_LOCAL_GAP_RESOLVE_PER_COMPONENT:
                break
            if find(ids[i]) == find(ids[j]):
                continue
            key = (min(i, j), max(i, j))
            if key in attempted:
                continue
            attempted.add(key)

            candidate_m = LineString([coords_m[i], coords_m[j]])
            if not poly_prep.contains(candidate_m):
                self._stitch_diag["gap_resolve"]["poly_reject"] += 1
                continue

            n_added = self._remesh_local_gap(i, j, ids, coords_m, coords_wgs84, poly_m, poly_prep, utm,
                                             source_tier, source_id)
            if n_added:
                added += n_added
                resolved += 1
                union(ids[i], ids[j])
                self._stitch_diag["gap_resolve"]["remesh_success"] += 1
            else:
                self._stitch_diag["gap_resolve"]["remesh_failed"] += 1

        if resolved:
            self.local_gap_resolve_stats["gaps_resolved"] += resolved
            self.local_gap_resolve_stats["edges_added"] += added
        return added

    # ------------------------------------------------------------------
    # Step C — skeleton (medial-axis centerline) extraction
    # ------------------------------------------------------------------
    def _stamp_node(self, node_id, node_kind_id, source_tier, source_id):
        d = self.graph.nodes[node_id]
        d["node_kind_id"] = node_kind_id
        d["source_tier"] = source_tier
        if source_id is not None:
            d["source_id"] = source_id

    def _land_union_for(self, polygon, utm_crs):
        """Union of land polygons intersecting `polygon`, reprojected to utm_crs (or None)."""
        land = self.gdfs.get("land", gpd.GeoDataFrame())
        if land is None or land.empty:
            return None
        try:
            idx = land.sindex.query(polygon, predicate="intersects")
            sub = land.iloc[idx]
        except Exception:
            sub = land[land.intersects(polygon)]
        if sub.empty:
            return None
        merged = unary_union([g for g in sub.geometry if g is not None])
        if merged.is_empty:
            return None
        return gpd.GeoSeries([merged], crs="EPSG:4326").to_crs(utm_crs).iloc[0]

    def _rasterize_water_polygon(self, poly_m, land_m, pixel_size_m):
        """Rasterize a metre-projected water polygon (minus land) to a boolean mask.

        Returns (mask, transform) or (None, None) if too small. pixel_size_m may be
        enlarged to keep the raster under a memory cap for long/curved channels.
        """
        MAX_RASTER_PIXELS = 50_000_000
        minx, miny, maxx, maxy = poly_m.bounds
        w_m, h_m = maxx - minx, maxy - miny
        px = float(pixel_size_m)
        est = (w_m / px + 1) * (h_m / px + 1)
        if est > MAX_RASTER_PIXELS:
            px *= math.sqrt(est / MAX_RASTER_PIXELS)
            logger.info(f"  Raster too large; enlarging pixel size to {px:.1f}m for this polygon.")
        width = int(math.ceil(w_m / px)) + 1
        height = int(math.ceil(h_m / px)) + 1
        if width < 3 or height < 3:
            return None, None, px
        transform = _rio_from_origin(minx, maxy, px, px)
        mask = _rio_rasterize([(poly_m, 1)], out_shape=(height, width),
                              transform=transform, fill=0, dtype="uint8", all_touched=False)
        if land_m is not None and not land_m.is_empty:
            land_mask = _rio_rasterize([(land_m, 1)], out_shape=(height, width),
                                       transform=transform, fill=0, dtype="uint8", all_touched=True)
            mask = np.where(land_mask == 1, 0, mask)
        return mask.astype(bool), transform, px

    def _lock_protection_mask(self, shape_hw, transform, utm_crs, polygon_wgs84, buffer_m):
        """Pixels within `buffer_m` of any `locks` polygon -- these must never be
        suppressed by axis-dedup, regardless of how close an inland_waterways axis runs.

        Root cause this exists to close (verified directly on data/zeeland_live_clip,
        Krammersluizen lock): `_add_lock_crossing_edges` derives its chamber entry/exit
        points from where an hw line crosses the LOCK POLYGON's own boundary, then
        hooks a quadrant search onto whatever coastal graph nodes already exist nearby --
        it runs after build_network, so it can only connect to nodes that already exist
        by then. A lock chamber is exactly the kind of place an inland_waterways axis
        line runs through (it's the whole point of the lock), so axis-dedup suppressing
        the coastal centerline there is the worst possible case for it: measured against
        a real build, the node axis-dedup removed at Krammersluizen (51.66105, 4.15994,
        `requires_lock` edges to both banks) sat AT 0.0m from the lock polygon's own
        boundary (i.e. inside/on it) -- not near it, inside it. POI-pair reachability
        (§6.1's method) went from 0 lost pairs to 278/10,878 (2.56%), 100% of them
        through Krammersluizen, once the fix below was added this dropped back to 0.

        `buffer_m` reuses `axis_dedup_cap_m` rather than introducing a second guessed
        constant: suppression itself can never reach farther than that cap from any
        axis line (tol = clip(fraction*width, floor, cap) <= cap always), so buffering
        every lock polygon by that same cap guarantees no pixel axis-dedup could
        possibly have suppressed near a lock is left unprotected, while leaving
        suppression fully unaffected everywhere more than a cap-width away from any
        lock (i.e. everywhere axis-dedup's own reach could not have touched lock
        infrastructure in the first place). Lock polygons in this dataset run
        37.5-302m on their major axis (20 named locks measured directly), so this
        buffer is a modest addition on top of already-lock-scaled geometry, not the
        dominant term.

        Returns a boolean array shaped `shape_hw` -- all-False (cheap) when no lock
        polygon falls near this piece.
        """
        locks_gdf = self.gdfs.get("locks")
        if locks_gdf is None or locks_gdf.empty:
            return np.zeros(shape_hw, dtype=bool)

        margin_lon_deg, margin_lat_deg = _lonlat_margin_deg(polygon_wgs84, buffer_m)
        candidates = _candidates_by_bounds_static(locks_gdf, polygon_wgs84,
                                                    margin=margin_lat_deg, margin_lon=margin_lon_deg)
        if candidates.empty:
            return np.zeros(shape_hw, dtype=bool)

        cand_utm = candidates.to_crs(utm_crs)
        buffered = [(geom.buffer(buffer_m), 1) for geom in cand_utm.geometry
                    if geom is not None and not geom.is_empty]
        if not buffered:
            return np.zeros(shape_hw, dtype=bool)

        protect = _rio_rasterize(buffered, out_shape=shape_hw, transform=transform,
                                  fill=0, dtype="uint8", all_touched=True)
        return protect.astype(bool)

    def _axis_dedup_suppression_mask(self, mask, transform, utm_crs, pixel_size_m, polygon_wgs84):
        """SPEC-GRAPH-DENSITY.md §4.3.2: per-pixel suppression mask for water pixels
        that sit within a width-coupled tolerance of an authoritative inland_waterways
        axis line (wtwaxs/RECTRC/NAVLNE), so build_skeleton_network never generates a
        redundant medial-axis "twin" next to a centerline _build_inland_network already
        ingested from the same source.

        `tol = clip(axis_dedup_fraction * local_width, axis_dedup_floor_m,
        axis_dedup_cap_m)` -- §4.3.1's measured constants (cap=50m, fraction=0.5,
        floor=5m), applied per water pixel. Local channel width is measured from the
        ORIGINAL, uncarved `mask` (step 1 of §4.3.2) so estimates near the axis are not
        distorted by the carving this function's own output goes on to do.

        Lock polygons are then excluded from the result entirely (see
        `_lock_protection_mask`) -- a lock chamber is precisely where an authoritative
        axis line is expected to run, but `_add_lock_crossing_edges` needs real coastal
        graph nodes there to hook its own chamber-transit edges onto, and that need does
        not go away just because the water also happens to be near a WTWAXS line.

        Returns `(suppress, line_iloc_by_suppressed_px)`:
          - `suppress`: boolean array shaped like `mask` -- all-False (cheaply, no
            rasterize call) when no inland_waterways line falls near this piece.
          - `line_iloc_by_suppressed_px`: `{(row, col): line_iloc}` for every pixel
            where `suppress` is True -- the positional index into
            `self.gdfs["inland_waterways"]` of the candidate line nearest that pixel
            (i.e. directly usable as `_connect_waterway_crossing`'s `line_iloc`
            argument via `.geometry.iloc[line_iloc]`). Empty on every no-op path.
            SPEC-GRAPH-DENSITY.md §6.3.1: this is what lets a caller reconnect a
            carve-induced dead end to the SPECIFIC line responsible for carving it,
            rather than just any nearby line. Known, accepted imprecision: where two
            candidates' suppression footprints truly overlap, `cand_utm.index`
            iteration order (below) decides which wins at those pixels --
            deterministic but arbitrary; acceptable since the goal is reconnecting to
            *an* authoritative line at that spot, not resolving which of two
            disagreeing lines is "more correct."

        Caller (only build_skeleton_network and _axis_dedup_carve_navmesh_pieces) is
        responsible for gating this on `axis_dedup_cap_m > 0.0`; this method does not
        re-check that itself, so it must never be called from the
        `axis_dedup_cap_m == 0.0` path (gate 1: that path's mask must be byte-identical
        to before this feature existed).
        """
        cfg = self.classification_config
        from scipy.ndimage import distance_transform_edt
        from rasterio import Affine

        inland_gdf = self.gdfs.get("inland_waterways")
        if inland_gdf is None or inland_gdf.empty:
            return np.zeros(mask.shape, dtype=bool), {}

        # Bounding-box prefilter against this piece's extent, same pattern
        # _candidates_by_bounds_static's other callers use, so a piece never pays to
        # rasterize the whole inland_waterways dataset. Margin covers the cap distance
        # so a line just outside the piece's own bbox -- but within suppression range of
        # a water pixel near the piece's edge -- is not missed. Uses _lonlat_margin_deg
        # rather than a single 111320 m/deg conversion applied to both axes: that
        # constant is correct for latitude but overstates longitude's degree length at
        # any latitude away from the equator, silently under-covering the east-west
        # search radius (CodeRabbit PR #14 review finding 1 -- see that function's
        # docstring for the measured impact).
        margin_lon_deg, margin_lat_deg = _lonlat_margin_deg(polygon_wgs84, cfg.axis_dedup_cap_m)
        candidates = _candidates_by_bounds_static(inland_gdf, polygon_wgs84,
                                                    margin=margin_lat_deg, margin_lon=margin_lon_deg)
        if candidates.empty:
            return np.zeros(mask.shape, dtype=bool), {}

        cand_utm = candidates.to_crs(utm_crs)
        # CodeRabbit PR #14 review round 3 finding 1: _build_inland_network (its own
        # docstring/call site above) only ever ingests LineString geometry -- a
        # MultiLineString feature is silently skipped there, contributing zero graph
        # topology. Suppressing coastal water on the strength of a candidate that isn't
        # actually in the graph would be a real hole (removing a "duplicate" that was
        # never built), not a duplicate-removal, so this candidate list must match what
        # _build_inland_network would actually ingest, not just "any non-empty geometry
        # in the layer". Warn (don't silently drop, matching parse_shapefiles' missing-
        # layer convention) rather than fail the build -- this is a data-quality gap in
        # the source layer, not a reason to abort.
        non_linestring = [g for g in cand_utm.geometry if g is not None and not g.is_empty
                           and not isinstance(g, LineString)]
        if non_linestring:
            logger.warning(f"  Axis-dedup: {len(non_linestring)} inland_waterways candidate(s) near "
                            f"this piece are not LineString geometry (e.g. MultiLineString) -- "
                            f"_build_inland_network wouldn't ingest them either, so they are excluded "
                            f"from suppression rather than claiming coverage the graph doesn't have.")
        # Burn each candidate with its own inland_waterways positional index (+1, to
        # avoid colliding with rasterize's fill=0 sentinel -- a real line can be
        # positional index 0) instead of a shared constant, so the raster doubles as a
        # line-id lookup below. `cand_utm.index` IS already the correct positional
        # index into self.gdfs["inland_waterways"]: parse_shapefiles loads that layer
        # via plain gpd.read_file with no reset_index/filtering, so its index is a
        # bare RangeIndex(0..n-1); _candidates_by_bounds_static's `gdf.iloc[candidates]`
        # and `.to_crs()` both preserve those original index labels. Never re-derive a
        # line_iloc by enumerating this (already-subset) cand_utm from scratch (e.g.
        # `for i, geom in enumerate(...)`) -- that index would NOT match
        # inland_waterways' own positional order, silently wiring a reconnect to the
        # wrong line. _inject_waterway_crossings avoids this same trap by using
        # `inland_gdf.sindex.query(...)` results directly as line_iloc; this mirrors it.
        line_shapes = [(geom, int(idx) + 1) for idx, geom in zip(cand_utm.index, cand_utm.geometry)
                       if geom is not None and not geom.is_empty and isinstance(geom, LineString)]
        if not line_shapes:
            return np.zeros(mask.shape, dtype=bool), {}

        # Step 1: local channel width per water pixel, from the ORIGINAL (uncarved)
        # mask -- exactly the quantity width_m() in _skeleton_raster_to_graph turns into
        # width_profile downstream, computed here for every water pixel instead of only
        # skeleton pixels (§4.3.2).
        width_est_m = distance_transform_edt(mask) * pixel_size_m * 2.0

        # Step 2: rasterize the candidate axis lines onto a grid padded by
        # axis_dedup_cap_m in every direction (crop back to mask's own shape below),
        # then distance-transform the inverse to get each pixel's distance to the
        # nearest axis line.
        #
        # Padded, not rasterized directly onto mask's own unpadded grid: a candidate
        # survives the bbox+margin prefilter above (its bbox overlaps this piece's
        # extent plus a cap-sized margin) precisely because its true geometry CAN sit
        # up to a cap's distance outside this piece's own raster footprint while still
        # being within tolerance of a water pixel near the piece's edge -- rasterizing
        # only onto the unpadded grid silently dropped exactly that case (the line
        # never touches a single pixel of the piece's own grid, so it contributes
        # nothing to axis_dist_m at all, regardless of how close it truly is to the
        # piece's edge pixels). Reuses axis_dedup_cap_m as the pad margin for the same
        # reason `_lock_protection_mask` reuses it as a buffer distance: suppression
        # can never reach farther than the cap, so padding by exactly the cap is
        # provably sufficient and never over-generous.
        #
        # The padded transform is a pure integer-pixel extension of the original (same
        # pixel_size_m on both grids, origin shifted by whole pixels), so cropping the
        # padded distance transform back to mask's own shape below is exact index
        # slicing, not resampling -- no fractional-pixel misalignment is possible.
        # dtype int32, not uint8: values now carry line_iloc+1, which can run past 255
        # at country scale (a plain "is there a line here" bit no longer suffices).
        pad_px = int(np.ceil(cfg.axis_dedup_cap_m / pixel_size_m))
        padded_shape = (mask.shape[0] + 2 * pad_px, mask.shape[1] + 2 * pad_px)
        padded_transform = transform * Affine.translation(-pad_px, -pad_px)
        axis_raster_padded = _rio_rasterize(line_shapes, out_shape=padded_shape, transform=padded_transform,
                                             fill=0, dtype="int32", all_touched=True)
        if not axis_raster_padded.any():
            # Every candidate survived the coarse bbox+margin prefilter (their overall
            # bounding boxes overlap this piece's extent) but NONE actually rasterizes
            # onto even the padded grid -- a real, if narrow, case: a line's own bbox is
            # a rectangle, so a diagonal or L-shaped feature can have its bbox reach into
            # the margin while the line itself passes nowhere near the piece, cap-padded
            # margin included. Found while hardening tests/test_axis_dedup.py's own
            # prefilter-vs-real-tolerance test (CodeRabbit PR #14 review round 3 finding
            # 2's fix): feeding an all-zero axis raster into distance_transform_edt does
            # NOT yield "far away everywhere" -- with no background pixel anywhere in the
            # array, scipy falls back to measuring from an implicit point outside the
            # array's own (0,0) corner, producing small, spurious distances near that one
            # corner regardless of where the real (unrasterized) line actually is.
            # Confirmed directly: a line 40m outside a piece's own (unpadded) grid still
            # wrongly carved a ~1900 m^2 sliver at the piece's raster-origin corner
            # before this guard existed; the padded grid now genuinely rasterizes that
            # case, and this guard is left in place for the residual case of a line
            # beyond even the padded margin.
            return np.zeros(mask.shape, dtype=bool), {}
        # return_indices=True: same distance-transform computation scipy already
        # performs, no second pass. indices_padded[0]/[1] give each pixel's nearest
        # foreground (axis) pixel's own (row, col) -- looking that up in
        # axis_raster_padded (which doubles as the line-id raster) gives the
        # responsible line per pixel, extracted below only for pixels that end up
        # actually suppressed.
        distances_padded, indices_padded = distance_transform_edt(
            axis_raster_padded == 0, return_distances=True, return_indices=True)
        axis_dist_padded_m = distances_padded * pixel_size_m
        axis_dist_m = axis_dist_padded_m[pad_px:pad_px + mask.shape[0], pad_px:pad_px + mask.shape[1]]

        # Step 3: tol = clip(fraction * width_est, floor, cap); suppress = axis_dist < tol.
        #
        # Tried <= here (SPEC-OVERRIDE-ZONES.md §7 follow-up's motivating-case
        # verification found a real near-miss: 51.6078N/4.1061E's node sat at
        # axis_dist_m == tol_m == 10.00m exactly, at 10m/px resolution -- both values
        # are derived from the SAME pixel-quantized distance_transform_edt grid, so an
        # exact tie there is systematic, not a rare coincidence, and <= suppressed it).
        # REVERTED: measured directly against a real rebuild, <= raised suppression
        # 670,804->729,973 px (5.2%->5.7%) system-wide -- not a narrow, single-tie fix,
        # a broad one, because ties of this kind recur throughout the dataset wherever
        # fraction*width_est's own pixel-quantized value happens to land on a multiple
        # of the pixel size. That broader reach cost a real POI pair: a 5-node stub near
        # Hansweert (51.383N, 4.031E, no axis line within 178m -- nowhere near this
        # comparison's own reach) went from connected (3m from the main component) to a
        # fully isolated island, costing 138/10,878 POI-pairs (confirmed by rebuilding
        # with <= removed: the node is back in the 45,012-node main component). Gate 4
        # (zero reachability loss) is the harder, non-negotiable bar on this branch, so
        # the motivating case's exact-tie near-miss is accepted as a known, narrow,
        # already-measured limitation rather than traded for a real regression elsewhere.
        tol_m = np.clip(cfg.axis_dedup_fraction * width_est_m, cfg.axis_dedup_floor_m, cfg.axis_dedup_cap_m)
        suppress = axis_dist_m < tol_m

        # Never suppress near a lock -- see _lock_protection_mask's docstring for the
        # measured Krammersluizen failure this closes.
        protect = self._lock_protection_mask(mask.shape, transform, utm_crs, polygon_wgs84,
                                              cfg.axis_dedup_cap_m)
        if protect.any():
            suppress = suppress & ~protect
        # Limit to actual water pixels (CodeRabbit PR #14 review finding 2): axis_dist_m
        # is computed over the whole raster grid, so suppress can be True on land near
        # an axis line. The carve (mask & ~suppress) was already unaffected either way,
        # but callers computing a suppression RATE against mask.sum() need this to not
        # overcount the numerator.
        suppress = mask & suppress

        # SPEC-GRAPH-DENSITY.md §6.3.1: record, per suppressed pixel, which candidate
        # line is responsible -- indices_padded/axis_raster_padded are in the PADDED
        # coordinate frame, so a suppressed pixel at (r, c) here looks up
        # (r+pad_px, c+pad_px) in them.
        sup_rows, sup_cols = np.nonzero(suppress)
        line_iloc_by_suppressed_px: Dict[Tuple[int, int], int] = {}
        if sup_rows.size:
            nearest_row = indices_padded[0][sup_rows + pad_px, sup_cols + pad_px]
            nearest_col = indices_padded[1][sup_rows + pad_px, sup_cols + pad_px]
            nearest_line_id = axis_raster_padded[nearest_row, nearest_col]
            line_iloc_by_suppressed_px = {
                (int(r), int(c)): int(lid) - 1
                for r, c, lid in zip(sup_rows, sup_cols, nearest_line_id)
            }
        return suppress, line_iloc_by_suppressed_px

    @staticmethod
    def _axis_dedup_nearest_line_for_suppressed_pixel(row, col, line_iloc_by_px,
                                                        max_radius=AXIS_DEDUP_DEADEND_SEARCH_RADIUS_PX):
        """SPEC-GRAPH-DENSITY.md §6.3.1 Phase B: is a skeleton degree-1 dead end at
        raster pixel (row, col) carve-induced -- adjacent to a pixel axis-dedup
        actually suppressed -- or genuine? Returns the nearest such neighbor's
        line_iloc (its responsible inland_waterways line), or None if (row, col) has
        no suppressed neighbor within max_radius pixels (a genuine dead end, left to
        the existing stitching passes, not this reconnect mechanism).
        """
        best, best_d2 = None, None
        for r in range(row - max_radius, row + max_radius + 1):
            for c in range(col - max_radius, col + max_radius + 1):
                if (r, c) == (row, col):
                    continue
                line_iloc = line_iloc_by_px.get((r, c))
                if line_iloc is None:
                    continue
                d2 = (r - row) ** 2 + (c - col) ** 2
                if best_d2 is None or d2 < best_d2:
                    best_d2, best = d2, line_iloc
        return best

    def _axis_dedup_carve_navmesh_pieces(self, poly_m, utm_crs):
        """SPEC-OVERRIDE-ZONES.md §7 / follow-up to SPEC-GRAPH-DENSITY.md §4.3: apply
        the SAME axis-dedup suppression the skeleton path already does to a navmesh
        (PSLG/triangle) piece, before it becomes triangulation input.

        `build_skeleton_network` was the only caller of `_axis_dedup_suppression_mask`
        until now -- `build_navmesh_region`'s wide-water triangulation path was never
        touched, because axis-dedup's own computed buffer is typically narrow enough to
        stay inside skeleton-classified (narrow) water. Verified this gap is real
        against the deployed Zeeland database: node 509788242410608 (Oosterschelde
        approach, a navmesh-perimeter vertex carrying a `navmesh_boundary` edge) sits
        ~9-14m from a real `inland_waterways` line -- well inside the 50m cap -- and was
        never suppressed, because nothing upstream of `build_navmesh_region` ever asked.

        Bridges the two representations rather than reimplementing the tolerance
        formula: rasterizes `poly_m` (reusing `_rasterize_water_polygon`, the exact
        pixel-size convention `build_skeleton_network` uses), calls
        `_axis_dedup_suppression_mask` (the real width-coupled formula, unchanged), then
        vectorizes the resulting boolean mask back to a polygon (`rasterio.features.
        shapes`) and subtracts it from `poly_m`.

        Performance: gated FIRST on a cheap bbox prefilter (same `_lonlat_margin_deg` +
        `_candidates_by_bounds_static` pattern as the skeleton path) against
        `inland_waterways` -- a piece with no candidate line nearby returns `[poly_m]`
        immediately, with NO rasterization at all. This matters here in a way it didn't
        for skeleton pieces: navmesh pieces can be up to `NAVMESH_TILE_MAX_EXTENT_M`
        (10km) per tile, so rasterizing unconditionally would be real, avoidable cost on
        every open-water region in a build, not just ones actually near an axis line.

        Carving can fragment a navmesh piece the same way it fragments a skeleton mask
        (SPEC-GRAPH-DENSITY.md §4.3.2's reasoning) -- but unlike the raster skeleton
        path, `build_navmesh_region` cannot triangulate a fragmented input in one call
        (`triangle` takes one exterior ring + holes, not disjoint pieces -- the same
        reason `_explode_polygonal` is already used everywhere else a navmesh piece
        might come apart). Rather than let `build_navmesh_region`'s existing "keep only
        the largest simplified fragment" behaviour (see its own boundary-simplify step)
        silently DROP a whole carved-off sub-region from the graph, this returns every
        surviving fragment as its own polygon, so the caller triangulates each one
        separately -- no water disappears, only the suppressed strip along the axis
        does. Reconnecting the resulting pieces is exactly the existing stitching
        passes' job, same as any other multi-piece water body.

        Returns `(pieces, carve_seam_coords, carve_line_iloc_by_coord)`:
          - `pieces`: list of Polygons (metric CRS, same `utm_crs` as `poly_m`) --
            `[poly_m]` unchanged when disabled, no candidates, or nothing suppressed;
            `[]` if carving consumes the entire piece (its water is already covered by
            the authoritative axis line via `_build_inland_network`, same as a
            fully-consumed skeleton mask).
          - `carve_seam_coords`: `set()` of `(x, y)` metric-CRS coordinates on the
            carve boundary -- vertices already present on a returned piece's own
            perimeter (from `poly_m.difference(...)`), NOT new inserted vertices, so
            this reuses `_seam_coord_set`'s exact-coordinate matching (the same
            mechanism `build_network`'s own width/depth seam computation already uses),
            not `_inject_waterway_crossings`'s position-in-ring bookkeeping (that
            mechanism exists because it inserts brand-new mid-segment vertices that
            must survive verbatim -- carve-boundary vertices are already there).
          - `carve_line_iloc_by_coord`: `{(x, y): line_iloc}` for each carve_seam_coords
            member -- SPEC-GRAPH-DENSITY.md §6.3.1: the responsible axis line for that
            boundary, letting `build_navmesh_region` reconnect a carve-induced dead end
            to the SPECIFIC line that caused its own creation via
            `_connect_waterway_crossing`, mirroring `_inject_waterway_crossings`'s own
            connector mechanism.
        """
        cfg = self.classification_config
        if cfg.axis_dedup_cap_m <= 0.0:
            return [poly_m], set(), {}

        stats = self.axis_dedup_navmesh_stats
        stats["pieces_checked"] += 1

        polygon_wgs84 = gpd.GeoSeries([poly_m], crs=utm_crs).to_crs(self.CRS_WGS84).iloc[0]

        inland_gdf = self.gdfs.get("inland_waterways")
        if inland_gdf is None or inland_gdf.empty:
            return [poly_m], set(), {}
        margin_lon_deg, margin_lat_deg = _lonlat_margin_deg(polygon_wgs84, cfg.axis_dedup_cap_m)
        candidates = _candidates_by_bounds_static(inland_gdf, polygon_wgs84,
                                                    margin=margin_lat_deg, margin_lon=margin_lon_deg)
        if candidates.empty:
            return [poly_m], set(), {}

        import time
        t0 = time.perf_counter()
        stats["pieces_rasterized"] += 1

        b = poly_m.bounds
        min_dim = min(b[2] - b[0], b[3] - b[1])
        px = cfg.pixel_size_for(min_dim)
        land_m = self._land_union_for(polygon_wgs84, utm_crs)
        mask, transform, px = self._rasterize_water_polygon(poly_m, land_m, px)
        if mask is None or int(mask.sum()) < 3:
            stats["rasterize_seconds"] += time.perf_counter() - t0
            return [poly_m], set(), {}

        suppress, line_iloc_by_suppressed_px = self._axis_dedup_suppression_mask(
            mask, transform, utm_crs, px, polygon_wgs84)
        stats["rasterize_seconds"] += time.perf_counter() - t0
        if not suppress.any():
            return [poly_m], set(), {}

        # Group suppression polygons PER LINE (not one merged union) so each carve
        # boundary can be attributed to the specific line responsible for it, by
        # scattering line_iloc_by_suppressed_px back into a small id raster and
        # vectorizing per distinct value -- mirrors the id-raster convention
        # _axis_dedup_suppression_mask itself uses (line_iloc + 1, so 0 stays "no line").
        from rasterio.features import shapes as _rio_shapes
        from shapely.geometry import shape as _shapely_shape
        line_id_arr = np.zeros(mask.shape, dtype=np.int32)
        for (r, c), line_iloc in line_iloc_by_suppressed_px.items():
            line_id_arr[r, c] = line_iloc + 1
        suppress_polys_by_line: Dict[int, List[Polygon]] = defaultdict(list)
        for geom, val in _rio_shapes(line_id_arr, mask=suppress, transform=transform):
            if val:
                suppress_polys_by_line[int(val) - 1].append(_shapely_shape(geom))
        if not suppress_polys_by_line:
            return [poly_m], set(), {}
        suppression_poly_m = self._clean_polygonal(
            unary_union([p for polys in suppress_polys_by_line.values() for p in polys]))

        carved = self._clean_polygonal(poly_m.difference(suppression_poly_m))
        stats["pieces_carved"] += 1
        if carved.is_empty:
            stats["pieces_fully_consumed"] += 1
            return [], set(), {}

        carve_seam_coords: set = set()
        carve_line_iloc_by_coord: Dict[Tuple[float, float], int] = {}
        for line_iloc, polys in suppress_polys_by_line.items():
            line_poly = self._clean_polygonal(unary_union(polys))
            seam_geom = carved.boundary.intersection(line_poly.boundary)
            coords = self._seam_coord_set(seam_geom)
            carve_seam_coords |= coords
            for xy in coords:
                # Deterministic last-writer-wins on a true multi-line overlap at the
                # exact same coordinate -- narrow, accepted (see
                # _axis_dedup_suppression_mask's own docstring on this same tradeoff).
                carve_line_iloc_by_coord[xy] = line_iloc

        pieces = self._explode_polygonal(carved)
        if len(pieces) > 1:
            stats["pieces_fragmented"] += 1
        return pieces, carve_seam_coords, carve_line_iloc_by_coord

    @staticmethod
    def _extract_medial_axis_skeleton(mask):
        """medial_axis with the distance transform (width profile) in one call.

        Seeded: see MEDIAL_AXIS_SEED. Without it the same input yields a different
        centerline on every run, which makes builds irreproducible and -- because
        node ids are coordinate-derived (_coord_to_id) -- means two independently
        built adjacent regions cannot be relied on to agree about a shared seam node.
        """
        kwargs = {"return_distance": True}
        if _MEDIAL_AXIS_RNG_KW is not None:
            kwargs[_MEDIAL_AXIS_RNG_KW] = MEDIAL_AXIS_SEED
        skel, dist = medial_axis(mask, **kwargs)
        return skel, dist

    def _skeleton_raster_to_graph(self, skel, dist, transform, utm_crs, pixel_size_m) -> nx.Graph:
        """Collapse an 8-connected skeleton raster into a graph of centerline chains.

        Degree-1 (endpoints) and degree>=3 (junctions) pixels become nodes; degree-2
        runs collapse into a single edge carrying the full lon/lat polyline (`pts`),
        a `width_profile` (channel diameter in metres sampled from the distance
        transform), and `length_m`.
        """
        from pyproj import Transformer
        ys, xs = np.nonzero(skel)
        skelset = set(zip(ys.tolist(), xs.tolist()))
        if len(skelset) < 2:
            return nx.Graph()
        NBR = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

        def neighbors(p):
            r, c = p
            return [(r + dr, c + dc) for dr, dc in NBR if (r + dr, c + dc) in skelset]

        degree = {p: len(neighbors(p)) for p in skelset}
        nodepix = {p for p, d in degree.items() if d == 1 or d >= 3}
        transformer = Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)

        def to_lonlat(p):
            r, c = p
            x, y = transform * (c + 0.5, r + 0.5)
            lon, lat = transformer.transform(x, y)
            return (lon, lat)

        def width_m(p):
            r, c = p
            return float(dist[r, c] * 2.0 * pixel_size_m)

        def step_len(a, b):
            dr, dc = abs(a[0] - b[0]), abs(a[1] - b[1])
            return pixel_size_m * (math.sqrt(2.0) if (dr and dc) else 1.0)

        G = nx.Graph()
        visited_steps = set()
        for start in nodepix:
            for nb in neighbors(start):
                if frozenset((start, nb)) in visited_steps:
                    continue
                chain = [start, nb]
                prev, cur = start, nb
                while cur not in nodepix:
                    nxts = [q for q in neighbors(cur) if q != prev]
                    if not nxts:
                        break
                    prev, cur = cur, nxts[0]
                    chain.append(cur)
                visited_steps.add(frozenset((chain[0], chain[1])))
                visited_steps.add(frozenset((chain[-1], chain[-2])))
                end = chain[-1]
                if end == start and len(chain) < 4:
                    continue
                length_m = sum(step_len(chain[i - 1], chain[i]) for i in range(1, len(chain)))
                pts = [to_lonlat(p) for p in chain]
                widths = [round(width_m(p), 1) for p in chain]
                G.add_node(start, lonlat=to_lonlat(start))
                G.add_node(end, lonlat=to_lonlat(end))
                if G.has_edge(start, end):
                    # keep the longer of parallel chains between the same node pair
                    if G[start][end]["length_m"] >= length_m:
                        continue
                G.add_edge(start, end, pts=pts, width_profile=widths, length_m=length_m)
        return G

    def _prune_skeleton_spurs(self, G: nx.Graph, min_spur_length_m: float):
        """Iteratively drop short dead-end edges (raster skeletonization corner artifacts)."""
        changed = True
        while changed:
            changed = False
            for u, v, d in list(G.edges(data=True)):
                if d["length_m"] < min_spur_length_m and (G.degree(u) == 1 or G.degree(v) == 1):
                    G.remove_edge(u, v)
                    for n in (u, v):
                        if n in G and G.degree(n) == 0:
                            G.remove_node(n)
                    changed = True

    def _resample_long_skeleton_edges(self, pts, widths, max_segment_m,
                                       max_chord_sagitta_m=0.0,
                                       sagitta_width_fraction=0.5):
        """Split a centerline polyline into segments, closing each when EITHER its
        sagitta (max chord-to-centerline deviation) would exceed tolerance, OR
        max_segment_m is reached (retained as a hard backstop).

        Re-inserting nodes keeps the existing straight-chord depth sampler valid on
        curved channels (per BACKGROUND.md §5.1) without modifying that worker.
        Yields (sub_pts, sub_widths) tuples.

        SPEC-GRAPH-DENSITY.md §4.1: max_chord_sagitta_m == 0.0 (the default) disables
        this entirely and reproduces the unconditional max_segment_m cut byte-for-byte
        -- see the fast path below, which is exactly the old function body. Sagitta
        relaxation only engages when it is both enabled AND every vertex of this
        (pre-split) chain carries a real measured width; a chain with any missing or
        999.0-sentinel width keeps the uniform behaviour in full (§4.1's width-
        exclusion rule -- an unmeasured channel must never simplify *more*
        aggressively than a measured one).

        `pin_start`/`pin_end` (connectivity-regression investigation, see
        the STITCH-DENSITY INVESTIGATION note for the full status -- NOT currently
        shipped): whether to keep legacy segment density within
        STITCH_PIN_RADIUS_M of pts[0]/pts[-1] respectively, regardless of how
        generous max_segment_m or the sagitta tolerance would otherwise allow.
        Both default to False -- pure sagitta/max_segment_m behaviour, as
        tested below -- because pinning is a stitching-pass concern, not a
        property of this chain in isolation. build_skeleton_network is the
        only real caller and currently passes False/False (measured
        insufficient alone -- recovers some largest-component connectivity at
        too high a node-count cost, and even combined with other fixes never
        reached the target; see the STITCH-DENSITY INVESTIGATION note). An earlier attempt
        restricting this to only degree-1 dead ends (excluding junction ends)
        erased almost the entire recovery that unconditional pinning did
        achieve, confirming Round 6's finding that a stitch candidate is
        routinely a junction/mid-chain node, "never a dead end, never
        seam-tagged" (see _stitch_component_pieces' Pass 0 docstring) -- so if
        this is revisited, pin BOTH kinds of end, not dead ends only.
        """
        n = len(pts)
        if n < 2:
            return

        sagitta_enabled = max_chord_sagitta_m > 0.0 and _chain_has_known_width(widths)

        if not sagitta_enabled:
            seg_start = 0
            acc = 0.0
            for i in range(1, n):
                _, _, d = self.geod.inv(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
                acc += d
                if acc >= max_segment_m or i == n - 1:
                    yield pts[seg_start:i + 1], widths[seg_start:i + 1]
                    seg_start = i
                    acc = 0.0
            return

        # Sagitta-bounded adaptive path. Walks forward exactly once; a candidate
        # extension that would push the segment's sagitta over tolerance closes the
        # segment at the PREVIOUS point instead (re-evaluating the same i as the
        # second point of the next segment, via `continue` without advancing i) --
        # so every emitted segment already satisfies its own tolerance, and the
        # very first extension of any new segment (2 points, no interior vertex)
        # always trivially passes. max_segment_m stays live as a backstop on the
        # non-sagitta-closed path, which also keeps candidate segments short
        # (<= max_segment_m / min pixel step) so the O(k) sagitta recheck on each
        # extension never turns quadratic in practice (§4.1 complexity note).
        #
        # Cumulative arc length from the chain's own start, precomputed once so
        # the stitch-density pin below can also see distance-to-END without a
        # second pass -- same geod.inv calls the loop needed anyway, just made
        # available up front.
        cum = [0.0] * n
        for k in range(1, n):
            _, _, d = self.geod.inv(pts[k - 1][0], pts[k - 1][1], pts[k][0], pts[k][1])
            cum[k] = cum[k - 1] + d
        total_len = cum[-1]

        seg_start = 0
        acc = 0.0
        i = 1
        while i < n:
            step_len = cum[i] - cum[i - 1]
            is_last = (i == n - 1)

            if (i - seg_start) >= 2:
                tol = _sagitta_tolerance_m(widths[seg_start:i + 1],
                                            max_chord_sagitta_m, sagitta_width_fraction)
                if _max_sagitta_m(pts[seg_start:i + 1]) > tol:
                    yield pts[seg_start:i], widths[seg_start:i]
                    seg_start = i - 1
                    acc = 0.0
                    continue

            # Stitch-density pin (connectivity-regression fix, see
            # the STITCH-DENSITY INVESTIGATION note above): within STITCH_PIN_RADIUS_M
            # of a dead-end end of THIS chain, tighten the backstop to the
            # legacy density instead of the caller's (possibly kilometres-long)
            # max_segment_m. _stitch_component_pieces and
            # _resolve_local_skeleton_gaps both search within a few hundred
            # metres of exactly these ends for a cross-piece candidate; a long
            # relaxed segment there was measured to remove the redundant nearby
            # nodes those passes depend on without shortening the actual gap.
            # A junction end is excluded (pin_start/pin_end False) -- it is
            # already part of this piece's own connected interior via its other
            # chains, so protecting it buys no stitching benefit, only cost.
            # The chain's INTERIOR -- where the node-count win actually comes
            # from -- is untouched. Only engages for chains longer than
            # STITCH_PIN_MIN_CHAIN_M, a threshold kept independent of
            # STITCH_PIN_RADIUS_M (see that constant's comment) -- a chain
            # shorter than it is, by construction, within reach of one of its
            # own ends for most/all of its length, so pinning it would
            # collapse to "never relax short chains at all" rather than
            # "protect the ends of long ones" -- not the intended effect, and
            # not what the measured regression (driven by long collapsed
            # reaches) needs fixed.
            effective_max_segment_m = max_segment_m

            acc += step_len
            if acc >= effective_max_segment_m or is_last:
                yield pts[seg_start:i + 1], widths[seg_start:i + 1]
                seg_start = i
                acc = 0.0
            i += 1

    def build_skeleton_network(self, polygon, source_tier=DEFAULT_SOURCE_TIER, source_id=None):
        """Extract medial-axis centerlines for one channel polygon and emit them into the graph."""
        # DIAGNOSTIC (connectivity-regression investigation): see build_navmesh_region's
        # matching comment -- same per-piece context tagging for this skeleton piece.
        self._piece_counter += 1
        piece_ctx = f"skel:{self._piece_counter}"
        cfg = self.classification_config
        utm = self._local_utm_crs(polygon)
        poly_m = gpd.GeoSeries([polygon], crs="EPSG:4326").to_crs(utm).iloc[0]
        b = poly_m.bounds
        min_dim = min(b[2] - b[0], b[3] - b[1])
        px = cfg.pixel_size_for(min_dim)
        land_m = self._land_union_for(polygon, utm)

        mask, transform, px = self._rasterize_water_polygon(poly_m, land_m, px)
        if mask is None or int(mask.sum()) < 3:
            return

        # SPEC-GRAPH-DENSITY.md §4.3: carve the mask BEFORE skeletonizing, so a
        # redundant medial-axis twin next to an authoritative inland_waterways axis
        # line is never generated (not generated-then-pruned). Gated on
        # axis_dedup_cap_m > 0.0 -- the default 0.0 skips this block entirely, so
        # gate 1 (byte-identical mask at cap 0) holds by construction, not by a
        # runtime check inside _axis_dedup_suppression_mask.
        line_iloc_by_suppressed_px: Dict[Tuple[int, int], int] = {}
        if cfg.axis_dedup_cap_m > 0.0:
            total_px = int(mask.sum())
            suppress, line_iloc_by_suppressed_px = self._axis_dedup_suppression_mask(
                mask, transform, utm, px, polygon)
            n_suppressed = int(suppress.sum())
            self.axis_dedup_stats["pieces_processed"] += 1
            self.axis_dedup_stats["total_water_px"] += total_px
            if n_suppressed:
                mask = mask & ~suppress
                self.axis_dedup_stats["pieces_with_suppression"] += 1
                self.axis_dedup_stats["suppressed_px"] += n_suppressed
                if int(mask.sum()) < 3:
                    return
            else:
                line_iloc_by_suppressed_px = {}

        skel, dist = self._extract_medial_axis_skeleton(mask)
        if int(skel.sum()) < 2:
            return
        G = self._skeleton_raster_to_graph(skel, dist, transform, utm, px)
        self._prune_skeleton_spurs(G, cfg.min_spur_length_m)

        # SPEC-GRAPH-DENSITY.md §6.3.1 Phase B: G's own nodes are raw raster pixel
        # tuples (row, col) -- recover that pixel identity per rounded lon/lat (the
        # same 5-decimal rounding _get_or_create_node itself applies) so a degree-1
        # dead end created below can be tested for carve-adjacency below without a
        # second geometric search. Built once here (not per dead end) and only when
        # this piece actually had any suppression at all.
        pixel_by_lonlat: Dict[Tuple[float, float], Tuple[int, int]] = {}
        if line_iloc_by_suppressed_px:
            for p, data in G.nodes(data=True):
                lon, lat = data["lonlat"]
                pixel_by_lonlat[(round(lon, 5), round(lat, 5))] = p

        added = 0
        node_occurrences: Dict[int, int] = {}
        for _, _, d in G.edges(data=True):
            # Stitch-density pin (STITCH_PIN_RADIUS_M): investigated, NOT
            # currently enabled -- see that constant's comment for the full
            # measurement history. Unconditional pinning at both ends (dead
            # end AND junction -- restricting to dead-ends-only erased almost
            # all of the recovery, since a real stitch candidate is routinely
            # a junction/mid-chain node) recovered some largest-component
            # connectivity but at too high a node-count cost, and even
            # combined with wider stitch-pass search radii never closed the
            # gap to the required 86.49% -- Pass 2 / gap-resolve, the only
            # union-find-gated passes (the ones that actually control this
            # metric), stayed degraded regardless. Passing False/False here
            # keeps this function's sagitta path fully unaffected pending a
            # real fix for that specific deficit.
            for sub_pts, sub_widths in self._resample_long_skeleton_edges(
                    d["pts"], d["width_profile"], cfg.max_segment_m,
                    cfg.max_chord_sagitta_m, cfg.sagitta_width_fraction):
                u = self._get_or_create_node(sub_pts[0][0], sub_pts[0][1], "coastal", context=piece_ctx)
                v = self._get_or_create_node(sub_pts[-1][0], sub_pts[-1][1], "coastal", context=piece_ctx)
                if u == v:
                    continue
                self._stamp_node(u, NODE_KIND_POINT, source_tier, source_id)
                self._stamp_node(v, NODE_KIND_POINT, source_tier, source_id)
                node_occurrences[u] = node_occurrences.get(u, 0) + 1
                node_occurrences[v] = node_occurrences.get(v, 0) + 1
                wp = json.dumps({"min_m": min(sub_widths), "samples_m": sub_widths})
                attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_CENTERLINE,
                             width_profile=wp, min_width=min(sub_widths),
                             source_tier=source_tier, source_id=source_id)
                if not self.graph.has_edge(u, v):
                    self.graph.add_edge(u, v, **attrs)
                    self.graph.add_edge(v, u, **attrs)
                    added += 2
        # Dead-end nodes of this skeleton piece (degree 1 within this piece's own
        # chain set) are exactly where the raster medial axis terminates short of
        # a bordering piece's boundary -- the candidates _stitch_component_pieces
        # actually needs prioritized, same reasoning as navmesh_seam_node_ids
        # above (see that set's comment and _stitch_component_pieces's docstring).
        for node_id, occurrences in node_occurrences.items():
            if occurrences == 1:
                self.navmesh_seam_node_ids.add(node_id)

        # SPEC-GRAPH-DENSITY.md §6.3.1 Phase B: among those SAME degree-1 dead ends,
        # reconnect the ones the carve above actually created -- adjacent to a
        # suppressed pixel -- to the specific inland_waterways line responsible,
        # rather than leaving them to generic stitching passes alone (the Hansweert
        # regression this spec section is named for: a carve-induced stub 178m from
        # any axis line went unrecovered because nothing looked for it specifically).
        # A genuine dead end (no suppressed neighbor) is untouched, left to those same
        # existing passes exactly as before.
        if line_iloc_by_suppressed_px:
            line_m_cache: Dict[int, Tuple[list, np.ndarray]] = {}
            dead_end_candidates = []
            for node_id, occurrences in node_occurrences.items():
                if occurrences != 1:
                    continue
                lon = self.graph.nodes[node_id]["lon"]
                lat = self.graph.nodes[node_id]["lat"]
                p = pixel_by_lonlat.get((lon, lat))
                if p is None:
                    continue
                row, col = p
                line_iloc = self._axis_dedup_nearest_line_for_suppressed_pixel(
                    row, col, line_iloc_by_suppressed_px)
                if line_iloc is None:
                    continue
                x_m, y_m = transform * (col + 0.5, row + 0.5)
                dead_end_candidates.append((node_id, line_iloc, (x_m, y_m)))

            capped = self._cap_reconnect_candidates_per_line(dead_end_candidates, "skeleton piece")
            for node_id, line_iloc, xy_m in capped:
                self.axis_dedup_reconnect_stats["skeleton_candidates"] += 1
                self.axis_dedup_reconnect_stats["skeleton_edges"] += self._connect_waterway_crossing(
                    node_id, line_iloc, utm, xy_m, line_m_cache)

        logger.debug(f"  skeleton polygon -> {added} centerline edges (px={px:.1f}m)")


    def _add_opening_bridge_edges(self):
        """Creates accurate nodes exactly at the bridge opening span using centerline intersection."""
        bridges_gdf = self.gdfs.get("bridges", gpd.GeoDataFrame())
        if bridges_gdf.empty:
            return

        logger.info("Adding opening bridge crossing edges (Fairway Intersections)...")
        added = 0

        # Combine Inland Waterways and Fairways to find the true navigable opening
        fw_gdfs = []
        if not self.gdfs.get("fairways_unified", gpd.GeoDataFrame()).empty:
            fw_gdfs.append(self.gdfs["fairways_unified"])
        if not self.gdfs.get("inland_waterways", gpd.GeoDataFrame()).empty: fw_gdfs.append(self.gdfs["inland_waterways"])
        hw_gdf = pd.concat(fw_gdfs, ignore_index=True) if fw_gdfs else gpd.GeoDataFrame()

        for _, row in bridges_gdf.iterrows():
            is_movable = False
            catbrg = _s57_col(row, "catbrg", "CATAQA", "CatBrg")
            if _is_valid(catbrg):
                vals = _parse_catbrg(catbrg)
                if any(v in ("3", "4", "5", "6", "7") for v in vals): is_movable = True
            if not is_movable:
                vercop = _s57_col(row, "vercop", "VERCOP", "VerCop")
                if _is_valid(vercop): is_movable = True
            if not is_movable:
                continue

            bridge_geom = row.geometry
            opening_pts = []

            # Determine exact opening location by intersecting bridge polygon with fairways
            if not hw_gdf.empty:
                intersecting = hw_gdf[hw_gdf.intersects(bridge_geom)]
                for _, hw_row in intersecting.iterrows():
                    intersection = bridge_geom.intersection(hw_row.geometry)
                    if isinstance(intersection, Point):
                        opening_pts.append(intersection)
                    elif isinstance(intersection, (MultiPoint, LineString, MultiLineString)):
                        opening_pts.append(intersection.centroid)

            # Fallback to bridge centroid if no fairway exists
            if not opening_pts:
                opening_pts.append(bridge_geom.centroid)

            bridge_src = self.layer_source_ids.get("bridges") if hasattr(self, "layer_source_ids") else None
            for pt in opening_pts:
                c_lon, c_lat = pt.x, pt.y
                b_id = self._get_or_create_node(c_lon, c_lat, node_type="coastal", context="bridge")
                self.graph.nodes[b_id]["node_depth"] = 99.0
                self._stamp_node(b_id, NODE_KIND_POINT, DEFAULT_SOURCE_TIER, bridge_src)

                # Connect opening to the 4 nearest surrounding nodes via Quadrant Ray-Casting
                SEARCH_MARGIN = 0.015
                candidates = []
                for nid, data in self.graph.nodes(data=True):
                    if nid == b_id: continue
                    lon, lat = data["lon"], data["lat"]
                    if (c_lon - SEARCH_MARGIN <= lon <= c_lon + SEARCH_MARGIN and
                        c_lat - SEARCH_MARGIN <= lat <= c_lat + SEARCH_MARGIN):
                        dx = (lon - c_lon) * 111320 * math.cos(math.radians((lat + c_lat) / 2))
                        dy = (lat - c_lat) * 111320
                        candidates.append((math.sqrt(dx*dx + dy*dy), lon, lat, nid))
                
                quadrants = {"NE": [], "NW": [], "SE": [], "SW": []}
                for d, lon, lat, nid in candidates:
                    if lon >= c_lon and lat >= c_lat: quadrants["NE"].append((d, nid))
                    elif lon < c_lon and lat >= c_lat: quadrants["NW"].append((d, nid))
                    elif lon >= c_lon and lat < c_lat: quadrants["SE"].append((d, nid))
                    else: quadrants["SW"].append((d, nid))
                    
                # Round 9 Issue E: this used to connect unconditionally to the
                # single nearest node per quadrant, with no land-crossing check
                # at all, and hardcoded crosses_land=0 on the edge it created --
                # confirmed against real data to genuinely cross land (a 434m
                # edge from a real Zandkreeksluis bridge node). Opening-bridge
                # edges are also the one edge category _sanity_check_no_land_crossings
                # exempts from its audit pass entirely, so this was structurally
                # the least land-crossing-verified edge type in the whole
                # pipeline. Walk each quadrant's candidates nearest-first and take
                # the first one that doesn't genuinely cross land/drying terrain
                # (same check _stitch_component_pieces already gates its own
                # connectors with), instead of blindly taking the nearest.
                for q, nodes in quadrants.items():
                    if nodes:
                        nodes.sort()
                        for _, cand_nid in nodes:
                            if self.graph.has_edge(b_id, cand_nid):
                                break  # already connected in this quadrant
                            cand_lon = self.graph.nodes[cand_nid]["lon"]
                            cand_lat = self.graph.nodes[cand_nid]["lat"]
                            candidate_line = LineString([(c_lon, c_lat), (cand_lon, cand_lat)])
                            if self._crosses_land(candidate_line):
                                continue  # try the next-nearest candidate in this quadrant
                            be = dict(edge_type="coastal", crosses_land=0, is_opening_bridge_edge=True,
                                      source_tier=DEFAULT_SOURCE_TIER, source_id=bridge_src)
                            self.graph.add_edge(b_id, cand_nid, **be)
                            self.graph.add_edge(cand_nid, b_id, **be)
                            added += 2
                            break

        logger.info(f"Added {added} precise bridge opening edges.")

    @staticmethod
    def _lock_polygon_name(row):
        """The lock's name, or None. A LOKBSN feature with no name cannot be
        told apart from the mis-tagged ones in this source, so both lock passes
        use this as their evidence that the polygon is a real lock."""
        for key in ("OBJNAM", "objnam", "name", "NOBJNM"):
            val = row.get(key) if hasattr(row, "get") else None
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    def _add_lock_crossing_edges(self):
        """Creates real connectivity across each lock chamber, mirroring
        `_add_opening_bridge_edges`'s precise-opening-point pattern
        (`PHASE_4_DESIGN.md` §4c) -- but adapted for a lock's two-gate
        topology instead of a bridge's single mid-span opening.

        Round 9/10 root-cause finding (see NEXT_PHASES.md): `locks_gdf` was
        only ever consulted in `_edge_attr_worker` to annotate an edge's
        `min_width` -- nothing ever CREATED an edge across a lock chamber.
        Confirmed directly against a real build: zero edges connected either
        side of the Zandkreeksluis lock chamber (20 nodes west, 14 east, no
        edge between any pair), forcing routes to detour tens of km around.

        A bridge gets one opening node whose surrounding quadrant search
        naturally reaches both banks, because a bridge span is short. A lock
        chamber is long enough, and its gates real enough physical barriers,
        that a single mid-chamber node can't be relied on to reach both
        sides via the same land-crossing-safe connection -- so this creates
        one node per side (at the precise point where a fairway/inland-
        waterway centerline crosses the lock polygon's boundary, same
        intersection approach the bridge version uses against the same two
        layers) plus an explicit chamber-transit edge directly between them.
        Verified against the real pilot data: 15/17 lock polygons in
        `data/zeeland_clip` intersect exactly one hw feature giving a clean
        entry/exit pair; 2/17 have no hw intersection at all and fall back
        to a single bridge-style centroid node instead.

        Tagged `requires_lock`/`lock_id` (PHASE_4_DESIGN.md §4c's marker,
        analogous to `is_opening_bridge_edge`) rather than reusing the
        bridge flag -- a lock transit and a bridge opening are physically
        different enough (chamber cycle time vs. instantaneous) that a
        future wait-time model (routeiq's `feature-bridge-lock-waits.md`)
        will need to tell them apart. The chamber-transit edge itself is
        additionally flagged `is_lock_transit_edge` since it's specifically
        the edge a future wait-time cost would attach to, distinct from the
        shore-side connector edges that merely reach the opening node.
        """
        locks_gdf = self.gdfs.get("locks", gpd.GeoDataFrame())
        if locks_gdf.empty:
            return

        logger.info("Adding lock crossing edges (Fairway/Waterway Intersections)...")
        added = 0
        transit_added = 0
        fallback_count = 0
        lock_count = 0
        skipped_unnamed = 0

        # Same two layers, same reasoning, as _add_opening_bridge_edges.
        fw_gdfs = []
        if not self.gdfs.get("fairways_unified", gpd.GeoDataFrame()).empty:
            fw_gdfs.append(self.gdfs["fairways_unified"])
        if not self.gdfs.get("inland_waterways", gpd.GeoDataFrame()).empty: fw_gdfs.append(self.gdfs["inland_waterways"])
        hw_gdf = pd.concat(fw_gdfs, ignore_index=True) if fw_gdfs else gpd.GeoDataFrame()

        lock_src = self.layer_source_ids.get("locks") if hasattr(self, "layer_source_ids") else None

        for lock_idx, row in locks_gdf.iterrows():
            lock_geom = row.geometry
            if lock_geom is None or lock_geom.is_empty:
                continue
            # Named polygons only, for the reason spelled out in
            # _mark_edges_inside_locks: LOKBSN here carries features that are
            # not locks at all -- a 66 m2 polygon at Oude Tonge where there is
            # none, against 249 m2 for the smallest real one -- and an unnamed
            # feature gives nothing to tell them apart by. Building transit
            # edges for one invents a lock, and a route through it is then
            # charged an hour that does not exist.
            if not self._lock_polygon_name(row):
                skipped_unnamed += 1
                continue
            lock_count += 1
            lock_id = int(lock_idx) + 1  # opaque per-lock marker value, not a pois FK

            # Candidate opening-point PAIRS: for each intersecting hw feature,
            # where its own sub-line crosses the lock polygon's boundary
            # (typically exactly 2 points -- one entry, one exit). Kept
            # per-feature rather than pooling every point across every
            # intersecting feature and taking the global farthest pair --
            # confirmed against real data that a lock touched by several
            # unrelated short hw segments (a convergence of channels near
            # the lock, not real gate crossings) can otherwise produce a
            # spurious "pair" that spans across land.
            candidate_pairs = []  # (span, ptA, ptB)
            if not hw_gdf.empty:
                intersecting = hw_gdf[hw_gdf.intersects(lock_geom)]
                for _, hw_row in intersecting.iterrows():
                    b = lock_geom.boundary.intersection(hw_row.geometry)
                    pts = []
                    if isinstance(b, Point):
                        pts = [b]
                    elif isinstance(b, MultiPoint):
                        pts = list(b.geoms)
                    elif isinstance(b, (LineString, MultiLineString)):
                        continue  # tangential touch, not a clean crossing pair
                    if len(pts) >= 2:
                        best_pair, best_span = None, -1.0
                        for i in range(len(pts)):
                            for j in range(i + 1, len(pts)):
                                d = pts[i].distance(pts[j])
                                if d > best_span:
                                    best_span, best_pair = d, (pts[i], pts[j])
                        candidate_pairs.append((best_span, best_pair[0], best_pair[1]))

            # Prefer the widest span first (most likely the real full-chamber
            # crossing), but require it to actually clear land/drying terrain
            # -- same _crosses_land gate Round 9 Issue E added to the bridge
            # version's shore connections, applied here to the chamber-
            # spanning line itself too, not just the shore connectors below.
            candidate_pairs.sort(key=lambda t: -t[0])
            side_pts = None
            for _, pt_a, pt_b in candidate_pairs:
                transit_line = LineString([(pt_a.x, pt_a.y), (pt_b.x, pt_b.y)])
                if not self._crosses_land(transit_line):
                    side_pts = [pt_a, pt_b]
                    break

            if side_pts is None:
                # No safe two-point crossing found (no hw intersection at all,
                # or every candidate pair crosses land/drying terrain) --
                # fall back to the bridge pattern's single centroid node so
                # the quadrant search below at least has a chance of finding
                # land-crossing-safe connections on both sides.
                side_pts = [lock_geom.centroid]
                fallback_count += 1

            side_node_ids = []
            for pt in side_pts:
                c_lon, c_lat = pt.x, pt.y
                n_id = self._get_or_create_node(c_lon, c_lat, node_type="coastal", context="lock_crossing")
                self.graph.nodes[n_id]["node_depth"] = 99.0
                self._stamp_node(n_id, NODE_KIND_POINT, DEFAULT_SOURCE_TIER, lock_src)
                side_node_ids.append(n_id)

                # Connect this side's node outward to the nearest surrounding
                # nodes via the same quadrant ray-casting + land-crossing gate
                # _add_opening_bridge_edges uses (Round 9 Issue E's fix) --
                # do NOT hardcode crosses_land=0 and connect blindly.
                SEARCH_MARGIN = 0.015
                candidates = []
                for nid, data in self.graph.nodes(data=True):
                    if nid == n_id or nid in side_node_ids: continue
                    lon, lat = data["lon"], data["lat"]
                    if (c_lon - SEARCH_MARGIN <= lon <= c_lon + SEARCH_MARGIN and
                        c_lat - SEARCH_MARGIN <= lat <= c_lat + SEARCH_MARGIN):
                        dx = (lon - c_lon) * 111320 * math.cos(math.radians((lat + c_lat) / 2))
                        dy = (lat - c_lat) * 111320
                        candidates.append((math.sqrt(dx*dx + dy*dy), lon, lat, nid))

                quadrants = {"NE": [], "NW": [], "SE": [], "SW": []}
                for d, lon, lat, nid in candidates:
                    if lon >= c_lon and lat >= c_lat: quadrants["NE"].append((d, nid))
                    elif lon < c_lon and lat >= c_lat: quadrants["NW"].append((d, nid))
                    elif lon >= c_lon and lat < c_lat: quadrants["SE"].append((d, nid))
                    else: quadrants["SW"].append((d, nid))

                for q, nodes in quadrants.items():
                    if nodes:
                        nodes.sort()
                        for _, cand_nid in nodes:
                            if self.graph.has_edge(n_id, cand_nid):
                                break  # already connected in this quadrant
                            cand_lon = self.graph.nodes[cand_nid]["lon"]
                            cand_lat = self.graph.nodes[cand_nid]["lat"]
                            candidate_line = LineString([(c_lon, c_lat), (cand_lon, cand_lat)])
                            if self._crosses_land(candidate_line):
                                continue  # try the next-nearest candidate in this quadrant
                            le = dict(edge_type="coastal", crosses_land=0, requires_lock=True,
                                      lock_id=lock_id, source_tier=DEFAULT_SOURCE_TIER, source_id=lock_src)
                            self.graph.add_edge(n_id, cand_nid, **le)
                            self.graph.add_edge(cand_nid, n_id, **le)
                            added += 2
                            break

            # Explicit chamber-transit edge connecting the two side nodes
            # directly -- this is the actual connectivity fix. Without it,
            # both sides can each independently gain real quadrant
            # connections to their own bank and still never connect to each
            # other, which is exactly the confirmed Zandkreeksluis gap.
            # Already verified land/drying-safe above when the pair was
            # selected, so no further _crosses_land check needed here.
            if len(side_node_ids) == 2:
                a_id, b_id = side_node_ids
                if not self.graph.has_edge(a_id, b_id):
                    te = dict(edge_type="coastal", crosses_land=0, requires_lock=True,
                              lock_id=lock_id, is_lock_transit_edge=True,
                              source_tier=DEFAULT_SOURCE_TIER, source_id=lock_src)
                    self.graph.add_edge(a_id, b_id, **te)
                    self.graph.add_edge(b_id, a_id, **te)
                    added += 2
                    transit_added += 2

        logger.info(f"Added {added} lock crossing edges ({transit_added} chamber-transit) "
                    f"across {lock_count} named lock polygons ({fallback_count} single-node "
                    f"fallback, {skipped_unnamed} unnamed polygons skipped).")

    def _mark_edges_inside_locks(self):
        """Tag every edge that runs through a lock chamber, not just the transit
        edge this pipeline builds for it.

        `_add_lock_crossing_edges` derives its entry/exit pair from where a
        fairway or waterway centreline crosses the lock polygon's boundary. Where
        the centreline does not line up with the chamber -- the two-of-seventeen
        fallback case that method documents, and any lock the skeletonised
        network reaches by a different edge -- a route can pass straight through
        the chamber on edges carrying no lock marking at all. Observed on
        Krammersluizen: a route demonstrably inside the polygon, charged as an
        opening bridge because nothing on its path said "lock".

        Named polygons only. LOKBSN in this source is not reliable enough to
        trust unnamed features: the smallest named lock here is 249 m2, while
        unnamed ones include a 66 m2 feature at Oude Tonge where there is no lock
        at all. Marking by geometry would spread that error across every edge
        near it, so a name is required as the evidence that the polygon is real.
        A lock the charts omit entirely -- Grevelingensluis, which appears only
        as its bridges -- is not solvable here and belongs in the manual
        override data instead.
        """
        locks_gdf = self.gdfs.get("locks", gpd.GeoDataFrame())
        if locks_gdf.empty:
            return

        named = [
            (idx, row) for idx, row in locks_gdf.iterrows()
            if self._lock_polygon_name(row)
        ]
        if not named:
            logger.info("No named lock polygons — skipping in-chamber edge marking")
            return

        marked = 0
        for lock_idx, row in named:
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            # Same opaque marker _add_lock_crossing_edges uses, so an edge it
            # already tagged and one tagged here name the same lock.
            lock_id = int(lock_idx) + 1
            minx, miny, maxx, maxy = geom.bounds
            for u, v, data in self.graph.edges(data=True):
                if data.get("requires_lock"):
                    continue
                nu, nv = self.graph.nodes[u], self.graph.nodes[v]
                # Cheap bbox reject before the real intersection test: a lock is
                # metres across and the graph has hundreds of thousands of edges.
                if (max(nu["lon"], nv["lon"]) < minx or min(nu["lon"], nv["lon"]) > maxx
                        or max(nu["lat"], nv["lat"]) < miny or min(nu["lat"], nv["lat"]) > maxy):
                    continue
                seg = LineString([(nu["lon"], nu["lat"]), (nv["lon"], nv["lat"])])
                if geom.intersects(seg):
                    data["requires_lock"] = True
                    data["lock_id"] = lock_id
                    marked += 1
        logger.info(
            f"Marked {marked} existing edge(s) as passing through {len(named)} named lock(s)"
        )

    def _sanity_check_no_land_crossings(self):
        """Step F — land-crossing safety, retired as load-bearing for skeleton edges.

        Placeholder edges come from unconstrained Delaunay and are the ONE place
        land-crossing risk still remains, so they are actively stripped (as before).
        Skeleton/lane edges are land-safe by construction (built from the
        water-minus-land raster mask), so they get only an informational sampled
        spot-check — never stripped.

        NOTE (deviation from plan Step F): the plan proposed *not* stripping and
        merely asserting >0.5%. Real pilot data shows the placeholder path crosses
        land at ~2.5%, so stripping is retained for placeholder edges until Phase 1
        replaces them with constrained-Delaunay navmesh_regions.

        Phase 1: `build_navmesh_placeholder` is no longer called, so the
        `placeholder` bucket below is always empty in practice -- kept rather
        than removed since it's harmless (guarded division) and the function
        it detects remains in the file, unreferenced. Navmesh fallback edges
        (edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY) don't set `is_placeholder`,
        so they fall into the lenient `skeleton` bucket below by construction.

        Round 9 Issue E: opening-bridge edges used to be exempted from this
        entire audit pass ("never touched"), which combined with the quadrant
        connector's own lack of a land-crossing check (see
        `_add_opening_bridge_edges`) made them structurally the least
        land-crossing-verified edge type in the pipeline. `_add_opening_bridge_edges`
        now pre-checks each candidate before adding it, so this is defense in
        depth, not the only check -- but they're no longer exempt here either;
        they fall into the same `skeleton` bucket as everything else below.
        """
        coastal_gdf = self.gdfs.get("coastal_water", gpd.GeoDataFrame())
        land_gdf = self.gdfs.get("land", gpd.GeoDataFrame())
        if coastal_gdf.empty:
            return

        placeholder, skeleton = [], []
        for u, v, data in self.graph.edges(data=True):
            if data.get("edge_type") != "coastal" or u >= v:
                continue
            u_lon, u_lat = self.graph.nodes[u]["lon"], self.graph.nodes[u]["lat"]
            v_lon, v_lat = self.graph.nodes[v]["lon"], self.graph.nodes[v]["lat"]
            rec = {"u": u, "v": v,
                   "geometry": LineString([(u_lon, u_lat), (v_lon, v_lat)]),
                   "midpoint": Point((u_lon + v_lon) / 2.0, (u_lat + v_lat) / 2.0),
                   "edge_kind_id": data.get("edge_kind_id", EDGE_KIND_CENTERLINE)}
            (placeholder if data.get("is_placeholder") else skeleton).append(rec)

        # --- Placeholder edges: strip land-crossers (temporary path) ---
        removed = 0
        if placeholder:
            g = gpd.GeoDataFrame(placeholder, geometry="geometry", crs=self.CRS_WGS84)
            g["midpoint"] = gpd.GeoSeries([r["midpoint"] for r in placeholder], crs=self.CRS_WGS84)
            mids = g.set_geometry("midpoint")
            valid = gpd.sjoin(mids[["u", "v", "midpoint"]], coastal_gdf[["geometry"]],
                              predicate="within", how="inner")
            valid_uv = set(zip(valid["u"], valid["v"]))
            to_remove = set((r["u"], r["v"]) for r in placeholder if (r["u"], r["v"]) not in valid_uv)
            if not land_gdf.empty:
                survivors = g[[(r_u, r_v) in valid_uv for r_u, r_v in zip(g["u"], g["v"])]]
                if not survivors.empty:
                    crossed = gpd.sjoin(survivors.set_geometry("geometry"),
                                        land_gdf[["geometry"]], predicate="crosses", how="inner")
                    to_remove.update(zip(crossed["u"], crossed["v"]))
            for u, v in to_remove:
                if self.graph.has_edge(u, v):
                    self.graph.remove_edge(u, v); removed += 1
                if self.graph.has_edge(v, u):
                    self.graph.remove_edge(v, u); removed += 1
            rate = len(to_remove) / max(1, len(placeholder))
            logger.info(f"  Placeholder land-safety: stripped {removed} directed edges "
                        f"({rate:.1%} of {len(placeholder)} placeholder edges crossed land/left water).")

        # --- Skeleton/lane edges: strip only GENUINE land crossings ---
        # Skeleton edges are near-land-safe by construction, but the straight graph
        # chord between two medial-axis samples can clip a bank on a tight bend. We
        # remove only edges that actually intersect a land polygon (the true hazard),
        # not the over-strict midpoint-in-water test used for the placeholder path.
        if skeleton and not land_gdf.empty:
            sg = gpd.GeoDataFrame(skeleton, geometry="geometry", crs=self.CRS_WGS84)
            crossed = gpd.sjoin(sg[["u", "v", "geometry"]], land_gdf[["geometry"]],
                                predicate="intersects", how="inner")
            crossed_uv = set(zip(crossed["u"], crossed["v"]))
            sk_removed = 0
            for u, v in crossed_uv:
                if self.graph.has_edge(u, v):
                    self.graph.remove_edge(u, v); sk_removed += 1
                if self.graph.has_edge(v, u):
                    self.graph.remove_edge(v, u); sk_removed += 1
            rate = len(crossed_uv) / max(1, len(skeleton))
            logger.info(f"  Skeleton land-safety: stripped {sk_removed} directed edges "
                        f"({rate:.2%} of {len(skeleton)} skeleton edges clipped land on a bend).")

        # --- Ring-perimeter / stitching edges: also strip genuine drying/intertidal
        # crossings, not just land ---
        # Scoped to edge_kind_id==EDGE_KIND_NAVMESH_BOUNDARY (navmesh ring-perimeter
        # edges from build_navmesh_region, plus generic connectors from
        # _stitch_component_pieces) rather than every skeleton-bucket edge:
        # medial-axis centerline edges legitimately thread through complex
        # braided tidal-flat terrain where a raw DEPARE-drying intersection is
        # much noisier signal (see Round 9 Issue G, not yet resolved); a straight
        # polygon-boundary or stitching chord genuinely crossing charted drying
        # terrain is a much cleaner correctness signal -- this is the exact class
        # of edge the Round 9 master finding's 6,666m drval1=-2.0 example was.
        drying_gdf = self._drying_gdf()
        if skeleton and not drying_gdf.empty:
            ring_or_stitch = [r for r in skeleton if r["edge_kind_id"] == EDGE_KIND_NAVMESH_BOUNDARY]
            if ring_or_stitch:
                rg = gpd.GeoDataFrame(ring_or_stitch, geometry="geometry", crs=self.CRS_WGS84)
                dry_crossed = gpd.sjoin(rg[["u", "v", "geometry"]], drying_gdf[["geometry"]],
                                        predicate="intersects", how="inner")
                dry_crossed_uv = set(zip(dry_crossed["u"], dry_crossed["v"]))
                dry_removed = 0
                for u, v in dry_crossed_uv:
                    if self.graph.has_edge(u, v):
                        self.graph.remove_edge(u, v); dry_removed += 1
                    if self.graph.has_edge(v, u):
                        self.graph.remove_edge(v, u); dry_removed += 1
                rate = len(dry_crossed_uv) / max(1, len(ring_or_stitch))
                logger.info(f"  Ring/stitch drying-safety: stripped {dry_removed} directed edges "
                            f"({rate:.2%} of {len(ring_or_stitch)} navmesh-boundary-kind edges "
                            f"crossed charted drying/intertidal terrain).")

        for u, v, data in self.graph.edges(data=True):
            if data.get("edge_type") == "coastal":
                self.graph.edges[u, v]["crosses_land"] = 0
        logger.info("Land-crossing sanity check complete.")

    def _ensure_coastal_connectivity(self):
        """Final connectivity guarantee, run once, after all other edge construction
        and stripping is done for this run.

        Splitting each connected water body by width (build_network) and then
        stripping any edge that genuinely crosses land (_sanity_check_no_land_crossings,
        just above) are each individually necessary, but doing a per-piece stitch
        *during* construction (tried first) wastes the effort: edges added mid-build
        can still get stripped by the land-crossing check that runs afterward,
        re-fragmenting whatever they'd bridged. Running the connectivity guarantee
        here instead -- after stripping, once, over the whole graph -- means nothing
        it adds can be invalidated by a later step in this same run.
        """
        coastal_gdf = self.gdfs.get("coastal_water")
        if coastal_gdf is None or coastal_gdf.empty:
            return
        coastal_nodes = [n for n, d in self.graph.nodes(data=True) if d.get("node_type") != "inland"]
        if len(coastal_nodes) < 2:
            return

        # Round 15 (NEXT_PHASES.md §5.2.2): inland nodes whose coordinate falls
        # INSIDE a coastal_water component's own polygon get a shot at this
        # component's stitch pass too -- an inland-typed vertex sitting in what's
        # charted as open coastal water is a source-layering artifact (the
        # inland_waterways line-work happens to run through/near a stretch also
        # covered by coastal_water), not a real barrier. Scoped to actual polygon
        # CONTAINMENT, not merely stitch-radius proximity, so an inland canal
        # reach behind a lock (never inside a coastal_water polygon) can never
        # reach this pass -- can't manufacture a lock bypass. The connectivity
        # itself is still gated by _stitch_component_pieces' own
        # within(poly_m)/_crosses_land checks (its Pass 0d, mirroring Pass 0c) --
        # this only changes the candidate list.
        inland_nodes = [n for n, d in self.graph.nodes(data=True) if d.get("node_type") == "inland"]
        if inland_nodes:
            inland_points = gpd.GeoSeries(
                [Point(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"]) for n in inland_nodes],
                index=inland_nodes, crs=self.CRS_WGS84)
            inland_sindex = inland_points.sindex
        else:
            inland_points, inland_sindex = None, None

        # Scope the stitch pass to each ORIGINAL connected water body separately,
        # rather than unioning/buffering the whole dataset's coastal water in one
        # call: _stitch_component_pieces' own docstring says it only ever reconnects
        # pieces exploded from the same original component, so doing this per
        # component both bounds the per-call union/buffer cost (cheap at Zeeland-
        # pilot scale, a real cost at full-country scale) and makes that same-body-
        # only intent structurally enforced rather than incidentally true.
        components = self._connected_water_polygons(coastal_gdf)
        node_points = gpd.GeoSeries(
            [Point(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"]) for n in coastal_nodes],
            index=coastal_nodes, crs=self.CRS_WGS84)
        sindex = node_points.sindex

        total_added = 0
        total_inland_candidates = 0
        for i, component in enumerate(components):
            if (i + 1) % 20 == 0 or i == 0:
                logger.info(f"  Coastal connectivity: component {i + 1}/{len(components)}...")
            probe = component.buffer(0.001)  # ~100m in degrees; generous spatial pre-filter only
            candidates = list(node_points.index[sindex.query(probe, predicate="intersects")])

            inland_candidates = []
            if inland_sindex is not None:
                # Cheap bbox prefilter first (sindex, symmetric predicate), then an
                # exact vectorized containment test on the (small) surviving set --
                # same two-stage pattern build_navmesh_placeholder's _in_poly_coords
                # already uses for point-in-polygon filtering elsewhere in this file.
                bbox_idx = inland_points.index[inland_sindex.query(component, predicate="intersects")]
                if len(bbox_idx):
                    xs = np.array([self.graph.nodes[n]["lon"] for n in bbox_idx])
                    ys = np.array([self.graph.nodes[n]["lat"] for n in bbox_idx])
                    inside = shapely.contains_xy(component, xs, ys)
                    inland_candidates = [n for n, keep in zip(bbox_idx, inside) if keep]
                    total_inland_candidates += len(inland_candidates)

            all_candidates = candidates + inland_candidates
            if len(all_candidates) < 2:
                continue
            total_added += self._stitch_component_pieces(all_candidates, component, snap_radius_m=500.0)

        logger.info(f"Final coastal connectivity pass: added {total_added} stitching edges "
                    f"across {len(components)} components ({total_inland_candidates} in-polygon "
                    f"inland-node candidates included).")
        lgrs = self.local_gap_resolve_stats
        if lgrs["gaps_resolved"]:
            logger.info(f"Local gap-resolve pass: reconnected {lgrs['gaps_resolved']} narrow-throat "
                        f"gaps ({lgrs['edges_added']} edges added) that all passes above left "
                        f"disconnected.")

        # DIAGNOSTIC (connectivity-regression investigation): dump per-pass
        # attempt/outcome counters and aggregate union-find group counts. Not
        # part of the shipped feature -- read by scratchpad analysis scripts.
        gs = self._stitch_group_stats
        logger.info(f"STITCH_DIAG group_stats: initial_groups={gs['initial_groups']} "
                    f"final_groups={gs['final_groups']} "
                    f"components_with_residual={gs['components_with_residual']} "
                    f"residual_groups_total={gs['residual_groups_total']}")
        for pass_name in sorted(self._stitch_diag.keys()):
            d = self._stitch_diag[pass_name]
            parts = " ".join(f"{k}={v}" for k, v in sorted(d.items()))
            logger.info(f"STITCH_DIAG pass={pass_name} {parts}")

    def _log_node_origin_diag(self, label="post_stitch"):
        """DIAGNOSTIC (coordinator's follow-up on the connectivity-regression
        investigation): distinguish a genuine cross-piece/cross-subsystem
        coordinate merge (a node whose coordinate was independently requested by
        >1 context -- see self._node_contexts' comment in __init__) from a piece
        just reusing its own node, and cross-tab against final (undirected)
        degree. A degree>=3 "junction" in the exported graph that only ever had
        ONE context is a real raster nodepix junction (build_skeleton_network's
        own degree>=3 pixel, or build_navmesh_region's own perimeter branching);
        one with >1 context exists only because two independently-created things
        (different skeleton/navmesh pieces, or a bridge/lock/waterway-crossing
        node, or a gap-resolve remesh) landed on the exact same rounded
        (5-decimal, ~1m) coordinate. Run once, right after all stitching passes
        settle the graph's final topology -- calculate_edge_attributes/
        _compute_node_depths/export below never add or remove a node or edge.
        """
        if not self._node_origin_diag:
            return
        coastal_nodes = [n for n, d in self.graph.nodes(data=True) if d.get("node_type") != "inland"]
        total_nodes = len(coastal_nodes)
        no_context = 0
        cross_context = 0
        junctions_total = 0
        junctions_single_ctx = 0
        junctions_multi_ctx = 0
        for n in coastal_nodes:
            ctxs = self._node_contexts.get(n)
            n_ctx = len(ctxs) if ctxs else 0
            if n_ctx == 0:
                no_context += 1
            elif n_ctx > 1:
                cross_context += 1
            undirected_deg = len(set(self.graph.successors(n)) | set(self.graph.predecessors(n)))
            if undirected_deg >= 3:
                junctions_total += 1
                if n_ctx > 1:
                    junctions_multi_ctx += 1
                else:
                    junctions_single_ctx += 1
        logger.info(f"NODE_ORIGIN_DIAG label={label} total_coastal_nodes={total_nodes} "
                    f"no_context_tracked={no_context} cross_context_nodes={cross_context} "
                    f"junctions_total={junctions_total} junctions_single_context={junctions_single_ctx} "
                    f"junctions_multi_context={junctions_multi_ctx}")

    # ------------------------------------------------------------------
    # Round 25 cross-database seam stitching (STITCHING_DESIGN.md Section 3).
    # All three methods below are only ever called when self.stitch_registry_path
    # is truthy (see run_pipeline) -- with stitching off, none of this runs and
    # single-region behavior is byte-for-byte unchanged.
    # ------------------------------------------------------------------

    def _get_coverage_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """This region's coverage bbox: the straight rectangle used for both
        the adopt pass's registry query and the publish pass's boundary-node
        (near-clip-edge) detection. `self.coverage_bbox` (from `--coverage-bbox`,
        typically build_region.sh's already-overlap-expanded clip bbox) wins
        when given; otherwise falls back to the union bounds of every loaded
        GeoJSON layer, which is the actual clipped data extent for a build
        that didn't pass one explicitly. Cached -- the loaded layers don't
        change mid-build.
        """
        if self._coverage_bbox_cache is not None:
            return self._coverage_bbox_cache
        if self.coverage_bbox is not None:
            bbox = tuple(self.coverage_bbox)
        else:
            lons: List[float] = []
            lats: List[float] = []
            for gdf in self.gdfs.values():
                if gdf is None or gdf.empty:
                    continue
                b = gdf.total_bounds  # minx, miny, maxx, maxy
                if np.all(np.isfinite(b)):
                    lons.extend([b[0], b[2]])
                    lats.extend([b[1], b[3]])
            bbox = (min(lons), min(lats), max(lons), max(lats)) if lons else None
        self._coverage_bbox_cache = bbox
        return bbox

    def _adopt_seam_nodes(self) -> set:
        """Adopt pass (STITCHING_DESIGN.md Section 3.3): query the shared seam
        registry for nodes inside this region's coverage bbox and splice in any
        not already present in this build's graph, VERBATIM -- same id/lon/lat/
        node_kind_id/node_depth, never recomputed -- then connect each to up to
        2 nearby NATIVE (pre-existing, not itself adopted) graph nodes so the
        seam is genuinely reachable from this region's own interior. Runs after
        `_ensure_coastal_connectivity` (native connectivity is already settled)
        and before `calculate_edge_attributes` (so the new connector edges get
        normal edge-attribute computation like any other edge, per run_pipeline).

        Returns the set of node ids actually adopted this build (used by the
        publish pass's dedupe check).
        """
        bbox = self._get_coverage_bbox()
        if bbox is None:
            logger.warning("Seam registry adopt pass: no coverage bbox available, skipping.")
            return set()

        with SeamRegistry(self.stitch_registry_path) as registry:
            rows = registry.query_bbox(*bbox)

        if not rows:
            logger.info(f"Seam registry adopt pass: 0 candidate nodes in bbox {bbox}.")
            return set()

        # Native = whatever this build's own construction + connectivity passes
        # produced, BEFORE any adoption -- adopted nodes only ever connect to
        # this set, never to each other (STITCHING_DESIGN.md Section 3.3).
        native_coastal_ids = [n for n, d in self.graph.nodes(data=True)
                               if d.get("node_type") != "inland"]
        native_points = None
        native_sindex = None
        if native_coastal_ids:
            native_points = gpd.GeoSeries(
                [Point(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"])
                 for n in native_coastal_ids],
                index=native_coastal_ids, crs=self.CRS_WGS84)
            native_sindex = native_points.sindex

        # Diagnostic only, never connected to: the inland network is excluded from
        # candidates above (per Section 3.3), so when an adopted node has no
        # coastal neighbour we want to know whether it had an INLAND one -- that
        # distinguishes "published into water the adopter doesn't cover" from
        # "published next to water the adopter models as inland", which is the
        # known inland/coastal stitching gap (NEXT_PHASES Round 6, Section 5.2.1).
        inland_ids = [n for n, d in self.graph.nodes(data=True)
                      if d.get("node_type") == "inland"]
        self._adopt_inland_points = None
        self._adopt_inland_sindex = None
        if inland_ids:
            self._adopt_inland_points = gpd.GeoSeries(
                [Point(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"])
                 for n in inland_ids],
                index=inland_ids, crs=self.CRS_WGS84)
            self._adopt_inland_sindex = self._adopt_inland_points.sindex

        coastal_gdf = self.gdfs.get("coastal_water")
        components = (self._connected_water_polygons(coastal_gdf)
                      if coastal_gdf is not None and not coastal_gdf.empty else [])
        components_sindex = None
        if components:
            components_gs = gpd.GeoSeries(components, crs=self.CRS_WGS84)
            components_sindex = components_gs.sindex

        adopted_ids = set()
        already_present = 0
        unconnected = 0
        self._adopt_proj_cache = {}
        self._adopt_reject_stats = defaultdict(int)
        logger.info(f"Seam registry adopt pass: {len(rows)} candidate nodes in bbox {bbox}, "
                    f"connecting against {len(native_coastal_ids)} native nodes...")
        for i, row in enumerate(rows):
            if i and i % 200 == 0:
                logger.info(f"  Adopt pass: {i}/{len(rows)} candidates processed, "
                            f"{len(adopted_ids)} adopted so far.")
            node_id = row["node_id"]
            if node_id in self.graph:
                already_present += 1
                continue
            lon, lat = row["lon"], row["lat"]
            node_depth = row["node_depth"] if row["node_depth"] is not None else UNKNOWN_DEPTH
            # Added verbatim: same id, same coordinate, same kind -- this build
            # does NOT recompute or re-hash it (the whole point of the registry).
            self.graph.add_node(
                node_id, lon=lon, lat=lat, node_type="coastal",
                node_kind_id=row["node_kind_id"], node_depth=node_depth,
                source_tier=DEFAULT_SOURCE_TIER, source_id=None,
            )
            self.coords_to_node[(round(lon, 5), round(lat, 5))] = node_id
            adopted_ids.add(node_id)

            n_conn = self._connect_adopted_node(
                node_id, lon, lat, components, components_sindex,
                native_points, native_sindex)
            if n_conn == 0:
                unconnected += 1

        if unconnected:
            logger.info("  Adopt pass unconnected breakdown: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self._adopt_reject_stats.items())))
        logger.info(
            f"Seam registry adopt pass: {len(adopted_ids)} nodes adopted from "
            f"'{self.stitch_registry_path}' ({already_present} already present in "
            f"this build's graph, {unconnected} left unconnected -- no valid "
            f"native neighbour within {self.stitch_radius_m:.0f}m)."
        )
        return adopted_ids

    def _connect_adopted_node(self, node_id, lon: float, lat: float, components: List[Polygon],
                               components_sindex, native_points, native_sindex,
                               max_connectors: int = 2) -> int:
        """Connect one just-adopted seam node to up to `max_connectors` nearest
        NATIVE graph nodes, gated the same way Pass 0c/0d's local-adjacency
        connectors are (`_stitch_component_pieces`): candidates restricted to
        the same water component, the straight connector must stay `within`
        that component's polygon, and must not `_crosses_land`. Returns the
        number of edges added (0 is fine -- the node stays in the graph,
        unconnected on this side; harmless per STITCHING_DESIGN.md Section 3.3,
        and is what `--overlap-deg >= stitch_band_m` sizing (Section 3.5) is
        meant to make rare at a real seam).
        """
        if native_points is None or native_sindex is None or not components:
            return 0
        pt = Point(lon, lat)

        comp_idx = None
        if components_sindex is not None:
            hits = list(components_sindex.query(pt.buffer(0.0005), predicate="intersects"))
            if hits:
                comp_idx = int(hits[0])
        if comp_idx is None:
            # FP/erosion jitter can leave an adopted node just outside every
            # polygon boundary at the seam -- fall back to nearest component.
            # Via the spatial index: the plain linear scan over every component
            # was one of two reasons this pass stalled for >9 minutes with no
            # output on a real region (thousands of components x hundreds of
            # adopted nodes).
            if components_sindex is not None:
                try:
                    comp_idx = int(np.atleast_1d(components_sindex.nearest(pt))[-1])
                except Exception:
                    comp_idx = min(range(len(components)),
                                   key=lambda i: components[i].distance(pt))
            else:
                comp_idx = min(range(len(components)),
                               key=lambda i: components[i].distance(pt))
        target_component = components[comp_idx]

        # Candidate prefilter: a stitch_radius_m box around the adopted node, not
        # the whole buffered component. Equivalent by construction -- every
        # candidate is distance-filtered to stitch_radius_m below, and any
        # candidate in a *different* water body still fails the within(poly_m)
        # gate -- but bounded by the radius instead of by the component's size.
        # The old whole-component probe pulled in every native node of a body as
        # large as the Gulf of Maine and reprojected all of them, per adopted
        # node: the other reason this pass did not finish on a real region.
        deg_lat = self.stitch_radius_m / 111_320.0
        deg_lon = self.stitch_radius_m / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
        probe = box(lon - deg_lon, lat - deg_lat, lon + deg_lon, lat + deg_lat)
        cand_hits = native_sindex.query(probe, predicate="intersects")
        # int() is load-bearing: native_points.index is a pandas Int64Index, so
        # index[i] is a numpy.int64. Used as a networkx key it compares/hashes
        # equal to the plain int (so no duplicate node appears), but the freshly
        # adopted node's adjacency dict is empty, so IT is the object stored as
        # the key -- and sqlite3 binds numpy scalars through the buffer protocol,
        # writing edges.target as an 8-byte BLOB. routeiq then drops those rows
        # (target not in the node map), silently killing the outgoing half of
        # every adopt-pass connector edge.
        candidate_ids = [int(native_points.index[i]) for i in cand_hits]
        if not candidate_ids:
            # No coastal node in range at all. Distinguish the two causes: water
            # the adopter simply does not cover, versus water it models as part of
            # the inland network (which Section 3.3 excludes from candidates).
            if (self._adopt_inland_sindex is not None
                    and len(self._adopt_inland_sindex.query(probe, predicate="intersects"))):
                self._adopt_reject_stats["only_inland_nodes_in_radius"] += 1
            else:
                self._adopt_reject_stats["no_node_of_any_kind_in_radius"] += 1
            return 0

        # Per-component projection is cached: adopted seam nodes cluster along one
        # clip edge, so the same component recurs for hundreds of them, and
        # reprojecting a large component polygon each time is pure waste.
        cached = self._adopt_proj_cache.get(comp_idx)
        if cached is None:
            utm = self._local_utm_crs(target_component)
            base = gpd.GeoSeries([target_component], crs=self.CRS_WGS84).to_crs(utm).iloc[0]
            cached = {"utm": utm, "base": base, "tight": base.buffer(2.0), "tol": None}
            self._adopt_proj_cache[comp_idx] = cached
        utm = cached["utm"]
        pt_m = gpd.GeoSeries([pt], crs=self.CRS_WGS84).to_crs(utm).iloc[0]

        # `within(poly_m)` needs the WHOLE connector inside the water body -- the
        # adopted node included. A node authored from the neighbour's geometry
        # usually sits just outside this build's polygon, so the old flat 2m
        # buffer failed every candidate before they were even considered. Widen
        # the polygon only for such nodes, and only to ADOPT_POLY_TOLERANCE_M;
        # nodes already inside keep the original tight buffer unchanged.
        outside_m = pt_m.distance(cached["base"])
        if outside_m > ADOPT_POLY_TOLERANCE_M:
            self._adopt_reject_stats["node_too_far_outside_water"] += 1
            return 0
        if outside_m > 0:
            if cached["tol"] is None:
                cached["tol"] = cached["base"].buffer(ADOPT_POLY_TOLERANCE_M)
            poly_m = cached["tol"]
        else:
            poly_m = cached["tight"]
        cand_pts_wgs84 = [Point(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"])
                           for n in candidate_ids]
        cand_pts_m = gpd.GeoSeries(cand_pts_wgs84, crs=self.CRS_WGS84).to_crs(utm)

        scored = []
        for cid, c_wgs84, cm in zip(candidate_ids, cand_pts_wgs84, cand_pts_m):
            d = pt_m.distance(cm)
            if d <= self.stitch_radius_m:
                scored.append((d, cid, c_wgs84, cm))
        scored.sort(key=lambda t: t[0])
        # Why a node ends up unconnected matters: a node with no candidate in
        # range is a publish/overlap problem, while one whose candidates are all
        # rejected by the polygon or land gate is a gating problem. Round 25's
        # East Coast build could not tell those apart (Maryland reported 471 of
        # 478 unconnected while 421 of them had a native node within 500m).
        if not scored:
            self._adopt_reject_stats["no_candidate_in_radius"] += 1
        rejected_poly = rejected_land = 0

        added = 0
        for _dist_m, cid, c_wgs84, cm in scored:
            if added >= max_connectors:
                break
            if self.graph.has_edge(node_id, cid):
                continue
            if not LineString([pt_m, cm]).within(poly_m):
                rejected_poly += 1
                continue
            if self._crosses_land(LineString([(lon, lat), (c_wgs84.x, c_wgs84.y)])):
                rejected_land += 1
                continue
            attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                         source_tier=DEFAULT_SOURCE_TIER, source_id=None)
            self.graph.add_edge(node_id, cid, **attrs)
            self.graph.add_edge(cid, node_id, **attrs)
            added += 1
        if added == 0 and scored:
            if rejected_poly and not rejected_land:
                self._adopt_reject_stats["all_candidates_outside_water_polygon"] += 1
            elif rejected_land and not rejected_poly:
                self._adopt_reject_stats["all_candidates_cross_land"] += 1
            else:
                self._adopt_reject_stats["candidates_rejected_mixed"] += 1
        return added

    def _publish_seam_nodes(self, adopted_ids: set):
        """Publish pass (STITCHING_DESIGN.md Section 3.2): upsert this region's
        own BOUNDARY nodes -- graph nodes within `stitch_band_m` of the region's
        coverage-bbox rectangle edges (the artificial clip cut, not natural
        coastline) and sitting in water -- into the shared seam registry,
        tagged `source_region=self.region_name` for provenance. Idempotent
        (upsert keyed by node_id). Skips a boundary node that sits within a
        small dedupe radius of a node THIS build just adopted, so the registry
        doesn't accumulate near-duplicate seam nodes as more regions get built
        against it (Section 3.2's dedupe). Runs once the graph and its node
        depths are final for this build, right before export_to_sqlite.
        """
        bbox = self._get_coverage_bbox()
        if bbox is None:
            logger.warning("Seam registry publish pass: no coverage bbox available, skipping.")
            return
        min_lon, min_lat, max_lon, max_lat = bbox

        coastal_gdf = self.gdfs.get("coastal_water")
        water_union = None
        if coastal_gdf is not None and not coastal_gdf.empty:
            geoms = [g for g in coastal_gdf.geometry if g is not None and not g.is_empty]
            if geoms:
                # Small buffer (~2m in degrees) so a node sitting exactly on
                # the water polygon's own boundary (the common case for
                # navmesh perimeter / skeleton endpoint nodes) still counts
                # as "in water" despite shapely `contains` excluding boundary
                # points.
                water_union = unary_union(geoms).buffer(0.00002)

        # Degrees-per-metre at this bbox's latitude, for a cheap band-membership
        # test against the coverage-bbox rectangle edges -- a region-scale
        # approximation, not used for any routing geometry/distance elsewhere.
        mid_lat = (min_lat + max_lat) / 2.0
        lat_deg_per_m = 1.0 / 111320.0
        lon_deg_per_m = 1.0 / (111320.0 * max(0.1, math.cos(math.radians(mid_lat))))
        band_lat_deg = self.stitch_band_m * lat_deg_per_m
        band_lon_deg = self.stitch_band_m * lon_deg_per_m

        DEDUPE_RADIUS_M = 25.0  # "small radius" (Section 3.2) -- well under stitch_radius_m/band
        adopted_coords = [(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"])
                          for n in adopted_ids if n in self.graph]
        adopted_points_m = None
        dedupe_utm = None
        if adopted_coords:
            dedupe_utm = self._local_utm_crs(Point(adopted_coords[0]))
            adopted_points_m = gpd.GeoSeries(
                [Point(c) for c in adopted_coords], crs=self.CRS_WGS84).to_crs(dedupe_utm)

        rows = []
        skipped_land = 0
        skipped_dedupe = 0
        for n, data in self.graph.nodes(data=True):
            if data.get("node_type") == "inland":
                continue
            lon, lat = data["lon"], data["lat"]
            near_edge = (
                (lon - min_lon) <= band_lon_deg or (max_lon - lon) <= band_lon_deg or
                (lat - min_lat) <= band_lat_deg or (max_lat - lat) <= band_lat_deg
            )
            if not near_edge:
                continue
            if (lon < min_lon - band_lon_deg or lon > max_lon + band_lon_deg or
                    lat < min_lat - band_lat_deg or lat > max_lat + band_lat_deg):
                continue  # genuinely outside the coverage bbox -- shouldn't happen
            if water_union is not None and not water_union.contains(Point(lon, lat)):
                skipped_land += 1
                continue
            if adopted_points_m is not None:
                p_m = gpd.GeoSeries([Point(lon, lat)], crs=self.CRS_WGS84).to_crs(dedupe_utm).iloc[0]
                if adopted_points_m.distance(p_m).min() <= DEDUPE_RADIUS_M:
                    skipped_dedupe += 1
                    continue
            rows.append({
                "node_id": n, "lon": lon, "lat": lat,
                "node_kind_id": data.get("node_kind_id", NODE_KIND_POINT),
                "node_depth": data.get("node_depth", UNKNOWN_DEPTH),
                "source_region": self.region_name,
            })

        if not rows:
            logger.info("Seam registry publish pass: 0 boundary nodes to publish.")
            return

        with SeamRegistry(self.stitch_registry_path) as registry:
            registry.upsert_nodes(rows)

        logger.info(
            f"Seam registry publish pass: {len(rows)} boundary nodes upserted into "
            f"'{self.stitch_registry_path}' (source_region={self.region_name!r}; "
            f"{skipped_land} skipped as on land, {skipped_dedupe} skipped as within "
            f"{DEDUPE_RADIUS_M:.0f}m of a node this build adopted)."
        )

    def _compute_node_depths(self, adopted_ids: set = frozenset()):
        """Recompute node_depth from this build's own DEPARE coverage.

        Nodes in `adopted_ids` were just spliced in verbatim from the seam
        registry by `_adopt_seam_nodes()` (STITCHING_DESIGN.md Section 3.3),
        carrying another build's already-computed depth. Per NEXT_PHASES.md
        "2026-08-14": only overwrite an adopted node's depth when this
        build's own local DEPARE data actually contains that point -- an
        adopted node sitting just outside this build's DEPARE coverage
        (plausible right at a seam; `--overlap-deg` guarantees a nearby edge
        neighbour, not DEPARE polygon coverage at that exact point) keeps the
        registry's real depth instead of being silently downgraded to
        UNKNOWN_DEPTH.
        """
        depare_gdf = self.gdfs.get("depth_areas", gpd.GeoDataFrame())
        if depare_gdf.empty:
            for nid, data in self.graph.nodes(data=True):
                if nid in adopted_ids:
                    continue
                data["node_depth"] = UNKNOWN_DEPTH
            return

        logger.info("Computing node depths from DEPARE polygons...")
        positive = depare_gdf.copy()
        
        total = self.graph.number_of_nodes()
        found = 0
        for i, (nid, data) in enumerate(self.graph.nodes(data=True)):
            if (i + 1) % max(1, total // 10) == 0:
                logger.info(f"  Node depths: {i + 1}/{total} ({found} found)")

            pt = Point(data["lon"], data["lat"])
            candidates = list(positive.sindex.intersection(pt.bounds))
            # Evaluate every containing candidate, not just the first (CodeRabbit
            # PR #3) -- the same multi-scale-cell overlap Round 18 fixed for
            # edges: a coarse overview cell and a fine harbor cell routinely
            # both contain the same point, and taking whichever the spatial
            # index happened to return first made node_depth silently
            # order-dependent and able to disagree with the edges meeting it,
            # which always did evaluate every candidate. The deepest/finest
            # containing DRVAL1 wins here too, for the same reason.
            best = None
            best_upper = None
            best_cscl = None
            any_containing = False
            for idx in candidates:
                row = positive.iloc[idx]
                if row.geometry.contains(pt):
                    any_containing = True
                    if "DRVAL1" in row and pd.notnull(row["DRVAL1"]):
                        val = float(row["DRVAL1"])
                        cscl = row["src_cscl"] if "src_cscl" in row else None
                        row_cscl = int(cscl) if pd.notnull(cscl) else None
                        upper = row["DRVAL2"] if "DRVAL2" in row else None
                        row_upper = float(upper) if pd.notnull(upper) else None
                        if _depare_candidate_beats(val, row_cscl, row_upper,
                                                   best, best_cscl, best_upper):
                            best = val
                            best_upper = row_upper
                            best_cscl = row_cscl
            if not any_containing and nid in adopted_ids:
                # No local DEPARE reaches this adopted node -- keep the depth
                # spliced in verbatim from the seam registry rather than
                # downgrading a real, already-computed value to unknown.
                continue
            if best is not None:
                # Preserve sign -- see UNKNOWN_DEPTH: a genuine drying height
                # is real data, not flooring material.
                depth = best
                # See DRYING_BAND_IMPLAUSIBLE_DRVAL1_M: an implausibly extreme
                # DRVAL1 with a plausible DRVAL2 is a coarse-band placeholder
                # on the drying side, not a real reading.
                if (depth < DRYING_BAND_IMPLAUSIBLE_DRVAL1_M and best_upper is not None
                        and best_upper >= DRYING_BAND_IMPLAUSIBLE_DRVAL1_M):
                    depth = best_upper
                # Same coarse-cell check _edge_attr_worker applies (see
                # TRUSTED_SURVEY_CSCL_MAX): an exact-zero DRVAL1 from a coarse
                # cell is a band floor, not a survey minimum, whether
                # signalled by a wide DRVAL2 band or a coarse source CSCL --
                # this function previously had no such check at all, so a
                # node could read node_depth=0.0 (trusted) at the exact same
                # coordinate an edge correctly flags UNKNOWN_DEPTH.
                if depth == 0.0 and (
                    (best_upper is not None and best_upper >= COARSE_DEPTH_BAND_DRVAL2_M)
                    or (best_cscl is not None and best_cscl > TRUSTED_SURVEY_CSCL_MAX)
                ):
                    depth = UNKNOWN_DEPTH
                found += 1
            elif any_containing:
                depth = 99.0
                found += 1
            else:
                depth = UNKNOWN_DEPTH
            data["node_depth"] = depth

    def calculate_edge_attributes(self):
        logger.info("Calculating advanced edge attributes (multiprocessing)...")
        total_edges = self.graph.number_of_edges()
        
        num_workers = max(1, min(int(mp.cpu_count() * 0.8), 4))

        def edge_generator():
            for u, v, data in self.graph.edges(data=True):
                u_node = self.graph.nodes[u]
                v_node = self.graph.nodes[v]
                yield (
                    u, v,
                    u_node["lon"], u_node["lat"],
                    v_node["lon"], v_node["lat"],
                    data.get("edge_type", "coastal"),
                    data.get("is_opening_bridge_edge", False),
                    data.get("requires_lock", False),
                    data.get("min_width"),
                )

        def chunked_iterable(iterable, size):
            import itertools
            it = iter(iterable)
            while True:
                chunk = tuple(itertools.islice(it, size))
                if not chunk: break
                yield chunk

        fw_gdfs = []
        if not self.gdfs.get("fairways_unified", gpd.GeoDataFrame()).empty:
            fw_gdfs.append(self.gdfs["fairways_unified"])
        if not self.gdfs.get("inland_waterways", gpd.GeoDataFrame()).empty: fw_gdfs.append(self.gdfs["inland_waterways"])
        highways_gdf = pd.concat(fw_gdfs, ignore_index=True) if fw_gdfs else gpd.GeoDataFrame(geometry=[])

        worker_gdfs = {
            "land_metric": self.gdfs_metric.get("land", gpd.GeoDataFrame()),
            "depth_areas": self.gdfs.get("depth_areas", gpd.GeoDataFrame()),
            "bridges": self.gdfs.get("bridges", gpd.GeoDataFrame()),
            "fairways": highways_gdf,
            "dredged_areas": self.gdfs.get("dredged_areas", gpd.GeoDataFrame()),
            "locks": self.gdfs.get("locks", gpd.GeoDataFrame()),
            "obstacles": self.gdfs.get("obstacles", gpd.GeoDataFrame()),
            "obstacles_soft": self.gdfs.get("obstacles_soft", gpd.GeoDataFrame()),
        }
        for gdf in worker_gdfs.values():
            if not gdf.empty:
                _ = gdf.sindex  

        logger.info(f"  Spawning {num_workers} workers for {total_edges} edges...")
        merged = 0
        CHUNK_SIZE = 25000 

        with mp.Pool(num_workers, initializer=_edge_attr_init, initargs=(self.geod, worker_gdfs)) as pool:
            result_iterator = pool.imap_unordered(
                _edge_attr_worker, 
                chunked_iterable(edge_generator(), CHUNK_SIZE)
            )
            for results in result_iterator:
                for (u, v), attrs in results.items():
                    for key, value in attrs.items():
                        self.graph.edges[u, v][key] = value
                    merged += 1
                if merged % 250000 == 0 or merged == total_edges:
                    logger.info(f"  Edge attributes merged: {merged}/{total_edges} ({(merged/total_edges)*100:.1f}%)")

    def _compute_boundary_geometry(self) -> Tuple[str, str]:
        nodes = [(data["lon"], data["lat"]) for _, data in self.graph.nodes(data=True)]
        if not nodes:
            empty = json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]})
            return (json.dumps({"min_lat": 0, "min_lon": 0, "max_lat": 0, "max_lon": 0}), empty)

        lons, lats = zip(*nodes)
        bbox = json.dumps({
            "min_lat": min(lats), "min_lon": min(lons),
            "max_lat": max(lats), "max_lon": max(lons)
        })

        if len(nodes) < 3:
            poly = Polygon([(min(lons), min(lats)), (min(lons), max(lats)),
                            (max(lons), max(lats)), (max(lons), min(lats)),
                            (min(lons), min(lats))])
        else:
            points = [Point(lon, lat) for lon, lat in nodes]
            hull = MultiPoint(points).convex_hull
            if isinstance(hull, (Point, LineString)):
                poly = Polygon([(min(lons), min(lats)), (min(lons), max(lats)),
                                (max(lons), max(lats)), (max(lons), min(lats)),
                                (min(lons), min(lats))])
            else:
                poly = hull

        geom_json = json.dumps(poly.__geo_interface__)
        return (bbox, geom_json)

    def export_to_sqlite(self):
        logger.info(f"Exporting data to SQLite database at '{self.db_path}'...")
        
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.executescript("""
                CREATE TABLE metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    country TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    last_update_date TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    bounding_box TEXT,
                    boundary_geometry TEXT,
                    schema_version INTEGER DEFAULT 2,
                    -- The value this build reserves for "depth unknown" (see
                    -- UNKNOWN_DEPTH). A consumer must read this column, not
                    -- schema_version, to decide whether a negative min_depth/
                    -- node_depth is a real drying height or unknown --
                    -- schema_version numbers the database FORMAT and had
                    -- already reached 3 on legacy-convention builds for
                    -- unrelated reasons before this column existed, so no
                    -- schema_version threshold can separate the two
                    -- conventions (routeiq ROUTEIQ_NEXT_PHASES.md, "Depth-
                    -- unknown is declared by the file, not inferred").
                    -- NULL (the default) means the legacy convention: any
                    -- negative value is unknown.
                    depth_unknown_sentinel REAL DEFAULT NULL,
                    contributor TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    license TEXT DEFAULT '',
                    copyright TEXT DEFAULT '',
                    architecture TEXT DEFAULT '',
                    dataset_version TEXT DEFAULT ''
                );

                CREATE TABLE data_sources (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    url TEXT,
                    license TEXT,
                    attribution_text TEXT,
                    accessed_date TEXT,
                    default_tier INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE edge_type_enum (
                    id INTEGER PRIMARY KEY,
                    description TEXT NOT NULL
                );
                INSERT INTO edge_type_enum VALUES (0, 'coastal'), (1, 'inland');

                CREATE TABLE poi_type_enum (
                    id INTEGER PRIMARY KEY,
                    description TEXT NOT NULL
                );
                INSERT INTO poi_type_enum VALUES
                    (0, 'harbour'), (1, 'lock'), (2, 'bridge'), (3, 'fairway'), (4, 'waterway');

                CREATE TABLE edge_kind_enum (
                    id INTEGER PRIMARY KEY,
                    description TEXT NOT NULL
                );
                INSERT INTO edge_kind_enum VALUES
                    (0, 'centerline'), (1, 'navmesh_boundary'), (2, 'lane'), (3, 'macro');

                CREATE TABLE node_kind_enum (
                    id INTEGER PRIMARY KEY,
                    description TEXT NOT NULL
                );
                INSERT INTO node_kind_enum VALUES
                    (0, 'point'), (1, 'navmesh_vertex'), (2, 'supernode');

                CREATE TABLE nodes (
                    id INTEGER PRIMARY KEY,
                    lat REAL,
                    lon REAL,
                    node_depth REAL DEFAULT -999,
                    region_id INTEGER REFERENCES metadata(id) ON DELETE CASCADE,
                    node_kind_id INTEGER DEFAULT 0 REFERENCES node_kind_enum(id),
                    source_tier INTEGER DEFAULT 1,
                    source_id INTEGER REFERENCES data_sources(id)
                );

                CREATE TABLE edges (
                    source INTEGER,
                    target INTEGER,
                    distance REAL,
                    min_depth REAL,
                    drval1 REAL,
                    max_air_draft REAL,
                    min_width REAL,
                    cost_factor REAL DEFAULT 1.2,
                    distance_to_land REAL DEFAULT 9999.0,
                    edge_type_id INTEGER DEFAULT 0 REFERENCES edge_type_enum(id),
                    traffic_mode INTEGER DEFAULT 0,
                    crosses_land INTEGER DEFAULT 0,
                    crosses_obstacle INTEGER DEFAULT 0,
                    edge_kind_id INTEGER DEFAULT 0 REFERENCES edge_kind_enum(id),
                    source_tier INTEGER DEFAULT 1,
                    source_id INTEGER REFERENCES data_sources(id),
                    width_profile TEXT,
                    requires_lock INTEGER DEFAULT 0,
                    lock_id INTEGER,
                    FOREIGN KEY(source) REFERENCES nodes(id),
                    FOREIGN KEY(target) REFERENCES nodes(id)
                );

                CREATE TABLE pois (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    type_id INTEGER REFERENCES poi_type_enum(id),
                    properties TEXT,
                    lat REAL,
                    lon REAL,
                    region_id INTEGER REFERENCES metadata(id) ON DELETE CASCADE,
                    source_tier INTEGER DEFAULT 1,
                    source_id INTEGER REFERENCES data_sources(id)
                );

                -- Phase 1 tables: DDL present, zero rows in Phase 0 (avoids a future ALTER/schema bump)
                CREATE TABLE navmesh_regions (
                    id INTEGER PRIMARY KEY,
                    region_id INTEGER REFERENCES metadata(id) ON DELETE CASCADE,
                    boundary_geometry TEXT,
                    vertices TEXT,
                    triangles TEXT,
                    triangle_adjacency TEXT,
                    boundary_node_ids TEXT,
                    depth_ceiling_m REAL,
                    source_tier INTEGER DEFAULT 1,
                    source_id INTEGER REFERENCES data_sources(id)
                );

                CREATE TABLE override_provenance (
                    id INTEGER PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_ref TEXT NOT NULL,
                    reason TEXT,
                    evidence TEXT,
                    contributor TEXT,
                    reviewer TEXT NOT NULL,
                    date TEXT,
                    source_pr_url TEXT
                );

                CREATE INDEX idx_edges_source ON edges(source);
                CREATE INDEX idx_edges_target ON edges(target);
                CREATE INDEX idx_nodes_lat_lon ON nodes(lat, lon);
            """)

            cursor.executemany(
                """INSERT INTO data_sources
                   (id, name, source_type, url, license, attribution_text, accessed_date, default_tier)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(i + 1, s["name"], s["source_type"], s["url"], s["license"],
                  s["attribution_text"], s["accessed_date"], s["default_tier"])
                 for i, s in enumerate(_default_data_sources())]
            )
            
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            bbox_json, boundary_json = self._compute_boundary_geometry()
            cursor.execute(
                """INSERT INTO metadata
                   (country, name, description, last_update_date, tags, bounding_box, boundary_geometry, schema_version, depth_unknown_sentinel, contributor, url, license, copyright, architecture, dataset_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (self.country, self.region_name, self.description, now_utc,
                 self.tags, bbox_json, boundary_json, DEPTH_SENTINEL_SCHEMA_VERSION, UNKNOWN_DEPTH,
                 self.contributor, self.url,
                 self.license, self.copyright, self.architecture, self.dataset_version)
            )
            region_id = cursor.lastrowid
            
            # int() on every node id below is a guardrail, not cosmetics: a
            # numpy integer id reaching sqlite3 is bound through the buffer
            # protocol and lands as an 8-byte BLOB instead of an INTEGER, which
            # consumers silently drop (they look ids up in a numeric node map).
            nodes_data = [(int(n), data["lat"], data["lon"],
                           data.get("node_depth", UNKNOWN_DEPTH),
                           region_id,
                           data.get("node_kind_id", NODE_KIND_POINT),
                           data.get("source_tier", DEFAULT_SOURCE_TIER),
                           data.get("source_id"))
                          for n, data in self.graph.nodes(data=True)]
            cursor.executemany("INSERT INTO nodes (id, lat, lon, node_depth, region_id, node_kind_id, source_tier, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", nodes_data)
            
            edges_data = [(
                int(u), int(v),
                data.get("distance", 0.0),
                data.get("min_depth", 99.0),
                data.get("drval1"),
                data.get("max_air_draft", 999.0),
                data.get("min_width", 999.0),
                data.get("cost_factor", 1.2),
                data.get("distance_to_land", 9999.0),
                EDGE_TYPE_COASTAL if data.get("edge_type", "coastal") == "coastal" else EDGE_TYPE_INLAND,
                data.get("traffic_mode", TRAFFIC_TWO_WAY),
                int(data.get("crosses_land", 0)),
                int(data.get("crosses_obstacle", 0)),
                data.get("edge_kind_id", EDGE_KIND_CENTERLINE),
                data.get("source_tier", DEFAULT_SOURCE_TIER),
                data.get("source_id"),
                data.get("width_profile"),
                int(data.get("requires_lock", False)),
                data.get("lock_id")
            ) for u, v, data in self.graph.edges(data=True)]
            cursor.executemany("""
                INSERT INTO edges
                (source, target, distance, min_depth, drval1, max_air_draft, min_width, cost_factor, distance_to_land, edge_type_id, traffic_mode, crosses_land, crosses_obstacle, edge_kind_id, source_tier, source_id, width_profile, requires_lock, lock_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, edges_data)

            navmesh_regions_data = [(
                region_id,
                r["boundary_geometry"], r["vertices"], r["triangles"], r["triangle_adjacency"],
                r["boundary_node_ids"], r["depth_ceiling_m"], r["source_tier"], r["source_id"]
            ) for r in self.navmesh_region_rows]
            cursor.executemany("""
                INSERT INTO navmesh_regions
                (region_id, boundary_geometry, vertices, triangles, triangle_adjacency, boundary_node_ids, depth_ceiling_m, source_tier, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, navmesh_regions_data)

            def _poi_name(row) -> str:
                for col in ("OBJNAM", "NOBJNM", "name"):
                    val = row.get(col)
                    if val is not None and not (isinstance(val, float) and np.isnan(val)):
                        return str(val)
                return ""

            def _poi_point(geom):
                if isinstance(geom, Point):
                    return (geom.y, geom.x)
                elif isinstance(geom, Polygon):
                    c = geom.centroid
                    return (c.y, c.x)
                elif isinstance(geom, LineString):
                    c = geom.interpolate(0.5, normalized=True)
                    return (c.y, c.x)
                return None

            POI_TYPE_MAP = {
                "harbour": POI_TYPE_HARBOUR,
                "lock": POI_TYPE_LOCK,
                "bridge": POI_TYPE_BRIDGE,
                "fairway": POI_TYPE_FAIRWAY,
                "waterway": POI_TYPE_WATERWAY,
            }

            def _poi_properties(row, default_type) -> str:
                props = {}
                if default_type == "bridge":
                    is_opening = False
                    catbrg = _s57_col(row, "catbrg", "CATAQA", "CatBrg")
                    if _is_valid(catbrg):
                        vals = _parse_catbrg(catbrg)
                        if any(v in ("3", "4", "5", "6", "7") for v in vals):
                            is_opening = True
                    if not is_opening:
                        vercop = _s57_col(row, "vercop", "VERCOP", "VerCop")
                        if _is_valid(vercop):
                            is_opening = True
                    if is_opening:
                        props["subtype"] = "opening"
                    else:
                        props["subtype"] = "fixed"
                        verclr = _s57_get_val(row, "verclr", "VERCLR", "VerClr")
                        if verclr is not None and pd.notnull(verclr):
                            props["height"] = float(verclr)
                return json.dumps(props)

            poi_layers = [
                ("pois", "harbour"),
                ("locks", "lock"),
                ("bridges", "bridge"),
                ("fairways", "fairway"),
                ("inland_waterways", "waterway"),
            ]
            layer_source_ids = getattr(self, "layer_source_ids", {})
            poi_data = []
            for layer_key, default_type in poi_layers:
                gdf = self.gdfs.get(layer_key, gpd.GeoDataFrame())
                if gdf.empty:
                    continue
                poi_src = layer_source_ids.get(layer_key)
                for _, row in gdf.iterrows():
                    name = _poi_name(row)
                    if not name:
                        continue
                    pt = _poi_point(row.geometry)
                    if pt is None:
                        continue
                    pid = self._generate_poi_id(default_type, pt[0], pt[1])
                    type_id = POI_TYPE_MAP.get(default_type, POI_TYPE_HARBOUR)
                    poi_data.append((pid, name, type_id, _poi_properties(row, default_type),
                                     pt[0], pt[1], region_id, DEFAULT_SOURCE_TIER, poi_src))
            if poi_data:
                cursor.executemany("INSERT OR IGNORE INTO pois (id, name, type_id, properties, lat, lon, region_id, source_tier, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", poi_data)
                logger.info(f"Inserted {len(poi_data)} named POIs from {len(poi_layers)} layers")
            else:
                logger.warning("No named POIs found in any layer")
                
            conn.commit()
            cursor.execute("VACUUM;")
        logger.info("Export completed and database vacuumed/compressed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build routing graph SQLite database from GeoJSON layers.")
    parser.add_argument("--input-dir", default="./output_geojson",
                        help="Directory containing preprocessed GeoJSON files (default: ./output_geojson)")
    parser.add_argument("--output", default="./routing_graph.sqlite",
                        help="Output SQLite database path (default: ./output_geojson)")
    parser.add_argument("--country", default="",
                        help="ISO country code for this region (e.g., NL, BE)")
    parser.add_argument("--name", default="",
                        help="Human-readable region name (e.g., Netherlands)")
    parser.add_argument("--description", default="",
                        help="Optional description of the data coverage")
    parser.add_argument("--tags", default="[]",
                        help='JSON array of tags, e.g. \'["official","rws","enc"]\'')
    parser.add_argument("--contributor", default="",
                        help="GitHub username or organization that contributed this data")
    parser.add_argument("--url", default="",
                        help="Source URL for the original data")
    parser.add_argument("--license", default="",
                        help="License string for the dataset (metadata.license)")
    parser.add_argument("--copyright", default="",
                        help="Copyright / attribution string (metadata.copyright)")
    parser.add_argument("--architecture", default="navmesh-hybrid-phase1",
                        help="Graph architecture label (metadata.architecture)")
    parser.add_argument("--dataset-version", default="",
                        help="Dataset version string (metadata.dataset_version)")
    parser.add_argument("--navmesh-edge-m", type=float, default=NAVMESH_TARGET_EDGE_M,
                        help="Target navmesh triangle edge length in metres (default "
                             f"{NAVMESH_TARGET_EDGE_M:.0f}). Raise it for a coarse offshore "
                             "passage layer -- e.g. 12000 for open ocean, where 650m detail "
                             "is meaningless and merely expensive.")
    parser.add_argument("--depth-ceiling", type=float, default=6.0,
                        help="Depth ceiling (m) for water-body classification (Step B; used from Session 2 onward)")
    parser.add_argument("--sagitta-cap", type=float, default=0.0,
                        help="Cap (m) on chord-to-centerline deviation for sagitta-bounded adaptive "
                             "skeleton resampling (SPEC-GRAPH-DENSITY.md §4.1). The effective tolerance "
                             "is min(this cap, 0.5 * local channel width) -- narrow channels always get a "
                             "tighter tolerance than the cap. Default 0.0 DISABLES this entirely and "
                             "reproduces today's unconditional max_segment_m resampling; edges with no "
                             "measured width are excluded from relaxation regardless of this value.")
    parser.add_argument("--max-segment-m", type=float, default=None,
                        help="Override ClassificationConfig.max_segment_m, the hard length "
                             "backstop on resampled centerline segments (default 100.0). "
                             "Raise it when --sagitta-cap is active: at 100m the backstop "
                             "binds before the sagitta rule on every straight reach, which "
                             "is exactly where the node reduction was supposed to come from.")
    parser.add_argument("--axis-dedup-cap", type=float, default=0.0,
                        help="Cap (m) on suppression distance from an authoritative inland_waterways "
                             "axis line (wtwaxs/RECTRC/NAVLNE) when carving the coastal-water raster "
                             "before skeletonizing (SPEC-GRAPH-DENSITY.md §4.3). The effective "
                             "tolerance is clip(0.5 * local channel width, 5.0, this cap) -- narrow "
                             "channels always get a tighter tolerance than the cap. Default 0.0 "
                             "DISABLES this entirely and reproduces today's skeleton raster byte-for-"
                             "byte -- build_skeleton_network never generates a redundant medial-axis "
                             "twin next to a channel the pipeline already ingested from the "
                             "inland_waterways source.")
    parser.add_argument("--inland-densify-max-segment-m", type=float, default=0.0,
                        help="Insert interpolated vertices into inland_waterways lines so no "
                             "segment exceeds this length in metres (SPEC-GRAPH-DENSITY.md "
                             "§6.4). A sparsely-digitized chart-scale line (vertices 1000m+ "
                             "apart) makes _connect_waterway_crossing's nearest-existing-vertex "
                             "search and _build_inland_network's raw ingestion collapse many "
                             "distinct crossing points onto one distant vertex, producing "
                             "fan/star-shaped 'hub' nodes. Default 0.0 DISABLES this entirely "
                             "and leaves inland_waterways geometry byte-identical to today's. "
                             "Recommended once enabled: 100-150m, comfortably under "
                             "WATERWAY_CONNECTOR_MAX_M (250m). Must be finite and >= "
                             f"{INLAND_DENSIFY_MIN_SEGMENT_M}m if enabled (raises otherwise) "
                             "-- shapely.segmentize has no vertex-count ceiling of its own.")
    parser.add_argument("--stitch-registry", nargs="?", const="data/seam_registry.sqlite", default="",
                        help="Enable Round 25 cross-database seam stitching (STITCHING_DESIGN.md "
                             "Section 3) against the shared global-node registry at this SQLite "
                             "path. Give a bare flag with no path to use the default "
                             "'data/seam_registry.sqlite'. Falls back to the SK_ROUTING_STITCH_REGISTRY "
                             "env var if omitted. Omit entirely (default): stitching is off and "
                             "behavior is unchanged from before Round 25.")
    parser.add_argument("--coverage-bbox", default="",
                        help='"min_lon,min_lat,max_lon,max_lat" -- this region\'s data coverage '
                             "extent (its clip rectangle, INCLUDING any --overlap-deg expansion "
                             "already baked into the input GeoJSON). Used only when --stitch-registry "
                             "is set, for the adopt/publish bbox query and boundary-node detection. "
                             "Defaults to the union bounds of the loaded GeoJSON layers when omitted.")
    parser.add_argument("--stitch-band-m", type=float, default=300.0,
                        help="Publish pass: a graph node within this many metres of the coverage-bbox "
                             "rectangle edge is a 'boundary node' eligible for publishing to the seam "
                             "registry (default 300).")
    parser.add_argument("--stitch-radius-m", type=float, default=500.0,
                        help="Adopt pass: max distance (m) from an adopted seam node to a native "
                             "graph node it may connect to (default 500).")
    args = parser.parse_args()

    stitch_registry_path = args.stitch_registry or os.environ.get("SK_ROUTING_STITCH_REGISTRY", "")
    coverage_bbox = None
    if args.coverage_bbox:
        parts = [float(x) for x in args.coverage_bbox.split(",")]
        if len(parts) != 4:
            raise SystemExit("--coverage-bbox must be 'min_lon,min_lat,max_lon,max_lat'")
        coverage_bbox = tuple(parts)

    data_sources = {
        "land": os.path.join(args.input_dir, "land_polygons.geojson"),
        "coastal_water": os.path.join(args.input_dir, "coastal_water_polygons.geojson"),
        "inland_waterways": os.path.join(args.input_dir, "inland_waterways_lines.geojson"),
        "depth_areas": os.path.join(args.input_dir, "depare_polygons.geojson"),
        "bridges": os.path.join(args.input_dir, "bridges_polygons.geojson"),
        "locks": os.path.join(args.input_dir, "locks_polygons.geojson"),
        "fairways": os.path.join(args.input_dir, "fairways_polygons.geojson"),
        "dredged_areas": os.path.join(args.input_dir, "dredged_areas_polygons.geojson"),
        "pois": os.path.join(args.input_dir, "pois_points.geojson"),
        "restricted_areas": os.path.join(args.input_dir, "restricted_areas_polygons.geojson"),
        "obstacles": os.path.join(args.input_dir, "obstructions_points.geojson"),
        "hulks": os.path.join(args.input_dir, "hulks_polygons.geojson"),
        "mariculture": os.path.join(args.input_dir, "mariculture_polygons.geojson"),
        "caution_areas": os.path.join(args.input_dir, "caution_areas_polygons.geojson")
    }

    pipeline = NauticalRoutingPipeline(data_paths=data_sources, db_path=args.output,
                                       country=args.country, region_name=args.name,
                                       description=args.description,
                                       tags=args.tags, contributor=args.contributor,
                                       url=args.url, license=args.license,
                                       copyright=args.copyright,
                                       architecture=args.architecture,
                                       dataset_version=args.dataset_version,
                                       stitch_registry_path=stitch_registry_path,
                                       coverage_bbox=coverage_bbox,
                                       stitch_band_m=args.stitch_band_m,
                                       stitch_radius_m=args.stitch_radius_m,
                                       navmesh_edge_m=args.navmesh_edge_m,
                                       sagitta_cap=args.sagitta_cap,
                                       max_segment_m=args.max_segment_m,
                                       axis_dedup_cap=args.axis_dedup_cap,
                                       inland_densify_max_segment_m=args.inland_densify_max_segment_m)
    pipeline.run_pipeline()
