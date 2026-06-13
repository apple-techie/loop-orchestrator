"""Fixtures pointing the Python layer at tests/fakes/bin instead of tmux."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Tests must not depend on the venv's editable .pth: macOS sets UF_HIDDEN on
# dot-directory contents (iCloud-synced paths) and CPython >= 3.13 skips
# hidden .pth files, silently dropping the editable install from sys.path.
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"


@pytest.fixture
def fakes_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """LOOP_SUBSTRATE_BIN -> tests/fakes/bin; returns the FAKE_CALL_LOG path.

    Clears the FAKE_* behavior toggles so each test starts from the canned
    defaults (all lanes idle, dispatch succeeds, brain emits a valid decision).
    """
    log = tmp_path / "fake-calls.log"
    monkeypatch.setenv("LOOP_SUBSTRATE_BIN", str(FAKES_BIN))
    monkeypatch.setenv("FAKE_CALL_LOG", str(log))
    for var in (
        "FAKE_LANE_STATUS_OVERRIDE",
        "FAKE_DISPATCH_FAIL",
        "FAKE_BRAIN_MODE",
        "FAKE_METRICS_FAIL",
        "FAKE_LINT_FAIL",
        "FAKE_ROSTER_JSON",
        "FAKE_HEALTH",
    ):
        monkeypatch.delenv(var, raising=False)
    return log


@pytest.fixture
def call_log(fakes_env: Path):
    """Reader for the fake-call log: one 'script arg arg ...' line per spawn."""

    def read() -> list[str]:
        if not fakes_env.exists():
            return []
        return fakes_env.read_text(encoding="utf-8").splitlines()

    return read
