---
name: setting-up-devkit
description: Use when asked to set up devkit for a repository or folder ("set up devkit for ~/some-repo"), when a repository needs a .devkit.toml, when adding a project to devkit, when `devkit` reports "no .devkit.toml found", when a devkit environment starts with wrong settings, leaked tokens or a missing tool server, or when asked which settings, secrets or MCP servers a disposable environment should receive.
---

# Setting up devkit for a repository

## Overview

devkit creates disposable environments: a Proxmox container cloned from a template, with Docker and Claude Code inside. The tool holds nothing about any project. Everything project-specific lives in one file, `.devkit.toml`, at the root of the app repo. This skill writes and checks that file.

**Core principle:** an environment gets only what its work needs. Every setting, secret and tool server it receives is named in `.devkit.toml`; nothing else leaves the dev machine.

## The whole setup, start to finish

The repository is the argument: the path the user names, or the current folder. Work in that folder for every step; pass `-C <repo>` to devkit when it is not the current one.

1. **Write `<repo>/.devkit.toml`** as the sections below describe. Ask the user only the decisions that are theirs.
2. **Stop once and ask the user to type one line**, exactly: `! devkit -C <repo> setup`
   - Typed with `!` it runs as the user. It installs whatever the machine is missing and adds the permission rules to that project. A Claude session cannot do this step: adding its own permissions is refused, and so is every devkit command until they exist.
   - Read its output. Any `NEEDS YOU` line is the user's to resolve; say what it means and wait.
3. **Offer a project template** when the project builds Docker images or installs dependencies: add `[template] prepare` to the file (see below) and run `devkit -C <repo> template build`. If the command is refused, ask the user to type it with `!`. It takes a few minutes and makes every later `create` fast. Skip it for a project that needs nothing prepared.
4. **Verify** with a probe environment, as the Verify section describes, and destroy the probe.
5. **Report**: what the environment will receive, what is switched off, what is still live, and that `.devkit.toml` and `.claude/settings.json` are ready to review and commit.

Do not run `devkit setup` yourself, and do not edit `.claude/settings.json` to add the rules. If a devkit command is refused, the user has not run step 2 yet.

## Where configuration lives

| What | Where | Holds |
|---|---|---|
| The tool | `~/devkit/` | no project or server details |
| The server | `~/.config/devkit/config.toml` | Proxmox address, pool, template id, cap, SSH keys, secrets file, `[variables]` |
| The project | `<repo>/.devkit.toml` | everything below |

Read `~/.config/devkit/config.toml` first:

- `[variables]` (for example `shared_host`) are placeholders. `{shared_host}` is expanded everywhere in `.devkit.toml`, including `note`, `force` values and `replace` rules.
- `[secrets] file` is where secret values are looked up. It is usually the same file as `settings.source`.
- devkit always strips its own secrets from the pushed file: the Proxmox token, the Claude token, and every tool server's `secret_key`. Do not list those in `drop`.

## The file

```toml
repo = "git@github.com:owner/app.git"
base_branch = "development"
workdir = "/home/dev/app"

note = """
# Devkit environment
- Databases are shared, at {shared_host}. They are not started here.
- Start only what the task needs, always with `--no-deps`.
- Never start: <schedulers, databases, migration jobs>.
"""

[settings]
source = ".env"
target = ".env"
drop = ["SOME_TOOLING_TOKEN"]

[settings.force]
MYSQL_HOST = "{shared_host}"
EMAIL_SEND_ENABLED = "false"

[settings.replace]
"@mongodb:" = "@{shared_host}:"

[[tool_servers]]
name = "posthog"
url = "https://mcp.posthog.com/mcp"
header = "Authorization: Bearer {secret}"
secret_key = "POSTHOG_ENV_API_KEY"
```

## Filling each field

| Field | How to derive it |
|---|---|
| `repo` | `git remote get-url origin` |
| `base_branch` | the branch feature work is cut from, not necessarily the default branch; look at where recent feature branches merged, or ask |
| `workdir` | the path the repo has inside the template; ask if unknown, a wrong path fails at create |
| `settings.source` | the env file the app reads on the dev machine |
| `settings.target` | where that file goes inside `workdir`; normally the same name |
| `note` | pushed as `CLAUDE.local.md`; written for the Claude session inside |

### Classify every variable in the env file

Never print values. Work from names and from shape checks that reveal no secret:

```bash
grep -oE '^[A-Za-z_][A-Za-z0-9_]*' .env                          # names
grep -c '@mongodb:' .env                                          # does a replace rule match?
sed -nE 's#^([A-Za-z_]+)=.*[@/]([A-Za-z0-9_.-]+):[0-9]+.*#\1 -> \2#p' .env   # host part only
grep -rhoE "getenv\([^)]*\)" app | sort -u                        # defaults that live in code
```

For each name, find who reads it: `grep -rl NAME` over the app code, the scripts and the compose file.

| The variable is | Do | Why |
|---|---|---|
| Read only by tooling the app never runs (automation scripts, CI, issue mirrors), or by nothing at all | `drop`, together with its non-secret companions | an environment must not hold credentials its work does not use |
| An address of a service the environment will not run (database, vector store) | `force` the host, or `replace` inside a URL | compose service names do not resolve when that service is not started |
| An outward side effect with a switch: email, error reporting, analytics, tracing | `force` the switch off, using the exact value the code tests for | a test environment must not mail customers or pollute dashboards |
| An outward side effect with no switch, only a URL or credential: chat webhooks, spreadsheet targets, error-reporting addresses | `force` it to `""` after confirming the code treats empty as "skip" | same |
| An address of a service started inside the environment (queue, cache) | leave | it resolves locally |
| Read by compose only for services on the never list (database root passwords, migration settings) | leave | removing them breaks compose parsing; the never list is the control |
| A credential the app needs to do real work: marketplace, payment, storage, AI provider | **ask the user** | see below |
| Anything else | leave | |

To tell "will not run" from "started inside", read the compose file and decide per service.

Rules of the mechanism:

- `replace` is plain text, applied to every value that is neither dropped nor forced. A rule that matches nothing prints a warning at create; treat that warning as a failure.
- `force` wins over `replace` for the same key. A forced key missing from the source is appended, which is how a default that lives in code gets overridden.
- An empty forced value (`KEY = ""`) is valid.

### Decisions that belong to the user

Do not decide these silently. List them, say what each allows, and ask:

- **Live credentials the app uses** (marketplaces, payments, storage, AI provider): leaving them means a publish, a charge or an upload from an environment is real; dropping them means those flows cannot be exercised there.
- **Public addresses** (base URL, webhook URL): an environment completing an external connection could register an address against a real account.
- **Auto-login or debug switches**: convenient for browser checks, and they act as a real user on shared data.
- **Whether the app runs data migrations on boot**: against shared databases, a branch's migration is applied for everyone.

Record the outcome under "Still live" in the note.

### Template preparation

`[template] prepare` is a list of shell commands that `devkit template build` runs once, in the project folder, as the environment's user. Whatever they leave on disk is in every environment from then on.

```toml
[template]
prepare = [
    "docker compose build -q app",
    "for s in worker scheduler; do docker tag myproject-app myproject-$s; done",
    "docker compose pull -q redis",
]
```

- **Put the slow, repeatable things here:** building the app's image, pulling the images of services environments run themselves, installing dependencies into a cache.
- **Build each image once.** When several compose services build from the same Dockerfile, build one and tag it for the others; compose names images `<folder>-<service>`.
- **Only the services environments start.** Not the shared databases, not their migration jobs.
- **No secrets are available.** The settings file exists during preparation but every value is blank, and it is removed before the template is frozen. A command that needs a real credential cannot go here.
- **Leave it out** when nothing needs preparing: `create` then starts from the base and clones the repo.
- `name` at the top of the file names the project's template; it defaults to the folder name. `[environment] memory_mb` and `cores` override the server's size for this project.

### The note

State, for the session inside: which services are shared and where, the smallest set of containers that runs the tests (confirm it by running them, see Verify), what is switched off, what is still live, and the list that must never start.

Build the never list by opening **every** service in the compose file, not from service names:

- anything that fires jobs on a timer, including a worker whose command also starts a scheduler
- the shared databases and their migration jobs
- backup and restore jobs
- a bare `docker compose up`, which reaches all of the above through `depends_on`

A scheduler in an environment runs real jobs against shared data and real external accounts.

### Tool servers

Only services that accept a key in a request header fit. Browser-login-only services cannot be expressed and stay out. Stored logins are never copied from the dev machine.

- `{secret}` in `header` is replaced by a reference to `secret_key`; the value is read from the secrets file and written only to a protected file in the environment.
- A missing secret skips that server with a message; create still succeeds. Check the key exists in the secrets file (`grep -c '^NAME=' file`); if it does not, tell the user to create it.
- Use a key made for environments, separate from any automation key, with the narrowest scopes that work. PostHog's server rejects keys lacking `user:read`.
- List only what work inside an environment needs.

## Verify

```bash
devkit create probe <base_branch> --no-check
devkit ssh probe -- bash -c 'for k in <dropped keys>; do echo "$k $(grep -c "^$k=" .env)"; done; grep -E "^(<forced keys>)=" .env; cat CLAUDE.local.md | head -5'
devkit claude probe -- mcp list
devkit destroy probe -y
```

Every dropped key must count 0, the create output must show no `warning:` line, and each shared service must be reachable from inside (`timeout 2 bash -c 'echo >/dev/tcp/<host>/<port>'`). Then start the smallest container set named in the note and run the project's tests inside before calling the file done.

## Facts that shape the file

- No containers are started at create. The compose file is used unchanged, with `--no-deps`.
- Only branches pushed to the remote reach an environment; a new branch is cut from `base_branch`.
- `create` starts from the project's template when `devkit template build` has made one, and from the base otherwise, cloning the repo. `~/devkit/TEMPLATE.md` explains both.
- The cap on environments is in the server config. `devkit list` shows usage.
- Each environment is its own machine, so fixed container names and published ports in the compose file do not collide.
- Shared services must be published on the dev machine's network address, not only on its internal Docker network.
- `destroy` refuses when uncommitted or unpushed work is inside; `--force` overrides.

## Common mistakes

| Mistake | Result |
|---|---|
| Printing the env file to classify it | secrets in the transcript; read names and shapes only |
| Guessing a `replace` rule from an example | it matches nothing and the app targets a dead host; check with `grep -c` |
| Deciding alone to keep live payment or marketplace credentials | real charges from a test environment; ask |
| Leaving a tooling token in the pushed file | every environment can act as that tooling |
| Rewriting the queue or cache address to the shared host | environments consume each other's and the dev machine's tasks |
| Forgetting defaults that live in code | the app silently targets a host that does not exist |
| A server's address written literally | use a `[variables]` placeholder so the file works on another server |
| Omitting the scheduler from the never list | timed jobs run twice, against real accounts |
