"""check_contract gate: tolerate same/missing major, reject a greater one."""

from __future__ import annotations

import pytest

from loop_orchestrator.contract import EXPECTED_CONTRACT_MAJOR, ContractMismatch, check_contract


def test_same_major_passes():
    payload = {"contract_version": EXPECTED_CONTRACT_MAJOR, "lanes": []}
    assert check_contract(payload, "loop-digest") is payload


def test_missing_version_tolerated():
    payload = {"lanes": []}
    assert check_contract(payload, "loop-digest") is payload


def test_non_int_version_tolerated():
    payload = {"contract_version": "99"}
    assert check_contract(payload, "loop-digest") is payload


def test_greater_major_raises():
    found = EXPECTED_CONTRACT_MAJOR + 1
    with pytest.raises(ContractMismatch) as exc:
        check_contract({"contract_version": found}, "loop-tmux")
    assert "loop-tmux" in str(exc.value)
    assert exc.value.found == found
