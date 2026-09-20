"""Offline regression tests for scripts/zendesk_llm_denylist_scan.py.

Run with: uv run python -m pytest tests/test_zendesk_llm_denylist_scan.py -q

These cover the Copilot PR-#40 review findings:
- stop ID 0 is rejected (must be strictly positive)
- existing denylist entries are parsed with the connector's strict
  ASCII-digit rule (no '+45748', no fullwidth digits)
- comments pagination (next_page) forces unsure, never auto-allow
- truncated descriptions skip the LLM call entirely
- LLM request failures propagate (abort) instead of consuming the range
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "zendesk_llm_denylist_scan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("zendesk_llm_denylist_scan", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def scan():
    return _load_module()


class FakeZendeskClient:
    """Stub mirroring ZendeskClient's fetch/show methods."""

    def __init__(self, tickets: dict[int, dict], comments: dict[int, tuple[list, bool]]):
        self._tickets = tickets
        self._comments = comments

    def show_many_tickets(self, ids):
        return {i: self._tickets[i] for i in ids if i in self._tickets}

    def show_many_users(self, ids):
        return {i: {"id": i, "email": f"u{i}@example.com"} for i in ids}

    def fetch_ticket_comments(self, ticket_id):
        return self._comments.get(ticket_id, ([], False))


class FakeLLMClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def classify(self, payload: str) -> str:
        self.calls.append(payload)
        if not self._responses:
            raise RuntimeError("LLM request failed after 4 attempts: HTTP 401")
        return self._responses.pop(0)


def _ticket(ticket_id: int, description: str = "body") -> dict:
    return {
        "id": ticket_id,
        "subject": f"ticket {ticket_id}",
        "description": description,
        "requester_id": 77,
    }


def test_parse_verdict_accepts_only_known_verdicts(scan):
    ok = json.dumps({"ticket_id": 5, "verdict": "deny", "reason": "x"})
    assert scan._parse_verdict(ok, 5) == ("deny", "")
    bad_id = json.dumps({"ticket_id": 6, "verdict": "deny"})
    assert scan._parse_verdict(bad_id, 5)[0] == "unsure"
    unknown = json.dumps({"ticket_id": 5, "verdict": "maybe"})
    assert scan._parse_verdict(unknown, 5)[0] == "unsure"


def test_format_ticket_block_reports_truncation(scan):
    ticket = _ticket(1, description="x" * 100)
    block, names, truncated = scan._format_ticket_block(
        ticket, [{"attachments": [{"file_name": "credit_app.pdf"}]}], "a@b.c", desc_cap=10
    )
    assert truncated is True
    assert names == ["credit_app.pdf"]
    assert "[…description truncated…]" in block


def _run_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan, *, stop_id: str, llm, zendesk, reset: bool = False):
    """Drive main() with fakes wired in via monkeypatch; returns captured SystemExit/code."""
    env = {
        "ZENDESKTICKET_SUBDOMAIN": "x",
        "ZENDESKTICKET_USER": "x",
        "ZENDESKTICKET_TOKEN": "x",
        "OPENAI_BASE_URL": "http://llm",
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "LLM_SCAN_STOP_TICKET_ID": stop_id,
        "LLM_SCAN_PROMPT_FILE": str(tmp_path / "prompt.md"),
        "LLM_SCAN_DENYLIST_FILE": str(tmp_path / "deny.txt"),
        "LLM_SCAN_REVIEW_FILE": str(tmp_path / "review.txt"),
        "LLM_SCAN_STATE_FILE": str(tmp_path / "state.json"),
        "LLM_SCAN_MAX_PER_RUN": "0",
    }
    monkeypatch.setattr(scan, "REQUIRED_ENV", list(env.keys()))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "prompt.md").write_text("deny credit applications")
    monkeypatch.setattr(scan, "ZendeskClient", lambda **kw: zendesk)
    monkeypatch.setattr(scan, "LLMClient", lambda **kw: llm)
    argv = ["prog"]
    if reset:
        argv.append("--reset")
    monkeypatch.setattr(scan.sys, "argv", argv)
    scan.main()


def test_stop_id_zero_rejected(scan, tmp_path, monkeypatch):
    env = {
        "ZENDESKTICKET_SUBDOMAIN": "x",
        "ZENDESKTICKET_USER": "x",
        "ZENDESKTICKET_TOKEN": "x",
        "OPENAI_BASE_URL": "http://llm",
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "LLM_SCAN_STOP_TICKET_ID": "0",
        "LLM_SCAN_PROMPT_FILE": str(tmp_path / "prompt.md"),
        "LLM_SCAN_DENYLIST_FILE": str(tmp_path / "deny.txt"),
        "LLM_SCAN_REVIEW_FILE": str(tmp_path / "review.txt"),
        "LLM_SCAN_STATE_FILE": str(tmp_path / "state.json"),
    }
    monkeypatch.setattr(scan, "REQUIRED_ENV", list(env.keys()))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "prompt.md").write_text("deny credit applications")
    monkeypatch.setattr(scan.sys, "argv", ["prog"])
    with pytest.raises(SystemExit) as excinfo:
        scan.main()
    assert excinfo.value.code == 1  # this script's _die exit code (config error)


def test_existing_denylist_strict_ascii_parse(scan, tmp_path, monkeypatch):
    denylist = tmp_path / "deny.txt"
    # '+45748' previously slipped past int() and produced an entry the
    # connector would reject
    denylist.write_text("+45748\n")
    env = {
        "ZENDESKTICKET_SUBDOMAIN": "x",
        "ZENDESKTICKET_USER": "x",
        "ZENDESKTICKET_TOKEN": "x",
        "OPENAI_BASE_URL": "http://llm",
        "OPENAI_API_KEY": "k",
        "OPENAI_MODEL": "m",
        "LLM_SCAN_STOP_TICKET_ID": "5",
        "LLM_SCAN_PROMPT_FILE": str(tmp_path / "prompt.md"),
        "LLM_SCAN_DENYLIST_FILE": str(denylist),
        "LLM_SCAN_REVIEW_FILE": str(tmp_path / "review.txt"),
        "LLM_SCAN_STATE_FILE": str(tmp_path / "state.json"),
    }
    monkeypatch.setattr(scan, "REQUIRED_ENV", list(env.keys()))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "prompt.md").write_text("deny credit applications")
    monkeypatch.setattr(scan.sys, "argv", ["prog"])
    with pytest.raises(SystemExit) as excinfo:
        scan.main()
    assert excinfo.value.code == 1  # _die exit code (malformed denylist entry)


def test_paginated_comments_force_unsure_without_llm(scan, tmp_path, monkeypatch):
    # Ticket 1: 250 comments -> Zendesk paginates; comments beyond page 1
    # are NOT fetched; classification must skip the LLM and record unsure.
    comments = [{"id": i, "body": "c", "attachments": []} for i in range(250)]
    zendesk = FakeZendeskClient(
        tickets={1: _ticket(1)},
        comments={1: (comments, True)},  # (first_page, more_pages=True)
    )
    llm = FakeLLMClient(responses=[])
    _run_scan(tmp_path, monkeypatch, scan, stop_id="1", llm=llm, zendesk=zendesk)
    review = (tmp_path / "review.txt").read_text()
    assert "1  # unsure: comments paginated" in review
    assert llm.calls == []  # no LLM request made on partial evidence
    assert not (tmp_path / "deny.txt").exists()


def test_truncated_description_skips_llm(scan, tmp_path, monkeypatch):
    # desc longer than cap -> LLM never called, forced unsure
    monkeypatch.setenv("LLM_SCAN_DESC_CHAR_CAP", "10")
    zendesk = FakeZendeskClient(
        tickets={1: _ticket(1, description="x" * 100)},
        comments={1: ([], False)},
    )
    llm = FakeLLMClient(responses=[])
    _run_scan(tmp_path, monkeypatch, scan, stop_id="1", llm=llm, zendesk=zendesk)
    review = (tmp_path / "review.txt").read_text()
    assert "description truncated" in review
    assert llm.calls == []


def test_llm_request_failure_aborts_not_consumed(scan, tmp_path, monkeypatch):
    # LLM auth failure must abort the whole run (SystemExit via the outer
    # handler) instead of marking tickets unsure and advancing
    zendesk = FakeZendeskClient(
        tickets={1: _ticket(1), 2: _ticket(2)},
        comments={1: ([], False), 2: ([], False)},
    )
    llm = FakeLLMClient(responses=[])  # first classify() raises LLMRequestError
    env_backup = dict(os.environ)
    try:
        with pytest.raises(SystemExit) as excinfo:
            _run_scan(tmp_path, monkeypatch, scan, stop_id="2", llm=llm, zendesk=zendesk)
        assert excinfo.value.code == 1  # infra-abort exit code (not per-ticket failure)
        # cursor must NOT have advanced past ticket 1
        state = json.loads((tmp_path / "state.json").read_text())
        # state is saved by the abort handler with next_id ...
        assert state["next_id"] <= 1
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


class MalformedLLMClient:
    """Returns 2xx responses whose bodies are unusable (empty/malformed choices)."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def classify(self, payload: str) -> str:
        self.calls += 1
        if not self._payloads:
            raise scan_mod.RuntimeError("should not be called again")
        body = self._payloads.pop(0)
        raise scan_mod.MalformedCompletionError(body)


scan_mod = None


def test_malformed_completion_yields_per_ticket_unsure_not_abort(scan, tmp_path, monkeypatch):
    """R1-F-bd4a1e9a: a 2xx response with empty/malformed choices must land
    that ONE ticket in the review file as unsure and let the scan continue,
    not abort the run like a transport failure."""
    global scan_mod
    scan_mod = scan
    zendesk = FakeZendeskClient(
        tickets={1: _ticket(1), 2: _ticket(2)},
        comments={1: ([], False), 2: ([], False)},
    )
    llm = MalformedLLMClient(["empty choices", "malformed completion payload: bad"])
    _run_scan(tmp_path, monkeypatch, scan, stop_id="2", llm=llm, zendesk=zendesk)
    review = (tmp_path / "review.txt").read_text()
    assert "1  # unsure: malformed completion: empty choices" in review
    assert "2  # unsure: malformed completion: malformed completion payload: bad" in review
    assert llm.calls == 2  # both tickets attempted; neither aborted the run
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["next_id"] == 3  # full segment completed normally


def test_env_int_rejects_negative_and_nonnumeric(scan, monkeypatch):
    monkeypatch.setenv("LLM_SCAN_MAX_PER_RUN", "-1")
    with pytest.raises(SystemExit) as excinfo:
        scan._env_int("LLM_SCAN_MAX_PER_RUN", 1000, minimum=0)
    assert excinfo.value.code == 1

    monkeypatch.setenv("LLM_SCAN_MAX_PER_RUN", "abc")
    with pytest.raises(SystemExit) as excinfo:
        scan._env_int("LLM_SCAN_MAX_PER_RUN", 1000, minimum=0)
    assert excinfo.value.code == 1

    monkeypatch.setenv("LLM_SCAN_MAX_PER_RUN", "")
    assert scan._env_int("LLM_SCAN_MAX_PER_RUN", 1000, minimum=0) == 1000

    monkeypatch.setenv("LLM_SCAN_MAX_PER_RUN", "5")
    assert scan._env_int("LLM_SCAN_MAX_PER_RUN", 1000, minimum=0) == 5


def test_env_float_rejects_nan_inf_negative(scan, monkeypatch):
    for bad in ("nan", "inf", "-inf", "-1", "0", "abc"):
        monkeypatch.setenv("LLM_SCAN_TIMEOUT_SECONDS", bad)
        with pytest.raises(SystemExit) as excinfo:
            scan._env_float("LLM_SCAN_TIMEOUT_SECONDS", 120.0, minimum=0.1)
        assert excinfo.value.code == 1

    monkeypatch.setenv("LLM_SCAN_TIMEOUT_SECONDS", "")
    assert scan._env_float("LLM_SCAN_TIMEOUT_SECONDS", 120.0, minimum=0.1) == 120.0

    monkeypatch.setenv("LLM_SCAN_TIMEOUT_SECONDS", "30.5")
    assert scan._env_float("LLM_SCAN_TIMEOUT_SECONDS", 120.0, minimum=0.1) == 30.5
