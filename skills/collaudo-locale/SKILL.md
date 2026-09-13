---
name: collaudo-locale
description: Collaudo — acceptance test of a pull request against the app running locally, driving the backend states its screens need, walking the UI with a browser tool (Playwright MCP under Claude Code, OpenCode or the Kilo CLI), and recording esiti as PR comments with screenshots on disk. Use when asked to collaudare/test a PR locally, verify a frontend PR end-to-end against the local backend, run several collaudi in parallel on one machine, or when ralph-gh runs its collaudo phase.
---

# Collaudo locale

A **collaudo** is an acceptance run of one PR against the app running locally: build the backend states the PR's screens need, verify every expectation on the real UI (or, for a backend-only PR, on the real payloads), and leave the esiti where the next reader finds them — on the PR. You are testing, not fixing: change no code, commit nothing, push nothing.

This skill is agent-neutral. Under Claude Code it is `/collaudo-locale`; under OpenCode or the Kilo CLI the agent loads it with the `skill` tool (both read `~/.claude/skills`). The browser is the same everywhere — the Playwright MCP server (section 6) — so a headless session can walk the UI too. When ralph-gh runs the collaudo phase it exports `COLLAUDO_DIR` (where evidence goes) and, when the app is already up, `COLLAUDO_URL`; interactively, pick them yourself.

## 1. Read the PR and name the tests

Fetch the PR — `gh pr view <n> --repo <owner/repo> --json title,body,headRefName` — and read its `## How to test manually` recipe and `## Design notes`, then the issue's acceptance criteria (`gh issue view <n> --comments`). A previous collaudo of the same PR left a `collaudo: esiti` comment: read it, its state recipes are the ones that worked. Done when you can name every test you are about to run and the states each one requires.

## 2. Claim a slot

One machine, one database, one dev-server port: nothing is namespaced per tester, and two collaudi on the same account overwrite each other's state. A **slot** is one collaudo's private lane — an app port, a tunnel port, an account from the pool — claimed atomically so concurrent sessions agree on nothing and you pick no numbers:

```
eval "$(bash ~/.claude/skills/collaudo-locale/scripts/collaudo-slot.sh acquire <pr-branch>)"
```

It exports `COLLAUDO_SLOT`, `COLLAUDO_PORT`, `COLLAUDO_FWD_PORT` and — when `COLLAUDO_ACCOUNTS` lists test accounts, one per slot — `COLLAUDO_ACCOUNT`. Use the variables everywhere below in place of a literal port or account. `list` names the held slots, `release $COLLAUDO_SLOT` gives yours back in step 7, and `exclusive` exits 0 only when nobody holds one — the guard for anything that resets the shared database or restarts the whole stack. Leases live under `~/.ralph-gh/collaudo-leases`; a slot whose ports stopped listening is reclaimed after 30 minutes, so a crashed session frees itself. If no slot is free, stop and say so (under ralph-gh: write `COLLAUDO_FAIL`).

## 3. Backend up — only what the PR touches

If `COLLAUDO_URL` is set, the app is already running: use it and start nothing. Otherwise bring the backend up with **the commands that are actually in the repo** — `make`, `docker compose up <service>`, the script the PR's recipe names — never an invented one, and start only the services the PR's surface needs: the rest costs RAM another slot is using. A cold stack's bootstrap or a seed reset belongs to no slot and wipes every other one, so run it only when `collaudo-slot.sh exclusive` exits 0.

Wire and check the entry point before anything else, and re-check it before blaming credentials — a dead tunnel surfaces as a 503 on login:

```
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:$COLLAUDO_FWD_PORT/<health-route>"
```

When the PR's own backend build must run (its change is server-side), build and run *that* branch on your slot's port rather than replacing the shared instance another slot reads; scale nothing shared down unless you are alone (`exclusive`).

## 4. Frontend on the PR branch — when the PR has a UI surface

A backend-only PR skips this step and step 6: its collaudo is steps 5 and 7 with the API as the screen — drive the states, read the routes the PR changes with `curl` and the test account's token, and the payloads are the evidence.

For a frontend PR the working copy is already on the PR branch (ralph-gh's workspace, or a checkout of your own — never `git checkout` in a tree another slot is serving). Install and start the dev server **on your slot's port, explicitly**: a dev server that finds its port taken often answers "skipping" and looks started. Clear the bundler cache when switching branches, copy in any untracked env file the README names, and poll `curl` until the port answers 200 before opening the browser. When you restart it, kill only your own listener: `kill $(lsof -tiTCP:"$COLLAUDO_PORT" -sTCP:LISTEN)`.

## 5. Drive the backend states

The seed data is the floor you build on; the PR's recipe says what it does *not* create. Drive your slot's account and rows only, and leave the other slots' alone. Prefer the app's **real writers** (its API, its scheduled jobs) over direct database edits — forcing a row is legitimate when one injection loop gets you there, but an esito that rests on a state the app can never produce proves nothing. Write the recipe you used down as you go: it becomes part of the esiti.

Before blaming the app, read what the backend actually serves: log in with the test account via `curl` and inspect the route behind the screen. A collaudo that finds the recipe wrong records the corrected recipe.

## 6. Walk the UI with the browser

The browser is the **Playwright MCP server** (`@playwright/mcp`), whichever agent you are in:

- **ralph-gh** wires it into the collaudo session itself (`--mcp-config` for Claude Code, inline config for OpenCode/Kilo) with `--output-dir "$COLLAUDO_DIR"`.
- **Interactive OpenCode / Kilo CLI**: register it once in `opencode.json` / `kilo.jsonc`:

```json
{ "mcp": { "playwright": { "type": "local", "command": ["npx", "-y", "@playwright/mcp@latest", "--output-dir", "/path/to/collaudo/screenshots"] } } }
```

- **Interactive Claude Code**: `claude --mcp-config` with the same server, or the `claude-in-chrome` extension if it is connected (`tabs_context_mcp` first; screenshots with `save_to_disk: true`; `zoom` for crops; `read_network_requests` for payloads).

With Playwright: `browser_navigate` to `http://localhost:$COLLAUDO_PORT`, log in as `$COLLAUDO_ACCOUNT` (the profile is fresh every session), `browser_snapshot` for the accessibility tree — its refs are the click targets, there are no pixel coordinates — then `browser_click` / `browser_type` by ref. `browser_network_requests` lists the calls since the page opened and `browser_network_request` returns one call's headers and body; `browser_console_messages` the console.

- Reach deep screens by **direct URL**: click-chains lose taps fired before hydration, and reloads reset in-page navigation.
- **Screenshot every verified expectation as a file** — `browser_take_screenshot` with a `filename`; an image you only looked at cannot be checked by anyone else. Pass the card's `ref` as the element for a crop, or `browser_resize` to a narrow viewport first: a full page renders the sentence under test too small to read.
- **Name the files for the test they prove as you go** (`03-message-when-probe-already-assigned.png`) under `$COLLAUDO_DIR`. Timestamped names mean that at the end of a long run nobody can tell which of eleven `page-1787585….png` is which.
- Judge payloads from the network, not from the screen — a screen can render a stale value.
- A write action (save, suspend) that the test needs is part of the collaudo; reverse it afterwards through the API or the state recipe.

Done when every test has a pass/fail decided by something you saw on screen or in a payload — never by the code looking right.

## 7. Record esiti and file the findings

The esiti live **on the PR**, as comments prefixed `collaudo: ` so a later session (ralph-gh's address pass) can tell them from review comments:

- **one comment per issue found** — wrong payload, mishandled state, acceptance criterion not met — with the exact reproduction (route, state recipe, account) and observed vs expected. Each must stand alone: they are fixed one by one, possibly by a session that never saw the app.
- **one closing comment**: `collaudo: local acceptance run passed.` followed by the esiti, or `collaudo: esiti` with the full one-line-per-test table (what was verified, how, pass/fail), the state recipes that worked, and the screenshot file names.

Screenshots stay on the tester's machine (`$COLLAUDO_DIR`, copy the folder somewhere durable: `~/ralph-gh-collaudo/<repo>-<pr>/`); GitHub's API cannot attach images to a comment, so name them in the esiti and drag the ones that matter into the PR by hand. A finding that belongs to another repo becomes an issue in that repo, linked from the comment.

Then reverse every write your tests needed, hand the slot back (`collaudo-slot.sh release $COLLAUDO_SLOT`) and kill the dev server and tunnels you started — also when the collaudo failed.

**Under ralph-gh, end with exactly one marker file at the repo root** — the orchestrator reads only these: `COLLAUDO_OK` when the collaudo *ran to completion* (first line `PASS` or `ISSUES <n>`, then one line per test; failing tests are findings on the PR, not a reason for the other file), `COLLAUDO_FAIL` only when it *could not run* (app would not start, no free slot, surface not exercisable locally) — plain English: what blocked you, the decisive error line verbatim, what a human must do.

The collaudo is complete when the PR tells the next tester both *how* to reproduce and *what happened last time*.

## Concurrency ceiling

Slots are cheap on the cluster side and expensive on the host: each frontend slot is a dev server plus a browser, roughly a gigabyte. Read the real numbers (`docker stats`, Activity Monitor) before opening a third slot, and remember the shared database is one: two slots driving the same account are one collaudo with two witnesses, not two collaudi.
