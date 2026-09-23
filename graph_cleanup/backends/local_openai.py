"""A local OpenAI-compatible server (llama.cpp `llama-server`, vLLM, ...) as a
Pass B review backend (`docs/SPEC-GRAPH-CLEANUP.md` §6.8).

Standard library only (`http.client`, direct connection, proxy env vars ignored); no new dependency. It talks to
`POST <base_url>/chat/completions` and sends both tile images as base64
`data:image/png;base64,...` `image_url` content parts, each preceded by a short
text label, followed by `context.json` -- the same three inputs (same PNG
bytes, same context.json) `ClaudeBackend` sends, in this API's message format.

**Safety rule: anything the backend cannot trust becomes `unsure`, and `unsure`
is treated as `keep` -- never a drop.** Concretely:

* reply that cannot be parsed as one JSON object (garbage, truncated by
  `max_tokens` or stopped by `content_filter`, several conflicting objects --
  also an unfenced one next to a fenced one --, a repeated key such as
  `{"1": keep, "1": drop}`, a top-level array, non-text `content`, only a
  `<think>` block) -> after one resample every candidate of the tile gets
  `{"verdict": "unsure", "why": "local: ..."}`, and the tile is reported as
  *degraded* (`consume_degraded`) so the runner marks its answer and
  `--resume` retries it instead of trusting it;
* a candidate that is missing, or whose verdict is not exactly
  `keep`/`drop`/`unsure`, or whose entry is not an object -> that candidate is
  `unsure`; the *other* candidates in the same reply keep their verdicts;
* numbers the reply invents (not in `context.json`) are discarded;
* a timeout / connection failure / HTTP error raises `BackendError` (only
  connection errors incl. a connect timeout, truncated/garbled HTTP responses,
  and HTTP 408/425/429/5xx are retried here, with backoff; a read timeout and
  other 4xx are not), which `runner.run_tile` turns -- after its own extra
  attempt -- into an *unanswered* tile: no `answer.json`, no ops (so also a
  keep), and a re-run of the same command picks the tile up again
  (`--resume`). `runner.run_all` aborts after N consecutive such tiles.

The connect timeout (default 10 s) is separate from the read timeout
(default 300 s): a dead host fails fast, a slow generation is still allowed.

Every call appends one record to `<tile_dir>/local_audit.jsonl` (append-only,
so successive review rounds with a tuned prompt stay comparable): prompt
version (sha256 of `prompt.txt` plus an optional human label), model and
sampling parameters, and per attempt the latency, server-reported token
`usage`, `finish_reason`, the *raw model output*, and any error; plus the
per-candidate outcome including which candidates were downgraded to `unsure`
and why.

Thinking: Qwen3-family models emit `<think>...</think>` into `content` when
served with `reasoning_format none`. By default the request carries
`chat_template_kwargs: {"enable_thinking": false}` so no thinking tokens are
generated at all; `<think>` blocks are still stripped from the reply in case
the server ignores it.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple, Union

from .base import BackendError, read_context, read_prompt

DEFAULT_BASE_URL = "http://192.168.10.111:8000/v1"
DEFAULT_MODEL = "Qwen3.8-27B-GSQ-RCO-IQ3_S"
# Low temperature + a moderately tight nucleus: the answer is a small
# structured JSON object, not prose; sampling variety only adds invalid output.
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TOP_P = 0.8
# Thinking is off by default, so the answer is ~40 tokens per candidate (a tile
# has at most 25); this leaves headroom for verbose `why` strings and for a
# `<think>` block when thinking is switched on.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_S = 300.0        # read timeout: one generation may be slow
DEFAULT_CONNECT_TIMEOUT_S = 10.0  # connect timeout: a dead host must fail fast
DEFAULT_MAX_RETRIES = 2          # extra attempts on connection errors / 5xx
DEFAULT_PARSE_RETRIES = 1        # extra samples when the reply is unusable
DEFAULT_BACKOFF_S = 2.0
MAX_CONCURRENCY = 4              # the server has 4 slots
MAX_WHY_CHARS = 500
AUDIT_FILENAME = "local_audit.jsonl"

VALID_VERDICTS = ("keep", "drop", "unsure")
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class LocalHTTPError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


class LocalConnectionError(Exception):
    """Could not reach the server (refused, DNS, reset, connect timeout,
    truncated/garbled HTTP response). Retryable."""


class LocalTimeout(Exception):
    """The connected server did not answer within the read timeout."""


Timeout = Union[float, Tuple[float, float]]  # read, or (connect, read)
Transport = Callable[[str, dict, Dict[str, str], Timeout], dict]


def _split_url(url: str) -> Tuple[str, Optional[int], bool, str]:
    """`(host, port, https, path+query)`; ValueError with a clear message for
    anything that is not `http(s)://host[:port]/...` with a valid port."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"URL must start with http:// or https://, got {url!r}"
                         + ("" if parts.scheme else " (scheme is missing)"))
    if not parts.hostname:
        raise ValueError(f"URL has no host: {url!r}")
    try:
        port = parts.port      # raises ValueError for a non-numeric / out-of-range port
    except ValueError as exc:
        raise ValueError(f"URL has an invalid port: {url!r} ({exc})") from exc
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, port, parts.scheme == "https", path


def normalize_url(base_url: Optional[str] = None) -> str:
    """The full `.../chat/completions` URL for `base_url` (else
    `$LOCAL_LLM_URL`, else the default). Raises ValueError -- before any run
    starts -- if it is not a usable http(s) URL."""
    base = (base_url or os.environ.get("LOCAL_LLM_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    _split_url(url)
    return url


def urllib_transport(url: str, payload: dict, headers: Dict[str, str],
                     timeout: Timeout) -> dict:
    """POST `payload` as JSON, return the parsed JSON response. `timeout` is
    the read timeout, or `(connect, read)`. Failures are normalised to
    LocalHTTPError / LocalConnectionError / LocalTimeout; nothing else escapes
    (incl. `http.client.HTTPException`: IncompleteRead, BadStatusLine,
    RemoteDisconnected, ...). Talks to the host directly (proxy environment
    variables are not consulted -- this is a LAN server)."""
    connect_t, read_t = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    conn = None
    try:
        # The backend validates its URL up front (`normalize_url`); everything
        # that can still go wrong here is mapped, never raised raw.
        host, port, https, path = _split_url(url)
        conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=connect_t)
        try:
            conn.connect()
        except (socket.timeout, TimeoutError) as exc:
            raise LocalConnectionError(f"connect timed out after {connect_t}s: {exc}") from exc
        try:
            conn.sock.settimeout(read_t)
            conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json", **headers})
            resp = conn.getresponse()
            status = resp.status
            body = resp.read()
        except (socket.timeout, TimeoutError) as exc:
            raise LocalTimeout(str(exc)) from exc
    except LocalConnectionError:
        raise
    except http.client.HTTPException as exc:
        raise LocalConnectionError(f"{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:  # bad URL/port that got past normalize_url
        raise LocalHTTPError(0, f"bad URL {url!r}: {exc}") from exc
    except OSError as exc:  # ConnectionError, socket.gaierror, ssl.SSLError, ...
        raise LocalConnectionError(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()
    if status >= 300:
        raise LocalHTTPError(status, body.decode("utf-8", "replace"))
    try:
        return json.loads(body)
    except ValueError as exc:
        raise LocalHTTPError(200, f"response is not JSON: {body[:200]!r}") from exc


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------

def _image_part(path: str) -> dict:
    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{data}"}}


def _user_content(tile_dir: str, context: dict) -> List[dict]:
    return [
        {"type": "text", "text": "chart.png (the chart alone):"},
        _image_part(os.path.join(tile_dir, "chart.png")),
        {"type": "text", "text": "candidates.png (the chart with the routing graph "
                                 "and the numbered candidates):"},
        _image_part(os.path.join(tile_dir, "candidates.png")),
        {"type": "text", "text": "context.json:\n" + json.dumps(context, indent=2)},
    ]


def answer_schema(numbers: List[str]) -> dict:
    """JSON schema of a valid answer for a tile, for servers that support
    grammar-constrained decoding (`response_format: json_schema`)."""
    entry = {"type": "object",
             "properties": {"verdict": {"type": "string", "enum": list(VALID_VERDICTS)},
                            "why": {"type": "string"}},
             "required": ["verdict", "why"]}
    return {"type": "object", "properties": {n: entry for n in numbers},
            "required": list(numbers)}


# --------------------------------------------------------------------------
# Reply parsing
# --------------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class ReplyError(ValueError):
    """The reply as a whole is unusable (not a per-candidate problem)."""


def strip_thinking(text: str) -> str:
    """Remove Qwen `<think>` blocks. A `</think>` with no opening tag (the chat
    template already opened it) discards everything before it; an opening tag
    that is never closed (thinking ran into `max_tokens`) discards everything
    after it -- there is no answer in that reply."""
    return _strip_thinking(text)[0]


def _strip_thinking(text: str) -> Tuple[str, bool]:
    """`(text without think blocks, an opening tag was never closed)`."""
    text = _THINK_BLOCK.sub("", text)
    lower = text.lower()
    if "</think>" in lower:
        text = text[lower.rfind("</think>") + len("</think>"):]
    lower = text.lower()
    unclosed = "<think>" in lower
    if unclosed:
        text = text[:lower.find("<think>")]
    return text, unclosed


class DuplicateKeyError(ReplyError):
    """A JSON object repeats a key: which value the model meant is unknowable
    (Python would silently keep the last one -- possibly a `drop`)."""


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise DuplicateKeyError(f"repeated JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _reply_text(content) -> str:
    """The text of `message.content`: a string, or a list of
    `{"type": "text", "text": ...}` parts (joined). Anything else (null,
    number, dict, parts of another type) is not a reply we can trust."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
            for p in content):
        return "".join(p["text"] for p in content)
    raise ReplyError(f"message content is not text: {json.dumps(content, default=str)[:200]}")


def extract_json_object(raw: str) -> dict:
    """The single JSON object in a model reply.

    Tolerates `<think>` blocks, ```json fences, and prose before/after the
    object. Raises ReplyError (-> the caller falls back to `unsure`) when
    there is no object, more than one *different* object anywhere in the reply
    (fenced or not: an unfenced object next to a fenced one that disagree is
    ambiguous), a top-level array of objects, or an object that repeats a key
    at any level, a reply with a `<think>` that is never closed (even after a
    complete answer: the model was second-guessing itself), or an object/array
    that starts but does not parse while a `"verdict"` key follows it (an
    unbalanced wrapper such as `{"answer": {"1": ...}`: the inner objects are
    never salvaged out of a span that failed to decode)."""
    text, unclosed = _strip_thinking(raw or "")
    if unclosed:
        raise ReplyError("unclosed <think> block in the reply")
    decoder = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)
    found: List[dict] = []
    i = 0
    while True:
        starts = [k for k in (text.find("{", i), text.find("[", i)) if k >= 0]
        if not starts:
            break
        i = min(starts)
        try:
            obj, end = decoder.raw_decode(text, i)
        except DuplicateKeyError:
            raise
        except ValueError:
            # A span that fails to decode is never a source of inner objects.
            # The only thing skipped is a short bracketed phrase of plain
            # prose ("[note]", "{my}") -- no quote, brace or bracket inside,
            # closed within 60 chars. Anything else that fails while a
            # "verdict" key follows is a mangled answer -> unusable.
            closer = text.find("}" if text[i] == "{" else "]", i + 1)
            span = text[i + 1:closer] if closer >= 0 else None
            prose = span is not None and len(span) <= 60 and not re.search(r'[{}\[\]"]', span)
            if not prose and '"verdict"' in text[i + 1:]:
                raise ReplyError("malformed JSON (unbalanced or truncated) around "
                                 "verdict entries; not salvaging inner objects")
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        elif isinstance(obj, list) and any(isinstance(e, (dict, list)) for e in obj):
            raise ReplyError("top-level JSON array in the reply (expected one object)")
        i = end
    if not found:
        raise ReplyError("no JSON object found in the reply")
    if any(o != found[0] for o in found[1:]):
        raise ReplyError(f"{len(found)} different JSON objects in the reply (ambiguous)")
    return found[0]


def validate_answer(parsed: dict, context: dict) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """Strict per-candidate validation against the tile's own numbers.

    Returns `(verdicts, problems)`: `verdicts` has exactly one entry per
    candidate number in `context.json`, always valid; `problems` maps a
    number (or `"_extra"` for invented numbers) to what was wrong -- those
    candidates are `unsure`, never a guess."""
    expected = [str(c["n"]) for c in context["candidates"]]
    verdicts: Dict[str, dict] = {}
    problems: Dict[str, str] = {}
    for n in expected:
        entry = parsed.get(n)
        if entry is None:
            problems[n] = "missing from the reply"
        elif not isinstance(entry, dict):
            problems[n] = f"entry is not an object: {entry!r}"[:200]
        elif entry.get("verdict") not in VALID_VERDICTS:
            problems[n] = f"invalid verdict: {entry.get('verdict')!r}"[:200]
        else:
            why = entry.get("why", "")
            verdicts[n] = {"verdict": entry["verdict"],
                           "why": why[:MAX_WHY_CHARS] if isinstance(why, str) else ""}
            continue
        verdicts[n] = {"verdict": "unsure", "why": f"local: {problems[n]}"}
    extra = sorted(set(parsed) - set(expected))
    if extra:
        problems["_extra"] = f"discarded numbers not in this tile: {extra}"
    return verdicts, problems


def _count(value) -> int:
    """A server-reported token count; anything that is not a plain
    non-negative number counts as 0 (usage is bookkeeping, never a crash)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value == value and 0 <= value < 1e12 else 0


def _all_unsure(context: dict, reason: str) -> Dict[str, dict]:
    return {str(c["n"]): {"verdict": "unsure", "why": f"local: {reason}"[:MAX_WHY_CHARS]}
            for c in context["candidates"]}


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------

class LocalOpenAIBackend:
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 temperature: float = DEFAULT_TEMPERATURE, top_p: float = DEFAULT_TOP_P,
                 max_tokens: int = DEFAULT_MAX_TOKENS, timeout: float = DEFAULT_TIMEOUT_S,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
                 enable_thinking: bool = False, json_schema: bool = False,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 parse_retries: int = DEFAULT_PARSE_RETRIES,
                 backoff_s: float = DEFAULT_BACKOFF_S,
                 max_concurrency: int = MAX_CONCURRENCY,
                 prompt_label: Optional[str] = None, api_key: Optional[str] = None,
                 transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 audit: bool = True):
        self.url = normalize_url(base_url)
        self.model = model or os.environ.get("LOCAL_LLM_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.enable_thinking = enable_thinking
        self.json_schema = json_schema
        self.max_retries = max(0, max_retries)
        self.parse_retries = max(0, parse_retries)
        self.backoff_s = backoff_s
        self.prompt_label = prompt_label
        self.audit = audit
        api_key = api_key or os.environ.get("LOCAL_LLM_API_KEY")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self.max_concurrency = max(1, min(MAX_CONCURRENCY, max_concurrency))
        self._slots = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._degraded: set = set()
        self.stats = {"calls": 0, "tiles": 0, "fallback_tiles": 0, "downgraded_candidates": 0,
                      "latency_s": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

    # -- request ----------------------------------------------------------

    def _payload(self, prompt_text: str, tile_dir: str, context: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": _user_content(tile_dir, context)},
            ],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        if self.json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "prune_verdicts", "strict": True,
                                "schema": answer_schema(
                                    [str(c["n"]) for c in context["candidates"]])}}
        return payload

    def _post_with_retries(self, payload: dict) -> Tuple[dict, float, int]:
        """One logical call: retry connection errors / retryable HTTP statuses
        with exponential backoff (connection errors include a connect timeout
        and a truncated/garbled HTTP response). A read timeout and
        non-retryable statuses (4xx, e.g. a context overflow) are not retried. Returns (response, latency_s,
        n_transport_attempts); raises BackendError."""
        attempt = 0
        while True:
            attempt += 1
            t0 = time.monotonic()
            try:
                with self._slots:
                    resp = self._transport(self.url, payload, self.headers,
                                           (self.connect_timeout, self.timeout))
                return resp, time.monotonic() - t0, attempt
            except LocalTimeout as exc:
                raise BackendError(f"read timeout after {self.timeout}s: {exc}") from exc
            except LocalHTTPError as exc:
                if exc.status not in _RETRYABLE_STATUS:
                    raise BackendError(f"local server error: {exc}") from exc
                err: Exception = exc
            except LocalConnectionError as exc:
                err = exc
            if attempt > self.max_retries:
                raise BackendError(f"local server unavailable after {attempt} "
                                   f"attempt(s): {err}") from err
            self._sleep(self.backoff_s * (2 ** (attempt - 1)))

    # -- one tile ---------------------------------------------------------

    def answer_tile(self, tile_dir: str) -> str:
        context = read_context(tile_dir)
        prompt_text = read_prompt(tile_dir)
        with self._lock:
            self._degraded.discard(tile_dir)
        payload = self._payload(prompt_text, tile_dir, context)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tile_id": context.get("tile_id"),
            "backend": "local_openai", "url": self.url, "model": self.model,
            "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12],
            "prompt_label": self.prompt_label,
            "params": {"temperature": self.temperature, "top_p": self.top_p,
                       "max_tokens": self.max_tokens,
                       "enable_thinking": self.enable_thinking,
                       "json_schema": self.json_schema},
            "attempts": [], "outcome": None,
        }
        try:
            verdicts, problems = self._sample_until_parsed(payload, context, record)
        except BackendError as exc:
            record["outcome"] = {"status": "backend_error", "error": str(exc)}
            self._write_audit(tile_dir, record)
            raise
        fell_back = verdicts is None
        record["outcome"] = {"status": "fallback_unsure" if fell_back else "ok",
                             "problems": problems}
        if fell_back:
            verdicts = _all_unsure(context, problems.get("_reply", "unusable reply"))
            n_down = len(verdicts)
        else:
            n_down = sum(1 for k in problems if k != "_extra")
        with self._lock:
            self.stats["tiles"] += 1
            self.stats["fallback_tiles"] += int(fell_back)
            if fell_back:
                self._degraded.add(tile_dir)
            self.stats["downgraded_candidates"] += n_down
        record["outcome"]["verdicts"] = verdicts
        self._write_audit(tile_dir, record)
        return json.dumps(verdicts)

    def consume_degraded(self, tile_dir: str) -> bool:
        """True (once) if the last `answer_tile(tile_dir)` fell back to an
        all-`unsure` answer because no reply was usable. `runner.run_all`
        records that next to the answer so `--resume` retries the tile."""
        with self._lock:
            if tile_dir in self._degraded:
                self._degraded.discard(tile_dir)
                return True
            return False

    def _sample_until_parsed(self, payload: dict, context: dict, record: dict):
        """Returns (verdicts | None, problems). None means every reply was
        unusable as a whole (the caller falls back to all-`unsure`)."""
        last_reason = "no reply"
        for sample in range(self.parse_retries + 1):
            try:
                resp, latency, n_tries = self._post_with_retries(payload)
            except BackendError as exc:
                record["attempts"].append({"sample": sample, "error": str(exc)})
                raise
            usage = resp.get("usage") if isinstance(resp, dict) else None
            try:
                choice = resp["choices"][0]
                message = choice["message"]
                content = message.get("content")
                reasoning = message.get("reasoning_content")
                finish = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError, AttributeError):
                last_reason = "malformed API response (no choices[0].message)"
                record["attempts"].append({
                    "sample": sample, "latency_s": round(latency, 3),
                    "transport_attempts": n_tries, "usage": usage,
                    "raw_response": json.dumps(resp)[:4000], "error": last_reason})
                self._account(latency, usage)
                continue
            attempt = {"sample": sample, "latency_s": round(latency, 3),
                       "transport_attempts": n_tries, "usage": usage,
                       "finish_reason": finish,
                       "raw_output": content if isinstance(content, str)
                       else json.dumps(content, default=str)[:4000]}
            if reasoning:  # only present when thinking is enabled; kept for audit
                attempt["reasoning_content"] = str(reasoning)[:8000]
            record["attempts"].append(attempt)
            self._account(latency, usage)
            try:
                # Neither a truncated nor a filtered reply is trusted, even
                # when the text left over happens to parse.
                if finish == "length":
                    raise ReplyError("reply truncated by max_tokens")
                if finish == "content_filter":
                    raise ReplyError("reply stopped by the server's content filter")
                parsed = extract_json_object(_reply_text(content))
            except ReplyError as exc:
                last_reason = str(exc)
                attempt["error"] = last_reason
                continue
            verdicts, problems = validate_answer(parsed, context)
            return verdicts, problems
        return None, {"_reply": last_reason}

    def _account(self, latency: float, usage: Optional[dict]) -> None:
        with self._lock:
            self.stats["calls"] += 1
            self.stats["latency_s"] += latency
            if isinstance(usage, dict):
                self.stats["prompt_tokens"] += _count(usage.get("prompt_tokens"))
                self.stats["completion_tokens"] += _count(usage.get("completion_tokens"))

    def _write_audit(self, tile_dir: str, record: dict) -> None:
        if not self.audit:
            return
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(os.path.join(tile_dir, AUDIT_FILENAME), "a",
                              encoding="utf-8") as fh:
            fh.write(line + "\n")

    def summary(self) -> str:
        s = self.stats
        return (f"local backend: {s['calls']} call(s) for {s['tiles']} tile(s), "
                f"{s['latency_s']:.1f}s total, tokens prompt={s['prompt_tokens']} "
                f"completion={s['completion_tokens']}; {s['fallback_tiles']} tile(s) "
                f"fell back to all-unsure, {s['downgraded_candidates']} candidate(s) "
                f"downgraded to unsure")
