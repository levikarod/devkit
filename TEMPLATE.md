# Building the template by hand

Proxmox node `dev`, pool `devkit`. Current template: container 106.

## As the API token
- Create an unprivileged Debian 12 container in the pool: 4 cores, 3072 MB, 20 GB on `local-lvm`, `features=nesting=1`, DHCP, the devkit SSH public key
- Do not pass `tags`: the token is refused

## As root on the Proxmox server, once per template
- `echo 'lxc.apparmor.profile: unconfined' >> /etc/pve/lxc/<id>.conf`
- `pct set <id> --features keyctl=1,nesting=1`
- `pct reboot <id>`

## Inside the container, as root
- `apt-get install -y ca-certificates curl git gh make jq python3`
- `curl -fsSL https://get.docker.com | sh`
- `useradd -m -s /bin/bash -G docker dev`, then copy root's `authorized_keys` to `dev`
- As `dev`: `curl -fsSL https://claude.ai/install.sh | bash`
- As `dev`: clone or `git init` the app repo at the path named by `workdir` in the app's `.devkit.toml`
- Load the app's Docker images (`docker save ... | ssh ... docker load`)

## Identity wipe, last step before freezing
- Install a one-shot unit that runs `ssh-keygen -A` before `ssh.service` when host keys are missing
- `rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/* /var/lib/dbus/machine-id`
- `: > /etc/machine-id`
- Check no `.env`, token or credential file is present

## Freeze
- Shut the container down
- Convert it to a template
- Set `template = <id>` in `~/.config/devkit/config.toml`
