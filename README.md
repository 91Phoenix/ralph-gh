# ralph-gh

An **autonomous GitHub issue implementer**. Point it at a GitHub Project (v2)
and, for up to ~5 hours, it works the project's *ready frontier*: for every
open issue that is in the Ready column, carries the `ready-for-agent` label,
is unassigned and has no open blockers, it spins up a fresh headless Claude
Code session that implements the issue test-first, pushes a branch, opens a
draft pull request, waits for the GitHub checks (auto-fixing a red run up to
twice), marks the PR ready, reviews it with a second session, addresses one
round of review comments with a third, and leaves the PR open for **you** to
merge — or, with `RALPH_AUTO_MERGE=1`, merges it itself once every review
thread was answered and the checks are green. When a PR merges the issue
moves to Done — which unblocks the issues that depended on it — and the loop
keeps going.

It is a single-user tool. Everything goes through the `gh` CLI you are
already logged into: no MCP servers, no tokens of its own, no service
account. Commits and PRs are yours.

Inspired by the Jira/Bitbucket Ralph Loop in `bs-agents-skills`, rebuilt for
GitHub Issues + GitHub Projects.

## Prerequisites

- An agent CLI for the worker sessions — `claude` (Claude Code, the default)
  or `opencode` (see [Running on OpenCode](#running-on-opencode-or-kilo)) —
  plus `git`, `gh`, `python3` ≥ 3.9 on `PATH`.
- `gh auth login` done, **with the `project` scope**. The default `gh` token
  has `repo` and `workflow` but not `project`; add it once:

  ```bash
  gh auth refresh -h github.com -s project
  ```

- Local git access to every target repository through your own auth — either
  the gh credential helper (`gh auth setup-git`) or an SSH key that works
  non-interactively. The loop clones and pushes as you; it never embeds a
  token in a remote URL.
- Optional: the skills the worker prompts refer to (`/tdd`, `/code-review`,
  `/collaudo-locale`) installed in `~/.claude/skills/` — the last one ships
  in [skills/](skills/README.md), link it in. Workers are told to follow the
  same discipline by hand when a skill is missing.

## Set up the project once

1. Create a GitHub Project (user or org, the new "Projects v2" kind) and add
   the issues you want implemented. Issues from several repositories may live
   in one project.
2. Make sure the project's **Status** field has columns the loop can map to:
   a *Ready* column (`Ready`, `Todo` or `To Do`), an *In Progress* column, an
   *In Review* column and a *Done* column. Any other names work too — set
   `ST_READY`, `ST_INPROGRESS`, `ST_REVIEW`, `ST_DONE` to pipe-separated
   aliases (`ST_READY="Backlog|Ready"`). A missing In Review or Done column
   only costs a warning; a missing Ready column stops the loop.
3. Label the issues an agent may pick up with `ready-for-agent` (or set
   `AGENT_LABEL`; an empty value disables the requirement).
4. Express ordering with GitHub's native **issue dependencies** ("Blocked by"
   in the issue sidebar, GA since 2025) or with a `## Blocked by` heading in
   the body listing `#12`, `owner/repo#12` or issue URLs. An issue is blocked
   until every blocker is **closed**.

## Shape of an issue

Nothing is mandatory: the issue lives in a repository, so the loop already
knows where the work goes and which branch is the default. Two optional
headings refine that — they must be real Markdown headings, prose that merely
says "Repository: x" is ignored:

```markdown
## Repository
acme/backend            <- implement here instead of the issue's own repo

## Target branch
development             <- base the work on, and target the PR at, this branch
```

`## Repository` also accepts `acme/backend — trunk development` as a
one-liner, or `none` to mark a manual issue the loop must skip. Without a
target branch the repository's default branch is used — and corrected
automatically when git proves it is a stale stub of a real trunk (say `main`
with one commit while `development` carries everything).

## Run it

```bash
# from a checkout
bin/ralph-gh https://github.com/users/<you>/projects/3
bin/ralph-gh <you>/3                       # short form
bin/ralph-gh <you>/3 <you>/some-repo       # fallback repo for issues that
                                           # carry a "## Repository: none"-less
                                           # planning-repo body — rarely needed

# or install it
uv tool install .   # or: pipx install .
ralph-gh <you>/3
```

The **preflight** refuses to start unless the toolchain is real: the three
CLIs are on `PATH`, `gh auth status` passes, the project resolves (a token
without the `project` scope gets the exact `gh auth refresh` line to run),
the Status field has a Ready column, and your local git auth reaches the
frontier's repositories.

## What happens per issue

Each phase is a **fresh** headless agent session (`claude -p` by default,
`opencode run --auto` with `RALPH_AGENT=opencode`) run inside the issue's own
workspace clone (`~/ralph-gh-workspaces/<owner>__<repo>--<n>`), with
permission prompts bypassed. The session and the orchestrator talk
through files at the workspace root that are git-excluded so no worker can
commit them: `.ralph-ticket.md` (the issue), `.ralph-siblings.md` (one line
per other item in the project, context only), `.ralph-pr-body.md` (the PR
description the implement session writes), `.ralph-pr-comments.md` (the
review comments the address session reads), `BUILD_OK` / `BUILD_FAIL`.

1. **Claim.** Assign the issue to you, Status → In Progress, comment.
2. **Implement.** Clone/refresh the workspace, cut `feature/issue-<n>` from
   the target branch (`feature/<repo>-<n>` when the issue lives in a different
   repo than the code). The worker reads the issue, implements test-first,
   runs the same commands the repo's GitHub Actions workflow runs, and writes
   `BUILD_OK` + the PR body, or `BUILD_FAIL` with a four-line plain-English
   reason. A session that leaves neither died mid-stream (API stall, DNS
   blip): it is retried once from a clean tree, and if it keeps dying the
   issue is **requeued** — back to Ready, unassigned, no human needed — up to
   `RALPH_TRANSIENT_PARK_CAP` times before it is parked.
3. **Push + guard.** Push with `--force-with-lease`, then prove the branch is
   cleanly based on the target: the recorded base commit is still an ancestor
   of both sides and the PR would show **none of anybody else's commits**. If
   the default target fails the guard but exactly one other long-lived branch
   passes it, the PR is retargeted there with a comment; otherwise the issue
   is parked with the branch pushed.
4. **Open a draft PR** titled `<issue title> (#<n>)`, body = the worker's
   description under a `Closes #<n>` line. Status → In Review.
5. **Verify checks.** Poll the check runs and commit statuses on the branch
   head. Red → a *pipeline-fix* session reads the failing run with
   `gh run view --log-failed`, fixes, re-runs the build, commits; re-push;
   capped at `FIX_CAP`. Still red → the PR stays a draft and the issue is
   parked `needs-human`. A repository with no CI at all passes on the local
   build gate.
6. **Mark ready for review**, then a *review* session posts each finding as
   its own PR comment (`LGTM — no blocking issues.` when there are none). A
   review that posts nothing is reported honestly as UNREVIEWED.
6b. **Collaudo** (opt-in, `RALPH_COLLAUDO=1`): an acceptance run of the PR
   against the app running locally, following the shipped
   [`collaudo-locale`](skills/collaudo-locale/SKILL.md) skill — drive the
   states the PR's screens need, walk the UI in a real browser, judge every
   expectation from the screen or the payload. Findings land as `collaudo:`
   PR comments; the next phase fixes them together with the review. See
   [Collaudo](#collaudo-acceptance-run-against-the-local-app).
7. **Address.** One session works through every comment — review findings
   and `collaudo:` findings alike: fix it (with a test) or reply why not;
   code changes are pushed and re-verified.
8. **Hand-off, or auto-merge.** By default: comment "ready for a human to
   merge" and the PR is yours. With `RALPH_AUTO_MERGE=1` the loop merges the
   PR itself (`RALPH_MERGE_METHOD`, squash by default) when **all** of these
   hold at this moment: the address pass completed, no inline review thread
   is left without a fix or a reply (the address worker replies in the
   thread when it fixes something), the checks on the head being merged are
   green, and GitHub itself calls the PR `clean` (so branch protection, a
   required human review or a conflict still block it). Any doubt — or a
   merge the API refuses — is reported on the issue and the PR is handed to
   you as before. An auto-merge closes the issue at once; the sibling
   rebase happens on the next poll, like a manual merge. Never enable it on
   a repo where you want to read every diff before it lands.

A branch is kept **fresh** at two points: right after the implement session,
before anything is pushed or a PR exists, and right before the hand-off
comment. If the target moved on in the meantime (a sibling merged), the
branch is rebased onto it; a rebase that stops on conflicts gets one Claude
session to finish it, and if that fails the rebase is aborted and the issue
is parked `needs-human` with the branch untouched (pushed as it is when no PR
exists yet, so nothing is lost). A rebase before the hand-off re-verifies
the checks on the new head. The PR you are handed is mergeable at that
moment, not as of when it was opened.

Meanwhile, every poll (`POLL_SECONDS`, default 5 min):

- **Merges close issues.** A merged PR moves its issue to Done and closes it,
  which unblocks dependants. The loop's other open PRs on the same repo are
  rebased onto the target and force-pushed; a conflicting rebase gets one
  Claude session to resolve it, and is aborted (branch untouched, issue
  labelled `needs-human`) if that fails.
- **Edited issues resync.** An issue edited after its branch's last commit
  (with a grace of `RALPH_RESYNC_GRACE` seconds) gets one catch-up session
  that re-reads the acceptance criteria and closes only the gap, capped at
  `RALPH_RESYNC_CAP` passes. The loop's own comments and status changes do
  not count as edits.
- **Idle is explained.** When nothing is grabbable, one log line per open
  item says why (`status=In Progress`, `no ready-for-agent label`,
  `blocked-by acme/app#3`, `assigned:you`).
- **A watchdog** kills a pipeline that has run for `3 × SESSION_TIMEOUT`
  and releases its ticket like any other interruption.
- **Interrupted pipelines are released.** A pipeline whose process is gone
  without a verdict (the previous run was stopped with Ctrl-C, crashed, or
  the machine rebooted) has left its ticket In Progress and assigned, which
  the frontier would otherwise read as "somebody is on it" for ever. The
  poll gives it back: with a PR open the loop simply resumes tracking it;
  without one the ticket returns to Ready, unassigned, and any committed but
  unpushed work is pushed to the branch first so nothing is lost. Each
  interruption counts as a transient death towards
  `RALPH_TRANSIENT_PARK_CAP`.

Parking = `needs-human` label + a comment saying exactly what to decide, and
the issue back in Ready so you see it. The loop never picks up an issue that
carries `needs-human`; remove the label after acting.

## Running on OpenCode (or Kilo)

The worker sessions can run on [OpenCode](https://opencode.ai) instead of
Claude Code:

```bash
export RALPH_AGENT=opencode
export RALPH_MODEL=anthropic/claude-sonnet-4-5   # provider/model, OpenCode's format
ralph-gh <you>/3
```

What changes under the hood:

| | `RALPH_AGENT=claude` (default) | `RALPH_AGENT=opencode` |
|---|---|---|
| command | `claude -p <prompt> --permission-mode bypassPermissions --add-dir <ws>` | `opencode run --auto --dir <ws> <prompt>` |
| style prompt | `--append-system-prompt` | prepended to the message (OpenCode has no system-prompt flag) |
| model flag | `--model <name>` | `--model <provider/model>` |
| skills | `/tdd`, `/code-review` from `~/.claude/skills` | same folders — OpenCode reads `~/.claude/skills` and loads a skill with its `skill` tool; the prompts say so |
| permissions | bypassed | `--auto` approves everything not explicitly denied in `opencode.json` |

`RALPH_AGENT_EXTRA_ARGS` appends flags to every session in either mode
(`--variant high`, `--agent build`, …). The Kilo CLI is an OpenCode fork with
the same `run --auto` interface, so `RALPH_AGENT=opencode RALPH_AGENT_BIN=kilo`
runs the workers on Kilo; it reads `kilo.json`, not `opencode.json`.
Anything else — `gh`, git, the markers, the pipeline — is agent-agnostic. The
preflight checks that whichever binary you picked is on `PATH`.

## Collaudo: acceptance run against the local app

Headless sessions cannot load the Claude-in-Chrome extension, so the
collaudo's browser is the **Playwright MCP server** (`@playwright/mcp`, needs
`npx`), which every agent can load: Claude Code through a `--mcp-config` the
runner writes, OpenCode and the Kilo CLI through inline config
(`OPENCODE_CONFIG_CONTENT` / `KILO_CONFIG_CONTENT`). Screenshots are written
to `.ralph-collaudo/` in the workspace (git-excluded); esiti go on the PR.

```bash
export RALPH_COLLAUDO=1
export RALPH_COLLAUDO_PROBE='curl -fsS http://localhost:3000/health'   # app must be up
export RALPH_COLLAUDO_URL=http://localhost:3000                        # tell the worker where
export RALPH_COLLAUDO_AGENT=opencode RALPH_COLLAUDO_AGENT_BIN=kilo     # e.g. Kilo drives the browser
ralph-gh <you>/3
```

| Var | Default | Meaning |
|-----|---------|---------|
| `RALPH_COLLAUDO` | `0` | `1` turns the phase on |
| `RALPH_COLLAUDO_REPOS` | *(all)* | space-separated repos the local environment runs; others skip silently |
| `RALPH_COLLAUDO_PROBE` | *(none)* | shell command that must exit 0 before a session is spent (the app is up) |
| `RALPH_COLLAUDO_URL` | *(from the PR recipe)* | where the app answers; set it and the worker starts nothing |
| `RALPH_COLLAUDO_BROWSER` | `playwright` | `playwright` (real UI walk) or `none` (API-level collaudo with curl) |
| `RALPH_COLLAUDO_AGENT` / `RALPH_COLLAUDO_AGENT_BIN` | *(= `RALPH_AGENT` / `RALPH_AGENT_BIN`)* | run the collaudo on a different agent than the workers — Claude workers, Kilo collaudo, say |
| `RALPH_SKILL_COLLAUDO` | `collaudo-locale` | the skill the prompt names; link `skills/collaudo-locale` into `~/.claude/skills` |

The gate is orchestrator-side and cheap: disabled, repo not listed, agent or
`npx` missing, probe failing — each skips the session and (except the first
two) leaves an issue comment saying the PR was **not** collaudato and why. A
collaudo session ends with `COLLAUDO_OK` (`PASS` or `ISSUES <n>`; failing
tests are PR findings, not a failed collaudo) or `COLLAUDO_FAIL` (it could not
run: no slot, app down); the latter is reported on the issue and the pipeline
continues. Nothing in this phase can block a PR.

Concurrent collaudi share one machine, so the skill claims a **slot** (app
port, tunnel port, optional test account from `COLLAUDO_ACCOUNTS`) through
`skills/collaudo-locale/scripts/collaudo-slot.sh`.

## Configuration

Everything is an environment variable.

| Var | Default | Meaning |
|-----|---------|---------|
| `ST_READY` | `Ready\|Todo\|To Do` | Status option(s) an issue is picked from |
| `ST_INPROGRESS` | `In Progress` | Status while a session works |
| `ST_REVIEW` | `In Review\|Review` | Status once the PR exists |
| `ST_DONE` | `Done` | Status when the PR merged |
| `AGENT_LABEL` | `ready-for-agent` | required label; `""` disables |
| `NEEDS_HUMAN_LABEL` | `needs-human` | label a parked issue gets |
| `TARGET_BRANCH` | *(repo default)* | global fallback base/PR-target branch |
| `RALPH_TRUNK_CANDIDATES` | `development develop main master` | branches that may prove the default is a stub |
| `RALPH_WORKSPACES` | `~/ralph-gh-workspaces` | clones, `.state/`, `.logs/` |
| `MAX_CONCURRENT` | `2` | issues in flight at once |
| `POLL_SECONDS` | `300` | poll interval |
| `MAX_RUNTIME` | `18000` | total wall-clock seconds (~5h) |
| `SESSION_TIMEOUT` | `5400` | per worker session |
| `CHECKS_POLL_MAX` / `CHECKS_POLL_WAIT` | `40` / `30` | how long to wait for checks |
| `FIX_CAP` | `2` | auto-fix attempts on red checks |
| `RALPH_TRANSIENT_RETRIES` | `1` | in-pipeline retries of a session that died mid-stream |
| `RALPH_TRANSIENT_PARK_CAP` | `3` | requeues before a transient death parks the issue |
| `RALPH_RESYNC` / `RALPH_RESYNC_CAP` / `RALPH_RESYNC_GRACE` | `1` / `2` / `600` | catch-up passes on edited issues |
| `RALPH_REBASE_RESOLVE` | `1` | let Claude resolve sibling-rebase conflicts |
| `RALPH_DRAFT_PRS` | `1` | open PRs as drafts until checks are green |
| `RALPH_AUTO_MERGE` | `0` | `1` merges a PR itself once the review threads are all answered and the checks are green; off, the PR is handed to a human |
| `RALPH_MERGE_METHOD` | `squash` | `squash`, `merge` or `rebase` for the auto-merge |
| `RALPH_AGENT` | `claude` | `claude` or `opencode` — which CLI runs worker sessions |
| `RALPH_AGENT_BIN` | *(= `RALPH_AGENT`)* | executable override, e.g. `kilo` with `RALPH_AGENT=opencode` |
| `RALPH_MODEL` | *(CLI default)* | `--model` for worker sessions (`provider/model` on OpenCode) |
| `RALPH_WORKER_SYS` | terse-output prompt | style prompt for workers (system prompt on Claude, message prefix on OpenCode); `""` disables |
| `RALPH_AGENT_EXTRA_ARGS` | | extra flags appended to every worker session |
| `RALPH_SKILL_IMPLEMENT` / `RALPH_SKILL_REVIEW` | `tdd` / `code-review` | skills the prompts name |
| `RALPH_GIT_PROTOCOL` | `auto` | `https` (gh credential helper) or `ssh`; auto probes both |

The default worker system prompt asks for maximally compressed progress
narration (it is never read) while forcing **failure text into plain
English** — the `BUILD_FAIL` contents, the PR body and every PR comment are
written for a human. Set `RALPH_WORKER_SYS=""` for ordinary output.

## State and logs

```
~/ralph-gh-workspaces/
├── .state/    one file per issue attribute: <key>.state, .pr, .branch, .repo,
│              .target, .base, .started, .project, .resync_*, .transient_deaths
├── .logs/     <key>-implement.log, -review.log, -address.log,
│              -pipelinefix-N.log, -resync-N.log, -rebase-resolve.log
└── <owner>__<repo>--<n>/   the per-issue clone
```

Stopping the loop (Ctrl-C, SIGTERM, or the `MAX_RUNTIME` limit) ends the
live agent sessions and releases every ticket still claimed, so the project
is left as the loop found it and the next run starts clean.

State outlives runs and is shared by every project ever looped on the
machine; each entry records the project that launched it and every
merge-detect, rebase and resync pass skips entries another project owns. It
also survives being stale: a PR merged by hand between runs is noticed on the
next poll and closes its issue instead of being rebased.

## Development

```bash
uv run --group dev pytest -q      # no network, real git repos in tmp
```

`ralph_gh/` layout: `config` (env knobs) · `text` (issue-body parsing) ·
`github` (gh CLI runner + REST/GraphQL shapes) · `tracker` (Project-backed
tracker facade) · `frontier` (readiness) · `gitrepo` / `gitops` (git and the
branch-base invariants) · `worker` (sessions, markers, prompts) ·
`collaudo` (the acceptance-run gate) · `orchestrator` (the pipeline) ·
`launcher` / `__main__` (wiring, preflight, poll loop). Tests fake every collaborator and a conformance test pins the
fakes to the real classes' method sets.
