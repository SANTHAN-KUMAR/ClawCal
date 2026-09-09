"""Test configuration.

Tests run against a throwaway database so they never touch a real deployment's
state. `SOVEREIGN_DATA_DIR` is redirected before `sovereign.config` is imported,
because that module resolves every path at import time.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

_TMP = tempfile.mkdtemp(prefix="sovereign-tests-")
os.environ["SOVEREIGN_DATA_DIR"] = _TMP
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytest  # noqa: E402

from sovereign import db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    db.init_db()
    yield


@pytest.fixture()
def task_id():
    return db.new_id("test")


@pytest.fixture(scope="session")
def corpus_dir() -> Path:
    return ROOT / "corpus"
