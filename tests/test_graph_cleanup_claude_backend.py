"""Unit tests for the parts of `graph_cleanup/backends/claude.py` that don't
need a network call or an API key: fence-stripping the model's JSON answer,
and the request-shape helpers. Never hits the Anthropic API -- see the
module's own docstring for why a live call couldn't be tested in this
environment (no credentials available)."""
import base64
import json

import pytest

anthropic = pytest.importorskip("anthropic")

from graph_cleanup.backends.claude import _image_block, _system_block, parse_answer


def test_parse_answer_plain_json():
    assert parse_answer('{"1": {"verdict": "keep", "why": ""}}') == \
        {"1": {"verdict": "keep", "why": ""}}


def test_parse_answer_strips_json_fence():
    raw = '```json\n{"1": {"verdict": "drop", "why": "x"}}\n```'
    assert parse_answer(raw) == {"1": {"verdict": "drop", "why": "x"}}


def test_parse_answer_strips_bare_fence():
    raw = '```\n{"1": {"verdict": "unsure", "why": ""}}\n```'
    assert parse_answer(raw) == {"1": {"verdict": "unsure", "why": ""}}


def test_parse_answer_rejects_garbage():
    with pytest.raises(json.JSONDecodeError):
        parse_answer("not json at all")


def test_image_block_encodes_base64_png(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    path = tmp_path / "x.png"
    path.write_bytes(png_bytes)
    block = _image_block(str(path))
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(block["source"]["data"]) == png_bytes


def test_system_block_carries_cache_control():
    block = _system_block("hello reviewer")
    assert block[0]["text"] == "hello reviewer"
    assert block[0]["cache_control"] == {"type": "ephemeral"}
