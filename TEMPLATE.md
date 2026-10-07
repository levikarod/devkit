# Templates

An environment is a clone of a template: a frozen container that already has everything installed. Cloning takes seconds, which is why `devkit create` does.

There are two kinds, both built by `devkit template build`.

| Template | Name on the server | Contains | Build it |
|---|---|---|---|
| Base, one per server | `devkit-base` | Docker, Node, uv, Chrome, Claude Code, the usual tools, the environment user | `devkit template build` outside a project, or `--base` anywhere |
| Project, one per project | `devkit-tpl-<project>` | the base, plus the project's code and whatever its `prepare` commands produce | `devkit template build` inside the project |

`devkit create` uses the project's template when there is one, and the base otherwise. `devkit template list` shows what exists and which one the current project gets.

## The first base build needs root once

Docker cannot start containers inside a fresh Proxmox container: two settings stand in the way, and Proxmox only lets root change them. The build installs everything, finds that Docker cannot run, prints three commands with the right container number, and waits up to 30 minutes:

```bash
echo 'lxc.apparmor.profile: unconfined' >> /etc/pve/lxc/<id>.conf
pct set <id> --features keyctl=1,nesting=1
pct reboot <id>
```

Run them as root on the Proxmox server. The build notices within ten seconds and carries on.

- This happens once per server. Every later build clones an existing template, which inherits both settings.
- It weakens the wall between an environment and the server: the container still runs unprivileged, but without Proxmox's AppArmor profile. Do not run code you do not trust in an environment.

## What the base build does

1. Downloads the OS image named by `[template] os_template` if the server does not have it.
2. Creates an unprivileged container from it, with the size, storage and bridge from `[template]`, and devkit's public SSH key.
3. Upgrades the system packages and installs, as root: `ca-certificates curl git gh make jq python3 sudo rsync openssh-server`, Docker, Node (`node_major`), uv, Google Chrome.
4. Creates the user from `[ssh] user`, in the `docker` group, with passwordless `sudo`.
5. Installs Claude Code for that user, or updates it when it is already there.
6. Checks that Docker can run a container. If the test image cannot even be pulled, it stops and says the network is the problem; if it pulls but cannot run, it asks for the root step above.
7. Runs each `[template] prepare` command as the user.
8. Wipes the container's identity (SSH host keys, machine id, network lease) so every clone gets its own, and freezes it.

The OS image must be Debian or Ubuntu: the installation uses `apt`.

## What a project build does

1. Clones the base.
2. Clones the project's repository at its base branch into `workdir`.
3. Writes the project's settings file with every value blank, so tools that only need the file to exist will run.
4. Runs each `prepare` command from the project's `.devkit.toml`, in the project folder.
5. Mirrors this machine's Claude setup and lists the tool servers once, so the packages they download are cached.
6. Removes the settings file, the whole mirrored Claude setup (`~/.claude`, `~/.claude.json`) and any containers, and refuses to freeze if any of them is still there. Only the package caches stay.
7. Wipes the identity and freezes it, replacing the project's previous template.

No secret is frozen into a template: settings, tokens and your Claude setup are pushed into each environment when it is created. One thing to watch: if a `prepare` command builds a Docker image whose Dockerfile copies the whole folder, the blank settings file is copied into that image, with no values in it.

## Keeping templates current

| When | Run |
|---|---|
| Dependencies or Docker images of a project changed | `devkit template build` in that project |
| You want newer system packages, Docker, Node, Chrome, uv or Claude Code, or changed `[template] prepare` | `devkit template build --base`, then each project's build |
| The base is broken beyond repair | `devkit template build --base --fresh`: starts again from the OS image, and needs the root step again |

- Updating the base clones the current one, upgrades its packages and re-runs the installation, so it needs no root.
- A rebuild freezes the new template first, then removes the old one and takes its name. A failure in between leaves two templates, never none, and the next build finishes the job.
- Environments made earlier are unaffected by a rebuild.
- A build that fails leaves its container stopped, named `devkit-build-...`. `devkit template list` shows it; the next build of that template removes it, and so does `devkit template clean`.
- Only one build of a given template runs at a time, and builds wait for a running `create` before they clone or replace anything.
- A project template remembers the repository it was built for. Two projects whose names collide are refused with a message asking for a distinct `name` in `.devkit.toml`.

## Known limits

- **Storage:** built and tested on LVM-thin (`local-lvm`), where every clone is independent of its template. On storage where clones stay linked to the template (ZFS, directory), Proxmox may refuse to delete a template that still has clones, so replacing a template could fail. Untested.
- **OS:** Debian or Ubuntu images only.
- **Platform:** Proxmox containers only.

## Measured on the first server

| Step | Time |
|---|---|
| Base from the OS image | 190 s, plus the root step |
| Base update | 50 to 90 s |
| Project template with a 1.5 GB app image | about 130 s |
| `create` from a project template | 13 to 20 s |
| `create` from the base, cloning the repo | about 30 s |
