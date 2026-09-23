"""Drive a backend over a batch of prepared tiles: answer, validate, retry
once, record, and turn a `keep`/`drop`/`unsure` verdict into an `Op`
(`docs/SPEC-GRAPH-CLEANUP.md` §6).

Resumable by construction: each tile's raw answer is written to
`<tile_dir>/answer.json` as soon as it is received, so a killed or interrupted
run picks back up by skipping tiles that already have one (`--resume`, the
default). A tile that never produces valid JSON after one retry is marked
`unanswered` and contributes no ops -- **the default is always `keep`**,
because a tile a weak backend could not judge must never silently become a
deletion.

Three guards keep an old or doubtful answer from ever becoming a drop:

* every answer is written together with `answer.meta.json` recording the
  sha256 of the tile's `manifest.json` (the candidate ids/nodes the numbers
  stand for) and whether the backend flagged it *degraded* (an all-`unsure`
  fallback for an unusable reply). `answers_to_ops` refuses an answer whose
  recorded manifest is not the tile's current one, and `--resume` re-runs
  degraded and stale answers instead of trusting them;
* a tile that is being re-run first has its old `answer.json`, `answer.meta.json`
  and `error.txt` moved to `<tile>/history/NNNN/` (not deleted, not read back),
  so a backend that then fails cannot leave the previous round's verdicts in
  force, yet paid-for work survives an outage;
* `run_all` aborts after N consecutive tiles that failed with `BackendError`
  (a dead or wedged server) or M consecutive all-unsure fallbacks, instead of
  grinding through every tile.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .backends.base import Backend, BackendError, read_context
from .candidates import DEAD_END_STUB, SMALL_COMPONENT
from .ops import DROP_NODE, Op

VALID_VERDICTS = ("keep", "drop", "unsure")
ANSWER_FILE = "answer.json"
META_FILE = "answer.meta.json"
ERROR_FILE = "error.txt"
HISTORY_DIR = "history"
DEFAULT_MAX_CONSECUTIVE_BACKEND_ERRORS = 5
DEFAULT_MAX_CONSECUTIVE_DEGRADED = 10


class StaleAnswerError(ValueError):
    """An `answer.json` does not belong to the tile's current manifest."""


class BackendCircuitOpen(RuntimeError):
    """`run_all` gave up: too many tiles in a row failed with `BackendError`.
    `.stats` holds what was done before aborting; answers already written stay
    (rerun with --resume to continue)."""

    def __init__(self, message: str, stats: "RunStats"):
        super().__init__(message)
        self.stats = stats


@dataclass
class RunStats:
    total: int = 0
    answered: int = 0
    skipped_existing: int = 0
    retried: int = 0
    unanswered: int = 0
    degraded: int = 0
    verdicts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.answered}/{self.total} tiles answered "
                f"({self.skipped_existing} already done, {self.retried} retried, "
                f"{self.unanswered} unanswered, {self.degraded} degraded to all-unsure); "
                f"verdicts {self.verdicts}")


def _validate(raw: str, context: dict) -> Dict[str, dict]:
    """Parse and check a backend's answer against the tile's own candidate
    numbers. Raises ValueError on anything wrong -- the caller decides whether
    that's worth a retry."""
    from .backends.claude import parse_answer  # reuses the fence-stripping parser

    parsed = parse_answer(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"answer is not a JSON object: {type(parsed)}")
    expected = {str(c["n"]) for c in context["candidates"]}
    got = set(parsed.keys())
    unknown = got - expected
    if unknown:
        raise ValueError(f"answer references numbers not in this tile: {unknown}")
    missing = expected - got
    if missing:
        raise ValueError(f"answer omits numbers from this tile: {missing}")
    for n, entry in parsed.items():
        if not isinstance(entry, dict) or "verdict" not in entry:
            raise ValueError(f"entry {n!r} is missing 'verdict': {entry!r}")
        if entry["verdict"] not in VALID_VERDICTS:
            raise ValueError(f"entry {n!r} has an invalid verdict: {entry['verdict']!r}")
    return parsed


def manifest_digest(tile_dir: str) -> Optional[str]:
    """sha256 of the tile's `manifest.json` (None if there is none): the
    identity of the candidates that the answer's numbers refer to."""
    path = os.path.join(tile_dir, "manifest.json")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _read_meta(tile_dir: str) -> Optional[dict]:
    path = os.path.join(tile_dir, META_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except ValueError:
        return {"unreadable": True}
    return meta if isinstance(meta, dict) else {"unreadable": True}


def answer_status(tile_dir: str) -> str:
    """`missing` (no answer.json), `stale` (recorded for another manifest, or
    unreadable metadata), `degraded` (backend flagged an all-unsure fallback)
    or `valid`. An answer with no `answer.meta.json` (written by an older
    version or an external batch collector) counts as valid."""
    if not os.path.exists(os.path.join(tile_dir, ANSWER_FILE)):
        return "missing"
    meta = _read_meta(tile_dir)
    if meta is None:
        return "valid"
    recorded = meta.get("manifest_sha256")
    if meta.get("unreadable") or (recorded is not None and recorded != manifest_digest(tile_dir)):
        return "stale"
    return "degraded" if meta.get("degraded") else "valid"


def atomic_write_text(path: str, text: str) -> None:
    """Write `text` to `path` so a crash leaves either the old file or the new
    one, never a truncated mix: temp file in the same directory, fsync,
    `os.replace`. A failed write leaves `path` untouched and no temp file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def archive_results(tile_dir: str) -> Optional[str]:
    """Move a tile's previous round out of the way instead of deleting it.

    `answer.json`, `answer.meta.json` and `error.txt` (plus a copy of the
    `manifest.json` they were made against, unless the answer was stale) go to `<tile>/history/NNNN/`
    (NNNN = 0001, 0002, ... in the order the rounds were superseded). Nothing
    in `history/` is ever read back by `answer_status`/`answers_to_ops`, so a
    re-run that then fails cannot fall back to an old answer -- yet paid-for
    work survives an outage and rounds can be compared. `local_audit.jsonl`
    is append-only and stays where it is. A lone `error.txt` is just deleted.
    Returns the archive directory, or None if there was no answer to keep."""
    present = [n for n in (ANSWER_FILE, META_FILE, ERROR_FILE)
               if os.path.exists(os.path.join(tile_dir, n))]
    if ANSWER_FILE not in present and META_FILE not in present:
        for name in present:
            os.remove(os.path.join(tile_dir, name))
        return None
    history = os.path.join(tile_dir, HISTORY_DIR)
    os.makedirs(history, exist_ok=True)
    rounds = [int(d) for d in os.listdir(history) if d.isdigit()]
    dest = os.path.join(history, f"{max(rounds, default=0) + 1:04d}")
    os.makedirs(dest)
    manifest = os.path.join(tile_dir, "manifest.json")
    # only if it is the manifest the answer was made for (not for a stale one)
    if os.path.exists(manifest) and answer_status(tile_dir) != "stale":
        shutil.copy2(manifest, os.path.join(dest, "manifest.json"))
    for name in present:
        os.replace(os.path.join(tile_dir, name), os.path.join(dest, name))
    return dest


def _attempt_tile(backend: Backend, tile_dir: str, max_retries: int):
    """Returns (verdicts | None, last_error | None, ended_in_backend_error)."""
    context = read_context(tile_dir)
    last_error = None
    backend_failed = False
    for attempt in range(max_retries + 1):
        try:
            raw = backend.answer_tile(tile_dir)
        except BackendError as exc:
            last_error, backend_failed = str(exc), True
            continue
        try:
            return _validate(raw, context), None, False
        except (ValueError, json.JSONDecodeError) as exc:
            last_error, backend_failed = str(exc), False
            continue
    return None, last_error, backend_failed


def run_tile(backend: Backend, tile_dir: str, max_retries: int = 1) -> Optional[Dict[str, dict]]:
    """Answer one tile, validating and retrying. Returns the validated verdict
    dict, or None if it never became valid.

    `max_retries` is the *runner's* retry: up to `max_retries + 1` calls to
    `backend.answer_tile`, repeated after a `BackendError` as well as after an
    invalid answer. It multiplies with whatever retrying the backend does
    itself (the local backend already retries connection errors/5xx, and a
    timeout is not retried there, but is here -- so a timing-out tile costs
    `(max_retries + 1) * timeout`)."""
    error_path = os.path.join(tile_dir, ERROR_FILE)
    if os.path.exists(error_path):
        os.remove(error_path)  # an earlier round's error is not this round's
    verdicts, last_error, _ = _attempt_tile(backend, tile_dir, max_retries)
    if verdicts is None and last_error:
        atomic_write_text(error_path, last_error)
    return verdicts


def run_all(backend: Backend, tile_dirs: Sequence[str], resume: bool = True,
           max_retries: int = 1, sleep_between_s: float = 0.0,
           max_consecutive_backend_errors: int = DEFAULT_MAX_CONSECUTIVE_BACKEND_ERRORS,
           max_consecutive_degraded: int = DEFAULT_MAX_CONSECUTIVE_DEGRADED
           ) -> RunStats:
    """Answer every tile in `tile_dirs`, writing `answer.json` (plus
    `answer.meta.json`) into each.

    With `resume`, tiles whose answer is `valid` are skipped; `degraded` and
    `stale` answers are re-run. Without it everything is re-run. Either way the
    previous round's results are first *archived* under `<tile>/history/`
    (see `archive_results`), never deleted and never read back.

    Raises `BackendCircuitOpen` after `max_consecutive_backend_errors` tiles in
    a row ended in a `BackendError`, or after `max_consecutive_degraded` tiles
    in a row came back as an all-unsure fallback (0 disables either check).
    Skipped (already valid) tiles neither count nor reset the streaks.

    If a tile's manifest changes while the backend is answering it (someone
    re-prepared the tile), the answer belongs to no known build and is
    discarded."""
    stats = RunStats(total=len(tile_dirs))
    consecutive = 0
    consecutive_degraded = 0
    consume_degraded = getattr(backend, "consume_degraded", None)
    for tile_dir in tile_dirs:
        if resume and answer_status(tile_dir) == "valid":
            stats.skipped_existing += 1
            continue
        archive_results(tile_dir)
        digest = manifest_digest(tile_dir)     # the build the answer is *for*
        verdicts, last_error, backend_failed = _attempt_tile(backend, tile_dir, max_retries)
        degraded = bool(consume_degraded(tile_dir)) if consume_degraded else False
        if verdicts is not None and manifest_digest(tile_dir) != digest:
            verdicts, backend_failed = None, False
            last_error = "tile was re-prepared while it was being answered; answer discarded"
        if verdicts is None:
            stats.unanswered += 1
            if last_error:
                atomic_write_text(os.path.join(tile_dir, ERROR_FILE), last_error)
                stats.errors.append(f"{os.path.basename(tile_dir.rstrip('/'))}: {last_error}")
            consecutive = consecutive + 1 if backend_failed else 0
            if max_consecutive_backend_errors and consecutive >= max_consecutive_backend_errors:
                raise BackendCircuitOpen(
                    f"aborting: {consecutive} consecutive tiles failed with a backend "
                    f"error (last: {last_error}); {stats.answered} tile(s) answered so "
                    f"far -- fix the backend and re-run with --resume "
                    f"(--max-consecutive-backend-errors 0 disables this check)", stats)
            continue
        consecutive = 0
        # meta first: a kill in between leaves "no answer" (re-run), never a
        # degraded answer that looks valid. Both writes are atomic.
        atomic_write_text(os.path.join(tile_dir, META_FILE),
                          json.dumps({"manifest_sha256": digest, "degraded": degraded}))
        atomic_write_text(os.path.join(tile_dir, ANSWER_FILE), json.dumps(verdicts, indent=2))
        stats.answered += 1
        stats.degraded += int(degraded)
        for entry in verdicts.values():
            stats.verdicts[entry["verdict"]] = stats.verdicts.get(entry["verdict"], 0) + 1
        consecutive_degraded = consecutive_degraded + 1 if degraded else 0
        if max_consecutive_degraded and consecutive_degraded >= max_consecutive_degraded:
            raise BackendCircuitOpen(
                f"aborting: {consecutive_degraded} consecutive tiles came back unusable "
                f"(all-unsure fallback) -- the model or prompt is not producing JSON; "
                f"{stats.answered} tile(s) answered so far, re-run with --resume to retry "
                f"the degraded ones (--max-consecutive-degraded 0 disables this check)", stats)
        if sleep_between_s:
            time.sleep(sleep_between_s)
    return stats


def answers_to_ops(tile_dirs: Sequence[str], author: str) -> List[Op]:
    """Turn every tile's `answer.json` (once written by `run_all`, or by a
    batch collector) into `Op` records.

    Only `drop` produces an op -- `keep` and `unsure` both mean "leave it",
    which is already the graph's current state and needs no operation. A
    `small_component` drop removes every node in the component (its edges go
    with it); a `dead_end_stub` drop removes the stub's own chain, working from
    the free end inward so each `drop_node` sees a still-degree-1 node.
    """
    ops: List[Op] = []
    legacy: List[str] = []
    for tile_dir in tile_dirs:
        answer_path = os.path.join(tile_dir, ANSWER_FILE)
        manifest_path = os.path.join(tile_dir, "manifest.json")
        if not os.path.exists(answer_path) or not os.path.exists(manifest_path):
            continue
        tile_id = os.path.basename(tile_dir.rstrip("/"))
        status = answer_status(tile_dir)
        if status == "stale":
            raise StaleAnswerError(
                f"{tile_id}: answer.json was recorded for a different manifest "
                f"(another graph build or prepare) -- refusing to turn it into ops; "
                f"re-run the tile with --no-resume")
        if status == "degraded":
            continue  # all-unsure fallback: nothing to drop
        if not os.path.exists(os.path.join(tile_dir, META_FILE)):
            legacy.append(tile_id)
        with open(answer_path, encoding="utf-8") as fh:
            verdicts = json.load(fh)
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        if not isinstance(verdicts, dict) or set(verdicts) != set(manifest):
            raise StaleAnswerError(
                f"{tile_id}: answer.json covers candidate numbers "
                f"{sorted(verdicts) if isinstance(verdicts, dict) else '?'} but the "
                f"manifest has {sorted(manifest)} -- not the same tile build; "
                f"refusing to turn it into ops")
        for n, entry in verdicts.items():
            if not isinstance(entry, dict) or entry.get("verdict") != "drop":
                continue
            cand = manifest.get(n)
            if cand is None:
                continue
            reason = entry.get("why") or f"{cand['kind']} dropped by reviewer"
            ops += _drop_ops(cand, author, reason, tile_id)
    if legacy:
        print(f"warning: {len(legacy)} answer(s) have no {META_FILE} (older version or "
              f"external collector), so they cannot be tied to the graph build they were "
              f"made for; candidate numbers are only checked against the manifest's "
              f"numbers 1..N. Tiles: {', '.join(legacy[:10])}"
              f"{' ...' if len(legacy) > 10 else ''}", file=sys.stderr)
    return ops


#  The prompt asks for a verdict + a one-line reason, not a numeric confidence
#  -- there is nothing in a model's answer to read one from. A flat value below
#  1.0 (a deterministic pass's confidence) marks these as needing the human
#  review the tier-5 override workflow (README.md "Community override workflow")
#  calls for, without pretending to a precision the model never gave.
AI_DROP_CONFIDENCE = 0.7


def _drop_ops(cand: dict, author: str, reason: str, tile_id: str) -> List[Op]:
    kind = cand["kind"]
    nodes = cand.get("nodes")
    if not nodes:
        return []
    if kind == SMALL_COMPONENT:
        target_nodes = nodes
    elif kind == DEAD_END_STUB:
        # Drop from the free end inward -- dropping the junction end first
        # would delete a node still holding the rest of the graph together.
        target_nodes = nodes[:-1]
    else:
        return []
    return [Op(op=DROP_NODE, node=n, author=author, reason=reason,
               confidence=AI_DROP_CONFIDENCE, tile=tile_id) for n in target_nodes]
