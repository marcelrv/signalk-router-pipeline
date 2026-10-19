"""Hazards and notices: extraction from raw S-57 cells, tiering and linking.

Implements docs/SPEC-HAZARDS-AND-NOTICES.md. Geometry only: no DB code lives
here; later DB writers share NOTICES_DDL, notice_id() and notice_row().

    records = extract_hazards(cells, bbox)           # raw S-57 -> records
    sev, depth, why = classify(rec, surrounding_m)   # spec tiers
    tier_records(records, depth_fn)                  # sets severity/depth_m/reason
    link_edges(records, edges, axes)                 # edge <-> notice links
    link_nodes(records, nodes)                       # node <-> notice links
"""
import hashlib
import json
import logging
import math
import numbers
import os
import re
from collections import namedtuple

import numpy as np
import shapely
from shapely.geometry import LineString, Point, mapping, shape
from shapely.strtree import STRtree

log = logging.getLogger(__name__)

HAZARD_RADIUS_M = 30.0        # point hazard <-> edge/node link distance
AREA_TOLERANCE_M = 30.0       # prohibited-area boundary tolerance (coarse chart polygons)

SEV_INFO, SEV_CAUTION, SEV_DEPTH, SEV_PROHIBITED = 0, 1, 2, 3
SEVERITY_NAMES = {SEV_INFO: "info", SEV_CAUTION: "caution", SEV_DEPTH: "depth", SEV_PROHIBITED: "prohibited"}

# Restriction-type areas (RESTRN/CATREA decide the tier) vs object classes
# that carry a depth (point-hazard rules). HULKES is always prohibited.
RESTRICTION_CLASSES = ("RESARE", "MARCUL", "CTNARE")
AREA_CLASSES = ("RESARE", "resare", "MARCUL", "CTNARE", "HULKES")
POINT_CLASSES = ("WRECKS", "OBSTRN", "UWTROC")

_PHRASES = ("entry prohibited", "passage prohibited", "toegang verboden")
_PERIOD_KEYS = ("PERSTA", "PEREND", "DATSTA", "DATEND")
_TIME_FIELDS = ("period_start", "period_end", "valid_from", "valid_until")
REASON_SEASONAL = "seasonal"
REASON_UNREVIEWED = "conditions in chart text, unreviewed"
_M_PER_DEG = 111320.0

NOTICES_DDL = """
-- Router rule for time-limited notices (the DB bakes no verdict into them: no crosses_obstacle, no cost_factor):
-- a notice is ACTIVE on a date when the date lies inside [valid_from, valid_until] (a NULL bound is open) AND,
-- when period_start/period_end are set, inside the recurring window (a window whose start is later in the year
-- than its end wraps over New Year: '--1101'..'--0515' is November 1 to May 15). S-57 dates may be partial:
-- '--MMDD', '--MM', 'CCYYMMDD', 'CCYYMM', 'CCYY'. An edge whose only severity-3 links are time-limited is blocked
-- by the router inside the window and ignored outside it.
CREATE TABLE notices (
  id            INTEGER PRIMARY KEY,  -- deterministic: md5(source_class|lnam|geom_wkb)[:13] as int,
                                      -- so the same chart object gets the same id in every DB
  source_class  TEXT NOT NULL,        -- S-57 class: RESARE, MARCUL, WRECKS, OBSTRN, UWTROC, CTNARE, HULKES, ...
  source_ref    TEXT,                 -- LNAM (agency+FIDN+FIDS) for tracing back to the chart
  source_cell   TEXT,                 -- cell file name, e.g. 1R5OS101.000
  severity      INTEGER NOT NULL,     -- 0 info, 1 caution, 2 depth, 3 prohibited (tiers above)
  depth_m       REAL,                 -- least depth used for tier 2, NULL otherwise or if unknown
  name          TEXT,                 -- OBJNAM / NOBJNM as charted
  text          TEXT,                 -- INFORM + NINFOM as charted, unclassified, NULL if none
  period_start  TEXT,                 -- RECURRING window only: PERSTA raw S-57 ('--1101', may wrap over New Year;
  period_end    TEXT,                 -- a PERSTA with a year is stored raw too) / PEREND ('--0515')
  valid_from    TEXT,                 -- the object's VALIDITY: DATSTA raw ('20220425'); NULL = since ever
  valid_until   TEXT,                 -- DATEND raw; in the past = expired. NULL = open-ended
  period_source TEXT,                 -- 'chart' (an S-57 attribute above), 'text-reviewed' (a reviewed override read
                                      -- from the chart text), NULL = no time information
  text_review   INTEGER NOT NULL DEFAULT 0,  -- 1: a prohibited area whose text (name + INFORM/NINFOM) mentions time or
                                      -- conditions but which has no period/valid_until and no accepted override:
                                      -- unreviewed, it stays blocked
  attributes    TEXT NOT NULL,        -- JSON of every non-empty S-57 attribute (RESTRN, CATREA, CATWRK, WATLEV, VALSOU, ...)
  geometry      TEXT NOT NULL,        -- GeoJSON geometry (areas simplified to ~10 m) so a client can draw it
  min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL   -- bbox for spatial lookup
);
CREATE TABLE edge_notices (source INTEGER, target INTEGER, notice_id INTEGER,
                           severity INTEGER NOT NULL,  -- effective for this edge: 3 prohibited, 2 depth, 1 caution, 0 info
                           reason TEXT,  -- NULL, or 'boundary graze' / 'along charted axis' / 'accepted: <reason>' /
                                         -- 'seasonal' (severity 3 but time-limited: crosses_obstacle is NOT set) /
                                         -- 'conditions in chart text, unreviewed' (blocked until reviewed)
                           PRIMARY KEY (source, target, notice_id));
CREATE TABLE node_notices (node_id INTEGER, notice_id INTEGER, severity INTEGER NOT NULL,
                           PRIMARY KEY (node_id, notice_id));
CREATE INDEX idx_edge_notices_notice ON edge_notices(notice_id);
CREATE INDEX idx_node_notices_notice ON node_notices(notice_id);
CREATE INDEX idx_notices_bbox ON notices(min_lat, min_lon);
"""


# --------------------------------------------------------------------------
# attribute parsing
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _ints(v):
    """S-57 list attribute -> [int]. Accepts None, int, float, list (of anything
    here, recursively), "['7']", "['5' '7']", "7,8"."""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, np.ndarray)):
        out = []
        for x in v:
            out.extend(_ints(x))
        return out
    if isinstance(v, str):
        return [int(float(x)) for x in _NUM_RE.findall(v)]
    try:
        f = float(v)
    except (TypeError, ValueError):
        return []
    return [] if math.isnan(f) or math.isinf(f) else [int(f)]


def _first_int(v):
    vals = _ints(v)
    return vals[0] if vals else None


def _attr(attrs, name):
    """Attribute lookup tolerant of the upper/lower-case column variants."""
    v = attrs.get(name)
    if v is None or v == "" or v == []:
        v = attrs.get(name.lower())
    return v


def _empty(v):
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple)):
        return len(v) == 0
    return False


def _jsonable(v):
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    return str(v)


def _restrn(attrs):
    """Union of RESTRN and the IENC-style lowercase restrn."""
    out = []
    for key in ("RESTRN", "restrn"):
        for x in _ints(attrs.get(key)):
            if x not in out:
                out.append(x)
    return out


def _valsou(attrs):
    """Real VALSOU in metres or None. GDAL reads 'UNKNOWN' as 0.0; with WATLEV 3
    (always under water) a 0.0 sounding is self-contradictory, hence unknown."""
    v = _attr(attrs, "VALSOU")
    if _empty(v):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    if v == 0.0 and _first_int(_attr(attrs, "WATLEV")) == 3:
        return None
    return v


# --------------------------------------------------------------------------
# tiering
# --------------------------------------------------------------------------

def has_time_info(record):
    """True when the record carries a recurring period or a validity: the record fields (also set by an accepted
    override) or, for hand-built records, the raw S-57 attributes."""
    if any(not _empty(record.get(k)) for k in _TIME_FIELDS):
        return True
    a = record.get("attributes") or {}
    return any(not _empty(_attr(a, k)) for k in _PERIOD_KEYS)


def has_time_limit(record):
    """True when the record has a recurring period (PERSTA/PEREND) or an end of validity (DATEND). A start of
    validity alone (DATSTA: 'established 2022-04-25') limits nothing: such an area is as permanent as one without
    dates. valid_from is still stored for the router."""
    if any(not _empty(record.get(k)) for k in ("period_start", "period_end", "valid_until")):
        return True
    a = record.get("attributes") or {}
    return any(not _empty(_attr(a, k)) for k in ("PERSTA", "PEREND", "DATEND"))


def _prohibited_area(record):
    """(entry/passage prohibited, time-limited) of a restriction-class record, from structured attributes
    (phrase fallback only when RESTRN is absent)."""
    a = record.get("attributes") or {}
    restrn = _restrn(a)
    timed = has_time_limit(record)
    prohibited = 7 in restrn
    if not restrn:
        text = " ".join(str(x or "") for x in (record.get("name"), record.get("text"))).lower()
        prohibited = any(p in text for p in _PHRASES)
    return prohibited, timed


def is_time_limited(record):
    """True for an entry/passage-prohibited restriction area with a recurring period or a valid_until (chart attributes
    or an accepted override; valid_from alone does not count, see has_time_limit): classify's 'prohibited, seasonal' branch. The writers neither block nor re-cost such an
    area (the router applies the time window); caution areas and RESTRN 8 with a period are not covered."""
    if record["source_class"].upper() not in RESTRICTION_CLASSES:
        return False
    prohibited, timed = _prohibited_area(record)
    return prohibited and timed


def blocks(record, severity):
    """True when a link of this effective severity sets crosses_obstacle: severity 3 and not time-limited."""
    return severity == SEV_PROHIBITED and not is_time_limited(record)


def edge_reason(record, severity, downgrade_reason=None):
    """edge_notices.reason of a link: a graze/axis downgrade reason wins (the edge is not blocked then), else
    'seasonal' for a severity-3 time-limited area, else 'conditions in chart text, unreviewed' for a severity-3
    record flagged text_review, else None."""
    if downgrade_reason:
        return downgrade_reason
    if severity == SEV_PROHIBITED:
        if is_time_limited(record):
            return REASON_SEASONAL
        if record.get("text_review"):
            return REASON_UNREVIEWED
    return None


def classify(record, surrounding_depth_m):
    """Spec tiers -> (severity 0..3, depth_m or None, reason).

    Uses structured attributes only (plus the entry/passage-prohibited phrase
    fallback for areas without RESTRN). surrounding_depth_m is the charted depth
    at the object, in metres (negative = dries; None or 'land' = unknown).
    Pass the depth-area value (DRVAL1-like) at the object, not a sounding.
    """
    cls = record["source_class"]
    a = record.get("attributes") or {}
    ucls = cls.upper()

    if ucls == "HULKES":
        return SEV_PROHIBITED, None, "hulk"

    if ucls in RESTRICTION_CLASSES:
        restrn = _restrn(a)
        prohibited, timed = _prohibited_area(record)
        if prohibited:
            if timed:
                return SEV_PROHIBITED, None, "entry prohibited, seasonal"
            return SEV_PROHIBITED, None, "entry/passage prohibited"
        if 8 in restrn:
            return SEV_CAUTION, None, "entry restricted"
        if ucls == "CTNARE":
            return SEV_CAUTION, None, "caution area"
        return SEV_INFO, None, f"{cls} restrn={restrn}"

    # point hazards: WRECKS, OBSTRN, UWTROC
    watlev = _first_int(_attr(a, "WATLEV"))
    catwrk = _first_int(_attr(a, "CATWRK"))
    expsou = _first_int(_attr(a, "EXPSOU"))
    if ucls == "WRECKS" and catwrk == 5:
        return SEV_PROHIBITED, None, "wreck, mast showing"
    v = _valsou(a)
    if v is not None:
        if surrounding_depth_m is not None and v >= surrounding_depth_m:
            return SEV_INFO, None, f"VALSOU {v} >= charted depth {surrounding_depth_m}"
        return SEV_DEPTH, v, f"VALSOU {v} < charted depth {surrounding_depth_m}"
    if catwrk == 1 or expsou == 1:
        return SEV_INFO, None, f"unknown depth, non-dangerous (CATWRK={catwrk} EXPSOU={expsou})"
    if catwrk == 2 or expsou == 2:
        return SEV_DEPTH, 0.0, f"unknown depth, dangerous (CATWRK={catwrk} EXPSOU={expsou})"
    if watlev in (3, 4):
        # S-52: unknown least depth of a submerged object = surrounding charted depth
        return SEV_INFO, None, f"unknown depth, submerged (WATLEV={watlev}), surrounding {surrounding_depth_m}"
    return SEV_PROHIBITED, None, f"no depth, WATLEV={watlev}"


def tier_records(records, depth_fn):
    """Classify every record in place. depth_fn(lon, lat) must return the charted
    depth-area value (DRVAL1-like) at the object in metres, negative = dries,
    None or 'land' = unknown (non-numbers are treated as unknown).
    Sets record['severity'], ['depth_m'], ['reason']; returns the records."""
    for r in records:
        rp = r["geometry"].representative_point()
        d = depth_fn(rp.x, rp.y) if depth_fn else None
        if isinstance(d, bool) or not isinstance(d, numbers.Real) or math.isnan(float(d)):
            d = None
        r["severity"], r["depth_m"], r["reason"] = classify(r, None if d is None else float(d))
    return records


# --------------------------------------------------------------------------
# time information in the chart text, reviewed overrides
# --------------------------------------------------------------------------

_MONTHS_EN = ("january", "february", "march", "april", "june", "july", "august", "september", "october",
              "november", "december")
_MONTHS_ABBR = ("feb", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mrt")
_MONTHS_NL = ("januari", "februari", "maart", "mei", "juni", "juli", "augustus", "oktober")
_MONTH_ALT = "|".join(_MONTHS_EN + _MONTHS_NL)
# 'may' and 'mar' are ordinary words / noise: only next to a day number or after a range word
_AMBIG = "may|mar|jan"
_TIME_RE = re.compile("|".join((
    rf"\b(?:{_MONTH_ALT})\b",
    rf"\b(?:{'|'.join(_MONTHS_ABBR)})\b\.?",
    rf"\b(?:{_AMBIG})\b\s*\d{{1,2}}\b",
    rf"\b\d{{1,2}}(?:st|nd|rd|th|e)?\s*(?:{_AMBIG})\b",
    rf"\b(?:from|until|till|to|through|van|tot|between|and|en|of|in)\s+(?:{_AMBIG})\b",
    r"\bwinter", r"\bsummer", r"zomer", r"\bseason", r"seizoen", r"\btemporar", r"\btijdelijk",
    r"\bduring\b", r"tijdens", r"gedurende", r"\buntil\b", r"\btill\b", r"\btot\s+\d", r"\bvanaf\b",
    r"\b(?:from|van)\b[^.|]{0,40}\d[^.|]{0,40}\b(?:until|till|to|tot)\b",
    r"(?<![\d.,])(?:0?[1-9]|[12]\d|3[01])[-/](?:0?[1-9]|1[0-2])(?![\d.,])",
    r"resting area", r"rustgebied", r"foraging", r"foerageer", r"fourageer",
)), re.IGNORECASE)


def _text_of(record):
    return " | ".join(str(x) for x in (record.get("name"), record.get("text")) if x)


def text_mentions_time(record):
    """Conservative EN+NL detector: does the name/text mention a season, month, date, period or temporary
    condition? Only ever used to FLAG a notice for human review, never to pick a tier."""
    return bool(_TIME_RE.search(_text_of(record)))


def needs_text_review(record):
    """text_review flag: an entry/passage-prohibited restriction area whose text mentions time but which has no
    period or valid_until (neither chart attributes nor an accepted override). Caution and info areas are not
    flagged: their tier does not depend on the time."""
    return record["source_class"].upper() in RESTRICTION_CLASSES and _prohibited_area(record)[0] \
        and not has_time_limit(record) and text_mentions_time(record)


def text_sha1(record):
    """sha1 of the notice text an override was read from (name + INFORM/NINFOM)."""
    return hashlib.sha1(_text_of(record).encode("utf-8")).hexdigest()


_S57_DATE_RE = re.compile(r"^(?:--(\d{2})(\d{2})?|---(\d{2})|(\d{4})(?:(\d{2})(\d{2})?)?)$")


def validate_s57_date(v):
    """S-57 (partial) date -> the same string, or None for None/''. Forms: '--MMDD', '--MM', '---DD', 'CCYYMMDD',
    'CCYYMM', 'CCYY'. Raises ValueError otherwise."""
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise ValueError(f"S-57 date must be a string, got {v!r}")
    m = _S57_DATE_RE.match(v)
    if not m:
        raise ValueError(f"not an S-57 date: {v!r} (use '--MMDD', '--MM', 'CCYYMMDD', 'CCYYMM' or 'CCYY')")
    g = m.groups()
    month = g[0] or g[4]
    day = g[1] or g[2] or g[5]
    year = int(g[3]) if g[3] is not None else None
    if year == 0:
        raise ValueError(f"year 0000 in S-57 date {v!r}")
    if month is not None and not 1 <= int(month) <= 12:
        raise ValueError(f"bad month in S-57 date {v!r}")
    if day is not None:
        leap = year is None or (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))   # '--0229' is possible
        top = 31 if month is None else (29 if leap else 28) if int(month) == 2 else \
            30 if int(month) in (4, 6, 9, 11) else 31
        if not 1 <= int(day) <= top:
            raise ValueError(f"bad day in S-57 date {v!r}")
    return v


OVERRIDE_STATUSES = ("accepted", "proposed", "rejected")
DEFAULT_OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hazard_overrides.json")


def load_overrides(path):
    """{notice id (str): entry} from a hazard_overrides.json; {} when path is None or the file is absent."""
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object {{notice id: override}}")
    return data


def resolve_overrides(path=None):
    """The overrides dict a builder or viewer uses: an explicit `path` must exist (SystemExit otherwise); None
    means data/hazard_overrides.json, silently {} when that is absent."""
    if path:
        if not os.path.exists(path):
            raise SystemExit(f"--hazard-overrides {path}: file not found")
        return load_overrides(path)
    return load_overrides(DEFAULT_OVERRIDES_PATH)


def save_overrides(path, overrides):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(overrides, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def apply_overrides(records, overrides):
    """Apply the ACCEPTED reviewed overrides to records in place (before tiering). An accepted entry sets
    period_start/period_end/valid_from/valid_until (non-null fields) and text_review=0. period_source becomes
    'text-reviewed' only when the entry applies a date and the record has no chart time information (never
    over 'chart'); an all-null "reviewed, no time limit" entry only clears text_review. proposed and rejected
    entries do nothing. An entry whose text_sha1 is missing or no longer matches the record's text is stale (the
    chart text changed): ignored with one warning per notice. The entry is looked up by record['id'] when present
    (hazards.json records carry the stored id), else notice_id(record). A record that was already tiered (has
    'severity') is re-tiered when it is a restriction area (no depth needed). Returns the number changed."""
    if not overrides:
        return 0
    n, warned = 0, set()
    for r in records:
        nid = str(r["id"] if r.get("id") is not None else notice_id(r))
        ov = overrides.get(nid)
        if not isinstance(ov, dict) or ov.get("status") != "accepted":
            continue
        if ov.get("text_sha1") != text_sha1(r):
            if nid not in warned:
                warned.add(nid)
                log.warning("hazard override %s is stale (the chart text changed since it was reviewed): ignored", nid)
            continue
        try:
            vals = {k: validate_s57_date(ov.get(k)) for k in _TIME_FIELDS}
        except ValueError as exc:
            if nid not in warned:
                warned.add(nid)
                log.warning("hazard override %s ignored: %s", nid, exc)
            continue
        applied = {k: v for k, v in vals.items() if v is not None}
        had_chart = r.get("period_source") == "chart" or any(
            not _empty(_attr(r.get("attributes") or {}, k)) for k in _PERIOD_KEYS)
        r.update(applied)
        if applied and not had_chart:
            r["period_source"] = "text-reviewed"
        r["text_review"] = 0
        if "severity" in r and r["source_class"].upper() in RESTRICTION_CLASSES:
            r["severity"], r["depth_m"], r["reason"] = classify(r, None)
        n += 1
    return n


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def _cell_scale(ds):
    """DSID.DSPM_CSCL compilation scale denominator, or None."""
    lyr = ds.GetLayerByName("DSID")
    if lyr is None:
        return None
    lyr.ResetReading()
    f = lyr.GetNextFeature()
    if f is None:
        return None
    try:
        v = f.GetField("DSPM_CSCL")
    except Exception:
        return None
    return int(v) if v else None


def _local_xy(lat):
    return _M_PER_DEG * math.cos(math.radians(lat)), _M_PER_DEG


def _size(geom, lat):
    """Area (m2) for polygons, length (m) otherwise, for the dedupe similarity test."""
    kx, ky = _local_xy(lat)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom.area * kx * ky
    return geom.length * 0.5 * (kx + ky)


def latest_cells(cells_dir, bbox):
    """One path per cell name -- the lexically last edition directory -- whose extent
    (M_COVR, else DEPARE) overlaps bbox=(lon0, lat0, lon1, lat1). Shared by navline_agent.prepare_inputs and
    the main pipeline (--hazard-cells-dir)."""
    from osgeo import ogr
    found = []
    for root, _dirs, files in os.walk(cells_dir, followlinks=True):   # composed regions symlink state dirs
        found += [os.path.join(root, f) for f in files if f.endswith(".000")]
    best = {}
    for p in sorted(found):
        best[os.path.basename(p)] = p
    out = []
    for p in best.values():
        ds = ogr.Open(p)
        if ds is None:
            continue
        lyr = ds.GetLayerByName("M_COVR") or ds.GetLayerByName("DEPARE")
        if lyr is None:
            continue
        e = lyr.GetExtent()
        if e[1] < bbox[0] or e[0] > bbox[2] or e[3] < bbox[1] or e[2] > bbox[3]:
            continue
        out.append(p)
    return out


def extract_hazards(cells, bbox, dedupe_m=5.0):
    """Read hazard objects from raw S-57 cells inside bbox=(lon0, lat0, lon1, lat1).

    Overlapping cells/editions repeat objects; records are deduped (same LNAM, or
    equal attributes from another cell with centroid within dedupe_m and size within 20 %), keeping
    the one from the larger-scale cell (smallest DSPM_CSCL; cells without a
    scale rank last). See _is_duplicate for the exact merge rule.
    """
    from osgeo import ogr
    opened = []
    for p in cells:
        ds = ogr.Open(p)
        if ds is not None:
            opened.append((_cell_scale(ds) or float("inf"), os.path.basename(p), p, ds))
    opened.sort(key=lambda t: (t[0], t[1]))

    records, grid = [], {}
    cell = 1e-4  # ~11 m: a +-1 neighbourhood covers dedupe_m
    for scale, base, _p, ds in opened:
        for cls in AREA_CLASSES + POINT_CLASSES:
            lyr = ds.GetLayerByName(cls)
            if lyr is None:
                continue
            lyr.SetSpatialFilterRect(*bbox)
            lyr.ResetReading()
            while True:
                f = lyr.GetNextFeature()
                if f is None:
                    break
                g = f.GetGeometryRef()      # f must stay alive while g is used
                if g is None:
                    continue
                geom = shapely.force_2d(shapely.from_wkb(bytes(g.ExportToIsoWkb())))
                if geom.is_empty:
                    continue
                items = f.items()
                if not geom.is_valid:
                    geom = shapely.make_valid(geom)
                rec = _make_record(cls, items, base, scale, geom)
                if _is_duplicate(rec, grid, cell, dedupe_m):
                    continue
                records.append(rec)
                _register(rec, grid, cell)
    return _join_split_objects(records)


def _join_split_objects(records):
    """One chart object split over neighbouring cells (e.g. 2R71028N / 2R71028S) keeps its LNAM in
    every piece, and the pieces lie too far apart for _is_duplicate. Join them into one record (geometry
    union, attributes of the first, i.e. largest-scale, piece), so the object has one notice_id."""
    out, by_key = [], {}
    for r in records:
        key = (r["source_class"].upper(), r["source_ref"])
        if not r["source_ref"]:
            out.append(r)
        elif key in by_key:
            first = by_key[key]
            first["geometry"] = shapely.make_valid(shapely.union(first["geometry"], r["geometry"]))
            if r["source_cell"] not in first["source_cell"].split(","):
                first["source_cell"] += "," + r["source_cell"]
        else:
            by_key[key] = r
            out.append(r)
    return out


def _make_record(cls, items, base, scale, geom):
    attrs = {}
    for k, v in items.items():
        v = _jsonable(v)
        if _empty(v):
            continue
        attrs[k] = v
    if "VALSOU" in attrs and _valsou(attrs) is None:
        attrs.pop("VALSOU")                  # unknown, never store 0
    text = " | ".join(str(attrs[k]) for k in ("INFORM", "NINFOM") if attrs.get(k)) or None
    name = attrs.get("OBJNAM") or attrs.get("NOBJNM") or None
    rec = {
        "source_class": cls.upper() if cls.lower() == "resare" else cls,
        "source_ref": attrs.get("LNAM"),
        "source_cell": base,
        "cell_scale": None if scale == float("inf") else scale,
        "name": name,
        "text": text,
        "period_start": _attr(attrs, "PERSTA") or None,      # recurring window only
        "period_end": _attr(attrs, "PEREND") or None,
        "valid_from": _attr(attrs, "DATSTA") or None,         # validity of the object
        "valid_until": _attr(attrs, "DATEND") or None,
        "attributes": attrs,
        "geometry": geom,
    }
    rec["period_source"] = "chart" if any(rec[k] for k in _TIME_FIELDS) else None
    rec["text_review"] = int(needs_text_review(rec))
    return rec


_BOOKKEEPING = ("RCID", "RVER", "FIDN", "FIDS", "LNAM", "SORDAT", "SORIND", "SCAMIN")
_LNAM_NEAR_M = 100.0   # same LNAM only counts as the same object if it is also in the same place


def _core_attrs(rec):
    return {k: v for k, v in rec["attributes"].items() if k.upper() not in _BOOKKEEPING}


def _is_duplicate(rec, grid, cell, dedupe_m):
    """Merge only when LNAM is equal (same class, same place), or when the objects come
    from different cell files with equal attributes (ignoring record bookkeeping) and
    near-identical geometry. Never merge otherwise: neighbouring wrecks stay distinct."""
    c = rec["geometry"].centroid
    ix, iy = round(c.x / cell), round(c.y / cell)
    kx, ky = _local_xy(c.y)
    ucls = rec["source_class"].upper()
    lnam = rec.get("source_ref")
    if lnam:
        for o in grid.get((ucls, "lnam", lnam), ()):
            oc = o["geometry"].centroid
            if math.hypot((oc.x - c.x) * kx, (oc.y - c.y) * ky) <= _LNAM_NEAR_M:
                return True
    size = _size(rec["geometry"], c.y)
    core = _core_attrs(rec)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for o in grid.get((ucls, ix + dx, iy + dy), ()):
                if o["source_cell"] == rec["source_cell"]:
                    continue
                oc = o["geometry"].centroid
                if math.hypot((oc.x - c.x) * kx, (oc.y - c.y) * ky) > dedupe_m:
                    continue
                osize = _size(o["geometry"], oc.y)
                if max(size, osize) > 0 and abs(size - osize) > 0.2 * max(size, osize):
                    continue
                if _core_attrs(o) == core:
                    return True
    return False


def _register(rec, grid, cell):
    c = rec["geometry"].centroid
    ucls = rec["source_class"].upper()
    grid.setdefault((ucls, round(c.x / cell), round(c.y / cell)), []).append(rec)
    if rec.get("source_ref"):
        grid.setdefault((ucls, "lnam", rec["source_ref"]), []).append(rec)


# --------------------------------------------------------------------------
# notices
# --------------------------------------------------------------------------

def notice_id(record):
    """Deterministic id: md5(source_class|LNAM)[:13] as int, so geometry tweaks between
    editions keep the id. Without an LNAM it falls back to class + geometry WKB."""
    h = hashlib.md5()
    h.update(f"{record['source_class']}|{record.get('source_ref') or ''}".encode("utf-8"))
    if not record.get("source_ref"):
        h.update(b"|")
        h.update(shapely.to_wkb(record["geometry"]))
    return int(h.hexdigest()[:13], 16)


def notice_row(record, severity, depth_m):
    """Dict with the `notices` table columns (see NOTICES_DDL)."""
    orig = record["geometry"]
    geom = orig
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        simp = geom.simplify(10.0 / _M_PER_DEG, preserve_topology=True)
        if not simp.is_empty:
            geom = simp
    rounded = shapely.set_precision(geom, 1e-6)
    if not rounded.is_empty:
        geom = rounded
    minx, miny, maxx, maxy = orig.bounds
    return {
        "id": notice_id(record),
        "source_class": record["source_class"],
        "source_ref": record.get("source_ref"),
        "source_cell": record.get("source_cell"),
        "severity": int(severity),
        "depth_m": None if depth_m is None else float(depth_m),
        "name": record.get("name"),
        "text": record.get("text"),
        "period_start": record.get("period_start"),
        "period_end": record.get("period_end"),
        "valid_from": record.get("valid_from"),
        "valid_until": record.get("valid_until"),
        "period_source": record.get("period_source"),
        "text_review": int(bool(record.get("text_review"))),
        "attributes": json.dumps(record.get("attributes") or {}, sort_keys=True, ensure_ascii=False),
        "geometry": json.dumps(mapping(geom)),
        "min_lat": miny, "min_lon": minx, "max_lat": maxy, "max_lon": maxx,
    }


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------

EdgeLink = namedtuple("EdgeLink", "edge_key index record severity downgraded")
NodeLink = namedtuple("NodeLink", "node_key index record severity")


def _projector(lon0, lat0):
    k = math.cos(math.radians(lat0))

    def f(coords):
        c = np.asarray(coords, dtype=float)
        out = np.empty_like(c)
        out[:, 0] = (c[:, 0] - lon0) * _M_PER_DEG * k
        out[:, 1] = (c[:, 1] - lat0) * _M_PER_DEG
        return out
    return f


def _record_severity(r):
    return r["severity"] if "severity" in r else classify(r, None)[0]


def _is_area_hazard(r):
    return r["source_class"].upper() in AREA_CLASSES_UPPER and r["geometry"].geom_type in ("Polygon", "MultiPolygon")


AREA_CLASSES_UPPER = tuple({c.upper() for c in AREA_CLASSES})


def _probe_geoms(records, proj, radius_m):
    """Projected link geometry per record: areas as-is, everything else buffered."""
    out = []
    for r in records:
        g = shapely.transform(r["geometry"], proj)
        out.append(g if _is_area_hazard(r) else g.buffer(radius_m))
    return out


GRAZE_SLIVER_FRACTION = 0.10   # a graze may only cut off pieces smaller than this share of the area


def _penetration_m(chain_m, area_m, boundary_m=None, step=2.0):
    """Max distance from the area boundary reached by the part of the chain inside it."""
    inter = chain_m.intersection(area_m)
    if inter.is_empty:
        return 0.0
    parts = [g for g in getattr(inter, "geoms", [inter]) if g.length > 0]
    if not parts:
        return 0.0
    boundary_m = area_m.boundary if boundary_m is None else boundary_m
    pts = []
    for p in parts:
        n = max(2, int(p.length / step) + 1)
        pts.extend(shapely.line_interpolate_point(p, np.linspace(0.0, p.length, n)))
    return float(np.max(shapely.distance(boundary_m, np.array(pts))))


def _cuts_through(chain_m, area_m, fraction=GRAZE_SLIVER_FRACTION):
    """True if the chain splits the area into several substantial pieces (every piece but
    the largest below `fraction` of the area is a corner/boundary sliver, not a cut)."""
    pieces = area_m.difference(chain_m.buffer(0.5))
    polys = [g for g in getattr(pieces, "geoms", [pieces]) if g.geom_type == "Polygon" and g.area > 0]
    if len(polys) <= 1:
        return False
    areas = sorted((g.area for g in polys), reverse=True)
    return any(x >= fraction * area_m.area for x in areas[1:])


def _is_graze(chain_m, area_m, tol_m):
    """Graze test per polygon part the chain touches (a MultiPolygon's parts are
    separate areas, so the sliver fraction is relative to each part)."""
    parts = [g for g in getattr(area_m, "geoms", [area_m]) if g.intersects(chain_m)]
    return bool(parts) and all(
        _penetration_m(chain_m, g) < tol_m and not _cuts_through(chain_m, g) for g in parts)


CHAIN_SNAP_M = 10.0     # endpoints this close count as one shared point (Net joins lines within ~8 m)
CHAIN_INSIDE_M = 1.0    # an endpoint this close to the area counts as inside it


def _chains(edge_idx, edge_m, area_m=None):
    """Group the edges linked to ONE area into chains -> [[edge index]]. Two edges chain only when
    they share an endpoint (within CHAIN_SNAP_M) that lies inside the area (within CHAIN_INSIDE_M of it):
    consecutive pieces of one crossing, or lines meeting inside the area. Lines that merely fork at a
    vertex outside the area are separate crossings, and a line that leaves and re-enters yields two chains.
    area_m=None: every endpoint counts (no area to test against)."""
    parent = {e: e for e in edge_idx}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    pts, owner = [], []
    for e in edge_idx:
        cs = shapely.get_coordinates(edge_m[e])
        for pt in (cs[0], cs[-1]):
            p = Point(float(pt[0]), float(pt[1]))
            if area_m is None or area_m.distance(p) <= CHAIN_INSIDE_M:
                pts.append(p)
                owner.append(e)
    if len(pts) > 1:
        tree = STRtree(pts)
        qi, ti = tree.query(pts, predicate="dwithin", distance=CHAIN_SNAP_M)
        for i, j in zip(qi.tolist(), ti.tolist()):
            if owner[i] != owner[j]:
                parent[find(owner[i])] = find(owner[j])
    groups = {}
    for e in edge_idx:
        groups.setdefault(find(e), []).append(e)
    return list(groups.values())


def _bridges(grp, edge_m, area_m):
    """Short segments closing the gaps (< CHAIN_SNAP_M) between snapped endpoints of a chain inside the
    area, so the graze/cut-through geometry sees two lines meeting 5 m apart as one connected crossing."""
    ends = []
    for e in grp:
        cs = shapely.get_coordinates(edge_m[e])
        for pt in (cs[0], cs[-1]):
            p = Point(float(pt[0]), float(pt[1]))
            if area_m.distance(p) <= CHAIN_INSIDE_M:
                ends.append((e, p))
    out = []
    for i, (ea, pa) in enumerate(ends):
        for eb, pb in ends[i + 1:]:
            if ea != eb and 0 < pa.distance(pb) <= CHAIN_SNAP_M:
                out.append(LineString([pa, pb]))
    return out


def link_edges(records, edges, axes=None, radius_m=HAZARD_RADIUS_M, area_tolerance_m=AREA_TOLERANCE_M,
               axis_per_edge=False):
    """Link hazards to edges. edges: iterable of (edge_key, LineString lon/lat);
    axes: optional iterable of charted-axis LineStrings (lon/lat).

    Returns a list of EdgeLink(edge_key, index, record, severity, downgraded);
    index is the record's position in `records`. severity is the effective
    severity: record["severity"] (else classify(record, None)), with a
    prohibited restriction area (RESARE/MARCUL/CTNARE) downgraded to caution (1)
    per CHAIN of linked edges (edges sharing endpoints, so a crossing densified
    into several edges is judged as one) when either
      - the chain is a graze: max penetration < area_tolerance_m AND it does not
        cut the polygon through (every piece but the largest is < 10 % of the
        polygon); a chain ending inside the polygon does not cut it, or
      - a charted axis that itself crosses the area runs along the chain inside it.
    The verdict applies to every edge of the chain. Areas link on intersection;
    all other objects within radius_m.

    axis_per_edge=True (for mesh graphs, where the edges inside an area join into one web that never runs along
    an axis as a whole): the charted-axis test is applied to each edge on its own, so a link is downgraded when
    its chain grazes OR the edge itself runs along an axis that crosses the area. The graze test stays per chain.
    """
    edges = [(k, g) for k, g in edges]
    if not records or not edges:
        return []
    allb = np.array([g.bounds for _, g in edges])
    proj = _projector(float((allb[:, 0].min() + allb[:, 2].max()) / 2),
                      float((allb[:, 1].min() + allb[:, 3].max()) / 2))
    probes = _probe_geoms(records, proj, radius_m)
    edge_m = [shapely.transform(g, proj) for _, g in edges]
    axes_m = [shapely.transform(a, proj) for a in (axes or [])]
    axes_tree = STRtree(axes_m) if axes_m else None

    tree = STRtree(probes)
    ei, ri = tree.query(np.array(edge_m, dtype=object), predicate="intersects")
    pairs = list(zip(ei.tolist(), ri.tolist()))

    by_rec = {}
    for e, r in pairs:
        by_rec.setdefault(r, []).append(e)
    downgraded = set()   # (edge index, record index)
    for r, es in by_rec.items():
        rec = records[r]
        if _record_severity(rec) != SEV_PROHIBITED or rec["source_class"].upper() not in RESTRICTION_CLASSES \
                or not _is_area_hazard(rec):
            continue
        area = probes[r]
        for grp in _chains(es, edge_m, area):
            chain = shapely.union_all([edge_m[e] for e in grp] + _bridges(grp, edge_m, area))
            graze = _is_graze(chain, area, area_tolerance_m)
            if axis_per_edge:
                for e in grp:
                    if graze or _runs_along_axis(edge_m[e], area, axes_m, axes_tree, area_tolerance_m):
                        downgraded.add((e, r))
            elif graze or _runs_along_axis(chain, area, axes_m, axes_tree, area_tolerance_m):
                downgraded.update((e, r) for e in grp)

    out = []
    for e, r in pairs:
        rec = records[r]
        down = (e, r) in downgraded
        out.append(EdgeLink(edges[e][0], r, rec, SEV_CAUTION if down else _record_severity(rec), down))
    return out


def downgrade_reasons(records, edges, links, axes=None, axis_per_edge=False):
    """Why a link was downgraded: {(edge_key, record index): 'boundary graze' | 'along charted axis'}.
    `links` come from link_edges(records, edges, axes, axis_per_edge=...); `edges` is the same (key, LineString)
    iterable. A downgrade that survives without the axes is a boundary graze; one that needs them runs along an
    axis. `axes` and `axis_per_edge` are those of the link_edges call (the axes themselves are not needed to tell
    the two reasons apart: the graze check is re-run without them)."""
    down = [l for l in links if l.downgraded]
    if not down:
        return {}
    em = dict(edges)
    if axis_per_edge:
        # the graze verdict belongs to the whole chain: re-run it on every linked edge, not just the downgraded ones
        subset = sorted({l.edge_key for l in links}, key=str)
    else:
        subset = sorted({l.edge_key for l in down}, key=str)
    plain = {(l.edge_key, l.index) for l in link_edges(records, [(k, em[k]) for k in subset], None) if l.downgraded}
    return {(l.edge_key, l.index): "boundary graze" if (l.edge_key, l.index) in plain else "along charted axis"
            for l in down}


def _runs_along_axis(chain_m, area_m, axes_m, axes_tree, tol_m):
    """True if the chain's part inside the area follows a charted axis that crosses the area."""
    if axes_tree is None:
        return False
    inter = chain_m.intersection(area_m)
    if inter.is_empty or inter.length == 0:
        return False
    for i in axes_tree.query(inter.buffer(tol_m)).tolist():
        ax = axes_m[i]
        if not ax.intersects(area_m):
            continue
        if inter.intersection(ax.buffer(tol_m)).length >= 0.9 * inter.length:
            return True
    return False


def link_nodes(records, nodes, radius_m=HAZARD_RADIUS_M):
    """Link hazards to nodes. nodes: iterable of (node_key, (lon, lat) | Point).
    A node links to areas that contain it and to other objects within radius_m.
    Returns a list of NodeLink(node_key, index, record, severity)."""
    nodes = [(k, p if hasattr(p, "x") else Point(p[0], p[1])) for k, p in nodes]
    if not records or not nodes:
        return []
    xs = [p.x for _, p in nodes]
    ys = [p.y for _, p in nodes]
    proj = _projector((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    probes = _probe_geoms(records, proj, radius_m)
    pts = np.array([shapely.transform(p, proj) for _, p in nodes], dtype=object)
    tree = STRtree(probes)
    ni, ri = tree.query(pts, predicate="intersects")
    return [NodeLink(nodes[n][0], r, records[r], _record_severity(records[r]))
            for n, r in zip(ni.tolist(), ri.tolist())]
