#!/usr/bin/env python3
"""Measure `--navmesh-boundary-simplify-m` on REAL extracted geometry.

docs/SPEC-GRAPH-DENSITY.md §10.6 item 1 quotes numbers produced by this script;
this is what makes those numbers reproducible from repo data instead of from a
scratch pickle. It does NOT run a regional build (ROADMAP item 9 still owes that);
it measures the boundary pass and one `build_navmesh_region` call in isolation.

For each tolerance it reports, against the region's own `land_polygons.geojson`:
boundary vertices, parts, island rings, navmesh area over land, water removed
(gross) and area added outside the original (gross), the widest excursion outside
the original water, the shortest ring segment, and the runtime; then, on a metric
sub-crop, the nodes/edges/seam nodes `build_navmesh_region` actually registers.

Examples (both used for the numbers in §10.6 item 1):

  scripts/measure_navmesh_boundary_simplify.py \\
      --input-dir data/zeeland_clip --bbox 3.83 51.58 3.98 51.70 \\
      --crop-centre 3.89 51.63 --crop-half 2500 2500

  scripts/measure_navmesh_boundary_simplify.py \\
      --input-dir data/geojson/md_reclip --bbox -76.6 38.7 -76.2 39.1 \\
      --crop-centre -76.45 38.95 --crop-half 2500 2500
"""
import argparse
import os
import sys
import time

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import Point, box
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nautical_routing_pipeline import (  # noqa: E402
    ClassificationConfig, NauticalRoutingPipeline)

P = NauticalRoutingPipeline


def _pipeline(tol=None):
    p = P(data_paths={}, db_path=":memory:")
    if tol is not None:
        p.classification_config = ClassificationConfig(navmesh_boundary_simplify_m=tol)
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    return p


def _vcount(geom):
    return sum(len(p.exterior.coords) - 1 + sum(len(r.coords) - 1 for r in p.interiors)
               for p in P._explode_polygonal(geom))


def _min_segment(geom):
    shortest = float("inf")
    for p in P._explode_polygonal(geom):
        for ring in [p.exterior, *p.interiors]:
            c = np.asarray(ring.coords)
            shortest = min(shortest, float(np.hypot(np.diff(c[:, 0]), np.diff(c[:, 1])).min()))
    return shortest


def _max_excursion(result, water, step_m=2.0):
    """Widest distance any part of `result` reaches outside `water` -- the honest
    measure of "how far over land", as opposed to the overlap AREA. Sampled along
    the leak every `step_m` and measured against the water's boundary segments via
    an STRtree -- a plain `shapely.distance` against a region-sized polygon is
    minutes rather than seconds."""
    leak = result.difference(water)
    if leak.is_empty:
        return 0.0
    coords = shapely.get_coordinates(shapely.segmentize(leak, step_m))
    if not len(coords):
        return 0.0
    pts = shapely.points(coords)
    segs = []
    for part in P._explode_polygonal(water):
        for ring in [part.exterior, *part.interiors]:
            c = np.asarray(ring.coords)
            segs.append(shapely.linestrings(
                np.stack([c[:-1], c[1:]], axis=1).reshape(-1, 2),
                indices=np.repeat(np.arange(len(c) - 1), 2)))
    segs = np.concatenate(segs)
    nearest = shapely.STRtree(segs).nearest(pts)
    return float(shapely.distance(pts, segs[nearest]).max())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input-dir", required=True,
                    help="a preprocessed GeoJSON dir (coastal_water_polygons.geojson, "
                         "land_polygons.geojson)")
    ap.add_argument("--bbox", nargs=4, type=float, required=True,
                    metavar=("MINLON", "MINLAT", "MAXLON", "MAXLAT"))
    ap.add_argument("--tolerances", nargs="*", type=float,
                    default=[5.0, 15.0, 20.0, 30.0, 60.0, 99.0])
    ap.add_argument("--crop-centre", nargs=2, type=float, metavar=("LON", "LAT"),
                    help="centre of the build_navmesh_region sub-crop")
    ap.add_argument("--crop-half", nargs=2, type=float, default=[2500.0, 2500.0],
                    metavar=("HALF_X_M", "HALF_Y_M"))
    ap.add_argument("--seam-mode", choices=("cut-lines", "none"), default="cut-lines",
                    help="which crop-boundary coordinates count as cross-piece seam "
                         "coordinates: the ones lying on the crop box's own cut lines "
                         "(what a real tile/piece seam is), or none")
    args = ap.parse_args()

    water_path = os.path.join(args.input_dir, "coastal_water_polygons.geojson")
    land_path = os.path.join(args.input_dir, "land_polygons.geojson")
    t0 = time.time()
    gdf = gpd.read_file(water_path, bbox=tuple(args.bbox))
    merged = unary_union([g for g in gdf.geometry if g is not None and not g.is_empty])
    largest = max(P._explode_polygonal(merged), key=lambda p: p.area)
    utm = str(gpd.GeoSeries([largest], crs="EPSG:4326").estimate_utm_crs())
    water = gpd.GeoSeries([largest], crs="EPSG:4326").to_crs(utm).iloc[0].buffer(0)
    land = unary_union(list(gpd.read_file(land_path, bbox=tuple(args.bbox)).to_crs(utm).geometry))
    print(f"# {args.input_dir} bbox={tuple(args.bbox)} crs={utm} "
          f"({time.time() - t0:.0f}s to load)")
    print(f"# largest connected water body: {water.area / 1e6:.1f} km2, "
          f"{_vcount(water)} boundary vertices, {len(water.interiors)} island rings")
    print(f"{'tol':>5} {'verts':>7} {'parts':>5} {'isl':>5} {'land m2':>10} "
          f"{'cut km2':>8} {'added km2':>9} {'net km2':>8} {'maxout m':>8} "
          f"{'minseg':>7} {'s':>5}")
    for tol in args.tolerances:
        t = time.time()
        result = _pipeline(tol)._simplify_navmesh_boundary(water)
        dt = time.time() - t
        cut = water.difference(result).area
        added = result.difference(water).area
        print(f"{tol:5.1f} {_vcount(result):7d} {len(P._explode_polygonal(result)):5d} "
              f"{sum(len(p.interiors) for p in P._explode_polygonal(result)):5d} "
              f"{result.intersection(land).area:10.0f} {cut / 1e6:8.3f} {added / 1e6:9.3f} "
              f"{(cut - added) / 1e6:8.3f} {_max_excursion(result, water):8.2f} "
              f"{_min_segment(result):7.3f} {dt:5.1f}")

    if not args.crop_centre:
        return
    cx, cy = list(gpd.GeoSeries([Point(*args.crop_centre)], crs="EPSG:4326")
                  .to_crs(utm).iloc[0].coords)[0]
    hx, hy = args.crop_half
    crop = max(P._explode_polygonal(water.intersection(box(cx - hx, cy - hy, cx + hx, cy + hy))),
               key=lambda p: p.area)
    # A real seam is where this piece was cut away from the rest of the water --
    # here, the crop box's own edges -- not an arbitrary sample of the coastline.
    cut_lines = box(cx - hx, cy - hy, cx + hx, cy + hy).exterior
    seam = {(round(x, 3), round(y, 3))
            for x, y in shapely.get_coordinates(crop.boundary)
            if args.seam_mode == "cut-lines" and cut_lines.distance(Point(x, y)) < 1e-6}
    print(f"\n# build_navmesh_region crop: centre {tuple(args.crop_centre)} "
          f"+/-{hx:.0f}x{hy:.0f} m -> {crop.area / 1e6:.3f} km2, {_vcount(crop)} vertices, "
          f"{len(crop.interiors)} islands, {len(seam)} seam coordinates")
    print(f"{'tol':>5} {'nodes':>6} {'edges':>6} {'seam nodes':>10} {'regions':>8}")
    for tol in args.tolerances:
        p = _pipeline(tol)
        p.build_navmesh_region(crop, utm, set(seam))
        print(f"{tol:5.1f} {p.graph.number_of_nodes():6d} {p.graph.number_of_edges():6d} "
              f"{len(p.navmesh_seam_node_ids):10d} {len(p.navmesh_region_rows):8d}")


if __name__ == "__main__":
    main()
