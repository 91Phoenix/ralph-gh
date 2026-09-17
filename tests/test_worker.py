"""Seam: ralph_gh.worker — markers, session outcome classification, prompts,
runner command shape."""

import os

from ralph_gh import worker
from ralph_gh.config import Config

CFG = Config.from_env("o/1", env={})


def w(tmp_path, name, txt):
    with open(os.path.join(str(tmp_path), name), "w") as f:
        f.write(txt)


class TestMarkers:
    def test_build_markers(self, tmp_path):
        ws = str(tmp_path)
        assert not worker.build_ok(ws) and not worker.build_failed(ws)
        w(tmp_path, "BUILD_OK", "mvn test")
        w(tmp_path, "BUILD_FAIL", "x")
        assert worker.build_ok(ws) and worker.build_failed(ws)
        worker.clear_markers(ws)
        assert not worker.build_ok(ws) and not worker.build_failed(ws)

    def test_fail_reason_one_line_and_capped(self, tmp_path):
        w(tmp_path, "BUILD_FAIL", "a\n  b\n\nc")
        assert worker.build_fail_reason(str(tmp_path)) == "a b c"
        w(tmp_path, "BUILD_FAIL", "x" * 700)
        r = worker.build_fail_reason(str(tmp_path))
        assert r.startswith("x" * 600) and "truncated" in r
        os.unlink(tmp_path / "BUILD_FAIL")
        assert worker.build_fail_reason(str(tmp_path)) == ""

    def test_pr_body_verbatim_and_clear(self, tmp_path):
        assert worker.pr_body(str(tmp_path)) == ""
        w(tmp_path, worker.PR_BODY, "\n## What this does\n- a\n\n")
        assert worker.pr_body(str(tmp_path)) == "## What this does\n- a"
        worker.clear_markers(str(tmp_path))
        assert worker.pr_body(str(tmp_path))          # markers leave it alone
        worker.clear_pr_body(str(tmp_path))
        assert worker.pr_body(str(tmp_path)) == ""

    def test_collaudo_markers_and_summary(self, tmp_path):
        ws = str(tmp_path)
        assert not worker.collaudo_ok(ws) and not worker.collaudo_failed(ws)
        assert worker.collaudo_summary(ws) == ""
        w(tmp_path, "COLLAUDO_FAIL", "no slot\nfree")
        assert worker.collaudo_failed(ws) and worker.collaudo_summary(ws) == "no slot free"
        w(tmp_path, "COLLAUDO_OK", "ISSUES 2\n- t1 pass\n- t2 fail")
        assert worker.collaudo_summary(ws).startswith("ISSUES 2")   # OK wins
        worker.clear_collaudo_markers(ws)
        assert not worker.collaudo_ok(ws) and not worker.collaudo_failed(ws)
        w(tmp_path, "BUILD_OK", "x"); worker.clear_collaudo_markers(ws)
        assert worker.build_ok(ws)                                   # orthogonal

    def test_pr_comments_written_or_removed(self, tmp_path):
        worker.write_pr_comments(str(tmp_path), "# c")
        assert os.path.exists(tmp_path / worker.PR_COMMENTS)
        worker.write_pr_comments(str(tmp_path), "")
        assert not os.path.exists(tmp_path / worker.PR_COMMENTS)


def log_with(tmp_path, *lines):
    p = str(tmp_path / "s.log")
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


class TestSessionFailure:
    def test_clean(self, tmp_path):
        assert worker.session_failure(log_with(tmp_path, "did things", "done")) == (worker.CLEAN, "")
        assert worker.session_failure(str(tmp_path / "missing")) == (worker.CLEAN, "")

    def test_transient_shapes(self, tmp_path):
        for line in ("API Error: 529 overloaded", "Could not resolve host: api.anthropic.com",
                     "fetch failed", "stream disconnected", "Prompt is too long",
                     "API Error: 400 tool xyz not found in available tools"):
            kind, got = worker.session_failure(log_with(tmp_path, "ok", line))
            assert kind == worker.TRANSIENT, line
            assert got == line

    def test_terminal_shapes(self, tmp_path):
        for line in ("API Error: 401 authentication_error", "Invalid API key",
                     "API Error: 403 forbidden", "Credit balance is too low"):
            assert worker.session_failure(log_with(tmp_path, line))[0] == worker.TERMINAL, line

    def test_only_tail_counts(self, tmp_path):
        lines = ["API Error: 529"] + ["fine"] * 10
        assert worker.session_failure(log_with(tmp_path, *lines)) == (worker.CLEAN, "")

    def test_decisive_line_flattened_and_capped(self, tmp_path):
        kind, line = worker.session_failure(log_with(tmp_path, "API   Error: 503 " + "z" * 400))
        assert kind == worker.TRANSIENT and "API Error: 503" in line and line.endswith("…")

    def test_hints(self):
        assert "settings.json" in worker.failure_hint("x not found in available tools")
        assert "credit" in worker.failure_hint("Credit balance too low")
        assert worker.failure_hint("random") == ""

    def test_keep_failed_log(self, tmp_path):
        p = log_with(tmp_path, "x")
        kept = worker.keep_failed_log(p, 1)
        assert kept.endswith(".attempt1") and os.path.exists(kept) and not os.path.exists(p)
        assert worker.keep_failed_log(p, 2) == ""


class TestRunner:
    def test_command_shape(self):
        cmd = worker.WorkerRunner(CFG).command("/ws", "do it")
        assert cmd[:3] == ["claude", "-p", "do it"]
        assert "--permission-mode" in cmd and "bypassPermissions" in cmd
        assert "--append-system-prompt" in cmd and "--add-dir" in cmd and "/ws" in cmd
        assert "--model" not in cmd
        assert "--mcp-config" not in cmd and "--strict-mcp-config" not in cmd

    def test_model_and_extra_args(self):
        cfg = Config.from_env("o/1", env={"RALPH_MODEL": "opus", "RALPH_WORKER_SYS": "",
                                           "RALPH_AGENT_EXTRA_ARGS": "--verbose"})
        cmd = worker.WorkerRunner(cfg).command("/ws", "p")
        assert cmd[cmd.index("--model") + 1] == "opus"
        assert "--append-system-prompt" not in cmd
        assert cmd[-1] == "--verbose"

    def test_opencode_command_shape(self):
        cfg = Config.from_env("o/1", env={"RALPH_AGENT": "opencode",
                                           "RALPH_MODEL": "anthropic/claude-sonnet-4-5",
                                           "RALPH_WORKER_SYS": "STYLE",
                                           "RALPH_AGENT_EXTRA_ARGS": "--variant high"})
        cmd = worker.WorkerRunner(cfg).command("/ws", "do it")
        assert cmd[:3] == ["opencode", "run", "--auto"]
        assert cmd[cmd.index("--dir") + 1] == "/ws"
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
        assert "--variant" in cmd and "high" in cmd
        # the style prompt rides on the message; the prompt is the LAST arg
        assert cmd[-1].startswith("STYLE\n\n---\n\ndo it")
        for flag in ("-p", "--permission-mode", "--append-system-prompt", "--add-dir"):
            assert flag not in cmd

    def test_opencode_without_style_prompt(self):
        cfg = Config.from_env("o/1", env={"RALPH_AGENT": "opencode", "RALPH_WORKER_SYS": ""})
        assert worker.WorkerRunner(cfg).command("/ws", "p")[-1] == "p"

    def test_agent_bin_override_runs_kilo(self):
        cfg = Config.from_env("o/1", env={"RALPH_AGENT": "opencode", "RALPH_AGENT_BIN": "kilo"})
        cmd = worker.WorkerRunner(cfg).command("/ws", "p")
        assert cmd[:3] == ["kilo", "run", "--auto"]

    def test_collaudo_profile_claude_gets_playwright_mcp_config(self, tmp_path):
        cfg = Config.from_env("o/1", env={"RALPH_COLLAUDO": "1"})
        r = worker.WorkerRunner(cfg, profile="collaudo")
        ws = str(tmp_path)
        cmd = r.command(ws, "p")
        assert cmd[0] == "claude" and "--mcp-config" in cmd
        import json
        conf = json.load(open(cmd[cmd.index("--mcp-config") + 1]))
        srv = conf["mcpServers"]["playwright"]
        assert srv["command"] == "npx" and "@playwright/mcp@latest" in srv["args"]
        assert srv["args"][srv["args"].index("--output-dir") + 1] == os.path.join(ws, worker.COLLAUDO_DIR)
        env = r.env(ws)
        assert env["COLLAUDO_DIR"].endswith(worker.COLLAUDO_DIR) and "OPENCODE_CONFIG_CONTENT" not in env

    def test_collaudo_profile_opencode_gets_inline_mcp(self, tmp_path):
        cfg = Config.from_env("o/1", env={"RALPH_AGENT": "claude", "RALPH_COLLAUDO_AGENT": "opencode",
                                           "RALPH_COLLAUDO_AGENT_BIN": "kilo",
                                           "RALPH_COLLAUDO_URL": "http://localhost:3000"})
        r = worker.WorkerRunner(cfg, profile="collaudo")
        cmd = r.command(str(tmp_path), "p")
        assert cmd[:3] == ["kilo", "run", "--auto"] and "--mcp-config" not in cmd
        env = r.env(str(tmp_path))
        import json
        for var in ("OPENCODE_CONFIG_CONTENT", "KILO_CONFIG_CONTENT"):
            mcp = json.loads(env[var])["mcp"]["playwright"]
            assert mcp["type"] == "local" and mcp["command"][:3] == ["npx", "-y", "@playwright/mcp@latest"]
        assert env["COLLAUDO_URL"] == "http://localhost:3000"
        # the worker profile of the same config is untouched
        assert worker.WorkerRunner(cfg).command(str(tmp_path), "p")[0] == "claude"

    def test_collaudo_browser_none_wires_nothing(self, tmp_path):
        cfg = Config.from_env("o/1", env={"RALPH_COLLAUDO_BROWSER": "none",
                                           "RALPH_COLLAUDO_AGENT": "opencode"})
        r = worker.WorkerRunner(cfg, profile="collaudo")
        assert "--mcp-config" not in r.command(str(tmp_path), "p")
        assert "OPENCODE_CONFIG_CONTENT" not in r.env(str(tmp_path))

    def test_legacy_extra_args_env_still_read(self):
        cfg = Config.from_env("o/1", env={"RALPH_WORKER_EXTRA_ARGS": "--x"})
        assert worker.WorkerRunner(cfg).command("/ws", "p")[-1] == "--x"

    def test_run_passes_through(self):
        calls = []
        r = worker.WorkerRunner(CFG, exec_fn=lambda cmd, cwd, logf, t, env: calls.append((cmd[2], cwd, logf, t, env["RALPH_GH"])) or 0)
        assert r.run("/ws", "t", "prompt", "/l") == 0
        assert calls == [("prompt", "/ws", "/l", CFG.session_timeout, "1")]


class TestPrompts:
    K, N, F, B, T = "o/r#7", 7, "o/r", "feature/issue-7", "main"

    def test_implement(self):
        p = worker.implement_prompt(CFG, self.K, self.N, "Do X", self.F, self.B, self.T)
        for s in (worker.TICKET, worker.SIBLINGS, worker.PR_BODY, "BUILD_OK", "BUILD_FAIL",
                  "/tdd", ".github/workflows", "## How to test manually",
                  "## Automated checks", "feat(#7)", "Do NOT push",
                  "gh issue view 7 --repo o/r", "never commit them"):
            assert s in p, s
        assert "cherry-pick" in p and self.T in p

    def test_pipeline_fix_uses_gh_run(self):
        p = worker.pipeline_fix_prompt(CFG, self.K, self.N, self.F, self.B, "3")
        assert "gh run list --repo o/r --branch feature/issue-7" in p
        assert "--log-failed" in p and "gh pr checks 3" in p and "BUILD_FAIL" in p

    def test_review_posts_via_gh(self):
        p = worker.review_prompt(CFG, self.K, self.N, "3", self.F, self.B, self.T)
        assert "gh pr diff 3 --repo o/r" in p and "/code-review" in p
        assert "repos/o/r/pulls/3/comments" in p and "gh pr comment 3" in p
        assert "LGTM — no blocking issues." in p and "commit nothing" in p

    def test_address_reads_dump_and_replies(self):
        p = worker.address_prompt(CFG, self.K, self.N, "3", self.F, self.B)
        assert worker.PR_COMMENTS in p and "/replies" in p
        assert "fix(#7): address review" in p and "do NOT create BUILD_OK" in p
        assert "reply in its thread" in p

    def test_resync_closes_only_gap(self):
        p = worker.resync_prompt(CFG, self.K, self.N, "3", self.F, self.B, "the issue was edited")
        assert "Close ONLY that gap" in p and "the issue was edited" in p
        assert "Do NOT rebase" in p

    def test_collaudo_prompt_playwright(self):
        cfg = Config.from_env("o/1", env={"RALPH_COLLAUDO": "1"})
        p = worker.collaudo_prompt(cfg, self.K, self.N, "3", self.F, self.B)
        for s in ("TESTING, not fixing", "browser_take_screenshot", "browser_snapshot",
                  "COLLAUDO_OK", "COLLAUDO_FAIL", "ISSUES <n>", "collaudo: ",
                  "gh pr comment 3 --repo o/r", "/collaudo-locale", "release the slot",
                  "gh pr view 3 --repo o/r"):
            assert s in p, s

    def test_collaudo_prompt_api_level_and_opencode(self):
        cfg = Config.from_env("o/1", env={"RALPH_COLLAUDO_BROWSER": "none",
                                           "RALPH_COLLAUDO_AGENT": "opencode",
                                           "RALPH_COLLAUDO_URL": "http://localhost:3000"})
        p = worker.collaudo_prompt(cfg, self.K, self.N, "3", self.F, self.B)
        assert "NO browser" in p and "browser_snapshot" not in p
        assert "`collaudo-locale` skill (load it with the skill tool)" in p
        assert "answers at http://localhost:3000" in p

    def test_address_prompt_explains_collaudo_comments(self):
        p = worker.address_prompt(CFG, self.K, self.N, "3", self.F, self.B)
        assert "prefixed `collaudo:`" in p and "cover the observed failure with a test" in p

    def test_rebase_forbids_abort(self):
        p = worker.rebase_resolve_prompt(self.B, self.T)
        assert "Do NOT run `git rebase --abort`" in p and "Do NOT push" in p

    def test_opencode_prompts_name_the_skill_tool(self):
        cfg = Config.from_env("o/1", env={"RALPH_AGENT": "opencode"})
        p = worker.implement_prompt(cfg, self.K, self.N, "s", self.F, self.B, self.T)
        assert "`tdd` skill (load it with the skill tool)" in p and "/tdd" not in p

    def test_skill_names_configurable(self):
        cfg = Config.from_env("o/1", env={"RALPH_SKILL_IMPLEMENT": "my-tdd",
                                           "RALPH_SKILL_REVIEW": "my-review"})
        assert "/my-tdd" in worker.implement_prompt(cfg, self.K, self.N, "s", self.F, self.B, self.T)
        assert "/my-review" in worker.review_prompt(cfg, self.K, self.N, "3", self.F, self.B, self.T)
