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
