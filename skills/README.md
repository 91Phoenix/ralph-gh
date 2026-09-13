# skills/

Claude Code skills the loop's worker sessions may invoke. Each skill is a
folder holding a `SKILL.md` with YAML frontmatter (`name`, `description`) and
the instructions in the body:

```
skills/
└── <skill-name>/
    └── SKILL.md
```

One skill ships here: **`collaudo-locale`**, the acceptance-test procedure
the loop's collaudo phase follows (and a human can run interactively). The
worker prompts refer to three skills by name, all configurable and all
optional (a worker told to use a skill it cannot find is instructed to follow
the same discipline by hand):

| Env var | Default | Used by | Shipped here |
|---|---|---|---|
| `RALPH_SKILL_IMPLEMENT` | `tdd` | implement, pipeline-fix, resync, address | no |
| `RALPH_SKILL_REVIEW` | `code-review` | review | no |
| `RALPH_SKILL_COLLAUDO` | `collaudo-locale` | collaudo | yes |

Skills work under Claude Code (`/name`) and under OpenCode or the Kilo CLI
(the agent's `skill` tool): all three read `~/.claude/skills`.

Install skills for Claude Code by linking each folder into `~/.claude/skills/`:

```bash
ln -s "$PWD/skills/<skill-name>" ~/.claude/skills/<skill-name>
```
