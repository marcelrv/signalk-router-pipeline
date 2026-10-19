#!/usr/bin/env python3
"""Review chart text that mentions time or conditions but has no period/validity attribute.

A restriction area whose INFORM/NINFOM says "Winter resting area from November 1 until May 15" but carries RESTRN 7
and no PERSTA/PEREND is blocked all year (notices.text_review = 1, edge reason 'conditions in chart text, unreviewed').
This tool turns such text into a reviewed override in data/hazard_overrides.json, which the builders apply before tiering
(docs/SPEC-HAZARDS-AND-NOTICES.md). An LLM only ever PROPOSES; a human accepts.

  scripts/review_hazard_text.py list    (--cells-dir DIR --bbox lon0,lat0,lon1,lat1 | --db FILE.sqlite)
  scripts/review_hazard_text.py propose (--cells-dir DIR --bbox ... | --db FILE) [--base-url URL] [--model M]
  scripts/review_hazard_text.py propose --manual ID --period-start --1101 --period-end --0515 --note "..." --by NAME
        (--text-from-cells/--db are needed once so the text hash can be taken, unless --text-sha1 is given)
  scripts/review_hazard_text.py accept ID [ID ...] --by NAME
  scripts/review_hazard_text.py reject ID [ID ...] --by NAME

Common: --overrides PATH (default data/hazard_overrides.json). Dates are S-57 forms: '--MMDD', '--MM', 'CCYYMMDD',
'CCYYMM', 'CCYY'. `period_*` is the recurring window (can wrap over New Year), `valid_*` the object's validity.
An accepted entry with all four dates null records "reviewed: no time limit" (the area stays blocked, text_review 0).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hazards as hz  # noqa: E402

DATE_FIELDS = ("period_start", "period_end", "valid_from", "valid_until")

SYSTEM_PROMPT = """You read the chart text of one nautical chart object (an S-57 restriction area) and extract WHEN it applies.
Answer with ONE JSON object and nothing else, with exactly these keys:
  "period_start", "period_end": the RECURRING yearly window as S-57 dates '--MMDD' (for example November 1 -> '--1101',
      May 15 -> '--0515'; the window may wrap over New Year), or null
  "valid_from", "valid_until": a one-off validity as 'CCYYMMDD' (for example '20260101'), or null
  "note": one short sentence quoting the words you relied on
  "confident": true only when the text states the dates explicitly and unambiguously; otherwise false
Use null for anything the text does not state. Never guess a date. If the text gives no dates, return all four as null
and "confident": false."""


class NoBackend(Exception):
    """No LLM backend could be reached."""


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------

def _candidate(rec_id, cls, tier, name, text, sha1):
    return {"id": str(rec_id), "class": cls, "tier": tier, "name": name, "text": text, "text_sha1": sha1}


def from_records(records):
    """Candidates (text_review = 1, i.e. raw records, overrides not applied) from extract_hazards records."""
    out = []
    for r in records:
        if not r.get("text_review"):
            continue
        sev = hz.classify(r, None)[0]
        out.append(_candidate(hz.notice_id(r), r["source_class"], hz.SEVERITY_NAMES[sev], r.get("name"),
                              r.get("text"), hz.text_sha1(r)))
    return out


def from_cells(cells_dir, bbox):
    cells = hz.latest_cells(cells_dir, bbox)
    if not cells:
        raise SystemExit(f"no S-57 cells (*.000) under {cells_dir} overlap {bbox}")
    return from_records(hz.extract_hazards(cells, tuple(bbox)))


def from_db(path):
    con = sqlite3.connect(path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(notices)")}
        if "text_review" not in cols:
            raise SystemExit(f"{path}: not a schema-3 DB with notices.text_review (rebuild with the time-aware code)")
        rows = con.execute("SELECT id, source_class, severity, name, text FROM notices WHERE text_review = 1 "
                           "ORDER BY id").fetchall()
    finally:
        con.close()
    out = []
    for nid, cls, sev, name, text in rows:
        rec = {"name": name, "text": text}
        out.append(_candidate(nid, cls, hz.SEVERITY_NAMES.get(sev, str(sev)), name, text, hz.text_sha1(rec)))
    return out


def load_candidates(a):
    if a.db:
        return from_db(a.db)
    if a.cells_dir and a.bbox:
        return from_cells(a.cells_dir, [float(x) for x in a.bbox.split(",")])
    raise SystemExit("give --db FILE, or --cells-dir DIR with --bbox lon0,lat0,lon1,lat1")


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def cmd_list(cands, overrides, out=print):
    for c in cands:
        st = (overrides.get(c["id"]) or {}).get("status", "-")
        out(f"{c['id']}\t{c['class']}\t{c['tier']}\t[{st}]\t{c['name'] or ''}\t{c['text'] or ''}")
    out(f"{len(cands)} notice(s) with text_review=1")
    return cands


# --------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_dates(d):
    """{field: S-57 date or None} for the four date fields; ValueError on a bad form."""
    return {k: hz.validate_s57_date(d.get(k)) for k in DATE_FIELDS}


class LlmBackend:
    """Ask the local OpenAI-compatible server (the one navline_agent.local_agent / graph_cleanup use) one JSON answer."""

    def __init__(self, base_url=None, model=None, transport=None, timeout=(10.0, 300.0)):
        from graph_cleanup.backends import local_openai as lo
        self.lo = lo
        self.url = lo.normalize_url(base_url)
        self.model = model or lo.DEFAULT_MODEL
        self.transport = transport or lo.urllib_transport
        self.timeout = timeout
        self.name = f"llm:{self.model}"

    def __call__(self, cand):
        lo = self.lo
        user = f"Class: {cand['class']}\nName: {cand['name'] or ''}\nText: {cand['text'] or ''}"
        payload = {"model": self.model, "temperature": 0.0, "max_tokens": 1500,
                   "chat_template_kwargs": {"enable_thinking": False},
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]}
        try:
            resp = self.transport(self.url, payload, {}, self.timeout)
        except (lo.LocalConnectionError, lo.LocalTimeout, lo.LocalHTTPError, OSError) as exc:
            raise NoBackend(f"cannot reach the LLM backend at {self.url}: {exc}. Start the server, pass --base-url / "
                            f"$LOCAL_LLM_URL, or add proposals by hand with --manual.") from exc
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"malformed API response: {str(resp)[:200]}") from exc
        return lo.extract_json_object(lo._reply_text(content))


def parse_answer(ans):
    """Strict check of a backend answer -> entry fields. ValueError when keys are missing, dates are bad or
    `confident` is not a bool."""
    if not isinstance(ans, dict):
        raise ValueError("answer is not a JSON object")
    missing = [k for k in DATE_FIELDS + ("note", "confident") if k not in ans]
    if missing:
        raise ValueError(f"missing keys {missing}")
    if not isinstance(ans["confident"], bool):
        raise ValueError("'confident' must be true or false")
    out = validate_dates(ans)
    out["note"] = str(ans["note"] or "")
    out["confident"] = ans["confident"]
    return out


def _entry(cand, fields, status, proposed_by):
    e = {k: fields.get(k) for k in DATE_FIELDS}
    e.update({"note": fields.get("note", ""), "text_sha1": cand["text_sha1"], "status": status,
              "proposed_by": proposed_by, "reviewed_by": None, "reviewed_at": None})
    if "confident" in fields:
        e["confident"] = fields["confident"]
    return e


def cmd_propose(cands, overrides, backend, out=print):
    """Ask `backend(cand) -> dict` for every candidate without an entry for its current text (a stale entry,
    whose text_sha1 no longer matches, is replaced); store 'proposed' entries in
    `overrides` (in place). Returns (proposed, failed) counts. NoBackend propagates."""
    n_ok = n_bad = 0
    for c in cands:
        if c["id"] in overrides and overrides[c["id"]].get("text_sha1") == c["text_sha1"]:
            continue            # already proposed/accepted/rejected for this very text; a stale entry is re-proposed
        try:
            fields = parse_answer(backend(c))
        except NoBackend:
            raise
        except ValueError as exc:
            n_bad += 1
            out(f"{c['id']}: no usable proposal ({exc})")
            continue
        overrides[c["id"]] = _entry(c, fields, "proposed", getattr(backend, "name", "llm"))
        n_ok += 1
        out(f"{c['id']}: proposed {fields['period_start']}..{fields['period_end']} "
            f"valid {fields['valid_from']}..{fields['valid_until']} confident={fields['confident']}: {fields['note']}")
    out(f"{n_ok} proposed, {n_bad} without a usable answer")
    return n_ok, n_bad


def cmd_manual(cands, overrides, nid, fields, by, text_sha1=None):
    """Add a 'proposed' entry by hand. The text hash comes from the candidate list (or --text-sha1)."""
    nid = str(nid)
    fields = dict(fields)
    validate_dates(fields)
    cand = next((c for c in cands if c["id"] == nid), None)
    sha = text_sha1 or (cand and cand["text_sha1"])
    if not sha:
        raise SystemExit(f"notice {nid} is not in the candidate list: give --cells-dir/--bbox or --db, or --text-sha1")
    overrides[nid] = _entry({"text_sha1": sha}, fields, "proposed", by)
    return overrides[nid]


# --------------------------------------------------------------------------
# accept / reject
# --------------------------------------------------------------------------

def cmd_review(overrides, ids, status, by):
    """Set status 'accepted' or 'rejected' on existing entries (dates re-validated on accept)."""
    if not by:
        raise SystemExit("--by NAME is required")
    for nid in ids:
        e = overrides.get(str(nid))
        if e is None:
            raise SystemExit(f"no override entry for notice {nid}: propose one first")
        if status == "accepted":
            try:
                validate_dates(e)
            except ValueError as exc:
                raise SystemExit(f"notice {nid}: {exc}")
            if not e.get("text_sha1"):
                raise SystemExit(f"notice {nid}: entry has no text_sha1")
    for nid in ids:
        e = overrides[str(nid)]
        if status == "accepted" and bool(e.get("period_start")) != bool(e.get("period_end")):
            print(f"WARNING: notice {nid}: only one of period_start/period_end is set", file=sys.stderr)
        e.update(status=status, reviewed_by=by, reviewed_at=_now())
    return overrides


# --------------------------------------------------------------------------

def main(argv=None, backend=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overrides", default=hz.DEFAULT_OVERRIDES_PATH,
                    help="Path of the reviewed-overrides JSON file (default %(default)s).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def source(p):
        p.add_argument("--cells-dir", help="directory of raw S-57 .000 cells to read hazard text from")
        p.add_argument("--bbox", help="lon0,lat0,lon1,lat1")
        p.add_argument("--db", help="a schema-3 routing DB")

    source(sub.add_parser("list"))
    pp = sub.add_parser("propose")
    source(pp)
    pp.add_argument("--base-url", default=None, help="local OpenAI-compatible server URL (AI proposal mode)")
    pp.add_argument("--model", default=None, help="model name to ask on that server")
    pp.add_argument("--manual", metavar="ID", help="propose by hand for this notice id instead of asking a model")
    pp.add_argument("--period-start", help="manual: recurring window start, S-57 partial date (e.g. --1101)")
    pp.add_argument("--period-end", help="manual: recurring window end, S-57 partial date (e.g. --0515; may wrap over New Year)")
    pp.add_argument("--valid-from", help="manual: one-off validity start, S-57 date (CCYYMMDD, CCYYMM or CCYY)")
    pp.add_argument("--valid-until", help="manual: one-off validity end, S-57 date (CCYYMMDD, CCYYMM or CCYY)")
    pp.add_argument("--note", default="", help="manual: free-text reason recorded with the proposal")
    pp.add_argument("--text-sha1", help="manual: sha1 of the chart text the proposal applies to")
    pp.add_argument("--by", default=None, help="who proposes (manual mode)")
    for name in ("accept", "reject"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+", help="notice ids to %s" % name)
        p.add_argument("--by", required=True, help="reviewer name recorded with the decision")
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, tok in enumerate(argv[:-1]):     # '--period-start --1101': argparse would read '--1101' as an option
        if tok in ("--period-start", "--period-end") and re.match(r"^---?\d", argv[i + 1]):
            argv[i:i + 2] = [f"{tok}={argv[i + 1]}", None]
    a = ap.parse_args([x for x in argv if x is not None])

    overrides = hz.load_overrides(a.overrides)
    if a.cmd == "list":
        cmd_list(load_candidates(a), overrides)
        return 0
    if a.cmd in ("accept", "reject"):
        cmd_review(overrides, a.ids, "accepted" if a.cmd == "accept" else "rejected", a.by)
        hz.save_overrides(a.overrides, overrides)
        print(f"{a.cmd}ed {len(a.ids)} override(s) in {a.overrides}")
        return 0
    # propose
    if a.manual:
        cands = load_candidates(a) if (a.db or a.cells_dir) else []
        try:
            cmd_manual(cands, overrides, a.manual, {"period_start": a.period_start, "period_end": a.period_end,
                                                    "valid_from": a.valid_from, "valid_until": a.valid_until,
                                                    "note": a.note}, a.by or "manual", a.text_sha1)
        except ValueError as exc:
            raise SystemExit(str(exc))
        hz.save_overrides(a.overrides, overrides)
        print(f"proposed override for {a.manual} in {a.overrides}")
        return 0
    cands = load_candidates(a)
    try:
        backend = backend or LlmBackend(a.base_url, a.model)
        _, bad = cmd_propose(cands, overrides, backend)
    except NoBackend as exc:
        if overrides:
            hz.save_overrides(a.overrides, overrides)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:       # bad --base-url
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    hz.save_overrides(a.overrides, overrides)
    return 0


if __name__ == "__main__":
    sys.exit(main())
