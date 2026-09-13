# skills/

Claude Code skills the loop's worker sessions may invoke. Each skill is a
folder holding a `SKILL.md` with YAML frontmatter (`name`, `description`) and
the instructions in the body:

```
skills/
└── <skill-name>/
    └── SKILL.md
```

Nothing is shipped here yet. The worker prompts refer to two skills by name,
both configurable and both optional (a worker told to use a skill it cannot
find is instructed to follow the same discipline by hand):

| Env var | Default | Used by |
|---|---|---|
| `RALPH_SKILL_IMPLEMENT` | `tdd` | implement, pipeline-fix, resync, address |
| `RALPH_SKILL_REVIEW` | `code-review` | review |

Install skills for Claude Code by linking each folder into `~/.claude/skills/`:

```bash
ln -s "$PWD/skills/<skill-name>" ~/.claude/skills/<skill-name>
```
