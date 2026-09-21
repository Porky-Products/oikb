"""Offline regression tests for scripts/verify_zendesk_denylist.py.

Covers the exit-code contract: 0=CLEAN, 1=LEAKED, 2=ERROR. In particular,
operator configuration errors (bad VERIFY_TIMEOUT_SECONDS: non-numeric,
nan, inf, non-positive) must exit 2 — never 1, which CI gates would read
as a confirmed leak (R1-F-54e37363). Pagination completeness is judged on
unique entry ids: duplicate/overlapping/shifting pages can never fake a
CLEAN verdict (R4 findings).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import os

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_zendesk_denylist.py"


@pytest.fixture()
def verify():
    spec = importlib.util.spec_from_file_location("verify_zendesk_denylist", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def denylist(tmp_path: Path) -> Path:
    p = tmp_path / "deny.txt"
    p.write_text("45748\n")
    return p


def test_timeout_nonnumeric_exits_2(verify, denylist, tmp_path, monkeypatch):
    env = {
        "OPEN_WEBUI_URL": "http://openwebui",
        "OPEN_WEBUI_API_KEY": "k",
        "VERIFY_TIMEOUT_SECONDS": "abc",
    }
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
            with pytest.raises(SystemExit) as excinfo:
                verify.main()
    assert excinfo.value.code == 2


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "0", "1e400"])
def test_timeout_nonfinite_or_nonpositive_exits_2(verify, denylist, monkeypatch, bad):
    env = {
        "OPEN_WEBUI_URL": "http://openwebui",
        "OPEN_WEBUI_API_KEY": "k",
        "VERIFY_TIMEOUT_SECONDS": bad,
    }
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
            with pytest.raises(SystemExit) as excinfo:
                verify.main()
    assert excinfo.value.code == 2


def _run_list_kb_files(verify, monkeypatch, transport_side_effect):
    """Drive _list_kb_files with a fake transport; return (exit_code, stderr)."""
    import io
    import contextlib

    captured = io.StringIO()
    with mock.patch.object(verify, "_http_get", side_effect=transport_side_effect) if hasattr(verify, "_http_get") else contextlib.nullcontext():
        pass
    # The module uses urllib.request.urlopen directly; stub it.
    with mock.patch.object(verify.urllib.request, "urlopen", side_effect=transport_side_effect):
        with contextlib.redirect_stderr(captured):
            try:
                verify._list_kb_files("http://openwebui", "key", "kb1", timeout=30.0)
                code = 0
            except SystemExit as exc:
                code = exc.code
    return code, captured.getvalue()


class _FakeResponse:
    """Minimal urlopen() response object."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_non_json_kb_body_reports_not_json_not_url_error(verify, monkeypatch):
    """R4-F-2b595037: a non-JSON KB response must produce the dedicated
    'KB response was not JSON' message, not 'invalid OPEN_WEBUI_URL' —
    the except ValueError clause must not shadow JSONDecodeError."""
    code, err = _run_list_kb_files(verify, monkeypatch, lambda *a, **k: _FakeResponse(b"<html>gateway error</html>"))
    assert code == 2
    assert "KB response was not JSON" in err
    assert "invalid OPEN_WEBUI_URL" not in err


def test_non_utf8_kb_body_reports_unreadable_not_url_error(verify, monkeypatch):
    """R4-F-2b595037 (decode variant): non-UTF-8 KB body must produce the
    dedicated 'failed or was unreadable' message, not the URL message."""
    code, err = _run_list_kb_files(verify, monkeypatch, lambda *a, **k: _FakeResponse(b"ok\xff\x93bytes"))
    assert code == 2
    assert "was unreadable" in err
    assert "invalid OPEN_WEBUI_URL" not in err


@pytest.mark.parametrize("bad_url", ["openwebui", "open-webui", "openwebui.example.com"])
def test_schemeless_openwebui_url_exits_2(verify, denylist, bad_url):
    """R2-F-bcbfb1e6: urllib raises ValueError at Request construction for a
    scheme-less base_url (the main oikb config accepts bare hosts), and that
    uncaught ValueError used to exit 1 — the documented LEAKED code."""
    env = {
        "OPEN_WEBUI_URL": bad_url,
        "OPEN_WEBUI_API_KEY": "k",
    }
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
            with pytest.raises(SystemExit) as excinfo:
                verify.main()
    assert excinfo.value.code == 2


def test_timeout_valid_value_accepted(verify, denylist):
    """A valid VERIFY_TIMEOUT_SECONDS must be parsed and actually reach the
    socket call as urlopen's timeout; transport then fails deterministically
    (mocked — no live network dependency) and routes to exit 2."""
    import urllib.error

    captured: dict = {}

    def _boom(request, timeout=None):
        captured["timeout"] = timeout
        raise urllib.error.URLError("deterministic transport failure")

    env = {
        "OPEN_WEBUI_URL": "http://openwebui",
        "OPEN_WEBUI_API_KEY": "k",
        "VERIFY_TIMEOUT_SECONDS": "30",
    }
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
            with mock.patch.object(verify.urllib.request, "urlopen", _boom):
                with pytest.raises(SystemExit) as excinfo:
                    verify.main()
    assert excinfo.value.code == 2
    # main() reached transport with the parsed timeout: config was accepted.
    assert captured["timeout"] == 30.0


def test_bom_prefixed_denylist_parses(verify, tmp_path):
    """The verifier reads the same operator-authored denylist files as the
    connector, so a UTF-8 BOM must be tolerated here too: an otherwise
    valid denylist must not fail verification."""
    p = tmp_path / "deny.txt"
    p.write_text("\ufeff45748\n", encoding="utf-8")
    assert verify._load_deny_ids([str(p)]) == {"45748"}


def test_huge_digit_denylist_entry_exits_2(verify, tmp_path):
    """A 4301-digit entry passes isdigit() but overflows CPython's int/str
    conversion limit; the verifier must exit 2 (ERROR) — never 1 (LEAKED),
    which a raw ValueError traceback would produce."""
    huge = tmp_path / "deny-huge.txt"
    huge.write_text("9" * 4301 + "\n")
    with pytest.raises(SystemExit) as excinfo:
        verify._load_deny_ids([str(huge)])
    assert excinfo.value.code == 2


def _run_main_with_kb(verify, denylist, monkeypatch, body: bytes):
    """Drive main() against a stubbed KB listing; return (exit_code, stdout)."""
    import contextlib
    import io

    env = {
        "OPEN_WEBUI_URL": "http://openwebui",
        "OPEN_WEBUI_API_KEY": "k",
        "VERIFY_TIMEOUT_SECONDS": "30",
    }
    captured = io.StringIO()
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
            with mock.patch.object(
                verify.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(body)
            ):
                with contextlib.redirect_stdout(captured):
                    try:
                        verify.main()
                        code = 0
                    except SystemExit as exc:
                        code = exc.code
    return code, captured.getvalue()


def test_clean_listing_exits_0(verify, denylist, monkeypatch):
    """R2-F-cd48e4b3: a complete KB listing containing no denied-ID
    filenames is the exit-0 CLEAN path (previously untested)."""
    import json

    body = json.dumps(
        {
            "items": [
                {"id": "f1", "meta": {"name": "1001-order.md"}},
                {"id": "f2", "meta": {"name": "1002-notes.md"}},
            ],
            "total": 2,
        }
    ).encode()
    code, out = _run_main_with_kb(verify, denylist, monkeypatch, body)
    assert code == 0
    assert "VERDICT: CLEAN" in out


def test_leaked_listing_exits_1_with_filenames(verify, denylist, monkeypatch):
    """R2-F-cd48e4b3: a denied ticket's KB files are the exit-1 LEAKED path;
    both filename forms (<id>.md and <id>-prefixed) are reported."""
    import json

    body = json.dumps(
        {
            "items": [
                {"id": "f1", "meta": {"name": "1001-order.md"}},
                {"id": "f2", "meta": {"name": "45748.md"}},
                {"id": "f3", "meta": {"name": "45748-attachment.png"}},
            ],
            "total": 3,
        }
    ).encode()
    code, out = _run_main_with_kb(verify, denylist, monkeypatch, body)
    assert code == 1
    assert "VERDICT: LEAKED" in out
    assert "45748.md" in out
    assert "45748-attachment.png" in out


def test_leaked_item_filename_fallback_field(verify, denylist, monkeypatch):
    """Items without meta.name fall back to the filename field; a denied
    ID surfaced only that way still reports LEAKED (exit 1)."""
    import json

    body = json.dumps({"items": [{"id": "f1", "filename": "45748.md"}], "total": 1}).encode()
    code, out = _run_main_with_kb(verify, denylist, monkeypatch, body)
    assert code == 1
    assert "VERDICT: LEAKED" in out
    assert "45748.md" in out


def _run_main_with_kb_pages(verify, denylist, monkeypatch, pages: list[bytes]):
    """Drive main() against a page-aware stubbed KB listing: the page=N
    query parameter in the request URL selects the response body. Returns
    (exit_code, combined stdout+stderr)."""
    import contextlib
    import io
    import urllib.parse

    def _fake_urlopen(request, timeout=None):
        query = urllib.parse.urlparse(request.full_url).query
        page = int(urllib.parse.parse_qs(query).get("page", ["1"])[0])
        return _FakeResponse(pages[page - 1])

    env = {
        "OPEN_WEBUI_URL": "http://openwebui",
        "OPEN_WEBUI_API_KEY": "k",
        "VERIFY_TIMEOUT_SECONDS": "30",
    }
    captured = io.StringIO()
    try:
        err = io.StringIO()
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
                with mock.patch.object(verify.urllib.request, "urlopen", _fake_urlopen):
                    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(err):
                        try:
                            verify.main()
                            code = 0
                        except SystemExit as exc:
                            code = exc.code
        return code, captured.getvalue() + err.getvalue()
    finally:
        captured.close()


def _page(items, total):
    import json

    return json.dumps({"items": items, "total": total}).encode()


def test_duplicate_pages_cannot_fake_completion(verify, denylist, monkeypatch):
    """A server re-serving the same page while the listing is incomplete
    used to satisfy len(items) >= total with duplicated entries and report
    a false CLEAN; pagination must stall-detect and exit 2 instead."""
    pages = [
        _page([{"id": "f1", "meta": {"name": "1001-order.md"}},
               {"id": "f2", "meta": {"name": "1002-notes.md"}}], 3),
        _page([{"id": "f1", "meta": {"name": "1001-order.md"}},
               {"id": "f2", "meta": {"name": "1002-notes.md"}}], 3),
    ]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 2
    assert "no new items" in out


def test_overlapping_pages_complete_by_unique_count(verify, denylist, monkeypatch):
    """Overlapping pages that nonetheless cover every unique file verify
    CLEAN: completion is judged on unique ids, not raw entry count."""
    pages = [
        _page([{"id": "f1", "meta": {"name": "1001-order.md"}},
               {"id": "f2", "meta": {"name": "1002-notes.md"}}], 3),
        _page([{"id": "f2", "meta": {"name": "1002-notes.md"}},
               {"id": "f3", "meta": {"name": "1003-notes.md"}}], 3),
    ]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 0
    assert "VERDICT: CLEAN" in out


def test_total_changing_between_pages_exits_2(verify, denylist, monkeypatch):
    """A total that changes mid-pagination means the listing is a moving
    target; a CLEAN verdict on it would rest on unverifiable evidence."""
    pages = [
        _page([{"id": "f1", "meta": {"name": "1001-order.md"}},
               {"id": "f2", "meta": {"name": "1002-notes.md"}}], 3),
        _page([{"id": "f3", "meta": {"name": "1003-notes.md"}}], 4),
    ]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 2
    assert "total' changed between pages" in out


def test_entry_without_id_exits_2(verify, denylist, monkeypatch):
    """Every dict entry must carry a usable id: deduplication keys on it,
    and without it completeness cannot be verified (fail closed, exit 2)."""
    pages = [_page([{"meta": {"name": "1001-order.md"}}], 1)]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 2
    assert "without id" in out


def test_incomplete_listing_exits_2(verify, denylist, monkeypatch):
    """A listing that never reaches its declared total must refuse to
    report CLEAN, even when pagination keeps advancing without error."""
    pages = [
        _page([{"id": "f1", "meta": {"name": "1001-order.md"}}], 3),
        _page([{"id": "f2", "meta": {"name": "1002-notes.md"}}], 3),
        _page([], 3),  # server stops serving new items before the total
    ]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 2
    assert "no new items" in out


def test_unmatchable_item_refuses_clean_exits_2(verify, denylist, monkeypatch):
    """R1-F-742f665e: an item with id but neither meta.name nor filename
    cannot be matched against the denylist; a complete listing of such
    items must refuse CLEAN (exit 2) instead of reporting a vacuous pass."""
    pages = [_page([{"id": "f1", "hash": "abc"}], 1)]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 2
    assert "VERDICT: CLEAN" not in out
    assert "no resolvable filename" in out
    assert "f1" in out


def test_leak_wins_over_unmatchable_items(verify, denylist, monkeypatch):
    """R1-F-742f665e: when a real leak is found among matchable items, the
    LEAKED verdict (exit 1) takes precedence — unmatchable items must not
    mask a confirmed leak behind an INDETERMINATE error."""
    pages = [
        _page(
            [
                {"id": "f1", "meta": {"name": "45748.md"}},
                {"id": "f2", "hash": "abc"},
            ],
            2,
        )
    ]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 1
    assert "VERDICT: LEAKED" in out
    assert "45748.md" in out


def test_non_dict_entry_refuses_clean_exits_2(verify, denylist, monkeypatch):
    """A non-object entry inside a non-null items list has no filename to
    check; skipping it would let the remaining objects reach `total` and
    report a vacuous CLEAN. Malformed entries must make the listing
    INDETERMINATE (exit 2) instead."""
    pages = [
        _page(
            [
                {"id": "f1", "meta": {"name": "1001-order.md"}},
                None,
            ],
            2,
        )
    ]
    code, out = _run_main_with_kb_pages(verify, denylist, monkeypatch, pages)
    assert code == 2
    assert "VERDICT: CLEAN" not in out
    assert "not a JSON object" in out
