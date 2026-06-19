from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loop_orchestrator import verify
from loop_orchestrator.paths import SessionPaths


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "verify@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Verify"], cwd=root, check=True)
    (root / "app.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    (root / "app.txt").write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "tip"], cwd=root, check=True, capture_output=True)
    tip = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, base, tip


def _json_reply(verdict: str, severity: str = "low") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "findings": []
            if verdict == "pass"
            else [{"severity": severity, "title": "bad path", "detail": "needs work"}],
            "summary": verdict,
        }
    )


def _verdict_reply(verdict: str, severity: str = "low") -> str:
    return f"```verdict\n{_json_reply(verdict, severity)}\n```"


def _set_gate(monkeypatch, passed: bool) -> None:
    monkeypatch.setattr(
        verify,
        "_run_gate",
        lambda worktree, timeout_s=verify.DEFAULT_GATE_TIMEOUT_S: verify.GateResult(
            passed, "gate ok" if passed else "gate failed"
        ),
    )


def test_all_pass_is_overall_pass(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    one = _stub(tmp_path / "one", f"cat <<'EOF'\n{_verdict_reply('pass')}\nEOF\n")
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, timeout_s=5)

    assert result.overall == "pass"
    assert result.gate.passed is True
    assert [lens.verdict for lens in result.lenses] == ["pass", "pass", "pass"]
    assert result.findings == []


def test_lens_fail_is_overall_fail(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    one = _stub(
        tmp_path / "one",
        "prompt=\"${@: -1}\"\n"
        f"if [[ \"$prompt\" == *\"adversarial\"* ]]; then reply='{_json_reply('fail')}'; "
        f"else reply='{_json_reply('pass')}'; fi\n"
        "printf '%s\\n%s\\n%s\\n' '```verdict' \"$reply\" '```'\n",
    )
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, timeout_s=5)

    assert result.overall == "fail"
    assert {lens.lens: lens.verdict for lens in result.lenses}["adversarial"] == "fail"
    assert result.findings[0]["title"] == "bad path"


def test_gate_failure_is_overall_fail_without_lenses(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, False)
    marker = tmp_path / "called"
    one = _stub(tmp_path / "one", f": > {marker}\n")
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, timeout_s=5)

    assert result.overall == "fail"
    assert result.gate.passed is False
    assert result.lenses == []
    assert not marker.exists()


def test_garbled_reply_degrades_to_concerns_parse_note(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    one = _stub(tmp_path / "one", 'printf "not json\\n"\n')
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, lenses=("code-review",), timeout_s=5)

    assert result.overall == "concerns"
    assert result.lenses[0].verdict == "concerns"
    assert "parse" in result.lenses[0].error
    assert result.findings[0]["title"] == "parse-note"


def test_verdict_fence_wins_after_json_example(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    reply = "\n".join(
        [
            "Example only:",
            "```json",
            _json_reply("pass"),
            "```",
            "Real verdict:",
            "```verdict",
            _json_reply("fail", "critical"),
            "```",
        ]
    )
    one = _stub(tmp_path / "one", f"cat <<'EOF'\n{reply}\nEOF\n")
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, lenses=("adversarial",), timeout_s=5)

    assert result.overall == "fail"
    assert result.lenses[0].verdict == "fail"
    assert result.findings[0]["severity"] == "critical"


def test_verdict_fence_wins_before_json_example(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    reply = "\n".join(
        [
            "Real verdict:",
            "```verdict",
            _json_reply("fail", "critical"),
            "```",
            "Example only:",
            "```json",
            _json_reply("pass"),
            "```",
        ]
    )
    one = _stub(tmp_path / "one", f"cat <<'EOF'\n{reply}\nEOF\n")
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, lenses=("adversarial",), timeout_s=5)

    assert result.overall == "fail"
    assert result.lenses[0].verdict == "fail"
    assert result.findings[0]["severity"] == "critical"


def test_empty_diff_is_concern_not_pass(tmp_path, monkeypatch):
    worktree, base, _tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)

    result = verify.run_verify(worktree, base, base, lenses=("code-review",), timeout_s=5)

    assert result.overall != "pass"
    assert result.lenses[0].lens == "diff"
    assert "nothing to review" in result.lenses[0].error


def test_blank_base_ref_is_fail_not_empty_diff_concern(tmp_path, monkeypatch):
    worktree, _base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)

    result = verify.run_verify(worktree, "", tip, lenses=("code-review",), timeout_s=5)

    assert result.overall == "fail"
    assert result.lenses[0].lens == "diff"
    assert "base ref must be non-empty" in result.lenses[0].error


def test_verify_result_includes_generated_at(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    one = _stub(tmp_path / "one", f"cat <<'EOF'\n{_verdict_reply('pass')}\nEOF\n")
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(worktree, base, tip, lenses=("code-review",), timeout_s=5)

    assert result.generated_at
    assert result.to_dict()["generated_at"] == result.generated_at


def test_explicit_configured_harness_keeps_extra_args(tmp_path, monkeypatch, fakes_env):
    worktree, _base, _tip = _repo(tmp_path)
    (worktree / "lane-config.yaml").write_text(
        "engine:\n  brain:\n    harness: claude\n    extra_args: ['--max-turns', '1']\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("LOOP_VERIFY_CMD", raising=False)

    argv, effective_harness = verify._argv_for_prompt(worktree, "prompt", harness="claude")

    assert effective_harness == "claude"
    assert argv == ["claude", "-p", "prompt", "--max-turns", "1"]


def test_parallel_lenses_use_distinct_transcript_locations(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    one = _stub(
        tmp_path / "one",
        "sleep 0.1\n"
        f"cat <<'EOF'\n{_verdict_reply('pass')}\nEOF\n",
    )
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))

    result = verify.run_verify(
        worktree,
        base,
        tip,
        lenses=("code-review", "silent-failure", "adversarial"),
        timeout_s=5,
    )

    assert result.overall == "pass"
    events = SessionPaths(worktree, "verify").events_path.read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in events if json.loads(line)["event"] == "verify-call"]
    response_paths = [Path(call["response_path"]) for call in calls]
    assert len(response_paths) == 3
    assert len(set(response_paths)) == 3
    assert {path.parent.name for path in response_paths} == {
        "code-review",
        "silent-failure",
        "adversarial",
    }


def test_cli_exit_codes_and_out_write(tmp_path, monkeypatch):
    worktree, base, tip = _repo(tmp_path)
    _set_gate(monkeypatch, True)
    one = _stub(tmp_path / "one", f"cat <<'EOF'\n{_verdict_reply('pass')}\nEOF\n")
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(one))
    out = tmp_path / "verify.json"

    code = verify.main(
        ["--worktree", str(worktree), "--base", base, "--tip", tip, "--out", str(out)]
    )

    assert code == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["overall"] == "pass"
    assert len(doc["lenses"]) == 3

    fail_one = _stub(
        tmp_path / "fail-one",
        f"cat <<'EOF'\n{_verdict_reply('fail')}\nEOF\n",
    )
    monkeypatch.setenv("LOOP_VERIFY_CMD", str(fail_one))
    assert verify.main(["--worktree", str(worktree), "--base", base, "--tip", tip]) == 1


def test_console_script_registered():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'loop-verify = "loop_orchestrator.verify:main"' in text
