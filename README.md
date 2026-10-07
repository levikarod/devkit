# devkit

Disposable development environments on Proxmox, with Docker and Claude Code inside.

## Quick start

```bash
python3 ~/devkit/devkit.py setup    # once per machine; repeat until it says everything is in place
devkit template build               # once per server: the base every environment starts from (about 3 minutes)

cd <project>                        # a project with a .devkit.toml
devkit setup                        # lets Claude sessions in this project use devkit
devkit template build               # optional: a template with this project prepared, for 15-second creates

devkit create fix-orders            # an environment on branch fix-orders
devkit code fix-orders              # open it in VS Code
devkit claude fix-orders            # or work in it with Claude Code
devkit task fix-orders "<brief>"    # or hand a task to a Claude worker inside
devkit destroy fix-orders           # refuses if work inside would be lost
```

Or tell Claude: **"set up devkit for ~/some-repo"**. The `setting-up-devkit` skill writes the project's `.devkit.toml`, asks you to type one `devkit setup` line, and verifies the result.

- Before the first run you need a Proxmox API token and a Claude token: see [Before you start](#before-you-start).
- How templates are built, and the one step that needs root: [TEMPLATE.md](TEMPLATE.md).
- What is planned: [ROADMAP.md](ROADMAP.md).

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
devkit template build               # in a project: its template; elsewhere or with --base: the base
devkit template list                # templates on the server, and which one this project uses
devkit template clean               # remove containers left by failed builds
```

Names are lowercase letters, digits and dashes, at most 30 characters.

### What `create` does

1. Refuses if the environment cap is reached or another create is running.
2. Clones the project's template, or the base when the project has none, starts it, waits for SSH. Applies the size from `[environment]` when one is set.
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

### Before you start

- **A Proxmox server** and a pool for devkit's containers. As root on the server:

  ```bash
  pvesh create /pools --poolid devkit
  pveum user add devkit@pve
  pveum user token add devkit@pve devkit            # prints the token; keep it
  G='--users devkit@pve --tokens devkit@pve!devkit'
  pveum acl modify /pool/devkit            $G --roles PVEVMAdmin
  pveum acl modify /storage/local-lvm      $G --roles PVEDatastoreUser
  pveum acl modify /storage/local          $G --roles PVEDatastoreAdmin
  pveum acl modify /sdn/zones/localnetwork $G --roles PVESDNUser
  pveum acl modify /nodes/<node>           $G --roles PVEAuditor
  ```

  The token can only touch containers in that pool. Put it in your secrets file as `PVEAPIToken=devkit@pve!devkit=<secret>`.
- **A Claude token** for sessions inside environments: run `claude setup-token` and put the result in the same file as `CLAUDE_CODE_OAUTH_TOKEN=...`.
- **An SSH key that can reach your git host**, on this machine. It is lent to environments through agent forwarding and never copied.

### Install

```bash
python3 ~/devkit/devkit.py setup      # first time, anywhere: installs the machine parts
cd <project> && devkit setup           # in each project that has a .devkit.toml
```

Run it yourself, in a terminal. It installs what it can and tells you what is left; run it again until it says everything is in place. It is safe to repeat.

| It checks | And, when missing |
|---|---|
| the `devkit` command on your PATH | links it |
| the two skills in `~/.claude/skills` | links them |
| Claude Code permission to run devkit, per project | adds allow rules to the project's `.claude/settings.json`; review and commit that file |
| the server config | writes a blank one for you to fill in |
| the SSH keys, the secrets file, the Claude token | says which is missing |
| Proxmox access and a template | says what is wrong, or that `devkit template build` is next |

- The permission rules cover `create`, `list`, `check`, `task`, `report`, `ssh`, `claude`, `code`, `stop` and `start`. `destroy` and `ship` are left out on purpose, so Claude Code still reviews them.
- Permissions are per project, next to the `.devkit.toml` that makes them meaningful: sessions working in that project may create environments, sessions elsewhere may not. Outside a project, setup skips this step and says so.
- Machine-wide devkit rules left by an earlier version are removed from `~/.claude/settings.json`, with a backup.
- Without those rules, Claude Code's auto mode refuses devkit commands from Claude sessions and subagents.
- A Claude session cannot run `setup` for you: changing its own permissions is refused. That is the point of the step being yours.
- Python 3.11 or newer. No dependencies.

### Server config: `~/.config/devkit/config.toml`

`devkit setup` writes a blank one. `[proxmox]`, `[ssh]`, `[secrets]` and `[limits] max_environments` are required; everything else is optional.

```toml
[proxmox]
host = "proxmox.example.lan"
node = "pve"
pool = "devkit"

[ssh]
user = "dev"                        # the user inside environments; created by the template build
key = "~/.ssh/devkit_ed25519"       # reaches environments; made by setup if missing
github_key = "~/.ssh/id_ed25519"    # lent to environments through agent forwarding
authorize = "~/.ssh/authorized_keys" # optional: these public keys may log into environments
host_key = "~/.config/devkit/host_ed25519"  # optional: one SSH identity for every environment, so a reused
                                    # address never trips "host identification has changed"; made if missing

[secrets]
file = "~/my-project/.env"          # where secret values are looked up
proxmox_token_key = "PVEAPIToken"
claude_token_key = "CLAUDE_CODE_OAUTH_TOKEN"

[limits]
max_environments = 3                # running ones; stopped environments do not count
min_free_memory_mb = 2048           # optional: create and start refuse below this

[template]                          # optional; these are the defaults
os_template = "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"   # Debian or Ubuntu; downloaded if missing.
                                    # Proxmox offers only the newest point release: update the name if the download fails
storage = "local-lvm"
bridge = "vmbr0"
interface = "eth0"
disk_gb = 20
cores = 4
memory_mb = 3072
swap_mb = 512
node_major = 22
prepare = []                        # extra commands run as the user while the base is built

[environment]                       # optional; without it environments have the template's size
memory_mb = 3072
cores = 4

[variables]
shared_host = "192.168.1.10"        # usable as {shared_host} in any .devkit.toml

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

- With `[network]`, keep that address range out of the router's DHCP pool.
- `[template] prepare` is for things your own Claude setup needs in every environment. Example: a Playwright plugin configured for its bundled browser needs
  `npx -y @playwright/mcp@latest --version >/dev/null && node "$(ls -d ~/.npm/_npx/*/node_modules/playwright-core | head -1)/cli.js" install chromium`.

### Project config: `<repo>/.devkit.toml`

Ask Claude to use the `setting-up-devkit` skill, or write it by hand:

```toml
name = "app"                        # optional; defaults to the folder name, names the project's template
repo = "git@github.com:owner/app.git"
base_branch = "development"
workdir = "/home/dev/app"           # where the repo lives inside an environment

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

[template]                          # optional: what `devkit template build` prepares for this project
prepare = [
    "docker compose build -q app",
    "docker compose pull -q redis",
]

[environment]                       # optional: overrides the server's size for this project
memory_mb = 4096

[[tool_servers]]                    # only services that take a key in a header
name = "posthog"
url = "https://mcp.posthog.com/mcp"
header = "Authorization: Bearer {secret}"
secret_key = "POSTHOG_ENV_API_KEY"  # looked up in the secrets file
```

- A project needs no template of its own: without one, `create` starts from the base and clones the repo. A project template saves that clone and keeps Docker images ready.
- `prepare` commands run as the environment's user in the project folder, with the settings file present but every value blank, and that file is removed before the template is frozen.
- devkit always strips its own secrets from the pushed file: the Proxmox token, the Claude token and every tool server key.
- A tool server whose key is missing is skipped with a message.
- A `replace` rule that matches nothing prints a warning.

## Tests

```bash
cd ~/devkit && PYTHONPATH=. python3 -m unittest tests.test_devkit
```
