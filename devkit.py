#!/usr/bin/env python3
import argparse
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


def public_keys(text):
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and line not in keys:
            keys.append(line)
    return keys


def editor_uri(user, address, workdir):
    return f'vscode-remote://ssh-remote+{user}@{address}{workdir}'


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

    def __init__(self, config, token):
        self.base = f"https://{config['host']}:{config.get('port', 8006)}/api2/json"
        self.node = config['node']
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
                if interface.get('name') == 'eth0' and interface.get('inet'):
                    return interface['inet'].split('/')[0]
            time.sleep(0.5)
        raise DevkitError(f'container {vmid} got no network address')

    def clone(self, template, vmid, hostname, pool, description):
        self.wait(self.call(
            'POST', f'/nodes/{self.node}/lxc/{template}/clone',
            newid=vmid, hostname=hostname, pool=pool, description=description,
        ))

    def set_network(self, vmid, net0):
        self.call('PUT', f'/nodes/{self.node}/lxc/{vmid}/config', net0=net0)

    def start(self, vmid):
        self.wait(self.call('POST', f'/nodes/{self.node}/lxc/{vmid}/status/start'))

    def stop(self, vmid):
        self.wait(self.call('POST', f'/nodes/{self.node}/lxc/{vmid}/status/stop'))

    def delete(self, vmid):
        self.wait(self.call('DELETE', f'/nodes/{self.node}/lxc/{vmid}?purge=1'))

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


class Devkit:

    def __init__(self, project_dir):
        self.server = load_toml(SERVER_CONFIG)
        self.project_config_path = find_project_config(project_dir)
        variables = {name: str(value) for name, value in self.server.get('variables', {}).items()}
        self.project = expand(load_toml(self.project_config_path), variables)
        self.project_root = self.project_config_path.parent
        secrets_path = Path(self.server['secrets']['file']).expanduser()
        self.secrets_text = secrets_path.read_text()
        token = read_env_value(self.secrets_text, self.server['secrets']['proxmox_token_key'])
        if not token:
            raise DevkitError(f"{self.server['secrets']['proxmox_token_key']} not found in {secrets_path}")
        self.proxmox = Proxmox(self.server['proxmox'], token)
        self.user = self.server['ssh']['user']
        self.key = self.server['ssh']['key']
        self.github_key = self.server['ssh']['github_key']
        self.workdir = self.project['workdir']

    def remote(self, vmid):
        return Remote(self.proxmox.address(vmid), self.user, self.key)

    def create(self, name, branch, check=True):
        validate_name(name)
        branch = branch or name
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOCK_PATH, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DevkitError('another create is running, try again in a moment') from None
            self._create(name, branch, check)

    def _create(self, name, branch, check=True):
        existing = self.proxmox.environments()
        if any(c['name'] == ENV_PREFIX + name for c in existing):
            raise DevkitError(f"environment '{name}' already exists")
        limit = self.server['limits']['max_environments']
        if len(existing) >= limit:
            names = ', '.join(c['name'][len(ENV_PREFIX):] for c in existing)
            raise DevkitError(f'limit of {limit} environments reached ({names}); destroy one first')
        vmid = self.proxmox.next_id()
        started = time.time()
        here = {}
        probe = threading.Thread(target=lambda: here.update(self._tool_servers_here()), daemon=True)
        if check:
            probe.start()
        step(f'cloning template into container {vmid}')
        self.proxmox.clone(
            self.server['proxmox']['template'], vmid, ENV_PREFIX + name,
            self.server['proxmox']['pool'], encode_description(branch),
        )
        try:
            network = self.server.get('network')
            if network:
                self.proxmox.set_network(
                    vmid, static_network(self.proxmox.config(vmid)['net0'], vmid, network),
                )
            self.proxmox.start(vmid)
            remote = self.remote(vmid)
            remote.wait()
            step(f'checking out {branch}')
            self._checkout(remote, branch)
            step('pushing settings')
            self._push_settings(remote)
            self._authorize_keys(remote)
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

    def _push_settings(self, remote):
        settings = self.project.get('settings', {})
        source = (self.project_root / settings.get('source', '.env')).read_text()
        drop = self._always_dropped() | set(settings.get('drop', ()))
        for unused in unmatched_replacements(source, drop, settings.get('force'), settings.get('replace')):
            step(f"warning: replace rule '{unused}' matched nothing in {settings.get('source', '.env')}")
        rewritten = rewrite_env(
            source, drop=drop, force=settings.get('force'), replace=settings.get('replace'),
        )
        target = shlex.quote(f"{self.workdir}/{settings.get('target', '.env')}")
        remote.run(f'umask 077; cat > {target}', stdin=rewritten)

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
        note = self.project.get('note')
        if note:
            remote.run(f'cat > {shlex.quote(self.workdir + "/" + NOTE_FILE)}', stdin=note.strip() + '\n')

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

    def list(self):
        environments = self.proxmox.environments()
        if not environments:
            print('no environments')
            return
        rows = [('NAME', 'BRANCH', 'ADDRESS', 'STATE', 'MEMORY', 'UP')]
        for container in environments:
            running = container['status'] == 'running'
            branch = decode_description(self.proxmox.config(container['vmid']).get('description'))
            address = self.proxmox.address(container['vmid'], timeout=3) if running else '-'
            memory = f"{container.get('mem', 0) / 2**20:.0f}/{container['maxmem'] / 2**20:.0f} MB"
            rows.append((
                container['name'][len(ENV_PREFIX):], branch, address, container['status'],
                memory, format_age(container.get('uptime', 0)) if running else '-',
            ))
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print('  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        print(f"\n{len(environments)} of {self.server['limits']['max_environments']} environments")

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
        extra = ' '.join(shlex.quote(part) for part in arguments)
        script = (
            f'[ -f {SECRETS_FILE} ] && . {SECRETS_FILE}; export PATH="$HOME/.local/bin:$PATH"; '
            f'cd {shlex.quote(self.workdir)} && exec claude {extra}'
        )
        with Agent(self.github_key) as env:
            return subprocess.run(
                remote.command(script, forward=True, tty=sys.stdin.isatty()), env=env,
            ).returncode

    def destroy(self, name, assume_yes):
        container = self.proxmox.find(validate_name(name))
        if not assume_yes:
            answer = input(f"destroy '{name}'? unpushed work inside is lost [y/N] ")
            if answer.strip().lower() not in ('y', 'yes'):
                print('kept')
                return
        self._remove(container['vmid'], running=container['status'] == 'running')
        print(f"'{name}' destroyed")

    def _remove(self, vmid, running=True):
        if running:
            try:
                self.proxmox.stop(vmid)
            except DevkitError:
                pass
        self.proxmox.delete(vmid)


def format_age(seconds):
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 86400:
        return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'
    return f'{seconds // 86400}d{seconds % 86400 // 3600}h'


def step(message):
    print(f'  {message}', flush=True)


def build_parser():
    parser = argparse.ArgumentParser(prog='devkit', description='Disposable development environments')
    parser.add_argument('-C', dest='project_dir', default='.', help='project folder holding .devkit.toml')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='create an environment')
    create.add_argument('name')
    create.add_argument('branch', nargs='?', help='defaults to the name')
    create.add_argument('--no-check', action='store_true', help='skip the tool server comparison')
    check = commands.add_parser('check', help='compare tool servers here and inside an environment')
    check.add_argument('name')
    commands.add_parser('list', help='list environments')
    shell = commands.add_parser('ssh', help='open a shell, or run a command after --')
    shell.add_argument('name')
    shell.add_argument('remote_command', nargs=argparse.REMAINDER)
    claude = commands.add_parser('claude', help='open Claude Code inside an environment')
    claude.add_argument('name')
    claude.add_argument('claude_arguments', nargs=argparse.REMAINDER)
    code = commands.add_parser('code', help='open an environment in VS Code over Remote-SSH')
    code.add_argument('name')
    destroy = commands.add_parser('destroy', help='destroy an environment')
    destroy.add_argument('name')
    destroy.add_argument('-y', '--yes', action='store_true', help='skip the confirmation prompt')
    return parser


def strip_separator(arguments):
    return arguments[1:] if arguments[:1] == ['--'] else arguments


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        devkit = Devkit(Path(arguments.project_dir).resolve())
        if arguments.command == 'create':
            devkit.create(arguments.name, arguments.branch, check=not arguments.no_check)
        elif arguments.command == 'check':
            devkit.check(arguments.name)
        elif arguments.command == 'list':
            devkit.list()
        elif arguments.command == 'ssh':
            return devkit.ssh(arguments.name, strip_separator(arguments.remote_command))
        elif arguments.command == 'claude':
            return devkit.claude(arguments.name, strip_separator(arguments.claude_arguments))
        elif arguments.command == 'code':
            devkit.code(arguments.name)
        elif arguments.command == 'destroy':
            devkit.destroy(arguments.name, arguments.yes)
    except DevkitError as error:
        print(f'devkit: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
