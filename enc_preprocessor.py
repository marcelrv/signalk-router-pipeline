import os
import re
import glob
import logging
import warnings
import argparse
import geopandas as gpd
import pandas as pd

# NOAA/IHO S-57 cell naming: <2-letter producer><1-digit usage band><4-char cell id>.000
# Usage band 1 = "overview" (ocean passage planning, ~1:1.5M+), 2 = "general" (~1:350k-1.5M).
# These are coarse, simplified charts meant for offshore routing, not per-vessel coastal/
# harbor detail — but NOAA bundles a handful of them into every state ZIP (e.g. US2EC02M
# covers the entire Atlantic seaboard, US1GC09M covers the entire Gulf of Mexico). Merging
# their DEPARE/coastal_water polygons in alongside real harbor/approach charts (bands 3-6)
# creates water-body components spanning thousands of km, which blows up navmesh
# triangulation memory (confirmed: a single US1GC09M cell produced a "3134x1978km piece"
# that OOM-killed the pipeline on a 281-cell SC+GA build). Skip bands 1-2 by default.
_OVERVIEW_BAND_RE = re.compile(r'^[A-Za-z]{2}([1-6])', re.IGNORECASE)


def _usage_band(file_path: str) -> str | None:
    m = _OVERVIEW_BAND_RE.match(os.path.basename(file_path))
    return m.group(1) if m else None

# Suppress fiona warnings about missing layers in specific .000 files
warnings.filterwarnings("ignore", category=RuntimeWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ENCToGeoJSONPreprocessor:
    def __init__(self, enc_directory: str, output_directory: str, skip_bands: set[str] | None = None):
        self.enc_directory = enc_directory
        self.output_directory = output_directory
        self.skip_bands = skip_bands if skip_bands is not None else {'1', '2'}
        
        # Configure GDAL to properly build S-57 polygons from geometries
        os.environ["OGR_S57_OPTIONS"] = "RETURN_PRIMITIVES=OFF,RETURN_LINKAGES=OFF,LNAM_REFS=ON"
        
        # Mapping S-57 Object Classes to our Pipeline Files
        self.layer_mapping = {
            'LNDARE': 'land_polygons.geojson',
            'DEPARE': 'depare_polygons.geojson',
            'BRIDGE': 'bridges_polygons.geojson',
            'LOKBSN': 'locks_polygons.geojson',
            'FAIRWY': 'fairways_polygons.geojson',
            # DRGARE (Dredged Area) is the primary US charting of "the channel" --
            # NOAA sparsely charts FAIRWY (regulated traffic lanes) but densely
            # charts DRGARE (maintained-depth dredged footprints), 5x FAIRWY at
            # harbour scale. See docs/SPEC-FAIRWAY-HARMONIZATION.md. Kept as its
            # own file (not merged into fairways_polygons.geojson here) so
            # provenance survives; the pipeline unifies the two at read time.
            'DRGARE': 'dredged_areas_polygons.geojson',
            'HRBFAC': 'pois_points.geojson',
            # We will merge RECTRC, NAVLNE and WTWAXS (IENC Waterway Axis) together
            # for inland waterways centerlines
            'RECTRC': 'inland_waterways_lines.geojson',
            'NAVLNE': 'inland_waterways_lines.geojson',
            'WTWAXS': 'inland_waterways_lines.geojson',
            # S-57 layers for navigational hazards and restricted areas (no-go zones)
            'RESARE': 'restricted_areas_polygons.geojson',
            'OBSTRN': 'obstructions_points.geojson',
            'HULKES': 'hulks_polygons.geojson',
            'MARCUL': 'mariculture_polygons.geojson',
            'CTNARE': 'caution_areas_polygons.geojson'
        }

        # Dictionary to store lists of GeoDataFrames for each output file
        self.extracted_data = {filename: [] for filename in set(self.layer_mapping.values())}

    def process_all(self):
        """Finds all .000 files and extracts the necessary layers."""
        if not os.path.exists(self.output_directory):
            os.makedirs(self.output_directory)

        # Recursively find all S-57 base cell files
        enc_files = glob.glob(os.path.join(self.enc_directory, '**', '*.000'), recursive=True)
        logger.info(f"Found {len(enc_files)} ENC (.000) files.")

        if self.skip_bands:
            skipped = [f for f in enc_files if _usage_band(f) in self.skip_bands]
            if skipped:
                logger.info(
                    f"Skipping {len(skipped)} overview/general-scale cells (usage band "
                    f"{sorted(self.skip_bands)}) — coarse offshore-planning charts that "
                    f"blow up navmesh memory if merged with coastal/harbor detail: "
                    f"{[os.path.basename(f) for f in skipped]}"
                )
            enc_files = [f for f in enc_files if f not in skipped]

        logger.info(f"Starting extraction of {len(enc_files)} cells...")

        for i, file_path in enumerate(enc_files, 1):
            logger.info(f"Processing [{i}/{len(enc_files)}]: {os.path.basename(file_path)}")
            self._extract_layers_from_file(file_path)

        self._merge_and_export()

    def _extract_layers_from_file(self, file_path: str):
        """Extracts configured S-57 layers from a single ENC file."""
        for s57_layer, output_filename in self.layer_mapping.items():
            try:
                # Read the specific layer from the S-57 file
                gdf = gpd.read_file(file_path, layer=s57_layer)
                
                if not gdf.empty:
                    # Standardize to WGS84
                    if gdf.crs and gdf.crs != "EPSG:4326":
                        gdf = gdf.to_crs("EPSG:4326")

                    if s57_layer in ('FAIRWY', 'DRGARE'):
                        # Record the originating S-57 object class: the pipeline
                        # unifies these two into one fairway signal at read time
                        # (see docs/SPEC-FAIRWAY-HARMONIZATION.md), and this is
                        # what lets a unified feature's origin still be told apart.
                        gdf = gdf.copy()
                        gdf['src_objl'] = s57_layer

                    self.extracted_data[output_filename].append(gdf)
            except Exception as e:
                # Fiona raises an exception if the layer doesn't exist in this specific chart
                # This is normal, as not all charts have bridges, locks, etc.
                pass

    def _merge_and_export(self):
        """Merges all extracted bits and saves them as GeoJSON."""
        logger.info("Merging extracted geometries and exporting to GeoJSON...")
        
        for output_filename, gdf_list in self.extracted_data.items():
            output_path = os.path.join(self.output_directory, output_filename)
            
            if not gdf_list:
                logger.warning(f"No data found for {output_filename}. Skipping.")
                continue
                
            # Merge all individual chart pieces into one massive GeoDataFrame
            merged_gdf = pd.concat(gdf_list, ignore_index=True)
            
            # Clean up: Drop completely empty/null geometries
            merged_gdf = merged_gdf[merged_gdf.geometry.notnull()]
            
            # Note for coastal_water: We copy DEPARE (Depth Areas) to act as coastal_water_polygons 
            # so the routing script can generate a navmesh over navigable water.
            if output_filename == 'depare_polygons.geojson':
                coastal_gdfs = [merged_gdf]
                locks_key = 'locks_polygons.geojson'
                if locks_key in self.extracted_data and self.extracted_data[locks_key]:
                    coastal_gdfs.append(pd.concat(self.extracted_data[locks_key], ignore_index=True))
                    logger.info(f"Merged {len(self.extracted_data[locks_key])} LOKBSN lock basin groups into coastal water")
                coastal_merged = pd.concat(coastal_gdfs, ignore_index=True)
                coastal_merged = coastal_merged[coastal_merged.geometry.notnull()]
                coastal_path = os.path.join(self.output_directory, 'coastal_water_polygons.geojson')
                coastal_merged.to_file(coastal_path, driver='GeoJSON')
                logger.info(f"Saved {coastal_path} (DEPARE + LOKBSN)")

            # Save the file
            try:
                merged_gdf.to_file(output_path, driver='GeoJSON')
                logger.info(f"Saved {output_path} with {len(merged_gdf)} features.")
            except Exception as e:
                logger.error(f"Failed to save {output_filename}: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract S-57 ENC layers into GeoJSON files.")
    parser.add_argument("--input", default="./data",
                        help="Root directory containing ENC .000 files in subdirectories (default: ./data)")
    parser.add_argument("--output", default="./output_geojson",
                        help="Output directory for extracted GeoJSON files (default: ./output_geojson)")
    parser.add_argument("--include-overview-charts", action="store_true",
                        help="Include usage-band 1 (overview) and 2 (general) charts. Off by "
                             "default — these are coarse offshore-planning scale charts that "
                             "can blow up navmesh memory when merged with coastal/harbor detail.")
    args = parser.parse_args()

    preprocessor = ENCToGeoJSONPreprocessor(
        enc_directory=args.input,
        output_directory=args.output,
        skip_bands=set() if args.include_overview_charts else {'1', '2'}
    )

    preprocessor.process_all()
    logger.info("Preprocessing complete. You can now run the routing pipeline.")

