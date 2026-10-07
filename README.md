# devkit

Disposable development environments. Each one is a Proxmox container cloned from a template, with Docker and Claude Code inside, your branch checked out, and the app's settings pushed in. Create one in about ten seconds, work in it, destroy it.

- What is planned: [ROADMAP.md](ROADMAP.md)
- How the template is built: [TEMPLATE.md](TEMPLATE.md)

## Usage

Run from inside a project that has a `.devkit.toml` (or pass `-C <project folder>`).

```bash
devkit create fix-orders            # branch fix-orders; cut from the base branch if it is new
devkit create review feature/x      # environment "review" on an existing branch
devkit list                         # name, branch, address, state, memory, uptime
devkit list --json                  # name, branch and state for scripts
devkit ssh fix-orders               # shell in the project folder
devkit ssh fix-orders -- make test  # run one command and return
devkit claude fix-orders            # Claude Code in the project folder
devkit claude fix-orders -- -p "summarise the last commit"
devkit task fix-orders "<brief>"    # hand a task to a Claude worker inside; prints its report
devkit task fix-orders --continue "<follow-up>"   # same worker session
devkit task fix-orders --detach "<brief>"         # return at once
devkit report fix-orders --wait     # collect a detached or interrupted worker
devkit report fix-orders --json     # state and report for scripts: none, running, finished, failed, timeout, stopped
devkit ship fix-orders -m "fix: ..." # commit everything inside and push its branch
devkit code fix-orders              # open the environment in VS Code over Remote-SSH
devkit check fix-orders             # compare tool servers here and inside
devkit stop fix-orders              # free its memory, keep its files
devkit start fix-orders             # bring it back; containers inside are not restarted
devkit destroy fix-orders           # refuses if work inside would be lost; asks first
devkit destroy --all                # every environment that has nothing to lose
```

Names are lowercase letters, digits and dashes, at most 30 characters.

### What `create` does

1. Refuses if the environment cap is reached or another create is running.
2. Clones the template, starts it, waits for SSH.
3. Fetches the branch from the remote inside the environment.
4. Pushes the app's settings file, rewritten as the project config says.
5. Pushes the Claude token and tool server keys to a separate protected file.
6. Registers the project's tool servers for Claude and marks the workspace trusted.
7. Pushes the project's note for Claude as `CLAUDE.local.md`.
8. Mirrors this machine's Claude setup: plugins, skills, settings and global instructions. Stored logins are never copied.
9. Compares tool servers: every one that connects on this machine should connect inside. Differences are printed. `--no-check` skips this.

If a step fails, the half-made environment is removed.

### Working inside

- No containers are started for you. Start what the task needs, with `--no-deps`:
  `docker compose up -d --no-deps --no-build <service>`
- The project's `CLAUDE.local.md` says which services are shared and which must never be started.
- Only branches pushed to the remote reach an environment.
- `git push` works from `devkit ssh` and `devkit claude` sessions, through SSH agent forwarding.
- `destroy` refuses when the checkout has uncommitted files or unpushed commits, and when the environment is stopped and cannot be checked. `--force` overrides; `-y` only skips the question.
- Remote control and cross-session messaging do not work inside: both need a full claude.ai login, and environments log in with a long-lived token that can only make model requests.
- The user inside has `sudo`.

### Workers

- `devkit task` starts a separate Claude session inside the environment and returns its final message.
- The worker runs detached: if the connection drops it keeps going, and `devkit report <name> --wait` collects it.
- One worker at a time per environment. `--continue` sends a follow-up to the same session, also after `stop` and `start`.
- A worker is stopped after one hour; change it with `[worker] timeout_seconds` in the server config.
- Workers cannot ask permission questions. They run with `--permission-mode auto`; change it with `[worker] claude_args` in the server config.
- A Claude session on this machine coordinates workers through the `delegating-to-devkit-workers` skill.

### Tool servers inside

- Nothing lists plugins or tool servers. Add a plugin on this machine and the next environment has it.
- Servers that need a browser login show "needs login" inside. Give them a key through `[[tool_servers]]`, or leave them out.
- A server can depend on a running container (one that reads a token from the backend, for example). It fails the check until that container is started; run `devkit check <name>` again afterwards.
- "Connected" means the server started, not that every tool in it works.

## Setup

### Install

```bash
ln -s ~/devkit/devkit.py ~/.local/bin/devkit
ln -s ~/devkit/skills/setting-up-devkit ~/.claude/skills/setting-up-devkit
ln -s ~/devkit/skills/delegating-to-devkit-workers ~/.claude/skills/delegating-to-devkit-workers
```

Python 3.11 or newer. No dependencies.

### Server config: `~/.config/devkit/config.toml`

```toml
[proxmox]
host = "192.168.1.50"
node = "dev"
pool = "devkit"
template = 107

[limits]
max_environments = 3                # running ones; stopped environments do not count
min_free_memory_mb = 2048           # optional: create and start refuse below this

[ssh]
user = "dev"
key = "~/.ssh/devkit_ed25519"       # reaches environments
github_key = "~/.ssh/id_ed25519"    # lent to environments through agent forwarding
authorize = "~/.ssh/authorized_keys" # optional: these public keys may log into environments
host_key = "~/.config/devkit/host_ed25519"  # optional: one SSH identity for every environment, so a reused
                                    # address never trips "host identification has changed"; made if missing

[secrets]
file = "~/droppo-v2/.env"           # where secret values are looked up
proxmox_token_key = "PVEAPIToken"
claude_token_key = "CLAUDE_CODE_OAUTH_TOKEN"

[variables]
shared_host = "192.168.1.102"       # usable as {shared_host} in any .devkit.toml

[claude]
mirror = true                       # default; false keeps this machine's Claude setup out
remote_control = false              # default; only useful once an environment has a full claude.ai login

[worker]
claude_args = ["--permission-mode", "auto"]   # default flags for `devkit task` workers
timeout_seconds = 3600                         # a worker is stopped after this long

[network]                           # optional; without it environments use DHCP
address = "192.168.1.{vmid}/24"     # container 104 gets 192.168.1.104
gateway = "192.168.1.1"
```

- The Proxmox token needs `PVEVMAdmin` on the pool, `PVEDatastoreUser` on the container storage, `PVESDNUser` on the network zone and `PVEAuditor` on the node.
- The Claude token comes from `claude setup-token`.
- With `[network]`, keep that address range out of the router's DHCP pool.

### Project config: `<repo>/.devkit.toml`

Ask Claude to use the `setting-up-devkit` skill, or write it by hand:

```toml
repo = "git@github.com:owner/app.git"
base_branch = "development"
workdir = "/home/dev/app"           # where the repo sits inside the template

note = """
Text for the Claude session inside: shared services, how to run tests, what never to start.
"""

[settings]
source = ".env"                     # on this machine
target = ".env"                     # inside workdir
drop = ["TOOLING_ONLY_TOKEN"]       # never pushed

[settings.force]                    # set to exactly this, added if missing
MYSQL_HOST = "{shared_host}"
EMAIL_SEND_ENABLED = "false"

[settings.replace]                  # plain-text replace inside every other value
"@mongodb:" = "@{shared_host}:"

[[tool_servers]]                    # only services that take a key in a header
name = "posthog"
url = "https://mcp.posthog.com/mcp"
header = "Authorization: Bearer {secret}"
secret_key = "POSTHOG_ENV_API_KEY"  # looked up in the secrets file
```

- devkit always strips its own secrets from the pushed file: the Proxmox token, the Claude token and every tool server key.
- A tool server whose key is missing is skipped with a message.
- A `replace` rule that matches nothing prints a warning.

## Tests

```bash
cd ~/devkit && PYTHONPATH=. python3 -m unittest tests.test_devkit
```
