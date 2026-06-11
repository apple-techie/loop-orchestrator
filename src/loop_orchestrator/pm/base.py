"""PM adapter contract: the shape every sync backend (Jira et al.) implements.

Task files are the source of truth (AGENTS.md '### Task files'); adapters are
optional enrichment. Conflicts are correctly-handled divergences under the
file-wins rule, not failures — they ride in PMSyncResult.conflicts and never
make a sync exit non-zero.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PMSyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


class PMAdapter(ABC):
    """One PM backend. Credentials come from the environment only — never
    from the repo or task files (AGENTS.md 'Jira sync contract')."""

    name: str = ""

    def __init__(self, project_root: str | Path = "."):
        self.project_root = Path(project_root)

    def available(self) -> bool:
        """True when credentials are present. NEVER raises."""
        try:
            return not self.validate_env()
        except Exception:
            return False

    @abstractmethod
    def validate_env(self) -> list[str]:
        """Names of required-but-missing environment variables."""

    @abstractmethod
    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        """Create task files from remote issues (file wins on divergence)."""

    @abstractmethod
    def push(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        """Update remote issue status from task-file frontmatter."""
