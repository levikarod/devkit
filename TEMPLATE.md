# Building the template by hand

Proxmox node `dev`, pool `devkit`. Current template: container 105.

## As the API token
- Create an unprivileged Debian 12 container in the pool: 4 cores, 3072 MB, 20 GB on `local-lvm`, `features=nesting=1`, DHCP, the devkit SSH public key
- Do not pass `tags`: the token is refused

## As root on the Proxmox server, only for a template built from scratch
- A template made by cloning an existing template inherits these settings; skip this section
- `echo 'lxc.apparmor.profile: unconfined' >> /etc/pve/lxc/<id>.conf`
- `pct set <id> --features keyctl=1,nesting=1`
- `pct reboot <id>`

## Inside the container, as root
- `apt-get install -y ca-certificates curl git gh make jq python3`
- `curl -fsSL https://get.docker.com | sh`
- `useradd -m -s /bin/bash -G docker dev`, then copy root's `authorized_keys` to `dev`
- Node 22 from NodeSource: `curl -fsSL https://deb.nodesource.com/setup_22.x | bash -`, then `apt-get install -y nodejs sudo`
- `curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh`
- Google Chrome: download `google-chrome-stable_current_amd64.deb` from `dl.google.com/linux/direct` and `apt-get install -y ./<file>`
- `echo "dev ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/dev && chmod 440 /etc/sudoers.d/dev`
- As `dev`: `curl -fsSL https://claude.ai/install.sh | bash`
- Optional warm-up as `dev`: run `claude mcp list` once in the project folder so tool server packages are cached, then delete `~/.claude.json`
- As `dev`: clone or `git init` the app repo at the path named by `workdir` in the app's `.devkit.toml`
- Load the app's Docker images (`docker save ... | ssh ... docker load`)
- Tag the app image once per compose service that builds from it (`docker tag <project>-<first> <project>-<service>`), so any of them starts with `--no-build`

## Identity wipe, last step before freezing
- Install a one-shot unit that runs `ssh-keygen -A` before `ssh.service` when host keys are missing
- `rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/* /var/lib/dbus/machine-id`
- `: > /etc/machine-id`
- Check no `.env`, token or credential file is present

## Freeze
- Shut the container down
- Convert it to a template
- Set `template = <id>` in `~/.config/devkit/config.toml`
