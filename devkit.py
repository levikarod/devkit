#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_CONFIG = Path(os.environ.get('DEVKIT_CONFIG', '~/.config/devkit/config.toml')).expanduser()
PROJECT_CONFIG_NAME = '.devkit.toml'
LOCK_PATH = Path('~/.cache/devkit/create.lock').expanduser()
ENV_PREFIX = 'env-'
NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]{0,29}$')
ENV_LINE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$')
SECRETS_FILE = '~/.devkit-secrets'
NOTE_FILE = 'CLAUDE.local.md'
CLAUDE_HOME = Path('~/.claude').expanduser()
MIRRORED = ('plugins', 'skills', 'settings.json', 'CLAUDE.md')
ACCOUNT_SERVER_PREFIX = 'claude.ai '
RUNS_DIR = '~/.devkit-runs'
WORKER_ARGS = ('--permission-mode', 'auto')
WORKER_TIMEOUT = 3600
TIMEOUT_EXIT = '124'
BASE_TEMPLATE = 'devkit-base'
PROJECT_TEMPLATE_PREFIX = 'devkit-tpl-'
BUILD_PREFIX = 'devkit-build-'
ROOT_STEP_TIMEOUT = 1800
TEMPLATE_DEFAULTS = {
    'os_template': 'local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst',
    'storage': 'local-lvm',
    'bridge': 'vmbr0',
    'interface': 'eth0',
    'disk_gb': 20,
    'cores': 4,
    'memory_mb': 3072,
    'swap_mb': 512,
    'node_major': 22,
    'prepare': [],
}
MCP_LINE = re.compile(r'^(?P<name>.+?): .* - (?P<mark>[✔✘!⊘])')
MCP_STATUS = {'✔': 'connected', '✘': 'failed', '!': 'needs login', '⊘': 'disabled'}


class DevkitError(Exception):
    pass


def validate_name(name):
    if not NAME_PATTERN.match(name):
        raise DevkitError(
            f"invalid name '{name}': lowercase letters, digits and dashes, at most 30 characters"
        )
    return name


def rewrite_env(text, drop=(), force=None, replace=None):
    force = dict(force or {})
    replace = dict(replace or {})
    lines = []
    for line in text.splitlines():
        match = ENV_LINE.match(line)
        if not match:
            lines.append(line)
            continue
        key, value = match.groups()
        if key in drop:
            continue
        if key in force:
            value = force.pop(key)
        else:
            for old, new in replace.items():
                value = value.replace(old, new)
        lines.append(f'{key}={value}')
    lines.extend(f'{key}={value}' for key, value in force.items())
    return '\n'.join(lines) + '\n'


def expand(value, variables):
    if isinstance(value, str):
        for name, replacement in variables.items():
            value = value.replace('{' + name + '}', replacement)
        return value
    if isinstance(value, dict):
        return {expand(key, variables): expand(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, variables) for item in value]
    return value


def unmatched_replacements(text, drop=(), force=None, replace=None):
    candidates = [
        match.group(2) for match in map(ENV_LINE.match, text.splitlines())
        if match and match.group(1) not in drop and match.group(1) not in (force or {})
    ]
    return [old for old in (replace or {}) if not any(old in value for value in candidates)]


def read_env_value(text, key):
    for line in text.splitlines():
        match = ENV_LINE.match(line)
        if match and match.group(1) == key:
            return match.group(2).strip().strip('\'"')
    return None


def parse_tool_server(entry):
    for field in ('name', 'url', 'header', 'secret_key'):
        if not entry.get(field):
            raise DevkitError(f"tool server entry is missing '{field}': {entry}")
    header_name, separator, header_value = entry['header'].partition(':')
    if not separator or '{secret}' not in header_value:
        raise DevkitError(
            f"tool server '{entry['name']}': header must look like 'Name: value with {{secret}}'"
        )
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', entry['secret_key']):
        raise DevkitError(f"tool server '{entry['name']}': secret_key must be a variable name")
    return {
        'name': entry['name'],
        'secret_key': entry['secret_key'],
        'config': {
            'type': 'http',
            'url': entry['url'],
            'headers': {
                header_name.strip(): header_value.strip().replace('{secret}', '${' + entry['secret_key'] + '}'),
            },
        },
    }


def resolve_tool_servers(entries, secrets_text):
    exports, servers, skipped = {}, {}, []
    for entry in entries or ():
        server = parse_tool_server(entry)
        value = read_env_value(secrets_text, server['secret_key'])
        if not value:
            skipped.append((server['name'], server['secret_key']))
            continue
        exports[server['secret_key']] = value
        servers[server['name']] = server['config']
    return exports, servers, skipped


def render_exports(exports):
    return ''.join(f'export {key}={shlex.quote(value)}\n' for key, value in exports.items())


def static_network(current, vmid, network):
    address = network['address'].replace('{vmid}', str(vmid))
    host = address.split('/')[0]
    octets = host.split('.')
    if len(octets) != 4 or not all(part.isdigit() and int(part) <= 254 for part in octets):
        raise DevkitError(f"container {vmid} does not map to a usable address ({host})")
    parts = [part for part in current.split(',') if not part.startswith(('ip=', 'gw='))]
    parts.append(f'ip={address}')
    if network.get('gateway'):
        parts.append(f"gw={network['gateway']}")
    return ','.join(parts)


def parse_mcp_list(text):
    servers = {}
    for line in text.splitlines():
        match = MCP_LINE.match(line.strip())
        if match:
            servers[match.group('name')] = MCP_STATUS[match.group('mark')]
    return servers


def compare_tool_servers(here, inside):
    working_here = {
        name for name, status in here.items()
        if status == 'connected' and not name.startswith(ACCOUNT_SERVER_PREFIX)
    }
    missing = sorted(
        (name, inside.get(name, 'absent')) for name in working_here if inside.get(name) != 'connected'
    )
    extra = sorted(name for name, status in inside.items() if status == 'connected' and name not in working_here)
    return len(working_here), missing, extra


WORK_PROBE = (
    'echo "## dirty"; git status --porcelain; '
    'echo "## unpushed"; git log --oneline --branches --not --remotes'
)


def parse_work(text):
    sections = {'dirty': [], 'unpushed': []}
    current = None
    for line in text.splitlines():
        if line.startswith('## '):
            current = line[3:].strip()
        elif line.strip() and current in sections:
            sections[current].append(line.rstrip())
    return sections


def describe_work(work):
    parts = []
    if work['unpushed']:
        parts.append(f"{len(work['unpushed'])} unpushed commit(s)")
    if work['dirty']:
        parts.append(f"{len(work['dirty'])} uncommitted file(s)")
    return ' and '.join(parts)


def free_memory_mb(node_status):
    memory = node_status['memory']
    return (memory['total'] - memory['used']) // 2**20


def parse_worker_output(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get('type') == 'result':
                return data
    return None


def worker_script(workdir, run_id, claude_args, resume=None, timeout=None):
    run = f'{RUNS_DIR}/{run_id}'
    resume_args = f'--resume {shlex.quote(resume)} ' if resume else ''
    extra = ' '.join(shlex.quote(part) for part in claude_args)
    bound = f'timeout {int(timeout)} ' if timeout else ''
    inner = (
        f'[ -f {SECRETS_FILE} ] && . {SECRETS_FILE}; export PATH="$HOME/.local/bin:$PATH"; '
        f'cd {shlex.quote(workdir)} && '
        f'{bound}claude -p {resume_args}--output-format json {extra} < {run}/prompt > {run}/output 2> {run}/errors; '
        f'echo $? > {run}/exit'
    )
    return (
        f'set -e; mkdir -p {run}; cat > {run}/prompt; '
        f'ln -sfn {run_id} {RUNS_DIR}/latest; '
        f'setsid nohup bash -c {shlex.quote(inner)} > /dev/null 2>&1 < /dev/null & echo started'
    )


def format_report(run_id, state, result):
    if state == 'none':
        return 'no worker has run in this environment'
    if state == 'running':
        return f'worker {run_id} is still running'
    if state == TIMEOUT_EXIT and result is None:
        return f'worker {run_id} was stopped at the time limit; see {RUNS_DIR}/{run_id}/errors inside'
    if result is None:
        return f'worker {run_id} ended without a report (exit {state}); see {RUNS_DIR}/{run_id}/errors inside'
    seconds = (result.get('duration_ms') or 0) // 1000
    status = 'failed' if result.get('is_error') else 'finished'
    footer = f"[worker {run_id} {status}: {result.get('num_turns', '?')} turns, {seconds}s]"
    return f"{(result.get('result') or '').strip()}\n\n{footer}"


def report_data(run_id, state, result):
    if state in ('none', 'running'):
        return {'run_id': run_id, 'state': state}
    if state == TIMEOUT_EXIT and result is None:
        return {'run_id': run_id, 'state': 'timeout'}
    if result is None or result.get('is_error'):
        outcome = 'failed'
    else:
        outcome = 'finished'
    result = result or {}
    return {
        'run_id': run_id,
        'state': outcome,
        'exit': state,
        'report': (result.get('result') or '').strip(),
        'session_id': result.get('session_id'),
        'turns': result.get('num_turns'),
        'seconds': (result.get('duration_ms') or 0) // 1000,
    }


def environment_summary(container, branch):
    return {'name': container['name'][len(ENV_PREFIX):], 'branch': branch, 'state': container['status']}


def running_names(environments):
    return [c['name'][len(ENV_PREFIX):] for c in environments if c['status'] == 'running']


def limit_reached(environments, limit):
    names = running_names(environments)
    return names if len(names) >= limit else None


def ship_script(workdir, message, author_name, author_email):
    identity = f'-c user.name={shlex.quote(author_name)} -c user.email={shlex.quote(author_email)}'
    return (
        f'set -e; cd {shlex.quote(workdir)}; git add -A; '
        'if git diff --cached --quiet; then echo "nothing to ship"; exit 0; fi; '
        f'git {identity} commit -q -m {shlex.quote(message)}; '
        'git push -q -u origin HEAD; '
        'echo "shipped $(git rev-parse --short HEAD) to $(git rev-parse --abbrev-ref HEAD)"'
    )


HOST_KEY_PATH = '/etc/ssh/ssh_host_ed25519_key'


def host_key_script():
    return (
        'set -e; umask 077; staged=$(mktemp); cat > "$staged"; '
        f'sudo install -m 600 -o root -g root "$staged" {HOST_KEY_PATH}; rm -f "$staged"; '
        f"sudo sh -c 'ssh-keygen -y -f {HOST_KEY_PATH} > {HOST_KEY_PATH}.pub; "
        f'echo "HostKey {HOST_KEY_PATH}" > /etc/ssh/sshd_config.d/devkit-hostkey.conf; '
        "systemctl restart ssh'"
    )


def public_keys(text):
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and line not in keys:
            keys.append(line)
    return keys


def editor_uri(user, address, workdir):
    return f'vscode-remote://ssh-remote+{user}@{address}{workdir}'


def project_slug(name):
    slug = re.sub(r'[^a-z0-9]+', '-', str(name).lower()).strip('-')[:40].strip('-')
    if not slug:
        raise DevkitError(f"'{name}' cannot be turned into a template name; set name = \"...\" in {PROJECT_CONFIG_NAME}")
    return slug


def template_settings(server):
    return {**TEMPLATE_DEFAULTS, **server.get('template', {})}


def choose_template(containers, slug=None, legacy=None):
    templates = {c.get('name'): c for c in containers if c.get('template')}
    wanted = ([PROJECT_TEMPLATE_PREFIX + slug] if slug else []) + [BASE_TEMPLATE]
    for name in wanted:
        if name in templates:
            return int(templates[name]['vmid']), name
    if legacy:
        for container in containers:
            if int(container['vmid']) == int(legacy) and container.get('template'):
                return int(legacy), container.get('name') or str(legacy)
    return None, None


def blank_env(text):
    lines = []
    for line in text.splitlines():
        match = ENV_LINE.match(line)
        if match:
            lines.append(f'{match.group(1)}=')
    return '\n'.join(lines) + '\n'


def environment_size(server, project):
    merged = {**server.get('environment', {}), **(project or {}).get('environment', {})}
    size = {}
    if merged.get('memory_mb'):
        size['memory'] = int(merged['memory_mb'])
    if merged.get('cores'):
        size['cores'] = int(merged['cores'])
    return size


def new_container_params(vmid, hostname, pool, settings, public_key):
    return {
        'vmid': vmid,
        'hostname': hostname,
        'pool': pool,
        'ostemplate': settings['os_template'],
        'rootfs': f"{settings['storage']}:{settings['disk_gb']}",
        'cores': settings['cores'],
        'memory': settings['memory_mb'],
        'swap': settings['swap_mb'],
        'unprivileged': 1,
        'features': 'nesting=1',
        'net0': f"name={settings['interface']},bridge={settings['bridge']},ip=dhcp,type=veth",
        'ssh-public-keys': public_key,
        'description': encode_template_description(),
    }


def provision_script(user, node_major):
    return f'''set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y -qq upgrade >/dev/null
apt-get install -y -qq ca-certificates curl git gh make jq python3 sudo rsync openssh-server >/dev/null
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh >/dev/null 2>&1
id {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}
usermod -aG docker {user}
install -d -m 700 -o {user} -g {user} /home/{user}/.ssh
install -m 600 -o {user} -g {user} /root/.ssh/authorized_keys /home/{user}/.ssh/authorized_keys
echo "{user} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/devkit
chmod 440 /etc/sudoers.d/devkit
if ! node --version 2>/dev/null | grep -q "^v{node_major}\\."; then
  curl -fsSL https://deb.nodesource.com/setup_{node_major}.x | bash - >/dev/null 2>&1
  apt-get install -y -qq nodejs >/dev/null
fi
if command -v uv >/dev/null; then
  uv self update >/dev/null 2>&1 || true
else
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null 2>&1
fi
if [ ! -x /opt/google/chrome/chrome ]; then
  curl -fsSLo /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y -qq /tmp/chrome.deb >/dev/null
  rm -f /tmp/chrome.deb
fi
cat > /etc/systemd/system/ssh-hostkeys.service <<UNIT
[Unit]
Description=Generate SSH host keys on first boot
Before=ssh.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
[Install]
WantedBy=multi-user.target
UNIT
systemctl enable -q ssh-hostkeys.service
echo "docker $(docker --version | cut -d, -f1 | cut -d" " -f3), node $(node --version), uv $(uv --version | cut -d" " -f2), chrome $(/opt/google/chrome/chrome --version 2>/dev/null | tail -1 | cut -d" " -f3)"
'''


CLAUDE_INSTALL = (
    'if [ -x ~/.local/bin/claude ]; then ~/.local/bin/claude update >/dev/null 2>&1 || true; '
    'else curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1; fi; '
    '~/.local/bin/claude --version'
)
DOCKER_PULL = 'docker pull -q hello-world >/dev/null 2>&1'
DOCKER_PROBE = 'docker run --rm hello-world >/dev/null 2>&1'
CLAUDE_STATE = ('~/.claude', '~/.claude.json', '~/.devkit-secrets', '~/.devkit-runs')
IDENTITY_WIPE = (
    'apt-get clean; rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/* /var/lib/dbus/machine-id; '
    ': > /etc/machine-id; sync'
)


def root_step_commands(vmid):
    return [
        f"echo 'lxc.apparmor.profile: unconfined' >> /etc/pve/lxc/{vmid}.conf",
        f'pct set {vmid} --features keyctl=1,nesting=1',
        f'pct reboot {vmid}',
    ]


def shell_path(path):
    if path.startswith('~/'):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def leftover_check(paths):
    tests = ' '.join(f'[ ! -e {shell_path(path)} ] &&' for path in paths)
    return f'{tests} echo clean'


REMOVE_CONTAINERS = 'docker ps -aq | xargs -r docker rm -f >/dev/null; true'


def remove_files_script(paths):
    return 'rm -rf ' + ' '.join(shell_path(path) for path in paths)


def prepare_commands(value, where):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise DevkitError(f'{where}: prepare must be a list of commands, like prepare = ["make deps"]')
    return value


def encode_template_description(repo=None):
    return 'devkit template' + (f' repo={repo}' if repo else '')


def decode_template_repo(description):
    match = re.search(r'devkit template repo=(\S+)', urllib.parse.unquote(description or ''))
    return match.group(1) if match else None


def encode_description(branch):
    return f'devkit branch={branch}'


def decode_description(description):
    match = re.search(r'devkit branch=(\S+)', urllib.parse.unquote(description or ''))
    return match.group(1) if match else ''


def find_project_config(start):
    for folder in [start, *start.parents]:
        candidate = folder / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise DevkitError(f'no {PROJECT_CONFIG_NAME} found in {start} or any parent folder')


def load_toml(path):
    if not path.is_file():
        raise DevkitError(f'missing config file: {path}')
    with open(path, 'rb') as handle:
        return tomllib.load(handle)


class Proxmox:

    def __init__(self, config, token, interface='eth0'):
        self.base = f"https://{config['host']}:{config.get('port', 8006)}/api2/json"
        self.node = config['node']
        self.interface = interface
        self.token = token
        self.context = ssl.create_default_context()
        if not config.get('verify_tls', False):
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def call(self, method, path, **params):
        data = urllib.parse.urlencode(params).encode() if params else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header('Authorization', f'PVEAPIToken={self.token}')
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30) as response:
                return json.load(response).get('data')
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors='replace')
            try:
                message = json.loads(body).get('message') or body
            except ValueError:
                message = body or error.reason
            raise DevkitError(f'Proxmox {method} {path}: {str(message).strip()}') from None
        except urllib.error.URLError as error:
            raise DevkitError(f'Proxmox unreachable: {error.reason}') from None

    def wait(self, upid, timeout=180):
        path = f'/nodes/{self.node}/tasks/{urllib.parse.quote(upid, safe="")}/status'
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.call('GET', path)
            if status['status'] == 'stopped':
                if status.get('exitstatus') != 'OK':
                    raise DevkitError(f"Proxmox task failed: {status.get('exitstatus')}")
                return
            time.sleep(0.5)
        raise DevkitError('Proxmox task timed out')

    def containers(self):
        return self.call('GET', f'/nodes/{self.node}/lxc') or []

    def environments(self):
        found = [
            c for c in self.containers()
            if c.get('name', '').startswith(ENV_PREFIX) and not c.get('template')
        ]
        return sorted(found, key=lambda c: c['vmid'])

    def find(self, name):
        for container in self.environments():
            if container['name'] == ENV_PREFIX + name:
                return container
        raise DevkitError(f"no environment named '{name}'")

    def config(self, vmid):
        return self.call('GET', f'/nodes/{self.node}/lxc/{vmid}/config') or {}

    def address(self, vmid, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for interface in self.call('GET', f'/nodes/{self.node}/lxc/{vmid}/interfaces') or []:
                if interface.get('name') == self.interface and interface.get('inet'):
                    return interface['inet'].split('/')[0]
            time.sleep(0.5)
        raise DevkitError(f'container {vmid} got no network address')

    def clone(self, template, vmid, hostname, pool, description):
        self.wait(self.call(
            'POST', f'/nodes/{self.node}/lxc/{template}/clone',
            newid=vmid, hostname=hostname, pool=pool, description=description,
        ))

    def set_network(self, vmid, net0):
        self.set_config(vmid, net0=net0)

    def set_config(self, vmid, **params):
        self.call('PUT', f'/nodes/{self.node}/lxc/{vmid}/config', **params)

    def create(self, **params):
        self.wait(self.call('POST', f'/nodes/{self.node}/lxc', **params), timeout=600)

    def to_template(self, vmid):
        self.call('POST', f'/nodes/{self.node}/lxc/{vmid}/template')

    def templates(self):
        return sorted((c for c in self.containers() if c.get('template')), key=lambda c: c['vmid'])

    def named(self, name):
        return [c for c in self.containers() if c.get('name') == name]

    def ensure_os_template(self, volid):
        storage, _, path = volid.partition(':')
        filename = path.rsplit('/', 1)[-1]
        content = self.call('GET', f'/nodes/{self.node}/storage/{storage}/content?content=vztmpl') or []
        if any(item.get('volid') == volid for item in content):
            return False
        self.wait(self.call('POST', f'/nodes/{self.node}/aplinfo', storage=storage, template=filename), timeout=900)
        return True

    def start(self, vmid):
        self.wait(self.call('POST', f'/nodes/{self.node}/lxc/{vmid}/status/start'))

    def stop(self, vmid):
        self.wait(self.call('POST', f'/nodes/{self.node}/lxc/{vmid}/status/stop'))

    def delete(self, vmid):
        self.wait(self.call('DELETE', f'/nodes/{self.node}/lxc/{vmid}?purge=1'))

    def shutdown(self, vmid):
        self.wait(self.call('POST', f'/nodes/{self.node}/lxc/{vmid}/status/shutdown'))

    def node_status(self):
        return self.call('GET', f'/nodes/{self.node}/status')

    def next_id(self):
        return int(self.call('GET', '/cluster/nextid'))


class Remote:

    def __init__(self, address, user, key):
        self.target = f'{user}@{address}'
        self.options = [
            '-i', str(Path(key).expanduser()),
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-o', 'ConnectTimeout=5',
        ]

    def command(self, script=None, forward=False, tty=False):
        argv = ['ssh', *self.options]
        if forward:
            argv.append('-A')
        if tty:
            argv.append('-t')
        argv.append(self.target)
        if script is not None:
            argv.append(script)
        return argv

    def run(self, script, stdin=None, forward=False, env=None):
        result = subprocess.run(
            self.command(script, forward=forward), input=stdin, text=True,
            capture_output=True, env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise DevkitError(f"remote step failed: {script.splitlines()[0][:60]}\n  " + '\n  '.join(detail))
        return result.stdout

    def wait(self, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if subprocess.run(self.command('true'), capture_output=True).returncode == 0:
                return
            time.sleep(0.5)
        raise DevkitError(f'{self.target} did not accept SSH')

    def succeeds(self, script):
        return subprocess.run(self.command(script), capture_output=True).returncode == 0

    def stream(self, script, stdin=None, forward=False, env=None, label=None):
        result = subprocess.run(self.command(script, forward=forward), input=stdin, text=True, env=env)
        if result.returncode != 0:
            raise DevkitError(f'step failed with exit {result.returncode}: {label or script.splitlines()[0][:80]}')


class Agent:

    def __init__(self, key):
        self.key = str(Path(key).expanduser())
        self.pid = None
        self.env = None

    def __enter__(self):
        output = subprocess.run(['ssh-agent', '-s'], capture_output=True, text=True, check=True).stdout
        values = dict(re.findall(r'(SSH_AUTH_SOCK|SSH_AGENT_PID)=([^;]+);', output))
        self.pid = values['SSH_AGENT_PID']
        self.env = {**os.environ, **values}
        added = subprocess.run(['ssh-add', '-q', self.key], env=self.env, capture_output=True, text=True)
        if added.returncode != 0:
            self.__exit__(None, None, None)
            raise DevkitError(f'could not load {self.key} into an SSH agent: {added.stderr.strip()}')
        return self.env

    def __exit__(self, *_):
        if self.pid:
            subprocess.run(['kill', self.pid], capture_output=True)
            self.pid = None


@contextlib.contextmanager
def file_lock(path, wait_seconds, busy):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_seconds
    with open(path, 'w') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise DevkitError(busy) from None
                time.sleep(1)
        yield


def clone_lock(wait_seconds=0):
    return file_lock(LOCK_PATH, wait_seconds, 'another create or template build is using the server, try again in a moment')


def build_lock(build_name):
    return file_lock(
        LOCK_PATH.with_name(f'{build_name}.lock'), 0, f'a build of this template is already running ({build_name})',
    )


class Devkit:

    def __init__(self, project_dir, need_project=True):
        self.server = load_toml(SERVER_CONFIG)
        variables = {name: str(value) for name, value in self.server.get('variables', {}).items()}
        try:
            self.project_config_path = find_project_config(project_dir)
        except DevkitError:
            if need_project:
                raise
            self.project_config_path = None
        if self.project_config_path:
            self.project = expand(load_toml(self.project_config_path), variables)
            self.project_root = self.project_config_path.parent
        else:
            self.project = None
            self.project_root = None
        secrets_path = Path(self.server['secrets']['file']).expanduser()
        self.secrets_text = secrets_path.read_text()
        token = read_env_value(self.secrets_text, self.server['secrets']['proxmox_token_key'])
        if not token:
            raise DevkitError(f"{self.server['secrets']['proxmox_token_key']} not found in {secrets_path}")
        self.template = template_settings(self.server)
        self.proxmox = Proxmox(self.server['proxmox'], token, self.template['interface'])
        self.user = self.server['ssh']['user']
        self.key = self.server['ssh']['key']
        self.github_key = self.server['ssh']['github_key']
        self.workdir = self.project['workdir'] if self.project else None
        self.slug = None
        if self.project:
            try:
                self.slug = project_slug(self.project.get('name') or self.project_root.name)
            except DevkitError:
                self.slug = None

    def remote(self, vmid):
        return Remote(self.proxmox.address(vmid), self.user, self.key)

    def create(self, name, branch, check=True):
        validate_name(name)
        branch = branch or name
        with clone_lock():
            self._create(name, branch, check)

    def _create(self, name, branch, check=True):
        existing = self.proxmox.environments()
        if any(c['name'] == ENV_PREFIX + name for c in existing):
            raise DevkitError(f"environment '{name}' already exists")
        self._require_room(existing)
        self._require_memory()
        template, template_name = self._template_for_project()
        vmid = self.proxmox.next_id()
        started = time.time()
        here = {}
        probe = threading.Thread(target=lambda: here.update(self._tool_servers_here()), daemon=True)
        if check:
            probe.start()
        step(f'cloning {template_name} into container {vmid}')
        self.proxmox.clone(
            template, vmid, ENV_PREFIX + name,
            self.server['proxmox']['pool'], encode_description(branch),
        )
        try:
            self._apply_network(vmid)
            size = environment_size(self.server, self.project)
            if size:
                self.proxmox.set_config(vmid, **size)
            self.proxmox.start(vmid)
            remote = self.remote(vmid)
            remote.wait()
            step(f'checking out {branch}')
            self._checkout(remote, branch)
            step('pushing settings')
            self._push_settings(remote)
            self._authorize_keys(remote)
            self._share_host_key(remote)
            if self.server.get('claude', {}).get('mirror', True):
                step('mirroring the Claude setup')
                self._mirror_claude(remote)
        except BaseException:
            step('create failed, removing the half-made environment')
            self._remove(vmid)
            raise
        address = remote.target.split('@')[1]
        print(f"\n{name} ready in {time.time() - started:.0f}s")
        print(f'  address  {address}')
        print(f'  branch   {branch}')
        print(f'  shell    devkit ssh {name}')
        print(f'  claude   devkit claude {name}')
        if check:
            print()
            step('checking tool servers')
            probe.join(timeout=120)
            self._report_tool_servers(here, self._tool_servers_inside(remote))

    def _checkout(self, remote, branch):
        repo = self.project['repo']
        base = self.project['base_branch']
        script = f'''set -e
if [ ! -d {shlex.quote(self.workdir)}/.git ]; then
  mkdir -p "$(dirname {shlex.quote(self.workdir)})"
  GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new" git clone -q {shlex.quote(repo)} {shlex.quote(self.workdir)}
fi
cd {shlex.quote(self.workdir)}
git config core.sshCommand "ssh -o StrictHostKeyChecking=accept-new"
git remote remove origin 2>/dev/null || true
git remote add origin {shlex.quote(repo)}
git fetch -q origin
if git rev-parse -q --verify refs/remotes/origin/{shlex.quote(branch)} >/dev/null; then
  git checkout -q -B {shlex.quote(branch)} origin/{shlex.quote(branch)}
else
  git checkout -q -B {shlex.quote(branch)} origin/{shlex.quote(base)}
fi
grep -qxF {NOTE_FILE} .git/info/exclude || echo {NOTE_FILE} >> .git/info/exclude
git log --oneline -1
'''
        with Agent(self.github_key) as env:
            remote.run(script, forward=True, env=env)

    def _template_for_project(self):
        template, name = choose_template(
            self.proxmox.containers(), self.slug, self.server['proxmox'].get('template'),
        )
        if template is None:
            raise DevkitError('no template on the server yet; build one with: devkit template build')
        if name.startswith(PROJECT_TEMPLATE_PREFIX):
            self._require_same_repo(template, name)
        return template, name

    def _require_same_repo(self, vmid, name):
        built_for = decode_template_repo(self.proxmox.config(vmid).get('description'))
        if built_for and built_for != self.project['repo']:
            raise DevkitError(
                f"{name} was built for {built_for}, not for this project; "
                f'give this project its own name = "..." in {PROJECT_CONFIG_NAME}'
            )

    def _apply_network(self, vmid):
        network = self.server.get('network')
        if network:
            self.proxmox.set_network(vmid, static_network(self.proxmox.config(vmid)['net0'], vmid, network))

    def _settings_target(self):
        return f"{self.workdir}/{self.project.get('settings', {}).get('target', '.env')}"

    def _rewritten_settings(self, warn=True):
        settings = self.project.get('settings', {})
        source_path = self.project_root / settings.get('source', '.env')
        if not source_path.is_file():
            raise DevkitError(f'the settings file {source_path} does not exist')
        source = source_path.read_text()
        drop = self._always_dropped() | set(settings.get('drop', ()))
        if warn:
            for unused in unmatched_replacements(source, drop, settings.get('force'), settings.get('replace')):
                step(f"warning: replace rule '{unused}' matched nothing in {settings.get('source', '.env')}")
        return rewrite_env(source, drop=drop, force=settings.get('force'), replace=settings.get('replace'))

    def _push_settings(self, remote):
        target = shlex.quote(self._settings_target())
        remote.run(f'umask 077; cat > {target}', stdin=self._rewritten_settings())

        exports, servers, skipped = resolve_tool_servers(
            self.project.get('tool_servers'), self.secrets_text,
        )
        for server_name, secret_key in skipped:
            step(f"tool server '{server_name}' skipped: {secret_key} is not set in the secrets file")
        claude_key = self.server['secrets'].get('claude_token_key')
        claude_token = read_env_value(self.secrets_text, claude_key) if claude_key else None
        if claude_token:
            exports['CLAUDE_CODE_OAUTH_TOKEN'] = claude_token
        remote.run(
            f'umask 077; cat > {SECRETS_FILE}; '
            f'grep -q devkit-secrets ~/.bashrc || '
            f'sed -i \'1i [ -f {SECRETS_FILE} ] && . {SECRETS_FILE}\' ~/.bashrc',
            stdin=render_exports(exports),
        )
        self._trust_workspace(remote, servers)
        note = self.project.get('note')
        if note:
            remote.run(f'cat > {shlex.quote(self.workdir + "/" + NOTE_FILE)}', stdin=note.strip() + '\n')

    def _trust_workspace(self, remote, servers):
        claude_config = (
            'import json, os\n'
            'path = os.path.expanduser("~/.claude.json")\n'
            'data = json.load(open(path)) if os.path.exists(path) else {}\n'
            'data["hasCompletedOnboarding"] = True\n'
            f'project = data.setdefault("projects", {{}}).setdefault({self.workdir!r}, {{}})\n'
            'project["hasTrustDialogAccepted"] = True\n'
            'project["enableAllProjectMcpServers"] = True\n'
            f'data.setdefault("mcpServers", {{}}).update(json.loads({json.dumps(servers)!r}))\n'
            'json.dump(data, open(path, "w"))\n'
        )
        remote.run('python3 -', stdin=claude_config)

    def _share_host_key(self, remote):
        configured = self.server['ssh'].get('host_key')
        if not configured:
            return False
        path = Path(configured).expanduser()
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'devkit-environments',
                            '-f', str(path)], check=True)
        remote.run(host_key_script(), stdin=path.read_text())
        return True

    def _authorize_keys(self, remote):
        source = self.server['ssh'].get('authorize')
        if not source or not Path(source).expanduser().is_file():
            return 0
        keys = public_keys(Path(source).expanduser().read_text())
        script = (
            'import os, sys\n'
            'path = os.path.expanduser("~/.ssh/authorized_keys")\n'
            'have = open(path).read().splitlines() if os.path.exists(path) else []\n'
            'new = [key for key in sys.stdin.read().splitlines() if key and key not in have]\n'
            'open(path, "a").write("".join(key + "\\n" for key in new))\n'
            'os.chmod(path, 0o600)\n'
        )
        remote.run(f'python3 -c {shlex.quote(script)}', stdin='\n'.join(keys) + '\n')
        return len(keys)

    def code(self, name):
        container = self.proxmox.find(validate_name(name))
        remote = self.remote(container['vmid'])
        address = remote.target.split('@')[1]
        count = self._authorize_keys(remote)
        self._share_host_key(remote)
        if not count:
            raise DevkitError(
                "no keys to authorize: set [ssh] authorize in the server config to the file "
                "listing the public keys of the machine your editor runs on"
            )
        uri = editor_uri(self.user, address, self.workdir)
        if os.environ.get('VSCODE_IPC_HOOK_CLI') and subprocess.run(
            ['code', '--folder-uri', uri], capture_output=True,
        ).returncode == 0:
            print(f'opening {name} in VS Code')
            return
        print(f'{name} accepts the {count} key(s) from {self.server["ssh"]["authorize"]}')
        print(f'  host     {self.user}@{address}')
        print(f'  folder   {self.workdir}')
        print(f'  VS Code  Remote-SSH: Connect to Host… → {self.user}@{address}')
        print(f'  or run   code --folder-uri {uri}')

    def _worker_state(self, remote):
        script = (
            f'cd {RUNS_DIR} 2>/dev/null || {{ echo none; exit 0; }}; '
            '[ -e latest ] || { echo none; exit 0; }; '
            'id=$(readlink latest); echo "$id"; '
            '[ -f latest/exit ] && { cat latest/exit; cat latest/output; } || echo running'
        )
        lines = remote.run(script).splitlines()
        if not lines or lines[0] == 'none':
            return None, 'none', None
        run_id, state = lines[0], lines[1] if len(lines) > 1 else 'running'
        result = parse_worker_output('\n'.join(lines[2:])) if state != 'running' else None
        return run_id, state, result

    def task(self, name, prompt, resume=False, detach=False):
        container = self.proxmox.find(validate_name(name))
        if container['status'] != 'running':
            raise DevkitError(f"'{name}' is stopped; start it first")
        remote = self.remote(container['vmid'])
        run_id, state, result = self._worker_state(remote)
        if state == 'running':
            raise DevkitError(f"worker {run_id} is still running in '{name}'; wait for it with: devkit report {name} --wait")
        session = None
        if resume:
            session = (result or {}).get('session_id')
            if not session:
                raise DevkitError(f"'{name}' has no finished worker to continue")
        new_id = time.strftime('%Y%m%d-%H%M%S')
        worker = self.server.get('worker', {})
        args = worker.get('claude_args', list(WORKER_ARGS))
        timeout = worker.get('timeout_seconds', WORKER_TIMEOUT)
        remote.run(worker_script(self.workdir, new_id, args, resume=session, timeout=timeout), stdin=prompt)
        if detach:
            print(f"worker {new_id} started in '{name}'; collect it with: devkit report {name} --wait")
            return 0
        return self.report(name, wait=True)

    def report(self, name, wait=False, as_json=False):
        container = self.proxmox.find(validate_name(name))
        if as_json and container['status'] != 'running':
            print(json.dumps({'run_id': None, 'state': 'stopped'}))
            return 0
        remote = self.remote(container['vmid'])
        run_id, state, result = self._worker_state(remote)
        while wait and state == 'running':
            time.sleep(3)
            try:
                run_id, state, result = self._worker_state(remote)
            except DevkitError:
                continue
        if as_json:
            print(json.dumps(report_data(run_id, state, result)))
            return 0
        print(format_report(run_id, state, result))
        return 0 if state not in ('running',) and result and not result.get('is_error') else (2 if state == 'running' else 1 if state != 'none' else 0)

    def ship(self, name, message):
        container = self.proxmox.find(validate_name(name))
        if container['status'] != 'running':
            raise DevkitError(f"'{name}' is stopped; start it first")
        author = [
            subprocess.run(['git', 'config', key], cwd=self.project_root, capture_output=True, text=True).stdout.strip()
            for key in ('user.name', 'user.email')
        ]
        if not all(author):
            raise DevkitError('git user.name and user.email are not set on this machine')
        remote = self.remote(container['vmid'])
        with Agent(self.github_key) as env:
            return subprocess.run(
                remote.command(ship_script(self.workdir, message, *author), forward=True), env=env,
            ).returncode

    def _mirror_claude(self, remote):
        items = [item for item in MIRRORED if (CLAUDE_HOME / item).exists()]
        if items:
            pack = subprocess.Popen(
                ['tar', '-C', str(CLAUDE_HOME), '-chf', '-', *items],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            unpack = subprocess.run(
                remote.command('mkdir -p ~/.claude && tar -C ~/.claude -xf -'),
                stdin=pack.stdout, capture_output=True, text=True,
            )
            pack.wait()
            if unpack.returncode != 0:
                raise DevkitError(f'mirroring the Claude setup failed: {unpack.stderr.strip()[-200:]}')
        rewrite = (
            'import glob, os\n'
            f'old = {str(Path.home())!r}\n'
            'new = os.path.expanduser("~")\n'
            'base = os.path.expanduser("~/.claude")\n'
            'for path in glob.glob(base + "/plugins/*.json") + [base + "/settings.json"]:\n'
            '    if os.path.isfile(path):\n'
            '        text = open(path).read()\n'
            '        if old in text:\n'
            '            open(path, "w").write(text.replace(old, new))\n'
        )
        remote.run('python3 -', stdin=rewrite)

    def _tool_servers_here(self):
        try:
            result = subprocess.run(
                ['claude', 'mcp', 'list'], cwd=self.project_root, capture_output=True, text=True, timeout=110,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        return parse_mcp_list(result.stdout)

    def _tool_servers_inside(self, remote):
        script = (
            f'[ -f {SECRETS_FILE} ] && . {SECRETS_FILE}; export PATH="$HOME/.local/bin:$PATH"; '
            f'cd {shlex.quote(self.workdir)} && timeout 170 claude mcp list'
        )
        result = subprocess.run(remote.command(script), capture_output=True, text=True)
        return parse_mcp_list(result.stdout)

    def _report_tool_servers(self, here, inside):
        if not here:
            step('could not read the tool server list on this machine; nothing to compare')
            return
        total, missing, extra = compare_tool_servers(here, inside)
        step(f'{total - len(missing)} of {total} tool servers that work here also work inside')
        for name, status in missing:
            step(f'  NOT WORKING inside: {name} ({status})')
        for name in extra:
            step(f'  only inside: {name}')

    def check(self, name):
        container = self.proxmox.find(validate_name(name))
        step('checking tool servers')
        here = self._tool_servers_here()
        self._report_tool_servers(here, self._tool_servers_inside(self.remote(container['vmid'])))

    def _always_dropped(self):
        secrets = self.server['secrets']
        keys = {secrets['proxmox_token_key'], 'CLAUDE_CODE_OAUTH_TOKEN'}
        if secrets.get('claude_token_key'):
            keys.add(secrets['claude_token_key'])
        keys.update(
            entry['secret_key'] for entry in self.project.get('tool_servers', ()) if entry.get('secret_key')
        )
        return keys

    def _branch_of(self, container):
        return decode_description(self.proxmox.config(container['vmid']).get('description'))

    def list(self, as_json=False):
        environments = self.proxmox.environments()
        if as_json:
            print(json.dumps([environment_summary(c, self._branch_of(c)) for c in environments]))
            return
        if not environments:
            print('no environments')
            return
        rows = [('NAME', 'BRANCH', 'ADDRESS', 'STATE', 'MEMORY', 'UP')]
        for container in environments:
            running = container['status'] == 'running'
            branch = self._branch_of(container)
            address = self.proxmox.address(container['vmid'], timeout=3) if running else '-'
            memory = f"{container.get('mem', 0) / 2**20:.0f}/{container['maxmem'] / 2**20:.0f} MB"
            rows.append((
                container['name'][len(ENV_PREFIX):], branch, address, container['status'],
                memory, format_age(container.get('uptime', 0)) if running else '-',
            ))
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print('  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        print(f"\n{len(running_names(environments))} of {self.server['limits']['max_environments']} "
              f"running, {len(environments)} in total")

    def ssh(self, name, command):
        container = self.proxmox.find(validate_name(name))
        remote = self.remote(container['vmid'])
        script = None
        if command:
            script = f'cd {shlex.quote(self.workdir)} && ' + ' '.join(shlex.quote(part) for part in command)
        with Agent(self.github_key) as env:
            return subprocess.run(
                remote.command(script, forward=True, tty=sys.stdin.isatty()), env=env,
            ).returncode

    def claude(self, name, arguments):
        container = self.proxmox.find(validate_name(name))
        remote = self.remote(container['vmid'])
        if not arguments and self.server.get('claude', {}).get('remote_control', False):
            arguments = ['--remote-control', name]
        extra = ' '.join(shlex.quote(part) for part in arguments)
        script = (
            f'[ -f {SECRETS_FILE} ] && . {SECRETS_FILE}; export PATH="$HOME/.local/bin:$PATH"; '
            f'cd {shlex.quote(self.workdir)} && exec claude {extra}'
        )
        with Agent(self.github_key) as env:
            return subprocess.run(
                remote.command(script, forward=True, tty=sys.stdin.isatty()), env=env,
            ).returncode

    def _require_room(self, environments):
        limit = self.server['limits']['max_environments']
        names = limit_reached(environments, limit)
        if names:
            raise DevkitError(
                f"limit of {limit} running environments reached ({', '.join(names)}); stop or destroy one first"
            )

    def _require_memory(self):
        needed = self.server['limits'].get('min_free_memory_mb')
        if not needed:
            return
        free = free_memory_mb(self.proxmox.node_status())
        if free < needed:
            raise DevkitError(
                f'the server has {free} MB of memory free and {needed} MB is required; '
                'stop or destroy an environment first'
            )

    def _work_inside(self, container):
        if container['status'] != 'running':
            return None
        remote = self.remote(container['vmid'])
        probe = f'cd {shlex.quote(self.workdir)} && (git fetch -q origin 2>/dev/null || true) && {WORK_PROBE}'
        with Agent(self.github_key) as env:
            result = subprocess.run(
                remote.command(probe, forward=True), capture_output=True, text=True, env=env,
            )
        return parse_work(result.stdout) if result.returncode == 0 else None

    def _may_destroy(self, name, container, force):
        if force:
            return True
        work = self._work_inside(container)
        if work is None:
            print(f"'{name}' kept: it is not running, so its work cannot be checked; "
                  f'start it, or pass --force')
            return False
        summary = describe_work(work)
        if summary:
            print(f"'{name}' kept: {summary} would be lost")
            for line in (work['unpushed'] + work['dirty'])[:8]:
                print(f'    {line}')
            print('  push or commit the work, or pass --force')
            return False
        return True

    def destroy(self, name, assume_yes, force=False):
        container = self.proxmox.find(validate_name(name))
        if not self._may_destroy(name, container, force):
            return 1
        if not assume_yes:
            answer = input(f"destroy '{name}'? [y/N] ")
            if answer.strip().lower() not in ('y', 'yes'):
                print('kept')
                return 0
        self._remove(container['vmid'], running=container['status'] == 'running')
        print(f"'{name}' destroyed")
        return 0

    def destroy_all(self, assume_yes, force=False):
        environments = self.proxmox.environments()
        if not environments:
            print('no environments')
            return 0
        names = [container['name'][len(ENV_PREFIX):] for container in environments]
        if not assume_yes:
            answer = input(f"destroy {len(names)} environment(s): {', '.join(names)}? [y/N] ")
            if answer.strip().lower() not in ('y', 'yes'):
                print('kept')
                return 0
        kept = 0
        for name, container in zip(names, environments):
            if self._may_destroy(name, container, force):
                self._remove(container['vmid'], running=container['status'] == 'running')
                print(f"'{name}' destroyed")
            else:
                kept += 1
        return 1 if kept else 0

    def stop(self, name):
        container = self.proxmox.find(validate_name(name))
        if container['status'] != 'running':
            print(f"'{name}' is already stopped")
            return
        self.proxmox.shutdown(container['vmid'])
        print(f"'{name}' stopped; its files are kept and its memory is freed")

    def start(self, name):
        container = self.proxmox.find(validate_name(name))
        if container['status'] == 'running':
            print(f"'{name}' is already running")
            return
        self._require_room(self.proxmox.environments())
        self._require_memory()
        self.proxmox.start(container['vmid'])
        remote = self.remote(container['vmid'])
        remote.wait()
        print(f"'{name}' running at {remote.target.split('@')[1]}; containers inside are not restarted")

    def _remove(self, vmid, running=True):
        if running:
            try:
                self.proxmox.stop(vmid)
            except DevkitError:
                pass
        self.proxmox.delete(vmid)

    def template_list(self):
        templates = [
            c for c in self.proxmox.templates()
            if c.get('name') == BASE_TEMPLATE or c.get('name', '').startswith(PROJECT_TEMPLATE_PREFIX)
        ]
        legacy = self.server['proxmox'].get('template')
        if not templates and not legacy:
            print('no templates; build one with: devkit template build')
            return
        rows = [('TEMPLATE', 'CONTAINER', 'FOR')]
        for container in templates:
            name = container['name']
            purpose = 'every project' if name == BASE_TEMPLATE else name[len(PROJECT_TEMPLATE_PREFIX):]
            rows.append((name, str(container['vmid']), purpose))
        widths = [max(len(row[i]) for row in rows) for i in range(3)]
        for row in rows if templates else []:
            print('  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        for container in self._leftover_builds():
            print(f"leftover from a failed build: {container['name']} (container {container['vmid']}); "
                  'remove it with: devkit template clean')
        if self.slug:
            _, chosen = choose_template(self.proxmox.containers(), self.slug, legacy)
            print(f'\n{self.slug} uses: {chosen or "nothing yet"}')
        if legacy:
            print(f'[proxmox] template = {legacy} is still set; it is only used when no named template exists')

    def _leftover_builds(self):
        return [
            c for c in self.proxmox.containers()
            if c.get('name', '').startswith(BUILD_PREFIX) and not c.get('template')
        ]

    def template_clean(self):
        leftovers = self._leftover_builds()
        if not leftovers:
            print('nothing to clean')
            return 0
        for container in leftovers:
            with build_lock(container['name']):
                self._remove(container['vmid'], running=container['status'] == 'running')
            print(f"removed {container['name']} (container {container['vmid']})")
        return 0

    def template_build(self, base=False, fresh=False):
        if fresh and not (base or self.project is None):
            raise DevkitError('--fresh rebuilds the base from the OS image; use it with --base')
        if base or self.project is None:
            return self._build_base(fresh)
        if not self.slug:
            raise DevkitError(f'this folder name cannot name a template; set name = "..." in {PROJECT_CONFIG_NAME}')
        if not self._frozen(BASE_TEMPLATE):
            step('no base template yet, building it first')
            self._build_base(fresh)
        return self._build_project()

    def _frozen(self, name):
        return [c for c in self.proxmox.named(name) if c.get('template')]

    def _recover_or_discard(self, build_name, final_name):
        for container in self.proxmox.named(build_name):
            if container.get('template') and not self._frozen(final_name):
                step(f'finishing an interrupted build: container {container["vmid"]} becomes {final_name}')
                self.proxmox.set_config(container['vmid'], hostname=final_name)
            elif container.get('template'):
                self.proxmox.delete(container['vmid'])
            else:
                step(f'removing a leftover build container ({container["vmid"]})')
                self._remove(container['vmid'], running=container['status'] == 'running')

    def _start_build(self, vmid):
        self._apply_network(vmid)
        self.proxmox.start(vmid)
        address = self.proxmox.address(vmid)
        root = Remote(address, 'root', self.key)
        root.wait()
        return root, Remote(address, self.user, self.key)

    def _abandon(self, vmid, build_name):
        try:
            self.proxmox.stop(vmid)
        except DevkitError:
            pass
        step(f'build failed; container {vmid} ({build_name}) is stopped and kept for inspection; '
             'the next build of this template removes it, or run: devkit template clean')

    def _freeze(self, vmid, root, final_name):
        root.run(IDENTITY_WIPE)
        self.proxmox.shutdown(vmid)
        with clone_lock(wait_seconds=300):
            self.proxmox.to_template(vmid)
            deadline = time.time() + 60
            while not any(c.get('template') and int(c['vmid']) == int(vmid) for c in self.proxmox.containers()):
                if time.time() >= deadline:
                    raise DevkitError(f'container {vmid} was frozen but the server does not list it as a template')
                time.sleep(1)
            for old in self.proxmox.named(final_name):
                step(f'replacing the previous {final_name} ({old["vmid"]})')
                self.proxmox.delete(old['vmid'])
            self.proxmox.set_config(vmid, hostname=final_name)

    def _docker_works(self, root, vmid):
        if root.succeeds(DOCKER_PROBE):
            return
        if not root.succeeds(DOCKER_PULL):
            raise DevkitError(
                'the build container cannot pull the hello-world image; check its network and Docker Hub access'
            )
        print()
        print('  Docker cannot start containers inside this container yet.')
        print('  Proxmox only lets root change that. On the Proxmox server, as root, run:')
        print()
        for command in root_step_commands(vmid):
            print(f'    {command}')
        print()
        step('waiting for those three commands (up to 30 minutes)')
        deadline = time.time() + ROOT_STEP_TIMEOUT
        while time.time() < deadline:
            time.sleep(10)
            if root.succeeds(DOCKER_PROBE):
                step('Docker works now')
                return
        raise DevkitError('Docker still cannot start containers after 30 minutes')

    def _build_base(self, fresh=False):
        settings = self.template
        prepare = prepare_commands(settings['prepare'], '[template] in the server config')
        build_name = BUILD_PREFIX + 'base'
        with build_lock(build_name):
            started = time.time()
            self._recover_or_discard(build_name, BASE_TEMPLATE)
            current = self._frozen(BASE_TEMPLATE)
            with clone_lock(wait_seconds=300):
                vmid = self.proxmox.next_id()
                if current and not fresh:
                    step(f'updating {BASE_TEMPLATE}: cloning it into container {vmid}')
                    self.proxmox.clone(
                        current[0]['vmid'], vmid, build_name, self.server['proxmox']['pool'],
                        encode_template_description(),
                    )
                else:
                    public_key = Path(str(Path(self.key).expanduser()) + '.pub').read_text().strip()
                    if self.proxmox.ensure_os_template(settings['os_template']):
                        step(f"downloaded {settings['os_template']}")
                    step(f"creating container {vmid} from {settings['os_template'].rsplit('/', 1)[-1]}")
                    self.proxmox.create(**new_container_params(
                        vmid, build_name, self.server['proxmox']['pool'], settings, public_key,
                    ))
            try:
                root, user = self._start_build(vmid)
                step('installing or updating Docker, Node, uv, Chrome and the tools')
                root.stream('bash -s', stdin=provision_script(self.user, settings['node_major']), label='provisioning')
                user.wait()
                step('installing or updating Claude Code')
                user.stream(CLAUDE_INSTALL, label='installing Claude Code')
                self._docker_works(root, vmid)
                for command in prepare:
                    step(f'prepare: {command[:70]}')
                    user.stream(f'export PATH="$HOME/.local/bin:$PATH"; {command}', label=command)
                root.run(REMOVE_CONTAINERS)
                root.run('docker image rm -f hello-world >/dev/null 2>&1 || true')
                step('freezing')
                self._freeze(vmid, root, BASE_TEMPLATE)
            except BaseException:
                self._abandon(vmid, build_name)
                raise
            print(f'\n{BASE_TEMPLATE} ready in {time.time() - started:.0f}s (container {vmid})')
            return 0

    def _build_project(self):
        final_name = PROJECT_TEMPLATE_PREFIX + self.slug
        build_name = BUILD_PREFIX + 'tpl-' + self.slug
        prepare = prepare_commands(
            self.project.get('template', {}).get('prepare'), f'[template] in {PROJECT_CONFIG_NAME}',
        )
        blank_settings = blank_env(self._rewritten_settings(warn=False))
        target = self._settings_target()
        with build_lock(build_name):
            started = time.time()
            for existing in self._frozen(final_name):
                self._require_same_repo(existing['vmid'], final_name)
            self._recover_or_discard(build_name, final_name)
            base = self._frozen(BASE_TEMPLATE)
            if not base:
                raise DevkitError(f'{BASE_TEMPLATE} is missing; build it with: devkit template build --base')
            with clone_lock(wait_seconds=300):
                vmid = self.proxmox.next_id()
                step(f'cloning {BASE_TEMPLATE} into container {vmid}')
                self.proxmox.clone(
                    base[0]['vmid'], vmid, build_name, self.server['proxmox']['pool'],
                    encode_template_description(self.project['repo']),
                )
            try:
                root, user = self._start_build(vmid)
                user.wait()
                step(f"cloning {self.project['repo']} at {self.project['base_branch']}")
                self._checkout(user, self.project['base_branch'])
                user.run(f'umask 077; cat > {shlex.quote(target)}', stdin=blank_settings)
                for command in prepare:
                    step(f'prepare: {command[:70]}')
                    user.stream(
                        f'export PATH="$HOME/.local/bin:$PATH"; cd {shlex.quote(self.workdir)} && {command}',
                        label=command,
                    )
                if self.server.get('claude', {}).get('mirror', True):
                    step('warming up the Claude tool servers')
                    self._mirror_claude(user)
                    self._trust_workspace(user, {})
                    user.succeeds(
                        f'export PATH="$HOME/.local/bin:$PATH"; cd {shlex.quote(self.workdir)} '
                        '&& timeout 240 claude mcp list'
                    )
                user.run(f'cd {shlex.quote(self.workdir)} && git checkout -q -- . 2>/dev/null; true')
                user.run(remove_files_script((target, *CLAUDE_STATE)))
                user.run(REMOVE_CONTAINERS)
                if user.run(leftover_check((target, *CLAUDE_STATE))).strip() != 'clean':
                    raise DevkitError('settings or Claude files are still inside the build container; not freezing it')
                step('freezing')
                self._freeze(vmid, root, final_name)
            except BaseException:
                self._abandon(vmid, build_name)
                raise
            print(f'\n{final_name} ready in {time.time() - started:.0f}s (container {vmid})')
            print(f'  new environments for {self.slug} start from it')
            return 0


def format_age(seconds):
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 86400:
        return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'
    return f'{seconds // 86400}d{seconds % 86400 // 3600}h'


def step(message):
    print(f'  {message}', flush=True)


REPO_ROOT = Path(__file__).resolve().parent
COMMAND_LINK = Path('~/.local/bin/devkit').expanduser()
CLAUDE_SETTINGS = CLAUDE_HOME / 'settings.json'
PERMITTED_COMMANDS = ('create', 'list', 'check', 'task', 'report', 'ssh', 'claude', 'code', 'stop', 'start')
REQUIRED_SERVER_KEYS = (
    ('proxmox', 'host'), ('proxmox', 'node'), ('proxmox', 'pool'),
    ('limits', 'max_environments'),
    ('ssh', 'user'), ('ssh', 'key'), ('ssh', 'github_key'),
    ('secrets', 'file'), ('secrets', 'proxmox_token_key'),
)
SERVER_CONFIG_TEMPLATE = '''[proxmox]
host = ""
node = ""
pool = "devkit"

[template]
os_template = "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
storage = "local-lvm"
bridge = "vmbr0"
disk_gb = 20
cores = 4
memory_mb = 3072

[limits]
max_environments = 3
min_free_memory_mb = 2048

[ssh]
user = "dev"
key = "~/.ssh/devkit_ed25519"
github_key = "~/.ssh/id_ed25519"
authorize = "~/.ssh/authorized_keys"

[secrets]
file = ""
proxmox_token_key = "PVEAPIToken"
claude_token_key = "CLAUDE_CODE_OAUTH_TOKEN"

[variables]
shared_host = ""
'''


def permission_rules():
    return [f'Bash(devkit {command}:*)' for command in PERMITTED_COMMANDS]


def add_permission_rules(settings, rules):
    allow = settings.setdefault('permissions', {}).setdefault('allow', [])
    added = [rule for rule in rules if rule not in allow]
    allow.extend(added)
    return added


def remove_permission_rules(settings, rules):
    allow = settings.get('permissions', {}).get('allow', [])
    removed = [rule for rule in allow if rule in rules]
    if removed:
        kept = [rule for rule in allow if rule not in rules]
        if kept:
            settings['permissions']['allow'] = kept
        else:
            del settings['permissions']['allow']
    return removed


def read_settings(path):
    return json.loads(path.read_text()) if path.exists() else {}


def write_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + '\n')


def missing_server_keys(config):
    return [
        f'[{section}] {key}' for section, key in REQUIRED_SERVER_KEYS
        if config.get(section, {}).get(key) in (None, '', 0)
    ]


class Setup:

    def __init__(self, project_dir):
        self.pending = 0
        try:
            self.project_root = find_project_config(project_dir).parent
        except DevkitError:
            self.project_root = None

    def ok(self, item, detail=''):
        print(f'  ok        {item}' + (f': {detail}' if detail else ''))

    def done(self, item, detail):
        print(f'  done      {item}: {detail}')

    def needs_you(self, item, detail):
        self.pending += 1
        print(f'  NEEDS YOU {item}: {detail}')

    def run(self):
        print('devkit setup')
        self.command_link()
        self.skills()
        self.permissions()
        server = self.server_config()
        if server:
            self.ssh_keys(server)
            self.secrets_and_proxmox(server)
        print()
        if self.pending:
            print(f'{self.pending} item(s) need you; run `devkit setup` again afterwards')
            return 1
        print('everything is in place')
        return 0

    def link(self, item, link, target):
        if link.is_symlink() and link.resolve() == target.resolve():
            self.ok(item)
            return
        if link.exists() or link.is_symlink():
            self.needs_you(item, f'{link} exists and is not a link to {target}; remove it and run setup again')
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        self.done(item, f'linked {link} -> {target}')

    def command_link(self):
        self.link('devkit command', COMMAND_LINK, REPO_ROOT / 'devkit.py')
        if str(COMMAND_LINK.parent) not in os.environ.get('PATH', '').split(os.pathsep):
            self.needs_you('PATH', f'{COMMAND_LINK.parent} is not on your PATH')

    def skills(self):
        for skill in sorted((REPO_ROOT / 'skills').iterdir()):
            if skill.is_dir():
                self.link(f'skill {skill.name}', CLAUDE_HOME / 'skills' / skill.name, skill)

    def permissions(self):
        self.project_permissions()
        self.machine_permissions()

    def project_permissions(self):
        item = 'Claude Code permissions'
        if self.project_root is None:
            print(f'  skipped   {item}: no {PROJECT_CONFIG_NAME} here; '
                  'run `devkit setup` inside a project to allow devkit there')
            return
        path = self.project_root / '.claude' / 'settings.json'
        try:
            settings = read_settings(path)
        except ValueError:
            self.needs_you(item, f'{path} is not valid JSON; fix it and run setup again')
            return
        added = add_permission_rules(settings, permission_rules())
        if not added:
            self.ok(item, f'{len(PERMITTED_COMMANDS)} devkit commands allowed in {self.project_root.name}; '
                          'destroy and ship still ask')
            return
        write_settings(path, settings)
        self.done(item, f'allowed {len(added)} command(s) in {path}')
        for rule in added:
            print(f'              {rule}')
        print('              not allowed on purpose: devkit destroy, devkit ship')
        print('              this file belongs to the project: review and commit it')
        print('              restart Claude Code sessions to pick this up')

    def machine_permissions(self):
        item = 'machine-wide permissions'
        try:
            settings = read_settings(CLAUDE_SETTINGS)
        except ValueError:
            return
        removed = remove_permission_rules(settings, permission_rules())
        if not removed:
            return
        backup = CLAUDE_SETTINGS.with_name(CLAUDE_SETTINGS.name + '.before-devkit')
        backup.write_text(CLAUDE_SETTINGS.read_text())
        write_settings(CLAUDE_SETTINGS, settings)
        self.done(item, f'removed {len(removed)} devkit rule(s) from {CLAUDE_SETTINGS}; '
                        'permissions are per project now')

    def server_config(self):
        item = 'server config'
        if not SERVER_CONFIG.exists():
            SERVER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            SERVER_CONFIG.write_text(SERVER_CONFIG_TEMPLATE)
            self.needs_you(item, f'wrote a blank {SERVER_CONFIG}; fill it in (see README, "Server config")')
            return None
        try:
            server = load_toml(SERVER_CONFIG)
        except tomllib.TOMLDecodeError as error:
            self.needs_you(item, f'{SERVER_CONFIG} does not parse: {error}')
            return None
        missing = missing_server_keys(server)
        if missing:
            self.needs_you(item, f'{SERVER_CONFIG} is missing ' + ', '.join(missing))
            return None
        self.ok(item, str(SERVER_CONFIG))
        return server

    def ssh_keys(self, server):
        key = Path(server['ssh']['key']).expanduser()
        if key.exists():
            self.ok('environment SSH key')
        else:
            key.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'devkit', '-f', str(key)], check=True,
            )
            self.done('environment SSH key', f'created {key}; templates built from now on accept it')
        github_key = Path(server['ssh']['github_key']).expanduser()
        if github_key.exists():
            self.ok('GitHub SSH key')
        else:
            self.needs_you('GitHub SSH key', f'{github_key} does not exist')

    def secrets_and_proxmox(self, server):
        secrets = server['secrets']
        path = Path(secrets['file']).expanduser()
        if not path.is_file():
            self.needs_you('secrets file', f'{path} does not exist')
            return
        text = path.read_text()
        token = read_env_value(text, secrets['proxmox_token_key'])
        if not token:
            self.needs_you('Proxmox token', f"{secrets['proxmox_token_key']} is not set in {path}")
            return
        claude_key = secrets.get('claude_token_key')
        if claude_key and read_env_value(text, claude_key):
            self.ok('Claude token')
        else:
            self.needs_you(
                'Claude token',
                f'run `claude setup-token` and put the result in {path} as {claude_key or "CLAUDE_CODE_OAUTH_TOKEN"}',
            )
        try:
            containers = Proxmox(server['proxmox'], token).containers()
        except DevkitError as error:
            self.needs_you('Proxmox access', str(error))
            return
        self.ok('Proxmox access')
        slug = None
        if self.project_root is not None:
            try:
                project = load_toml(self.project_root / PROJECT_CONFIG_NAME)
                slug = project_slug(project.get('name') or self.project_root.name)
            except (DevkitError, tomllib.TOMLDecodeError):
                slug = None
        _, name = choose_template(containers, slug, server['proxmox'].get('template'))
        if name is None:
            self.needs_you('template', 'none on the server yet; build one with: devkit template build')
        elif slug and name != PROJECT_TEMPLATE_PREFIX + slug:
            self.ok('template', f'{name}; `devkit template build` here makes one with {slug} prepared inside')
        else:
            self.ok('template', name)


def build_parser():
    parser = argparse.ArgumentParser(prog='devkit', description='Disposable development environments')
    parser.add_argument('-C', dest='project_dir', default='.', help='project folder holding .devkit.toml')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('setup', help='install devkit on this machine and report what is missing')
    template = commands.add_parser('template', help='build or list the templates environments start from')
    template_actions = template.add_subparsers(dest='template_action', required=True)
    build = template_actions.add_parser(
        'build', help="inside a project: that project's template; elsewhere: the base template",
    )
    build.add_argument('--base', action='store_true', help='build or update the base template')
    build.add_argument('--fresh', action='store_true', help='start the base from the OS image, not from the current base')
    template_actions.add_parser('list', help='list templates')
    template_actions.add_parser('clean', help='remove containers left by failed builds')
    create = commands.add_parser('create', help='create an environment')
    create.add_argument('name')
    create.add_argument('branch', nargs='?', help='defaults to the name')
    create.add_argument('--no-check', action='store_true', help='skip the tool server comparison')
    check = commands.add_parser('check', help='compare tool servers here and inside an environment')
    check.add_argument('name')
    listing = commands.add_parser('list', help='list environments')
    listing.add_argument('--json', dest='as_json', action='store_true', help='name, branch and state for scripts')
    shell = commands.add_parser('ssh', help='open a shell, or run a command after --')
    shell.add_argument('name')
    shell.add_argument('remote_command', nargs=argparse.REMAINDER)
    claude = commands.add_parser('claude', help='open Claude Code inside an environment')
    claude.add_argument('name')
    claude.add_argument('claude_arguments', nargs=argparse.REMAINDER)
    task = commands.add_parser('task', help='hand a task to a Claude worker inside and print its report')
    task.add_argument('name')
    task.add_argument('prompt', help="the task; '-' reads it from standard input")
    task.add_argument('--continue', dest='resume', action='store_true', help="continue the last worker's session")
    task.add_argument('--detach', action='store_true', help='return at once; collect with report')
    report = commands.add_parser('report', help="print the last worker's report")
    report.add_argument('name')
    report.add_argument('--wait', action='store_true', help='block until the worker finishes')
    report.add_argument('--json', dest='as_json', action='store_true', help='machine-readable state and report')
    ship = commands.add_parser('ship', help='commit everything inside an environment and push its branch')
    ship.add_argument('name')
    ship.add_argument('-m', '--message', required=True)
    code = commands.add_parser('code', help='open an environment in VS Code over Remote-SSH')
    code.add_argument('name')
    destroy = commands.add_parser('destroy', help='destroy an environment')
    destroy.add_argument('name', nargs='?')
    destroy.add_argument('--all', action='store_true', help='destroy every environment')
    destroy.add_argument('-y', '--yes', action='store_true', help='skip the confirmation prompt')
    destroy.add_argument('--force', action='store_true', help='destroy even with unpushed or unchecked work')
    stop = commands.add_parser('stop', help='stop an environment, keeping its files')
    stop.add_argument('name')
    start = commands.add_parser('start', help='start a stopped environment')
    start.add_argument('name')
    return parser


def strip_separator(arguments):
    return arguments[1:] if arguments[:1] == ['--'] else arguments


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == 'setup':
            return Setup(Path(arguments.project_dir).resolve()).run()
        if arguments.command == 'template':
            devkit = Devkit(Path(arguments.project_dir).resolve(), need_project=False)
            if arguments.template_action == 'list':
                return devkit.template_list()
            if arguments.template_action == 'clean':
                return devkit.template_clean()
            return devkit.template_build(base=arguments.base, fresh=arguments.fresh)
        devkit = Devkit(Path(arguments.project_dir).resolve())
        if arguments.command == 'create':
            devkit.create(arguments.name, arguments.branch, check=not arguments.no_check)
        elif arguments.command == 'check':
            devkit.check(arguments.name)
        elif arguments.command == 'list':
            devkit.list(as_json=arguments.as_json)
        elif arguments.command == 'ssh':
            return devkit.ssh(arguments.name, strip_separator(arguments.remote_command))
        elif arguments.command == 'claude':
            return devkit.claude(arguments.name, strip_separator(arguments.claude_arguments))
        elif arguments.command == 'task':
            prompt = sys.stdin.read() if arguments.prompt == '-' else arguments.prompt
            if not prompt.strip():
                raise DevkitError('the task is empty')
            return devkit.task(arguments.name, prompt, resume=arguments.resume, detach=arguments.detach)
        elif arguments.command == 'report':
            return devkit.report(arguments.name, wait=arguments.wait, as_json=arguments.as_json)
        elif arguments.command == 'ship':
            return devkit.ship(arguments.name, arguments.message)
        elif arguments.command == 'code':
            devkit.code(arguments.name)
        elif arguments.command == 'destroy':
            if arguments.all == bool(arguments.name):
                raise DevkitError('give an environment name, or --all')
            if arguments.all:
                return devkit.destroy_all(arguments.yes, arguments.force)
            return devkit.destroy(arguments.name, arguments.yes, arguments.force)
        elif arguments.command == 'stop':
            devkit.stop(arguments.name)
        elif arguments.command == 'start':
            devkit.start(arguments.name)
    except DevkitError as error:
        print(f'devkit: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
