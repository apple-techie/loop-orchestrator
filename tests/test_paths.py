from loop_orchestrator.engine.actions import _lane_worktree
from loop_orchestrator.paths import SessionPaths


def test_session_paths_resolves_project_root_and_lane_worktree(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)

    paths = SessionPaths(project / "nested" / "..", "demo")

    assert paths.project_root == project.resolve()
    assert _lane_worktree(paths, "web") == (
        project / ".loop" / "worktrees" / "demo" / "web"
    ).resolve()
