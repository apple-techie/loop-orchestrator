"""Substrate contract version handling (see CONTRACT.md)."""

from __future__ import annotations

EXPECTED_CONTRACT_MAJOR = 1


class ContractMismatch(RuntimeError):
    """A bash JSON payload declared a contract major we don't speak."""

    def __init__(self, source: str, found: object):
        super().__init__(
            f"{source}: contract_version {found!r} exceeds supported major "
            f"{EXPECTED_CONTRACT_MAJOR} — update the loop-orchestrator Python "
            "package to match the bash substrate (see CONTRACT.md)"
        )
        self.found = found


def check_contract(payload: dict, source: str) -> dict:
    """Validate a parsed JSON payload's contract_version; return the payload.

    Missing contract_version is tolerated (older substrate within the same
    major); a declared major GREATER than ours is not — fields we rely on may
    have changed meaning.
    """
    found = payload.get("contract_version")
    if isinstance(found, int) and found > EXPECTED_CONTRACT_MAJOR:
        raise ContractMismatch(source, found)
    return payload
