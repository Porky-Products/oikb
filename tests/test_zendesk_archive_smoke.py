"""Offline regression tests for scripts/zendesk_archive_smoke.py.

These cover the PR #49 review finding: a single GET that answers 200
with a malformed payload is indeterminate evidence — the run must abort
(exit 1) instead of letting the final verdict report the ID as
deleted/nonexistent (exit 3).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "zendesk_archive_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("zendesk_archive_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def smoke(monkeypatch: pytest.MonkeyPatch):
    module = _load_module()
    monkeypatch.setenv("ZENDESKTICKET_USER", "ops@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "token")
    return module


def _ticket(ticket_id: int) -> dict:
    return {"id": ticket_id, "subject": "s", "requester_id": 1}


def _stub_get(single_payload):
    """Fake _get: show_many and users/show_many answer well-formed; the
    per-ID single fetch answers with `single_payload`."""

    def fake_get(base_url, path_qs, auth, timeout):
        if path_qs.startswith("/tickets/show_many"):
            return 200, {"count": 1, "tickets": [_ticket(45748)]}, "{}"
        if path_qs.startswith("/users/show_many"):
            return 200, {"count": 1, "users": [{"id": 1, "email": "a@b.c"}]}, "{}"
        return 200, single_payload, "{}"

    return fake_get


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "payload"},
        {"ticket": "not-an-object"},
        ["not", "an", "object"],
    ],
)
def test_malformed_single_200_aborts_before_false_verdict(
    smoke, monkeypatch, capsys, payload
) -> None:
    monkeypatch.setattr(smoke, "_get", _stub_get(payload))
    monkeypatch.setattr("sys.argv", ["zendesk_archive_smoke.py", "45748"])
    with pytest.raises(SystemExit) as exc:
        smoke.main()
    assert exc.value.code == 1
    assert "malformed payload" in capsys.readouterr().err


def test_well_formed_single_200_still_recorded(
    smoke, monkeypatch, capsys
) -> None:
    """A well-formed single 200 is recorded normally: no abort, and the
    all-IDs-served verdict still exits 0."""
    monkeypatch.setattr(smoke, "_get", _stub_get({"ticket": _ticket(45748)}))
    monkeypatch.setattr("sys.argv", ["zendesk_archive_smoke.py", "45748"])
    with pytest.raises(SystemExit) as exc:
        smoke.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "fatal" not in captured.err
    assert "single GET: FOUND" in captured.out
