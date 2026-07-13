import os
import math
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
from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, mapping
from shapely.ops import triangulate, unary_union
from pyproj import Geod

# Phase 0 navmesh-hybrid skeleton extraction (Step C). Hard deps per requirements.txt.
from skimage.morphology import medial_axis
from rasterio.features import rasterize as _rio_rasterize
from rasterio.transform import from_origin as _rio_from_origin
# Phase 1 navmesh-region triangulation (Step B2). Hard dep per requirements.txt.
import triangle as _triangle

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

def _candidates_by_bounds_static(gdf, geom, margin=0.0):
    bounds = geom.bounds
    if margin:
        bounds = (bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)
    candidates = list(gdf.sindex.intersection(bounds))
    if candidates:
        return gdf.iloc[candidates]
    return gpd.GeoDataFrame()

def _edge_attr_worker(edge_chunk):
    geod = _EDGE_ATTR_GEOD
    gdfs = _EDGE_ATTR_GDFS
    CRS_WGS84 = "EPSG:4326"
    CRS_METRIC = "EPSG:3857"
    land_metric = gdfs.get('land_metric', gpd.GeoDataFrame())
    depare_gdf = gdfs.get('depth_areas', gpd.GeoDataFrame())
    bridges_gdf = gdfs.get('bridges', gpd.GeoDataFrame())
    fairways_gdf = gdfs.get('fairways', gpd.GeoDataFrame())
    locks_gdf = gdfs.get('locks', gpd.GeoDataFrame())
    obstacles_gdf = gdfs.get('obstacles', gpd.GeoDataFrame())

    results = {}
    for u, v, u_lon, u_lat, v_lon, v_lat, edge_type, is_opening_bridge_edge in edge_chunk:
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
                        sampled = []
                        for i in range(5):
                            f = i / 4.0
                            pt = Point(u_lon + f*(v_lon-u_lon), u_lat + f*(v_lat-u_lat))
                            found = None
                            for _, row in candidates.iterrows():
                                geom = row.geometry
                                if geom is not None and geom.contains(pt):
                                    found = row['DRVAL1']
                                    break
                            sampled.append(float(found) if pd.notna(found) else 99.0)
                        min_val = min(sampled)
                        attrs['min_depth'] = max(0.0, float(min_val))
                        attrs['drval1'] = min_val if min_val < 99.0 else None

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
                            if _is_valid(verclr):
                                clearance = float(verclr)
                            else:
                                clearance = 999.0
                        if clearance < min_clearance:
                            min_clearance = clearance
                    attrs['max_air_draft'] = min_clearance

        # Locks
        attrs['min_width'] = 999.0
        if not locks_gdf.empty:
            lock_candidates = _candidates_by_bounds_static(locks_gdf, edge_geom)
            if not lock_candidates.empty:
                intersecting = lock_candidates[lock_candidates.intersects(edge_geom)]
                if not intersecting.empty and 'HORCLR' in intersecting.columns:
                    attrs['min_width'] = float(intersecting['HORCLR'].min())

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

        # Obstacle crossing check — skipped for opening-bridge edges (same
        # exemption already applied to crosses_land at edge-creation time in
        # _add_opening_bridge_edges): these are precise, deliberately-computed
        # crossings of a bridge's actual navigable opening via fairway/
        # waterway centerline intersection, not generic geometry. A broad
        # obstacle polygon (e.g. a mariculture/marine-farm area) incidentally
        # overlapping the bridge's footprint must not hard-block the one
        # crossing point that exists specifically so vessels CAN pass through
        # the opening — that's the confirmed cause of a real Zeelandbrug
        # regression (opening never explored at all, not merely deprioritized).
        attrs['crosses_obstacle'] = 0
        if not is_opening_bridge_edge and not obstacles_gdf.empty:
            obs_candidates = _candidates_by_bounds_static(obstacles_gdf, edge_geom)
            if not obs_candidates.empty:
                intersecting = obs_candidates[obs_candidates.intersects(edge_geom)]
                if not intersecting.empty:
                    attrs['crosses_obstacle'] = 1

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
NAVMESH_TARGET_EDGE_M = 650.0  # target interior triangle edge length (spec's 500-800m band)
NAVMESH_PSLG_BUDGET = 20_000    # max len(vertices)+len(segments) fed into triangle's PSLG mode
NAVMESH_MAX_TRIANGLES = 200_000 # sanity cap on triangulate() output; retry coarser above this


@dataclass
class ClassificationConfig:
    """Tuning knobs for water-body classification (Step B) and skeleton raster (Step C)."""
    depth_ceiling_m: float = 6.0        # navigable-depth threshold separating deep open water from shoal
    min_navmesh_radius_m: float = 300.0  # a body must contain a disk of this radius to be navmesh-eligible
    pixel_min_m: float = 2.0            # medial-axis raster pixel floor
    pixel_max_m: float = 10.0           # medial-axis raster pixel ceiling
    pixel_dim_divisor: float = 200.0    # adaptive px = clamp(min_dim / divisor, floor, ceiling)
    min_spur_length_m: float = 60.0     # prune skeleton dead-ends shorter than this
    max_segment_m: float = 100.0        # resample collapsed centerlines to segments this long (narrow channels need <200m so straight-chord edges stay inside bends)

    def pixel_size_for(self, min_dimension_m: float) -> float:
        return float(np.clip(min_dimension_m / self.pixel_dim_divisor,
                             self.pixel_min_m, self.pixel_max_m))


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
    if restrn is not None and pd.notnull(restrn):
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

class NauticalRoutingPipeline:
    def __init__(self, data_paths: Dict[str, str], db_path: str,
                 country: str = "", region_name: str = "", description: str = "",
                 tags: Optional[str] = None, contributor: str = "", url: str = "",
                 license: str = "", copyright: str = "",
                 architecture: str = "navmesh-hybrid-phase1",
                 dataset_version: str = "", depth_ceiling: float = 6.0):
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
        self.classification_config = ClassificationConfig(depth_ceiling_m=depth_ceiling)
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
        
    def run_pipeline(self):
        self.parse_shapefiles()
        self.build_network()
        self._add_opening_bridge_edges()
        self._sanity_check_no_land_crossings()
        self._ensure_coastal_connectivity()
        self.calculate_edge_attributes()
        for u, v, data in self.graph.edges(data=True):
            if data.get("is_opening_bridge_edge"):
                data["max_air_draft"] = 999.0
        self._compute_node_depths()
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
        self.gdfs_metric = {
            name: gdf.to_crs(self.CRS_METRIC) for name, gdf in self.gdfs.items()
        }
        self._build_obstacle_layer()

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
        obstrn = self.gdfs.get("obstacles", gpd.GeoDataFrame())
        if not obstrn.empty:
            buf_metric = obstrn.to_crs(self.CRS_METRIC)
            buf_metric["geometry"] = buf_metric.geometry.buffer(OBSTACLE_BUFFER_METERS)
            buf_wgs84 = buf_metric.to_crs(self.CRS_WGS84)
            buf_wgs84["_layer"] = "obstructions"
            obstacle_parts.append(buf_wgs84)

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
            fairway_gdf = self.gdfs.get("fairways", gpd.GeoDataFrame())
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

                # Wide (navmesh-eligible) pieces DO need to stay exploded into individual
                # simple polygons: `triangle`'s PSLG triangulation takes one exterior ring
                # (+ holes) per call, not a MultiPolygon of disjoint areas.
                for piece_m in self._explode_polygonal(wide_m):
                    piece_wgs84 = gpd.GeoSeries([piece_m], crs=utm).to_crs(self.CRS_WGS84).iloc[0]
                    kind = self.classify_water_body(piece_wgs84, True, depth_gdf, fairway_gdf, cfg)
                    counts[kind] += 1
                    if kind == "navmesh":
                        navmesh_pieces.append((piece_m, utm, seam_coords))
                    else:
                        skeleton_pieces.append(piece_wgs84)

                # Build skeleton pieces before navmesh pieces so navmesh seam nodes can
                # coordinate-snap onto already-created skeleton endpoint nodes.
                for piece_wgs84 in skeleton_pieces:
                    self.build_skeleton_network(piece_wgs84, DEFAULT_SOURCE_TIER, src_id)
                for piece_m, piece_utm, piece_seam_coords in navmesh_pieces:
                    self.build_navmesh_region(piece_m, piece_utm, piece_seam_coords, DEFAULT_SOURCE_TIER, src_id)

            logger.info(f"Coastal water: {len(polygons)} connected components split into "
                        f"skeleton={counts['skeleton']}, laned={counts['laned']}, "
                        f"navmesh={counts['navmesh']} pieces.")
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
        """unary_union the water areas and explode into connected single polygons."""
        geoms = [g for g in coastal_gdf.geometry if g is not None and not g.is_empty]
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

    def _get_or_create_node(self, lon: float, lat: float, node_type: str = "coastal") -> int:
        coord = (round(lon, 5), round(lat, 5))
        if coord in self.coords_to_node:
            existing_id = self.coords_to_node[coord]
            if existing_id not in self.graph:
                del self.coords_to_node[coord]
                node_id = self._coord_to_id(lon, lat, node_type)
                self.graph.add_node(node_id, lon=coord[0], lat=coord[1], node_type=node_type)
                self.coords_to_node[coord] = node_id
                return node_id
            return existing_id
        node_id = self._coord_to_id(lon, lat, node_type)
        self.graph.add_node(node_id, lon=coord[0], lat=coord[1], node_type=node_type)
        self.coords_to_node[coord] = node_id
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
                    u = self._get_or_create_node(u_lon, u_lat, node_type="inland")
                    v = self._get_or_create_node(v_lon, v_lat, node_type="inland")
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
            u = self._get_or_create_node(pt[0], pt[1], "coastal")
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

    def build_navmesh_region(self, poly_m, utm_crs, seam_coord_set,
                             source_tier=DEFAULT_SOURCE_TIER, source_id=None):
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
        """
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

        max_area = (NAVMESH_TARGET_EDGE_M ** 2) * 0.433
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
        for start, count in ring_ranges:
            for k in range(count):
                i = start + k
                node_id = self._get_or_create_node(float(lons[i]), float(lats[i]), "coastal")
                self._stamp_node(node_id, NODE_KIND_NAVMESH_VERTEX, source_tier, source_id)
                perimeter_node_ids[i] = node_id
                x, y = pslg["vertices"][i]
                if (round(x, 3), round(y, 3)) in seam_coord_set:
                    boundary_node_ids.append(node_id)
                    self.navmesh_seam_node_ids.add(node_id)
            attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                         source_tier=source_tier, source_id=source_id)
            for k in range(count):
                u = perimeter_node_ids[start + k]
                v = perimeter_node_ids[start + (k + 1) % count]
                if u != v and not self.graph.has_edge(u, v):
                    self.graph.add_edge(u, v, **attrs)
                    self.graph.add_edge(v, u, **attrs)

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

    def _crosses_land(self, line_wgs84) -> bool:
        """True if a WGS84 LineString genuinely intersects the separate `land` layer.

        `land` is digitized independently from `coastal_water`, so the two don't
        always agree at the vertex level -- `_sanity_check_no_land_crossings` strips
        any edge that intersects `land_gdf` regardless of what a caller's own
        containment check against a water polygon concluded, so callers that
        generate connectivity-critical edges (ring-perimeter, stitching) should
        pre-check against this same layer to avoid building an edge only to have it
        stripped later, re-fragmenting whatever it was bridging.
        """
        land_gdf = self.gdfs.get("land", gpd.GeoDataFrame())
        if land_gdf is None or land_gdf.empty:
            return False
        try:
            return len(land_gdf.sindex.query(line_wgs84, predicate="intersects")) > 0
        except Exception:
            return bool(land_gdf.intersects(line_wgs84).any())

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

        if len({find(n) for n in ids}) <= 1:
            return 0  # already one connected piece

        coords_wgs84 = np.array([(self.graph.nodes[n]["lon"], self.graph.nodes[n]["lat"]) for n in ids])
        utm = self._local_utm_crs(component_polygon_wgs84)
        pts_m = gpd.GeoSeries(gpd.points_from_xy(coords_wgs84[:, 0], coords_wgs84[:, 1]),
                              crs=self.CRS_WGS84).to_crs(utm)
        coords_m = np.column_stack([pts_m.x.to_numpy(), pts_m.y.to_numpy()])
        poly_m = gpd.GeoSeries([component_polygon_wgs84], crs=self.CRS_WGS84).to_crs(utm).iloc[0].buffer(2.0)

        added = 0

        def try_add(i: int, j: int) -> bool:
            nonlocal added
            u, v = ids[i], ids[j]
            if find(u) == find(v):
                return False
            candidate_m = LineString([coords_m[i], coords_m[j]])
            if not candidate_m.within(poly_m):
                return False
            if self._crosses_land(LineString([coords_wgs84[i], coords_wgs84[j]])):
                return False
            attrs = dict(edge_type="coastal", edge_kind_id=EDGE_KIND_NAVMESH_BOUNDARY,
                         source_tier=DEFAULT_SOURCE_TIER, source_id=None)
            self.graph.add_edge(u, v, **attrs)
            self.graph.add_edge(v, u, **attrs)
            union(u, v)
            added += 2
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
                        continue
                    try_add(gi, gj)

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
                        continue
                    try_add(gi, gj)

            k_other = min(6, len(other_idx))
            _, nn_idxs2 = other_tree.query(navmesh_coords, k=k_other)
            if k_other == 1:
                nn_idxs2 = nn_idxs2.reshape(-1, 1)
            for local_i, neighbors in enumerate(nn_idxs2):
                gi = navmesh_idx[local_i]
                for local_j in neighbors:
                    gj = other_idx[int(local_j)]
                    if np.linalg.norm(coords_m[gi] - coords_m[gj]) > snap_radius_m:
                        continue
                    try_add(gi, gj)

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
                try_add(i, j)

        # Pass 2: guarantee, one merge round at a time. A region's exterior ring and
        # each interior (island) ring are separate cycles after ring-adjacency edges
        # alone (build_navmesh_region) -- they don't share vertices, so a region with
        # many islands starts as that many separate components here, and a boundary
        # that loops back close to itself (e.g. around a peninsula) can *also* leave
        # locally-dense clusters more than snap_radius_m apart along the direct path
        # between them. Repeatedly merge the two components with the globally nearest
        # valid (in-polygon) connector -- sorting ALL sample pairs (not just each
        # sample's own k-nearest) greedily merges the single best pair first each
        # round, which reconnects far more of the graph per round than a per-sample
        # k-nearest-neighbor query does. A full pairwise distance matrix is only safe
        # because MAX_TOTAL_SAMPLES bounds it to a fixed, small size regardless of how
        # many components exist -- an earlier version capped only *per group*, so
        # hundreds of components (real multi-island Zeeland regions) still produced a
        # tens-of-thousands-squared matrix (gigabytes) that ran for 15+ minutes and
        # got OOM-killed at full-dataset scale.
        MAX_TOTAL_SAMPLES = 1500
        MAX_ROUNDS = 30
        # Bounds the distance-MATRIX size, not the number of pairs walked off it --
        # a highly-fragmented component (many small groups, e.g. an estuary split
        # into hundreds of laned/skeleton pieces at full Zeeland scale) drives
        # per_group_cap down near 1, so nearly every one of the up to
        # MAX_TOTAL_SAMPLES**2/2 sorted pairs below is still cross-group early on
        # and falls through to try_add's expensive `within(poly_m)` +
        # `_crosses_land` checks. Confirmed against the real machine: without this
        # cap, one real Zeeland component's stitch pass alone ran for 1h45m+
        # before being killed, still stuck in this same loop (no multiprocessing
        # workers had even spawned yet for the next pipeline stage). Bounding the
        # number of *evaluated* pairs, not just the matrix they're sorted from, is
        # what actually fixes it -- same "constant regardless of dataset size"
        # idea as MAX_TOTAL_SAMPLES/MAX_ROUNDS above, just applied one level down.
        MAX_EVALUATIONS_PER_ROUND = 20000
        from scipy.spatial.distance import cdist
        for _round in range(MAX_ROUNDS):
            groups: Dict[Any, List[int]] = {}
            for idx, node in enumerate(ids):
                groups.setdefault(find(node), []).append(idx)
            if len(groups) <= 1:
                break
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
            for flat_idx in np.argsort(dmat, axis=None):
                si, sj = divmod(int(flat_idx), n_samples)
                if si >= sj:
                    continue
                if try_add(sample_idxs[si], sample_idxs[sj]):
                    merged_this_round += 1
                evaluated += 1
                if evaluated >= MAX_EVALUATIONS_PER_ROUND:
                    break
            if merged_this_round == 0:
                remaining = len({find(n) for n in ids})
                if remaining > 1:
                    logger.warning(f"  Stitch guarantee pass could not find a valid in-polygon "
                                   f"connector among sampled candidates; {remaining} components "
                                   f"left unmerged.")
                break
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

    @staticmethod
    def _extract_medial_axis_skeleton(mask):
        """medial_axis with the distance transform (width profile) in one call."""
        skel, dist = medial_axis(mask, return_distance=True)
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

    def _resample_long_skeleton_edges(self, pts, widths, max_segment_m):
        """Split a centerline polyline into segments no longer than max_segment_m.

        Re-inserting nodes keeps the existing straight-chord depth sampler valid on
        curved channels (per BACKGROUND.md §5.1) without modifying that worker.
        Yields (sub_pts, sub_widths) tuples.
        """
        seg_start = 0
        acc = 0.0
        n = len(pts)
        for i in range(1, n):
            _, _, d = self.geod.inv(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
            acc += d
            if acc >= max_segment_m or i == n - 1:
                yield pts[seg_start:i + 1], widths[seg_start:i + 1]
                seg_start = i
                acc = 0.0

    def build_skeleton_network(self, polygon, source_tier=DEFAULT_SOURCE_TIER, source_id=None):
        """Extract medial-axis centerlines for one channel polygon and emit them into the graph."""
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
        skel, dist = self._extract_medial_axis_skeleton(mask)
        if int(skel.sum()) < 2:
            return
        G = self._skeleton_raster_to_graph(skel, dist, transform, utm, px)
        self._prune_skeleton_spurs(G, cfg.min_spur_length_m)

        added = 0
        node_occurrences: Dict[int, int] = {}
        for _, _, d in G.edges(data=True):
            for sub_pts, sub_widths in self._resample_long_skeleton_edges(
                    d["pts"], d["width_profile"], cfg.max_segment_m):
                u = self._get_or_create_node(sub_pts[0][0], sub_pts[0][1], "coastal")
                v = self._get_or_create_node(sub_pts[-1][0], sub_pts[-1][1], "coastal")
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
        if not self.gdfs.get("fairways", gpd.GeoDataFrame()).empty: fw_gdfs.append(self.gdfs["fairways"])
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
                b_id = self._get_or_create_node(c_lon, c_lat, node_type="coastal")
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
                    
                for q, nodes in quadrants.items():
                    if nodes:
                        nodes.sort()
                        best_nid = nodes[0][1]
                        if not self.graph.has_edge(b_id, best_nid):
                            be = dict(edge_type="coastal", crosses_land=0, is_opening_bridge_edge=True,
                                      source_tier=DEFAULT_SOURCE_TIER, source_id=bridge_src)
                            self.graph.add_edge(b_id, best_nid, **be)
                            self.graph.add_edge(best_nid, b_id, **be)
                            added += 2

        logger.info(f"Added {added} precise bridge opening edges.")

    def _sanity_check_no_land_crossings(self):
        """Step F — land-crossing safety, retired as load-bearing for skeleton edges.

        Placeholder edges come from unconstrained Delaunay and are the ONE place
        land-crossing risk still remains, so they are actively stripped (as before).
        Skeleton/lane edges are land-safe by construction (built from the
        water-minus-land raster mask), so they get only an informational sampled
        spot-check — never stripped. Opening-bridge edges are never touched.

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
        """
        coastal_gdf = self.gdfs.get("coastal_water", gpd.GeoDataFrame())
        land_gdf = self.gdfs.get("land", gpd.GeoDataFrame())
        if coastal_gdf.empty:
            return

        placeholder, skeleton = [], []
        for u, v, data in self.graph.edges(data=True):
            if data.get("edge_type") != "coastal" or u >= v:
                continue
            if data.get("is_opening_bridge_edge"):
                continue  # never strip / never flag bridge crossings
            u_lon, u_lat = self.graph.nodes[u]["lon"], self.graph.nodes[u]["lat"]
            v_lon, v_lat = self.graph.nodes[v]["lon"], self.graph.nodes[v]["lat"]
            rec = {"u": u, "v": v,
                   "geometry": LineString([(u_lon, u_lat), (v_lon, v_lat)]),
                   "midpoint": Point((u_lon + v_lon) / 2.0, (u_lat + v_lat) / 2.0)}
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
        for i, component in enumerate(components):
            if (i + 1) % 20 == 0 or i == 0:
                logger.info(f"  Coastal connectivity: component {i + 1}/{len(components)}...")
            probe = component.buffer(0.001)  # ~100m in degrees; generous spatial pre-filter only
            candidates = node_points.index[sindex.query(probe, predicate="intersects")]
            if len(candidates) < 2:
                continue
            total_added += self._stitch_component_pieces(list(candidates), component, snap_radius_m=500.0)

        logger.info(f"Final coastal connectivity pass: added {total_added} stitching edges "
                    f"across {len(components)} components.")

    def _compute_node_depths(self):
        depare_gdf = self.gdfs.get("depth_areas", gpd.GeoDataFrame())
        if depare_gdf.empty:
            for _, data in self.graph.nodes(data=True): data["node_depth"] = -1
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
            depth = -1
            for idx in candidates:
                row = positive.iloc[idx]
                if row.geometry.contains(pt):
                    if "DRVAL1" in row and pd.notnull(row["DRVAL1"]):
                        depth = max(0.0, float(row["DRVAL1"]))
                    else:
                        depth = 99.0
                    found += 1
                    break
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
                )

        def chunked_iterable(iterable, size):
            import itertools
            it = iter(iterable)
            while True:
                chunk = tuple(itertools.islice(it, size))
                if not chunk: break
                yield chunk

        fw_gdfs = []
        if not self.gdfs.get("fairways", gpd.GeoDataFrame()).empty: fw_gdfs.append(self.gdfs["fairways"])
        if not self.gdfs.get("inland_waterways", gpd.GeoDataFrame()).empty: fw_gdfs.append(self.gdfs["inland_waterways"])
        highways_gdf = pd.concat(fw_gdfs, ignore_index=True) if fw_gdfs else gpd.GeoDataFrame(geometry=[])

        worker_gdfs = {
            "land_metric": self.gdfs_metric.get("land", gpd.GeoDataFrame()),
            "depth_areas": self.gdfs.get("depth_areas", gpd.GeoDataFrame()),
            "bridges": self.gdfs.get("bridges", gpd.GeoDataFrame()),
            "fairways": highways_gdf,
            "locks": self.gdfs.get("locks", gpd.GeoDataFrame()),
            "obstacles": self.gdfs.get("obstacles", gpd.GeoDataFrame()),
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
                    schema_version INTEGER DEFAULT 1,
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
                    node_depth REAL DEFAULT -1,
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
                   (country, name, description, last_update_date, tags, bounding_box, boundary_geometry, schema_version, contributor, url, license, copyright, architecture, dataset_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (self.country, self.region_name, self.description, now_utc,
                 self.tags, bbox_json, boundary_json, 1,
                 self.contributor, self.url,
                 self.license, self.copyright, self.architecture, self.dataset_version)
            )
            region_id = cursor.lastrowid
            
            nodes_data = [(n, data["lat"], data["lon"],
                           data.get("node_depth", -1),
                           region_id,
                           data.get("node_kind_id", NODE_KIND_POINT),
                           data.get("source_tier", DEFAULT_SOURCE_TIER),
                           data.get("source_id"))
                          for n, data in self.graph.nodes(data=True)]
            cursor.executemany("INSERT INTO nodes (id, lat, lon, node_depth, region_id, node_kind_id, source_tier, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", nodes_data)
            
            edges_data = [(
                u, v,
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
                data.get("width_profile")
            ) for u, v, data in self.graph.edges(data=True)]
            cursor.executemany("""
                INSERT INTO edges
                (source, target, distance, min_depth, drval1, max_air_draft, min_width, cost_factor, distance_to_land, edge_type_id, traffic_mode, crosses_land, crosses_obstacle, edge_kind_id, source_tier, source_id, width_profile)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    if catbrg is not None and pd.notnull(catbrg):
                        vals = _parse_catbrg(catbrg)
                        if any(v in ("3", "4", "5", "6", "7") for v in vals):
                            is_opening = True
                    if not is_opening:
                        vercop = _s57_col(row, "vercop", "VERCOP", "VerCop")
                        if vercop is not None and pd.notnull(vercop):
                            is_opening = True
                    if is_opening:
                        props["subtype"] = "opening"
                    else:
                        props["subtype"] = "fixed"
                        verclr = _s57_col(row, "verclr", "VERCLR", "VerClr")
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
    parser.add_argument("--depth-ceiling", type=float, default=6.0,
                        help="Depth ceiling (m) for water-body classification (Step B; used from Session 2 onward)")
    args = parser.parse_args()

    data_sources = {
        "land": os.path.join(args.input_dir, "land_polygons.geojson"),
        "coastal_water": os.path.join(args.input_dir, "coastal_water_polygons.geojson"),
        "inland_waterways": os.path.join(args.input_dir, "inland_waterways_lines.geojson"),
        "depth_areas": os.path.join(args.input_dir, "depare_polygons.geojson"),
        "bridges": os.path.join(args.input_dir, "bridges_polygons.geojson"),
        "locks": os.path.join(args.input_dir, "locks_polygons.geojson"),
        "fairways": os.path.join(args.input_dir, "fairways_polygons.geojson"),
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
                                       dataset_version=args.dataset_version)
    pipeline.run_pipeline()
