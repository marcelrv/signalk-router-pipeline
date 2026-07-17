#!/usr/bin/env python3
"""Clip the full-coverage GeoJSON layers to a small pilot bounding box.

Phase 0 pilots the navmesh-hybrid rewrite on the Zeelandbrug corridor
(Oosterschelde / Westerschelde). Clipping keeps iteration fast and keeps the
big ~350 MB layers (coastal_water, depare) out of every test run.

Uses ``gpd.read_file(bbox=...)`` to push the spatial filter down to the reader
(verified to work with the installed pyogrio), then a hard ``.clip(box)`` to
trim boundary-crossing polygons.

Example:
    python clip_pilot_data.py \
        --input-dir /home/node/signalkdev/signalk-routeiq/output_geojson \
        --bbox "3.2,51.2,4.3,51.7" \
        --output-dir ./data/zeeland_clip
"""
import argparse
import os

import geopandas as gpd
from shapely.geometry import box

# The 13 pipeline layers (filename per the pipeline CLI dict).
LAYER_FILES = [
    "land_polygons.geojson",
    "coastal_water_polygons.geojson",
    "inland_waterways_lines.geojson",
    "depare_polygons.geojson",
    "bridges_polygons.geojson",
    "locks_polygons.geojson",
    "fairways_polygons.geojson",
    "pois_points.geojson",
    "restricted_areas_polygons.geojson",
    "obstructions_points.geojson",
    "hulks_polygons.geojson",
    "mariculture_polygons.geojson",
    "caution_areas_polygons.geojson",
]


def clip_layer(path: str, bbox_tuple, clip_box, out_path: str):
    # bbox= pushes the spatial filter down to the reader (fast, avoids loading 350MB).
    gdf = gpd.read_file(path, bbox=bbox_tuple)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    if not gdf.empty:
        # Repair invalid geometry first — raw ENC polygons can have self-touching
        # rings that make the clip intersection throw a TopologyException.
        gdf["geometry"] = gdf.geometry.make_valid()
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()]
        # Hard-trim polygons that only partially overlap the bbox.
        gdf = gpd.clip(gdf, clip_box)
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()]
    gdf.to_file(out_path, driver="GeoJSON")
    return len(gdf)


def main():
    ap = argparse.ArgumentParser(description="Clip pipeline GeoJSON layers to a pilot bbox.")
    ap.add_argument("--input-dir", default="/home/node/signalkdev/signalk-routeiq/output_geojson",
                    help="Directory of the full-coverage GeoJSON layers.")
    ap.add_argument("--bbox", default="3.2,51.2,4.3,51.7",
                    help='"min_lon,min_lat,max_lon,max_lat" (default: Zeeland pilot).')
    ap.add_argument("--output-dir", default="./data/zeeland_clip",
                    help="Where to write the clipped layers.")
    args = ap.parse_args()

    bbox_tuple = tuple(float(x) for x in args.bbox.split(","))
    if len(bbox_tuple) != 4:
        raise SystemExit("--bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    clip_box = box(*bbox_tuple)
    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Clipping to bbox {bbox_tuple} -> {args.output_dir}")
    for fname in LAYER_FILES:
        src = os.path.join(args.input_dir, fname)
        dst = os.path.join(args.output_dir, fname)
        if not os.path.exists(src):
            print(f"  [skip] {fname}: source not found")
            continue
        try:
            n = clip_layer(src, bbox_tuple, clip_box, dst)
            print(f"  [ok]   {fname}: {n} features")
        except Exception as exc:  # noqa: BLE001 - report and continue per-layer
            print(f"  [FAIL] {fname}: {exc}")


if __name__ == "__main__":
    main()
