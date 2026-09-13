# ChatGPT ↔ Claude coordination prompt (paste into ChatGPT)

> Rafael runs this alpaca-mtf-bot repo with **two AI accounts** — you (ChatGPT) and a Claude Code
> account — alternating whenever one hits a usage limit. The Claude account must be able to
> **quickly review everything you change**. Follow these rules on every piece of work. They extend
> the repo's existing **DURABLE SYNC RULE** in `CLAUDE.md`, which you should also read and obey.

**1. Ship through git, always — nothing lives only in your chat.**
Every change is a commit with a clear conventional-commit message (`feat/fix/docs:` + a one-line
summary) and a PR whose body lists: files touched · what changed · why · gate status (statics /
cold-2nd / board / Gro-GAI). Never push straight to `main`. Never leave a silent uncommitted edit
on disk — if it's worth doing, it's worth committing.

**2. Keep `handoff.md`'s "⏩ pick up here" block current — the moment you ship or align.**
Rewrite the top ⏩ block to say (a) what you just did (PR # / commit), (b) the single exact next
step, and (c) anything unfinished or uncertain. This is the FIRST thing the Claude account reads.

**3. Maintain `logs/CROSS_ACCOUNT_CHANGELOG.md` — one line per meaningful change, newest on top.**
Format: `[YYYY-MM-DD HH:MM PT] <plain-English what changed> — PR #<n> / <short-sha> — <files>`.
This gives the Claude account a linear, skimmable list of everything since it last looked. (Do NOT
log routine auto-generated commits like report syncs — only real changes.)

**4. Never leave dangerous WIP loose.**
If you stop mid-task (usage limit), commit the WIP to a clearly-named branch OR `git stash` it with
a descriptive message, and note it in `handoff.md`. If any change was **rejected** by the board /
review, or is otherwise unsafe to ship, mark it in `handoff.md` as **`QUARANTINED — DO NOT SHIP`**
with the reason, and keep it OFF `main` (local branch only). *(A board-rejected stop-protection
commit once sat local and could have been shipped by accident — this rule exists to prevent that.)*

**5. Log gate outcomes.**
Record board / Gro / GAI verdicts and cold-2nd results in `logs/tb_audit_log.md`, so the Claude
account sees what was actually reviewed — not just what changed.

**6. Flag what you did NOT do.**
Anything started-but-unfinished, or deliberately deferred, goes in `handoff.md` explicitly. Never
leave it implicit.

**Net effect:** the Claude account can `git pull` → read `handoff.md` → read
`logs/CROSS_ACCOUNT_CHANGELOG.md` → skim the PRs, and have a complete, quick-to-review picture of
everything you changed — in under a minute.
