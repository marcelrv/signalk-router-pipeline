"""Group candidates into review tiles (`docs/SPEC-GRAPH-CLEANUP.md` §6).

A grid over the whole region would mostly render empty water -- most tiles
have nothing Pass A left unresolved. Instead this buckets candidates directly
into ~6 km cells and only ever produces a tile where at least one candidate's
anchor falls, which is what keeps the tile count (and therefore the review
cost) proportional to how much judgement is actually needed rather than to the
region's raw area.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .candidates import Candidate, anchor_latlon
from .graph import RoutingGraph

BBox = Tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)

DEFAULT_TILE_M = 6000.0
DEFAULT_PAD_FRACTION = 0.15
# A tile's candidate count is capped so a single dense cluster (many stubs in
# one small marina, say) doesn't produce a tile too crowded to read -- past
# this, split by re-bucketing at half the cell size for that cell only.
MAX_CANDIDATES_PER_TILE = 25


@dataclass
class Tile:
    id: str
    bbox: BBox
    candidates: List[Candidate] = field(default_factory=list)

    def numbered(self) -> Dict[int, Candidate]:
        """Tile-local number (1-based, stable sort by anchor node id) -> candidate."""
        ordered = sorted(self.candidates, key=lambda c: c.anchor)
        return {i: c for i, c in enumerate(ordered, start=1)}


def _cell_size_deg(lat_mid: float, tile_m: float) -> Tuple[float, float]:
    lat_step = tile_m / 111_320.0
    lon_step = tile_m / (111_320.0 * max(math.cos(math.radians(lat_mid)), 0.1))
    return lat_step, lon_step


def _pad_bbox(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
             lat_step: float, lon_step: float, pad_fraction: float) -> BBox:
    plat, plon = lat_step * pad_fraction, lon_step * pad_fraction
    return (min_lon - plon, min_lat - plat, max_lon + plon, max_lat + plat)


def build_tiles(g: RoutingGraph, candidates: Sequence[Candidate],
                tile_m: float = DEFAULT_TILE_M,
                pad_fraction: float = DEFAULT_PAD_FRACTION,
                max_per_tile: int = MAX_CANDIDATES_PER_TILE) -> List[Tile]:
    """Bucket `candidates` into tiles by their anchor point.

    A cell whose candidate count exceeds `max_per_tile` is re-bucketed at half
    the cell size, recursively, until every tile is readable -- rather than
    silently truncating the list and hiding candidates from review.
    """
    if not candidates:
        return []
    lat_mid = sum(anchor_latlon(g, c)[0] for c in candidates) / len(candidates)
    lat_step, lon_step = _cell_size_deg(lat_mid, tile_m)
    return _bucket(g, candidates, lat_step, lon_step, pad_fraction, max_per_tile, depth=0)


def _bucket(g: RoutingGraph, candidates: Sequence[Candidate],
           lat_step: float, lon_step: float, pad_fraction: float,
           max_per_tile: int, depth: int) -> List[Tile]:
    cells: Dict[Tuple[int, int], List[Candidate]] = {}
    for c in candidates:
        lat, lon = anchor_latlon(g, c)
        key = (math.floor(lat / lat_step), math.floor(lon / lon_step))
        cells.setdefault(key, []).append(c)

    out: List[Tile] = []
    for (ilat, ilon), members in cells.items():
        # The real stopping condition is the 1e-5 deg (~1.1 m) cell-size floor,
        # not the depth count -- a genuinely crowded spot (many stubs in one
        # small marina) needs more than 4 halvings to separate into readable
        # groups, and capping depth too low was silently leaving oversized
        # tiles right past that cap. `depth < 30` only guards against a
        # pathological input (candidates at the exact same coordinate) ever
        # recursing forever; `lat_step > 1e-5` is what actually terminates it
        # for any realistic cluster.
        if len(members) > max_per_tile and depth < 30 and lat_step > 1e-5:
            out += _bucket(g, members, lat_step / 2.0, lon_step / 2.0,
                          pad_fraction, max_per_tile, depth + 1)
            continue
        min_lat, max_lat = ilat * lat_step, (ilat + 1) * lat_step
        min_lon, max_lon = ilon * lon_step, (ilon + 1) * lon_step
        bbox = _pad_bbox(min_lat, min_lon, max_lat, max_lon, lat_step, lon_step,
                         pad_fraction)
        tile_id = f"t{depth}_{ilat}_{ilon}"
        out.append(Tile(id=tile_id, bbox=bbox, candidates=members))
    return out
