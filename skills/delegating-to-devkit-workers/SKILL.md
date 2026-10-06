---
name: delegating-to-devkit-workers
description: Use when asked to do work "in an environment", "in a VM", "in parallel", "in isolation" or "without touching my checkout", when two or more independent tasks could run at once on separate branches, when a task needs the app running without disturbing the main dev stack, or when asked to create, list, check on, hand work to, or clean up devkit environments.
---

# Delegating to devkit workers

## Overview

devkit creates disposable environments: a machine with the project checked out on its own branch, Docker, and Claude Code with this machine's plugins and tool servers. This session is the **coordinator**. It creates environments, hands each one a task, collects the reports and cleans up. A **worker** is a separate Claude session running inside an environment.

**This skill does not apply inside an environment.** If `devkit` is not on the PATH, or the project folder has a `CLAUDE.local.md` that starts with "Devkit environment", you are a worker: do the task you were given and report. You cannot create or destroy environments.

## When to delegate

| Situation | Do |
|---|---|
| Two or more independent tasks, each on its own branch | one environment per task, run together |
| A task needs the app running and must not disturb the dev stack or the user's checkout | one environment |
| Reading code, a quick question, a change the user wants in the current checkout | do it here; an environment adds only delay |
| Tasks that edit the same files or depend on each other's result | one environment, tasks in sequence |

When the user asks for an environment by name ("do this in an environment", "each in its own VM"), that request wins over this table.

## The loop

```bash
devkit list                                   # what exists; the last line is "N of M environments"
devkit create <name> [branch]                 # about 15s, plus about 35s comparing tool servers
devkit create <name> --no-check               # skip that comparison when the task needs no tool server
devkit task <name> "<brief>"                  # blocks until the worker reports
devkit task <name> --continue "<follow-up>"   # same worker session, keeps its context
devkit report <name>                          # the last worker's report again, any time
devkit ssh <name> -- <command>                # one command, run in the project folder
devkit destroy <name> -y                      # refuses if work inside would be lost
```

Run `devkit --help` for the rest (`stop`, `start`, `check`, `code`).

**Before creating:** check that every environment you need fits under the cap, and run `git log origin/<base>..HEAD --oneline` here. Workers start from the **remote** base branch, so commits that are only on this machine do not exist inside. If the task depends on them, tell the user they must be pushed first.

**Creating several:** one at a time. devkit refuses a second create while one is running.

**Tool server lines at create** (`NOT WORKING inside: …`) are information, not failure. Go on unless the task needs that server; if it does, start the containers it depends on and run `devkit check <name>`.

**Parallel workers:** start each with `devkit task <name> --detach "<brief>"`, then join each with `devkit report <name> --wait`. Where background commands with completion notices are available, running `devkit task <name> "<brief>"` in the background does the same in one step. Use one or the other, not both: they print the same report. A worker keeps running inside if the connection drops; `report --wait` collects it either way.

**Names:** short, lowercase, dashes, describing the task (`fix-order-net`). The name is also the branch unless you pass one. `destroy` leaves no branch behind, here or on the remote.

**The report ends with a footer:** `[worker <id> finished: N turns, Ns]`. `failed` there, or "ended without a report", means the worker did not complete: read `~/.devkit-runs/<id>/errors` inside with `devkit ssh`.

## Writing the brief

A worker starts with the project's instructions and nothing from this conversation. The brief is everything it knows about the task. Write, in this order:

1. **Goal**: what must be true when it is done, and why it matters.
2. **What is already known**: files, findings, decisions, what was ruled out. Paste the specifics; do not say "as discussed".
3. **Boundaries**: what not to touch, and whether to commit and push or leave changes uncommitted for review.
4. **Report**: exactly what to return, and ask for the commit it worked on (`git rev-parse --short HEAD`).
   - For a change: what changed (files), how it was verified (commands and their result lines), what was not done and why.
   - For an analysis: the findings with `file:line` for each, the commands that produced them, and what it judged rather than measured.

Define any term a careful reader could take two ways ("failure path", "unused", "slow"), and say how to rank when you ask for "the worst three".

For read-only work, say so in the boundaries: no edits, no commits, scratch files only under `/tmp`, and no containers, databases or external calls unless the task needs them.

A brief with shell-special characters goes through standard input: `devkit task <name> - <<'EOF' … EOF`.

## Before you report to the user

A worker's report is a claim, not evidence. Check it before repeating it.

For a change, look at what is actually there:

```bash
devkit ssh <name> -- git status --short
devkit ssh <name> -- git log --oneline -5
devkit ssh <name> -- git diff --stat
```

For an analysis, re-run one of the worker's commands or open several of the cited `file:line` references. Checking against this machine's checkout is fine when `git diff --stat <worker's commit> HEAD -- <paths>` is empty for the paths involved. For read-only work, the three git commands above also prove nothing was touched.

Then tell the user: what each worker found or did, which parts you verified yourself and which you are repeating, which environments still exist, and how to look (`devkit code <name>` opens one in VS Code).

## Rules

- **Only destroy environments you created in this conversation, each by its explicit name.** Never loop over `devkit list` and never use `destroy --all`. Any other environment belongs to the user or another session: ask first.
- **Never pass `--force` to `destroy` on your own.** A refusal means work would be lost. Have the worker commit and push, or ask the user.
- **Leave an environment in place when it holds changes the user will want to review.** Destroy when the work is pushed, when the task was read-only and you have reported it, or when the user says so. Say which environments you left running.
- **At the cap, do not make room by destroying.** `devkit list`, then ask the user which to stop or destroy.
- **Workers cannot ask permission questions.** They run in auto mode; an action they are refused is reported back. Do not ask a worker to do something that was refused or denied in this session.
- **Workers reach real things.** An environment uses the shared dev databases and the project's real external credentials. A brief that says "publish", "charge", "send" or "migrate" does it for real.
- **Only pushed branches exist inside.** Local commits on this machine are invisible to a worker until pushed.
- **Each worker is a full Claude session.** Delegate a task, not a step.

## Common mistakes

| Mistake | Result |
|---|---|
| A one-line brief ("fix the bug we found") | the worker rediscovers everything or fixes the wrong thing |
| Repeating the worker's report as fact | "tests pass" with no command behind it reaches the user |
| Destroying to tidy up before the user has looked | the review is gone |
| Starting a second task while one is running | devkit refuses; use `report --wait`, or `--continue` afterwards |
| Creating an environment, unasked, for a two-minute read-only question | about a minute of setup for nothing |
| Assuming the worker saw this machine's unpushed commits | findings about code the user is no longer running |
