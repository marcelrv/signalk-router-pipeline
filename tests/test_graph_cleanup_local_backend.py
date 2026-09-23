"""Unit tests for `graph_cleanup/backends/local_openai.py`. The HTTP layer is
always replaced by a fake transport -- no network, no server."""
import base64
import json
import os

import pytest

from graph_cleanup import runner
from graph_cleanup.backends.base import BackendError
from graph_cleanup.backends.local_openai import (
    AUDIT_FILENAME, DuplicateKeyError, LocalConnectionError, LocalHTTPError, LocalOpenAIBackend,
    LocalTimeout, MAX_CONCURRENCY, extract_json_object, ReplyError,
    strip_thinking, urllib_transport)

PNG = b"\x89PNG\r\n\x1a\nfake"
GOOD = {"1": {"verdict": "keep", "why": "near marina"},
        "2": {"verdict": "drop", "why": "open water"}}


def _tile(tmp_path, name="t0"):
    d = tmp_path / name
    d.mkdir()
    (d / "chart.png").write_bytes(PNG)
    (d / "candidates.png").write_bytes(PNG + b"2")
    (d / "prompt.txt").write_text("PROMPT TEXT")
    (d / "context.json").write_text(json.dumps({
        "tile_id": name,
        "candidates": [{"n": 1, "kind": "dead_end_stub", "candidate_id": "s:1"},
                       {"n": 2, "kind": "dead_end_stub", "candidate_id": "s:2"}]}))
    return str(d)


def _resp(content, usage=None, finish="stop"):
    r = {"choices": [{"message": {"role": "assistant", "content": content},
                      "finish_reason": finish}]}
    if usage is not None:
        r["usage"] = usage
    return r


class FakeTransport:
    """Plays back `script`: each item is a response dict or an Exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers,
                           "timeout": timeout})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def _backend(transport, **kw):
    kw.setdefault("sleep", lambda s: None)
    return LocalOpenAIBackend(base_url="http://x:1/v1", transport=transport, **kw)


def _answer(backend, tile):
    return json.loads(backend.answer_tile(tile))


# ---- request shape ------------------------------------------------------

def test_request_shape(tmp_path):
    tile = _tile(tmp_path)
    t = FakeTransport(_resp(json.dumps(GOOD)))
    b = _backend(t, model="m-x", temperature=0.3, timeout=12)
    assert _answer(b, tile) == GOOD
    call = t.calls[0]
    assert call["url"] == "http://x:1/v1/chat/completions"
    assert call["timeout"] == (10.0, 12)  # (connect, read)
    p = call["payload"]
    assert p["model"] == "m-x" and p["temperature"] == 0.3 and p["stream"] is False
    assert p["top_p"] == 0.8 and p["max_tokens"] > 0
    assert p["chat_template_kwargs"] == {"enable_thinking": False}
    assert "response_format" not in p
    system, user = p["messages"]
    assert system == {"role": "system", "content": "PROMPT TEXT"}
    images = [c for c in user["content"] if c["type"] == "image_url"]
    assert len(images) == 2
    prefix = "data:image/png;base64,"
    urls = [i["image_url"]["url"] for i in images]
    assert all(u.startswith(prefix) for u in urls)
    assert base64.b64decode(urls[0][len(prefix):]) == PNG
    assert base64.b64decode(urls[1][len(prefix):]) == PNG + b"2"
    text = [c["text"] for c in user["content"] if c["type"] == "text"][-1]
    assert text.startswith("context.json:") and '"candidate_id": "s:1"' in text


def test_defaults_and_env(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_URL", "http://envhost:9/v1/")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "env-model")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sekret")
    b = LocalOpenAIBackend(transport=FakeTransport({}))
    assert b.url == "http://envhost:9/v1/chat/completions"
    assert b.model == "env-model"
    assert b.temperature == 0.2
    assert b.headers == {"Authorization": "Bearer sekret"}


def test_json_schema_and_thinking_flags(tmp_path):
    t = FakeTransport(_resp(json.dumps(GOOD)))
    b = _backend(t, json_schema=True, enable_thinking=True)
    b.answer_tile(_tile(tmp_path))
    p = t.calls[0]["payload"]
    assert p["chat_template_kwargs"] == {"enable_thinking": True}
    schema = p["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["1", "2"]
    assert schema["properties"]["1"]["properties"]["verdict"]["enum"] == \
        ["keep", "drop", "unsure"]


def test_api_key_over_cleartext_http_warns(capsys):
    LocalOpenAIBackend(base_url="http://192.168.10.111:8000/v1", api_key="sekret",
                       transport=FakeTransport({}))
    assert "cleartext" in capsys.readouterr().err


def test_api_key_over_https_does_not_warn(capsys):
    LocalOpenAIBackend(base_url="https://example.com/v1", api_key="sekret",
                       transport=FakeTransport({}))
    assert capsys.readouterr().err == ""


def test_api_key_over_loopback_http_does_not_warn(capsys):
    LocalOpenAIBackend(base_url="http://127.0.0.1:8000/v1", api_key="sekret",
                       transport=FakeTransport({}))
    assert capsys.readouterr().err == ""


def test_concurrency_is_clamped_to_server_slots():
    assert _backend(FakeTransport({}), max_concurrency=99).max_concurrency == MAX_CONCURRENCY
    assert _backend(FakeTransport({}), max_concurrency=0).max_concurrency == 1


# ---- reply parsing ------------------------------------------------------

def test_fenced_json_with_leading_prose(tmp_path):
    reply = "Sure, here is my answer:\n```json\n" + json.dumps(GOOD) + "\n```\nHope it helps"
    assert _answer(_backend(FakeTransport(_resp(reply))), _tile(tmp_path)) == GOOD


def test_bare_fence(tmp_path):
    reply = "```\n" + json.dumps(GOOD) + "\n```"
    assert _answer(_backend(FakeTransport(_resp(reply))), _tile(tmp_path)) == GOOD


def test_think_block_stripped_even_with_braces_inside(tmp_path):
    reply = '<think>maybe {"1": {"verdict": "drop"}} hmm</think>\n' + json.dumps(GOOD)
    assert _answer(_backend(FakeTransport(_resp(reply))), _tile(tmp_path)) == GOOD


def test_closing_think_tag_only(tmp_path):
    reply = 'reasoning {"1": 1}\n</think>\n' + json.dumps(GOOD)
    assert _answer(_backend(FakeTransport(_resp(reply))), _tile(tmp_path)) == GOOD


def test_strip_thinking_unclosed_block_leaves_no_answer():
    assert strip_thinking('<think>still going {"1": {}}').strip() == ""
    with pytest.raises(ReplyError):
        extract_json_object('<think>still going {"1": {}}')


def test_conflicting_objects_are_ambiguous():
    with pytest.raises(ReplyError):
        extract_json_object('{"1": {"verdict": "keep"}} then {"1": {"verdict": "drop"}}')
    # the same object twice is not ambiguous
    assert extract_json_object('{"a": 1} {"a": 1}') == {"a": 1}


# ---- safety: everything doubtful becomes unsure / keep -------------------

def test_garbage_output_becomes_all_unsure_with_error_record(tmp_path):
    tile = _tile(tmp_path)
    t = FakeTransport(_resp("I cannot help with that."))
    b = _backend(t)
    out = _answer(b, tile)
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    assert set(out) == {"1", "2"}
    assert len(t.calls) == 2  # one resample (parse_retries=1)
    rec = json.loads(open(os.path.join(tile, AUDIT_FILENAME)).read())
    assert rec["outcome"]["status"] == "fallback_unsure"
    assert "no JSON object" in rec["outcome"]["problems"]["_reply"]
    assert [a["raw_output"] for a in rec["attempts"]] == ["I cannot help with that."] * 2


def test_truncated_reply_is_not_trusted_even_if_it_parses(tmp_path):
    t = FakeTransport(_resp(json.dumps(GOOD), finish="length"))
    out = _answer(_backend(t), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}


def test_resample_recovers_after_garbage(tmp_path):
    t = FakeTransport(_resp("garbage"), _resp(json.dumps(GOOD)))
    assert _answer(_backend(t), _tile(tmp_path)) == GOOD
    assert len(t.calls) == 2


def test_invalid_verdict_value_only_downgrades_that_candidate(tmp_path):
    tile = _tile(tmp_path)
    bad = {"1": {"verdict": "delete", "why": "x"}, "2": {"verdict": "drop", "why": "y"}}
    out = _answer(_backend(FakeTransport(_resp(json.dumps(bad)))), tile)
    assert out["1"]["verdict"] == "unsure" and "invalid verdict" in out["1"]["why"]
    assert out["2"] == {"verdict": "drop", "why": "y"}
    rec = json.loads(open(os.path.join(tile, AUDIT_FILENAME)).read())
    assert set(rec["outcome"]["problems"]) == {"1"}


@pytest.mark.parametrize("entry", ["drop", None, ["drop"], {"why": "no verdict"},
                                   {"verdict": "Drop"}, {"verdict": ["drop"]}])
def test_malformed_entries_never_become_drops(tmp_path, entry):
    bad = {"1": entry, "2": {"verdict": "keep", "why": ""}}
    out = _answer(_backend(FakeTransport(_resp(json.dumps(bad)))), _tile(tmp_path))
    assert out["1"]["verdict"] == "unsure"
    assert out["2"]["verdict"] == "keep"


def test_missing_and_invented_numbers(tmp_path):
    tile = _tile(tmp_path)
    reply = {"1": {"verdict": "drop", "why": "a"}, "99": {"verdict": "drop", "why": "b"}}
    out = _answer(_backend(FakeTransport(_resp(json.dumps(reply)))), tile)
    assert set(out) == {"1", "2"}  # 99 discarded (would fail the runner's own check)
    assert out["1"]["verdict"] == "drop"
    assert out["2"]["verdict"] == "unsure" and "missing" in out["2"]["why"]
    rec = json.loads(open(os.path.join(tile, AUDIT_FILENAME)).read())
    assert "99" in rec["outcome"]["problems"]["_extra"]


def test_malformed_api_response_is_unsure(tmp_path):
    out = _answer(_backend(FakeTransport({"error": "weird"})), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}


def test_answer_passes_the_runners_own_validation(tmp_path):
    tile = _tile(tmp_path)
    b = _backend(FakeTransport(_resp("<think>x</think>```json\n" + json.dumps(GOOD) + "```")))
    verdicts = runner.run_tile(b, tile)
    assert verdicts == GOOD


# ---- timeouts, retries ---------------------------------------------------

def test_timeout_is_a_backend_error_not_a_drop(tmp_path):
    tile = _tile(tmp_path)
    t = FakeTransport(LocalTimeout("read timed out"))
    b = _backend(t)
    with pytest.raises(BackendError, match="timeout"):
        b.answer_tile(tile)
    assert len(t.calls) == 1  # not retried in the backend
    # through the runner: tile is unanswered -> no answer.json, so no ops
    assert runner.run_tile(_backend(FakeTransport(LocalTimeout("x"))), tile) is None
    assert os.path.exists(os.path.join(tile, "error.txt"))
    assert not os.path.exists(os.path.join(tile, "answer.json"))
    stats = runner.run_all(_backend(FakeTransport(LocalTimeout("x"))), [tile])
    assert stats.unanswered == 1 and stats.answered == 0
    assert runner.answers_to_ops([tile], author="ai:test") == []
    rec = [json.loads(line) for line in open(os.path.join(tile, AUDIT_FILENAME))]
    assert rec[0]["outcome"]["status"] == "backend_error"


def test_connection_error_retried_with_backoff_then_succeeds(tmp_path):
    sleeps = []
    t = FakeTransport(LocalConnectionError("refused"), LocalHTTPError(503, "loading"),
                      _resp(json.dumps(GOOD)))
    b = _backend(t, sleep=sleeps.append, backoff_s=1.0)
    assert _answer(b, _tile(tmp_path)) == GOOD
    assert len(t.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_retries_are_bounded(tmp_path):
    t = FakeTransport(LocalConnectionError("refused"))
    with pytest.raises(BackendError, match="unavailable after 3"):
        _backend(t, max_retries=2).answer_tile(_tile(tmp_path))
    assert len(t.calls) == 3


def test_client_error_is_not_retried(tmp_path):
    t = FakeTransport(LocalHTTPError(400, "context overflow"))
    with pytest.raises(BackendError, match="400"):
        _backend(t).answer_tile(_tile(tmp_path))
    assert len(t.calls) == 1


# ---- audit artefacts -----------------------------------------------------

def test_audit_records_raw_output_prompt_version_latency_and_usage(tmp_path):
    tile = _tile(tmp_path)
    usage = {"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960}
    raw = "```json\n" + json.dumps(GOOD) + "\n```"
    b = _backend(FakeTransport(_resp(raw, usage=usage)), prompt_label="v2-tighter")
    b.answer_tile(tile)
    b.answer_tile(tile)  # a second round appends, never overwrites
    lines = open(os.path.join(tile, AUDIT_FILENAME)).read().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["prompt_label"] == "v2-tighter" and len(rec["prompt_sha256"]) == 12
    assert rec["model"] and rec["params"]["temperature"] == 0.2
    att = rec["attempts"][0]
    assert att["raw_output"] == raw and att["usage"] == usage
    assert att["latency_s"] >= 0 and att["finish_reason"] == "stop"
    assert rec["outcome"]["verdicts"] == GOOD
    assert b.stats["prompt_tokens"] == 1800 and b.stats["completion_tokens"] == 120
    assert "call(s)" in b.summary()


def test_prompt_hash_changes_with_prompt(tmp_path):
    tile = _tile(tmp_path)
    b = _backend(FakeTransport(_resp(json.dumps(GOOD))))
    b.answer_tile(tile)
    with open(os.path.join(tile, "prompt.txt"), "w") as fh:
        fh.write("PROMPT TEXT v2")
    b.answer_tile(tile)
    recs = [json.loads(x) for x in open(os.path.join(tile, AUDIT_FILENAME))]
    assert recs[0]["prompt_sha256"] != recs[1]["prompt_sha256"]


def test_audit_can_be_disabled(tmp_path):
    tile = _tile(tmp_path)
    _backend(FakeTransport(_resp(json.dumps(GOOD))), audit=False).answer_tile(tile)
    assert not os.path.exists(os.path.join(tile, AUDIT_FILENAME))


def test_reasoning_content_is_kept_in_audit_only(tmp_path):
    tile = _tile(tmp_path)
    r = _resp(json.dumps(GOOD))
    r["choices"][0]["message"]["reasoning_content"] = "let me think"
    out = _answer(_backend(FakeTransport(r)), tile)
    assert out == GOOD
    rec = json.loads(open(os.path.join(tile, AUDIT_FILENAME)).read())
    assert rec["attempts"][0]["reasoning_content"] == "let me think"


# ---- reply parsing: ambiguity, duplicates, arrays ------------------------

DUP = '{"1": {"verdict": "keep", "why": "a"}, "1": {"verdict": "drop", "why": "b"}, ' \
      '"2": {"verdict": "keep", "why": ""}}'


def test_duplicate_top_level_key_is_unusable_not_last_wins():
    with pytest.raises(DuplicateKeyError):
        extract_json_object(DUP)
    with pytest.raises(ReplyError):  # also when fenced / after prose
        extract_json_object("Here:\n```json\n" + DUP + "\n```")


def test_duplicate_nested_key_is_unusable():
    with pytest.raises(DuplicateKeyError):
        extract_json_object('{"1": {"verdict": "keep", "verdict": "drop"}}')


def test_duplicate_key_reply_falls_back_to_all_unsure_after_one_resample(tmp_path):
    tile = _tile(tmp_path)
    t = FakeTransport(_resp(DUP))
    b = _backend(t)
    out = _answer(b, tile)
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    assert len(t.calls) == 2
    assert b.consume_degraded(tile) is True


def test_duplicate_key_reply_can_recover_on_the_resample(tmp_path):
    t = FakeTransport(_resp(DUP), _resp(json.dumps(GOOD)))
    assert _answer(_backend(t), _tile(tmp_path)) == GOOD


def test_reply_wrapped_with_no_candidate_keys_is_retried_not_trusted(tmp_path):
    wrapped = json.dumps({"answer": "GOOD"})
    tile = _tile(tmp_path)
    t = FakeTransport(_resp(wrapped))
    b = _backend(t)
    out = _answer(b, tile)
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    assert len(t.calls) == 2
    assert b.consume_degraded(tile) is True


def test_reply_wrapped_with_no_candidate_keys_can_recover_on_the_resample(tmp_path):
    wrapped = json.dumps({"answer": "GOOD"})
    t = FakeTransport(_resp(wrapped), _resp(json.dumps(GOOD)))
    assert _answer(_backend(t), _tile(tmp_path)) == GOOD


def test_top_level_array_is_rejected_not_unwrapped(tmp_path):
    with pytest.raises(ReplyError, match="array"):
        extract_json_object(json.dumps([GOOD]))
    out = _answer(_backend(FakeTransport(_resp(json.dumps([GOOD])))), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}


def test_harmless_bracket_in_prose_is_still_fine():
    assert extract_json_object("see [1] and [note]: " + json.dumps(GOOD)) == GOOD


def test_unfenced_and_fenced_objects_that_disagree_are_ambiguous(tmp_path):
    drop = {"1": {"verdict": "drop", "why": ""}, "2": {"verdict": "drop", "why": ""}}
    reply = json.dumps(drop) + "\n```json\n" + json.dumps(GOOD) + "\n```"
    with pytest.raises(ReplyError, match="ambiguous"):
        extract_json_object(reply)
    out = _answer(_backend(FakeTransport(_resp(reply))), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}


def test_unfenced_and_fenced_copies_of_the_same_object_are_fine():
    reply = json.dumps(GOOD) + "\n```json\n" + json.dumps(GOOD) + "\n```"
    assert extract_json_object(reply) == GOOD


# ---- reply shape: content types, finish_reason, usage ---------------------

def test_list_of_text_parts_is_joined(tmp_path):
    text = json.dumps(GOOD)
    parts = [{"type": "text", "text": text[:10]}, {"type": "text", "text": text[10:]}]
    assert _answer(_backend(FakeTransport(_resp(parts))), _tile(tmp_path)) == GOOD


@pytest.mark.parametrize("content", [
    [{"type": "image_url", "image_url": {}}], [{"type": "text"}], ["x"],
    {"1": {"verdict": "drop"}}, 7, 1.5, True, None, []])
def test_odd_content_is_unusable_never_a_crash_or_a_drop(tmp_path, content):
    tile = _tile(tmp_path)
    out = _answer(_backend(FakeTransport(_resp(content))), tile)
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    rec = json.loads(open(os.path.join(tile, AUDIT_FILENAME)).read())
    assert rec["outcome"]["status"] == "fallback_unsure"


def test_content_filter_is_not_trusted_even_if_it_parses(tmp_path):
    t = FakeTransport(_resp(json.dumps(GOOD), finish="content_filter"))
    out = _answer(_backend(t), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    assert len(t.calls) == 2


@pytest.mark.parametrize("usage", [{"prompt_tokens": "lots", "completion_tokens": [1]},
                                   "oops", [1, 2], {"prompt_tokens": None},
                                   {"prompt_tokens": float("nan")}, 5])
def test_non_numeric_usage_does_not_crash(tmp_path, usage):
    b = _backend(FakeTransport(_resp(json.dumps(GOOD), usage=usage)))
    assert _answer(b, _tile(tmp_path)) == GOOD
    assert b.stats["prompt_tokens"] == 0 and b.stats["completion_tokens"] == 0


# ---- timeouts, retries ---------------------------------------------------

def test_http_client_errors_from_the_transport_are_retried_as_connection_errors(tmp_path):
    t = FakeTransport(LocalConnectionError("IncompleteRead"), _resp(json.dumps(GOOD)))
    assert _answer(_backend(t), _tile(tmp_path)) == GOOD
    assert len(t.calls) == 2


def test_connect_timeout_is_configurable_and_separate_from_read_timeout(tmp_path):
    t = FakeTransport(_resp(json.dumps(GOOD)))
    _backend(t, connect_timeout=3, timeout=99).answer_tile(_tile(tmp_path))
    assert t.calls[0]["timeout"] == (3, 99)


# ---- degraded marker -----------------------------------------------------

def test_consume_degraded_only_for_fallback_and_only_once(tmp_path):
    good, bad = _tile(tmp_path, "good"), _tile(tmp_path, "bad")
    b = _backend(FakeTransport(_resp(json.dumps(GOOD))))
    b.answer_tile(good)
    assert b.consume_degraded(good) is False
    b2 = _backend(FakeTransport(_resp("no json here")))
    b2.answer_tile(bad)
    assert b2.consume_degraded(bad) is True
    assert b2.consume_degraded(bad) is False


# ---- real transport: local 127.0.0.1 sockets / fake connection, no DNS ----

class _FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)


class _FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _fake_conn(monkeypatch, *, connect=None, request=None, response=None, log=None):
    import http.client

    class Conn:
        def __init__(self, host, port=None, timeout=None):
            self.host, self.port, self.timeout = host, port, timeout
            self.sock = _FakeSock()
            if log is not None:
                log.append(self)

        def connect(self):
            if connect:
                raise connect

        def request(self, method, path, body=None, headers=None):
            self.sent = (method, path, body, headers)
            if request:
                raise request

        def getresponse(self):
            if isinstance(response, Exception):
                raise response
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(http.client, "HTTPConnection", Conn)
    monkeypatch.setattr(http.client, "HTTPSConnection", Conn)


def test_urllib_transport_success_and_timeouts_split(monkeypatch):
    log = []
    _fake_conn(monkeypatch, response=_FakeResponse(200, b'{"ok": 1}'), log=log)
    assert urllib_transport("http://h:8/v1/chat/completions?x=1", {"a": 1},
                            {"Authorization": "B"}, (4, 77)) == {"ok": 1}
    c = log[0]
    assert (c.host, c.port, c.timeout) == ("h", 8, 4)        # connect timeout
    assert c.sock.timeouts == [77]                           # then read timeout
    assert c.sent[1] == "/v1/chat/completions?x=1" and c.sent[3]["Authorization"] == "B"
    assert c.closed


def test_urllib_transport_maps_errors(monkeypatch):
    import http.client
    import socket

    cases = [
        (dict(connect=socket.timeout("t")), LocalConnectionError),   # connect timeout: retryable
        (dict(connect=ConnectionRefusedError("no")), LocalConnectionError),
        (dict(connect=socket.gaierror(-5, "no address")), LocalConnectionError),
        (dict(request=BrokenPipeError("pipe")), LocalConnectionError),
        (dict(request=socket.timeout("t")), LocalTimeout),
        (dict(response=socket.timeout("t")), LocalTimeout),
        (dict(response=http.client.RemoteDisconnected("closed")), LocalConnectionError),
        (dict(response=http.client.BadStatusLine("junk")), LocalConnectionError),
        (dict(response=_FakeResponse(200, http.client.IncompleteRead(b"ab", 5))),
         LocalConnectionError),
        (dict(response=_FakeResponse(200, socket.timeout("slow body"))), LocalTimeout),
    ]
    for kw, expected in cases:
        _fake_conn(monkeypatch, **kw)
        with pytest.raises(expected):
            urllib_transport("http://x", {}, {}, 1)


def test_urllib_transport_http_status_and_bad_json(monkeypatch):
    _fake_conn(monkeypatch, response=_FakeResponse(503, b"loading"))
    with pytest.raises(LocalHTTPError) as ei:
        urllib_transport("http://x", {}, {}, 1)
    assert ei.value.status == 503
    _fake_conn(monkeypatch, response=_FakeResponse(200, b"<html>"))
    with pytest.raises(LocalHTTPError, match="not JSON"):
        urllib_transport("http://x", {}, {}, 1)


def _serve_once(handler):
    """A one-shot TCP server on 127.0.0.1 (no DNS); returns (port, thread)."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            handler(conn)
        finally:
            conn.close()
            srv.close()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return srv.getsockname()[1], th


def test_urllib_transport_over_a_real_local_socket():
    def ok(conn):
        conn.recv(65536)
        body = b'{"hello": "world"}'
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    port, th = _serve_once(ok)
    assert urllib_transport(f"http://127.0.0.1:{port}/v1", {}, {}, (2, 2)) == {"hello": "world"}
    th.join(2)

    port, th = _serve_once(lambda conn: conn.recv(65536))  # hangs up without answering
    with pytest.raises(LocalConnectionError):               # RemoteDisconnected
        urllib_transport(f"http://127.0.0.1:{port}/v1", {}, {}, (2, 2))
    th.join(2)

    def truncated(conn):
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{")
    port, th = _serve_once(truncated)
    with pytest.raises(LocalConnectionError):               # IncompleteRead
        urllib_transport(f"http://127.0.0.1:{port}/v1", {}, {}, (2, 2))
    th.join(2)

    import time

    def silent(conn):
        conn.recv(65536)
        time.sleep(1.0)
    port, th = _serve_once(silent)
    with pytest.raises(LocalTimeout):                       # connected, no reply
        urllib_transport(f"http://127.0.0.1:{port}/v1", {}, {}, (2, 0.2))
    th.join(3)


# ---- malformed / unbalanced replies are never salvaged --------------------

_INNER = '"1": {"verdict": "drop", "why": "a"}, "2": {"verdict": "drop", "why": "b"}'
UNBALANCED = {
    "outer brace missing": '{"answer": {' + _INNER + '}',
    "array not closed": '[{' + _INNER + '}',
    "unquoted wrapper key": '{answer: {' + _INNER + '}}',
    "wrapper with missing colon": '{"answer" {' + _INNER + '}}',
    "truncated mid-entry, finish=stop": '{"1": {"verdict": "drop", "why": "a"}, "2": {"verd',
    "junk object then wrapper": '{"a": {}, "answer" {' + _INNER + '}}',
}


@pytest.mark.parametrize("reply", list(UNBALANCED.values()), ids=list(UNBALANCED))
def test_unbalanced_reply_is_unusable_not_salvaged(tmp_path, reply):
    with pytest.raises(ReplyError):
        extract_json_object(reply)
    t = FakeTransport(_resp(reply))
    out = _answer(_backend(t), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    assert len(t.calls) == 2  # resampled once, then all-unsure


def test_unclosed_think_is_unusable_even_after_a_complete_answer(tmp_path):
    """Conservative choice: a reply that answers and then opens a `<think>`
    it never closes was second-guessing itself; the complete-looking object
    before it is not trusted."""
    reply = json.dumps(GOOD) + "<think>hmm, actually"
    with pytest.raises(ReplyError, match="unclosed"):
        extract_json_object(reply)
    out = _answer(_backend(FakeTransport(_resp(reply))), _tile(tmp_path))
    assert {v["verdict"] for v in out.values()} == {"unsure"}
    # a closed (even empty) think block before the answer is normal
    assert extract_json_object("<think>\n\n</think>\n" + json.dumps(GOOD)) == GOOD


def test_prose_brackets_around_a_valid_answer_are_still_tolerated():
    reply = 'ok {note} and [see] then ' + json.dumps(GOOD) + ' (a {b} c)'
    assert extract_json_object(reply) == GOOD


# ---- URL validation -------------------------------------------------------

@pytest.mark.parametrize("url", [
    "192.168.10.111:8000/v1", "localhost:8000/v1", "ftp://h/v1", "file:///etc/x",
    "http://h:abc/v1", "http://h:99999/v1", "http:///v1", "http://", "//h/v1"])
def test_bad_urls_fail_at_construction(url):
    with pytest.raises(ValueError):
        LocalOpenAIBackend(base_url=url, transport=FakeTransport({}))


def test_bad_env_url_fails_at_construction(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_URL", "10.0.0.1:8000")
    with pytest.raises(ValueError, match="http"):
        LocalOpenAIBackend(transport=FakeTransport({}))


def test_scheme_error_says_what_is_missing():
    with pytest.raises(ValueError, match="scheme is missing"):
        LocalOpenAIBackend(base_url="192.168.10.111:8000/v1")


@pytest.mark.parametrize("url", ["https://h:1/v1", "http://[::1]:8000/v1",
                                 "http://h/v1/chat/completions", " http://h:8/v1/ "])
def test_good_urls_are_accepted(url):
    assert LocalOpenAIBackend(base_url=url, transport=FakeTransport({})).url.endswith(
        "/chat/completions")


def test_transport_never_raises_raw_for_url_or_connection_construction(monkeypatch):
    import http.client
    for bad in ("nope", "http://h:abc/x", "ftp://h/x"):
        with pytest.raises(LocalHTTPError):
            urllib_transport(bad, {}, {}, 1)

    class Boom:
        def __init__(self, *a, **k):
            raise http.client.InvalidURL("bad port")
    monkeypatch.setattr(http.client, "HTTPConnection", Boom)
    with pytest.raises(LocalConnectionError):
        urllib_transport("http://h:1/x", {}, {}, 1)
