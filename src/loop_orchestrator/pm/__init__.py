"""loop-pm — modular PM adapters (Jira et al.) syncing against tasks/ files.

The task files are the source of truth; adapters are optional enrichment.
Discovery is via the `loop_orchestrator.pm_adapters` entry-point group so
third parties can ship adapters as pip packages.
"""
