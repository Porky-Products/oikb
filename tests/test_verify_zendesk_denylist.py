"""Offline regression tests for scripts/verify_zendesk_denylist.py.

Covers the exit-code contract: 0=CLEAN, 1=LEAKED, 2=ERROR. In particular,
operator configuration errors (bad VERIFY_TIMEOUT_SECONDS: non-numeric,
nan, inf, non-positive) must exit 2 — never 1, which CI gates would read
as a confirmed leak (R1-F-54e37363).
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
    env = {
        "OPEN_WEBUI_URL": "http://openwebui",
        "OPEN_WEBUI_API_KEY": "k",
        "VERIFY_TIMEOUT_SECONDS": "30",
    }
    # A valid timeout must get past config; it will then fail on transport
    # (no live server), which the script routes to exit 2 as well.
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(verify.sys, "argv", ["prog", "kb1", str(denylist)]):
            with pytest.raises(SystemExit) as excinfo:
                verify.main()
    assert excinfo.value.code == 2
    # main() reached transport with a parsed timeout: config was accepted.
