# devkit roadmap

## Version 0.1: just works

### Commands
- `create <name> [branch]`: environment from the template, app running, address printed
- `list`: name, branch, address, memory, age
- `ssh <name>`: shell inside, as the non-root user
- `claude <name>`: Claude Code inside, in the project folder
- `destroy <name>`: stop and delete, after a yes/no prompt

### Create steps
- Clone from the Proxmox template
- Start and wait for SSH
- Branch fetched from GitHub and created inside the environment
- New branch cut from the base branch when it does not exist yet
- GitHub reached through SSH agent forwarding, no key copied
- App `.env` pushed over SSH: database addresses rewritten, email, error reporting and analytics off
- Server and tooling tokens stripped from the pushed `.env`
- Claude token pushed to a separate protected file
- Workspace marked trusted
- Note for Claude pushed: shared databases, what never to start
- Tool servers from the app config registered for Claude, key by header
- Tool server skipped with a message when its key is missing
- No containers started; whoever works inside starts only what the task needs
- Fixed address per environment, following the container number
- Half-made environment removed when a step fails

### Limits
- Refusal above three environments
- Memory and CPU limit inherited from the template
- One create at a time, lock file on the dev machine

### Scope
- Proxmox only
- Nothing project-specific in this repo
- App settings in `.devkit.toml` inside the app repo
- Server settings in `~/.config/devkit/config.toml`
- Server values as placeholders in the app config, no addresses written in it
- `setting-up-devkit` skill: writes and checks an app's `.devkit.toml`
- Template built by hand, steps in `TEMPLATE.md`
- Shared dev MySQL, MongoDB and Qdrant
- Single Python file, standard library only
- State read from Proxmox, no state file

## Version 0.1.1: Claude setup that just works

- Dev machine's Claude setup mirrored at create: plugins, skills, settings, global instructions
- No list of plugins or tool servers anywhere
- Stored logins never mirrored
- General runtimes in the template: Node, Python with uv, Docker
- Google Chrome in the template, so browser tools work with their default settings
- Parity check at create: tool servers connected here compared with inside
- `check <name>` to repeat the comparison later
- `create --no-check` to skip it
- Account connectors (claude.ai) never expected inside
- Root for the inside user, for one-off fixes
- Project tool servers pre-approved inside

## Version 0.1.2: see the work

- `code <name>`: environment opened in VS Code over Remote-SSH
- Editor's public keys authorized in every environment at create
- Keys taken from the dev machine's own authorized list, nothing new to manage

## Version 0.2: comfortable

### Safety
- Unpushed-work check before destroy, `--force` to override
- Server memory check before create and start
- `destroy --all`
- `stop` and `start` without destroying
- Memory view, heaviest first
- Owner recorded per environment: person or Claude session
- Doctor command: Proxmox access, memory, template, network

### Claude
- Claude Code plugin with skills: create, list, destroy
- Skills as thin wrappers over the command-line tool
- Same limits for Claude and for people
- `login <name>`: full claude.ai login inside one environment, done once by hand in a browser
- Remote control and cross-session messaging for environments that have that login
- Coordinator here, workers inside: tasks handed over SSH, final report returned
- Workers run detached, so a dropped connection does not kill a task
- Keyed tool servers kept explicit in the app config
- Dedicated PostHog key for environments, with the scopes its tool server asks for
- Other login-based tool servers left out: MercadoLibre, MercadoPago, Cloudflare
- Deeper check than "connected": one real call per tool server
- Project memory mirrored in, transcripts and memory copied back before destroy

### Template
- `template build` command
- Slim app image, pulled or built during template build
- Identity wipe automated: machine id, SSH host keys, network lease
- Rebuild on a schedule or on demand

### Git
- Dedicated GitHub token for the app, replacing SSH agent forwarding
- Token scoped to the one repo: code and pull requests only
- Token injected at create, apart from the app `.env`, gone on destroy
- Push and pull requests from unattended sessions
- Branch created from the base branch when it does not exist

### Daily use
- Optional app start at create: chosen services, one combined worker
- Logs and restart shortcuts per environment
- Test suite run inside the environment
- Auto-login user per environment

## Later

### Lifecycle
- Auto-drop when the branch is merged or deleted
- Idle timeout that stops unused environments
- Orphan cleanup

### Controllers
- Controller per platform behind a small contract
- Contract: create, start, stop, destroy, list, run command, report usage
- Contract result: a machine with Docker and an address
- Compose handling above the controller, identical on every platform
- Kubernetes controller
- AWS EC2 controller
- Full VM option on Proxmox for stricter isolation

### Any project
- Nothing project-specific inside the tool
- Config file in each app repo: services, settings overrides, test command, base branch
- Optional private database for a branch with risky migrations
- Stable name-based address per environment

### Autofix pipeline
- Autofix runner moved out of droppo-v2
- Issue sources as plugins: GlitchTip, PostHog, more later
- Per-project config: sources, labels, base branch, prompts
- Each fix run inside its own environment
- Live API and browser validation in unattended runs
- Environment address posted in the draft PR
- Environment kept while the PR is open, dropped on merge or close
- Review comments applied in the same environment
- Several fixes in parallel, bounded by server memory
- Draft PR, label state and comment triggers kept as today

## Known constraints
- Docker-in-container settings set once per template, as root on the server
- Proxmox refuses two clones from one template at the same time
- Proxmox tags not settable with the current token
- No variable injection at container creation, SSH push instead
- About 690 MB per environment with the app running
- About 20 seconds from create to healthy app

## Open questions
- Tool name
- MongoDB migrations on a shared database
- Webhook flows in environments
- Memory sync: copy back on destroy, or shared folder
