import unittest

import devkit


class TestRewriteEnv(unittest.TestCase):

    SOURCE = (
        "# comment\n"
        "MYSQL_HOST=mysql\n"
        "PVEAPIToken=user@pam!name=secret\n"
        "MONGO_URI=mongodb://root:pw@mongodb:27017/droppo?authSource=admin\n"
        "\n"
        "EMAIL_SEND_ENABLED=true\n"
    )

    def rewrite(self):
        return devkit.rewrite_env(
            self.SOURCE,
            drop=('PVEAPIToken',),
            force={'MYSQL_HOST': '10.0.0.5', 'EMAIL_SEND_ENABLED': 'false', 'SENTRY_DSN': ''},
            replace={'@mongodb:': '@10.0.0.5:'},
        )

    def test_dropped_keys_never_reach_the_environment(self):
        self.assertNotIn('PVEAPIToken', self.rewrite())
        self.assertNotIn('secret', self.rewrite())

    def test_forced_values_replace_existing_ones(self):
        lines = self.rewrite().splitlines()
        self.assertIn('MYSQL_HOST=10.0.0.5', lines)
        self.assertIn('EMAIL_SEND_ENABLED=false', lines)
        self.assertNotIn('MYSQL_HOST=mysql', lines)

    def test_forced_values_missing_from_the_source_are_appended(self):
        self.assertEqual(self.rewrite().splitlines()[-1], 'SENTRY_DSN=')

    def test_replacements_apply_inside_values(self):
        self.assertIn(
            'MONGO_URI=mongodb://root:pw@10.0.0.5:27017/droppo?authSource=admin',
            self.rewrite().splitlines(),
        )

    def test_comments_and_blank_lines_survive(self):
        lines = self.rewrite().splitlines()
        self.assertEqual(lines[0], '# comment')
        self.assertIn('', lines)

    def test_a_forced_key_is_not_also_subject_to_replacement(self):
        result = devkit.rewrite_env('A=//mysql:1\n', force={'A': '//mysql:2'}, replace={'//mysql:': '//x:'})
        self.assertEqual(result, 'A=//mysql:2\n')


class TestUnmatchedReplacements(unittest.TestCase):

    def test_a_rule_that_matches_nothing_is_reported(self):
        self.assertEqual(
            devkit.unmatched_replacements('A=//db:1\n', replace={'//db:': '//x:', '@mongo:': '@x:'}),
            ['@mongo:'],
        )

    def test_a_match_only_inside_a_dropped_or_forced_key_does_not_count(self):
        text = 'A=@mongo:1\nB=@mongo:2\n'
        self.assertEqual(
            devkit.unmatched_replacements(text, drop=('A',), force={'B': 'x'}, replace={'@mongo:': '@x:'}),
            ['@mongo:'],
        )


class TestReadEnvValue(unittest.TestCase):

    def test_reads_a_value_containing_equals_signs(self):
        self.assertEqual(devkit.read_env_value('T=user@pam!n=abc\n', 'T'), 'user@pam!n=abc')

    def test_strips_quotes(self):
        self.assertEqual(devkit.read_env_value('T="abc"\n', 'T'), 'abc')

    def test_missing_key_is_none(self):
        self.assertIsNone(devkit.read_env_value('A=1\n', 'T'))


class TestToolServers(unittest.TestCase):

    ENTRY = {
        'name': 'posthog',
        'url': 'https://mcp.posthog.com/mcp',
        'header': 'Authorization: Bearer {secret}',
        'secret_key': 'POSTHOG_ENV_API_KEY',
    }

    def test_the_secret_stays_out_of_the_claude_config(self):
        exports, servers, skipped = devkit.resolve_tool_servers(
            [self.ENTRY], 'POSTHOG_ENV_API_KEY=phx_abc\n',
        )
        self.assertEqual(servers['posthog'], {
            'type': 'http',
            'url': 'https://mcp.posthog.com/mcp',
            'headers': {'Authorization': 'Bearer ${POSTHOG_ENV_API_KEY}'},
        })
        self.assertNotIn('phx_abc', str(servers))
        self.assertEqual(exports, {'POSTHOG_ENV_API_KEY': 'phx_abc'})
        self.assertEqual(skipped, [])

    def test_a_missing_secret_skips_the_server_instead_of_blocking_create(self):
        exports, servers, skipped = devkit.resolve_tool_servers([self.ENTRY], 'OTHER=1\n')
        self.assertEqual((exports, servers), ({}, {}))
        self.assertEqual(skipped, [('posthog', 'POSTHOG_ENV_API_KEY')])

    def test_no_entries_is_fine(self):
        self.assertEqual(devkit.resolve_tool_servers(None, ''), ({}, {}, []))

    def test_malformed_entries_are_refused(self):
        for broken in (
            {**self.ENTRY, 'header': 'Authorization Bearer {secret}'},
            {**self.ENTRY, 'header': 'Authorization: Bearer token'},
            {**self.ENTRY, 'secret_key': 'not a name'},
            {k: v for k, v in self.ENTRY.items() if k != 'url'},
        ):
            with self.assertRaises(devkit.DevkitError):
                devkit.parse_tool_server(broken)

    def test_exports_are_shell_quoted(self):
        self.assertEqual(devkit.render_exports({'A': "x'y z"}), 'export A=\'x\'"\'"\'y z\'\n')


class TestExpand(unittest.TestCase):

    def test_server_variables_fill_the_project_config(self):
        project = {
            'note': 'databases at {shared_host}',
            'settings': {'force': {'MYSQL_HOST': '{shared_host}'}, 'replace': {'@mongodb:': '@{shared_host}:'}},
            'drop': ['A'],
        }
        self.assertEqual(devkit.expand(project, {'shared_host': '10.0.0.5'}), {
            'note': 'databases at 10.0.0.5',
            'settings': {'force': {'MYSQL_HOST': '10.0.0.5'}, 'replace': {'@mongodb:': '@10.0.0.5:'}},
            'drop': ['A'],
        })

    def test_tool_server_secret_placeholder_is_left_alone(self):
        self.assertEqual(
            devkit.expand('Authorization: Bearer {secret}', {'shared_host': 'x'}),
            'Authorization: Bearer {secret}',
        )


class TestStaticNetwork(unittest.TestCase):

    CURRENT = 'name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,ip=dhcp,type=veth'
    NETWORK = {'address': '192.168.1.{vmid}/24', 'gateway': '192.168.1.1'}

    def test_the_address_follows_the_container_number(self):
        self.assertEqual(
            devkit.static_network(self.CURRENT, 104, self.NETWORK),
            'name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,type=veth,ip=192.168.1.104/24,gw=192.168.1.1',
        )

    def test_an_existing_static_address_and_gateway_are_replaced(self):
        current = 'name=eth0,bridge=vmbr0,ip=10.0.0.9/24,gw=10.0.0.1,type=veth'
        result = devkit.static_network(current, 105, self.NETWORK)
        self.assertNotIn('10.0.0', result)
        self.assertIn('ip=192.168.1.105/24', result)

    def test_a_container_number_outside_the_address_range_is_refused(self):
        with self.assertRaises(devkit.DevkitError):
            devkit.static_network(self.CURRENT, 300, self.NETWORK)


class TestToolServerParity(unittest.TestCase):

    HERE = """Checking MCP server health…
claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected
plugin:playwright:playwright: npx @playwright/mcp@latest - ✔ Connected
plugin:cloudflare:cloudflare-api: https://mcp.cloudflare.com/mcp (HTTP) - ! Needs authentication
ui5-mcp-server: npx -y @ui5/mcp-server - ✘ Failed to connect — CONNECTION_CLOSED: Connection closed
mercadopago: https://mcp.mercadopago.com/mcp (HTTP) - ⊘ Disabled for this project (re-enable via /mcp)
mysql-mcp-server: bash scripts/mcp_mysql.sh - ✔ Connected
"""

    def test_statuses_are_parsed(self):
        self.assertEqual(devkit.parse_mcp_list(self.HERE), {
            'claude.ai Gmail': 'connected',
            'plugin:playwright:playwright': 'connected',
            'plugin:cloudflare:cloudflare-api': 'needs login',
            'ui5-mcp-server': 'failed',
            'mercadopago': 'disabled',
            'mysql-mcp-server': 'connected',
        })

    def test_only_servers_working_here_are_expected_inside(self):
        here = devkit.parse_mcp_list(self.HERE)
        inside = {'plugin:playwright:playwright': 'failed', 'mysql-mcp-server': 'connected', 'posthog': 'connected'}
        total, missing, extra = devkit.compare_tool_servers(here, inside)
        self.assertEqual(total, 2)
        self.assertEqual(missing, [('plugin:playwright:playwright', 'failed')])
        self.assertEqual(extra, ['posthog'])

    def test_a_server_absent_inside_is_reported(self):
        total, missing, _ = devkit.compare_tool_servers({'a': 'connected'}, {})
        self.assertEqual(missing, [('a', 'absent')])

    def test_account_connectors_are_never_expected_inside(self):
        total, missing, _ = devkit.compare_tool_servers({'claude.ai Gmail': 'connected'}, {})
        self.assertEqual((total, missing), (0, []))


class TestEditorAccess(unittest.TestCase):

    def test_public_keys_skip_comments_blanks_and_repeats(self):
        text = '# laptop\nssh-ed25519 AAA me\n\nssh-ed25519 AAA me\nssh-rsa BBB other\n'
        self.assertEqual(devkit.public_keys(text), ['ssh-ed25519 AAA me', 'ssh-rsa BBB other'])

    def test_editor_uri_points_at_the_project_folder(self):
        self.assertEqual(
            devkit.editor_uri('dev', '192.168.1.104', '/home/dev/app'),
            'vscode-remote://ssh-remote+dev@192.168.1.104/home/dev/app',
        )


class TestWorkCheck(unittest.TestCase):

    def test_a_clean_pushed_checkout_has_nothing_to_lose(self):
        work = devkit.parse_work('## dirty\n## unpushed\n')
        self.assertEqual(work, {'dirty': [], 'unpushed': []})
        self.assertEqual(devkit.describe_work(work), '')

    def test_uncommitted_and_unpushed_work_are_both_counted(self):
        work = devkit.parse_work(
            '## dirty\n M app/a.py\n?? tests/b.py\n## unpushed\n8187534 fix: something\n'
        )
        self.assertEqual(work['dirty'], [' M app/a.py', '?? tests/b.py'])
        self.assertEqual(work['unpushed'], ['8187534 fix: something'])
        self.assertEqual(devkit.describe_work(work), '1 unpushed commit(s) and 2 uncommitted file(s)')


class TestMemory(unittest.TestCase):

    def test_free_memory_is_total_minus_used(self):
        status = {'memory': {'total': 24 * 2**30, 'used': 18 * 2**30, 'free': 1 * 2**30}}
        self.assertEqual(devkit.free_memory_mb(status), 6 * 1024)


class TestWorkers(unittest.TestCase):

    RESULT = '{"type":"result","is_error":false,"num_turns":4,"duration_ms":61500,"result":"Done: 17 files.","session_id":"abc-123"}'

    def test_the_result_is_found_after_warning_lines(self):
        output = 'Ignoring 3 permissions entries\n' + self.RESULT + '\n'
        self.assertEqual(devkit.parse_worker_output(output)['session_id'], 'abc-123')

    def test_output_without_a_result_is_none(self):
        self.assertIsNone(devkit.parse_worker_output('boom\n{"type":"system"}\n'))
        self.assertIsNone(devkit.parse_worker_output(''))

    def test_report_states(self):
        result = devkit.parse_worker_output(self.RESULT)
        self.assertEqual(devkit.format_report(None, 'none', None), 'no worker has run in this environment')
        self.assertIn('still running', devkit.format_report('r1', 'running', None))
        self.assertIn('ended without a report (exit 1)', devkit.format_report('r1', '1', None))
        self.assertEqual(
            devkit.format_report('r1', '0', result), 'Done: 17 files.\n\n[worker r1 finished: 4 turns, 61s]'
        )

    def test_the_task_text_never_reaches_a_command_line(self):
        script = devkit.worker_script('/home/dev/app', 'r1', ['--permission-mode', 'auto'])
        self.assertIn('cat > ~/.devkit-runs/r1/prompt', script)
        self.assertIn('< ~/.devkit-runs/r1/prompt', script)
        self.assertIn('setsid nohup', script)

    def test_a_follow_up_resumes_the_previous_session(self):
        script = devkit.worker_script('/home/dev/app', 'r2', [], resume='abc-123')
        self.assertIn('--resume abc-123', script)


class TestSetup(unittest.TestCase):

    def test_rules_are_added_next_to_existing_ones(self):
        settings = {'permissions': {'defaultMode': 'auto', 'allow': ['Bash(npm test)']}, 'model': 'opus'}
        added = devkit.add_permission_rules(settings, devkit.permission_rules())
        self.assertEqual(len(added), len(devkit.PERMITTED_COMMANDS))
        self.assertEqual(settings['permissions']['allow'][0], 'Bash(npm test)')
        self.assertEqual(settings['permissions']['defaultMode'], 'auto')
        self.assertEqual(settings['model'], 'opus')
        self.assertIn('Bash(devkit create:*)', settings['permissions']['allow'])

    def test_a_second_run_adds_nothing(self):
        settings = {}
        devkit.add_permission_rules(settings, devkit.permission_rules())
        self.assertEqual(devkit.add_permission_rules(settings, devkit.permission_rules()), [])
        self.assertEqual(len(settings['permissions']['allow']), len(devkit.PERMITTED_COMMANDS))

    def test_only_devkit_rules_are_removed(self):
        settings = {'permissions': {'defaultMode': 'auto', 'allow': ['Bash(npm test)']}}
        devkit.add_permission_rules(settings, devkit.permission_rules())
        removed = devkit.remove_permission_rules(settings, devkit.permission_rules())
        self.assertEqual(len(removed), len(devkit.PERMITTED_COMMANDS))
        self.assertEqual(settings, {'permissions': {'defaultMode': 'auto', 'allow': ['Bash(npm test)']}})

    def test_an_emptied_allow_list_is_dropped_and_nothing_else(self):
        settings = {'permissions': {'defaultMode': 'auto'}, 'model': 'opus'}
        devkit.add_permission_rules(settings, devkit.permission_rules())
        devkit.remove_permission_rules(settings, devkit.permission_rules())
        self.assertEqual(settings, {'permissions': {'defaultMode': 'auto'}, 'model': 'opus'})

    def test_removing_from_settings_without_rules_changes_nothing(self):
        settings = {'model': 'opus'}
        self.assertEqual(devkit.remove_permission_rules(settings, devkit.permission_rules()), [])
        self.assertEqual(settings, {'model': 'opus'})

    def test_commands_that_can_lose_or_publish_work_are_never_pre_approved(self):
        rules = ' '.join(devkit.permission_rules())
        for command in ('destroy', 'ship', 'setup'):
            self.assertNotIn(f'devkit {command}', rules)

    def test_every_permitted_command_exists(self):
        choices = devkit.build_parser()._subparsers._group_actions[0].choices
        for command in devkit.PERMITTED_COMMANDS:
            self.assertIn(command, choices)

    def test_blank_server_config_reports_every_required_key(self):
        import tomllib
        missing = devkit.missing_server_keys(tomllib.loads(devkit.SERVER_CONFIG_TEMPLATE))
        self.assertEqual(missing, [
            '[proxmox] host', '[proxmox] node', '[secrets] file',
        ])

    def test_a_complete_server_config_reports_nothing(self):
        config = {
            'proxmox': {'host': 'h', 'node': 'n', 'pool': 'p', 'template': 105},
            'limits': {'max_environments': 3},
            'ssh': {'user': 'dev', 'key': 'k', 'github_key': 'g'},
            'secrets': {'file': 'f', 'proxmox_token_key': 'T'},
        }
        self.assertEqual(devkit.missing_server_keys(config), [])


class TestTemplates(unittest.TestCase):

    CONTAINERS = [
        {'vmid': 105, 'name': 'devkit-template-v6', 'template': 1},
        {'vmid': 110, 'name': 'devkit-base', 'template': 1},
        {'vmid': 111, 'name': 'devkit-tpl-droppo-v2', 'template': 1},
        {'vmid': 112, 'name': 'devkit-tpl-other', 'template': 0},
        {'vmid': 104, 'name': 'env-fix', 'template': 0},
    ]

    def test_a_project_template_wins_over_the_base(self):
        self.assertEqual(devkit.choose_template(self.CONTAINERS, 'droppo-v2'), (111, 'devkit-tpl-droppo-v2'))

    def test_a_project_without_its_own_template_gets_the_base(self):
        self.assertEqual(devkit.choose_template(self.CONTAINERS, 'landing'), (110, 'devkit-base'))

    def test_a_container_that_is_not_frozen_is_never_chosen(self):
        self.assertEqual(devkit.choose_template(self.CONTAINERS, 'other'), (110, 'devkit-base'))

    def test_the_old_numbered_template_is_only_a_last_resort(self):
        only_old = [self.CONTAINERS[0]]
        self.assertEqual(devkit.choose_template(only_old, 'droppo-v2', legacy=105), (105, 'devkit-template-v6'))
        self.assertEqual(devkit.choose_template(self.CONTAINERS, 'droppo-v2', legacy=105)[0], 111)

    def test_no_template_at_all_is_none(self):
        self.assertEqual(devkit.choose_template([self.CONTAINERS[4]], 'x'), (None, None))
        self.assertEqual(devkit.choose_template([self.CONTAINERS[4]], 'x', legacy=999), (None, None))

    def test_slugs_are_safe_container_names(self):
        self.assertEqual(devkit.project_slug('Droppo_v2'), 'droppo-v2')
        self.assertEqual(devkit.project_slug('my.app (new)'), 'my-app-new')
        with self.assertRaises(devkit.DevkitError):
            devkit.project_slug('___')

    def test_settings_default_and_override(self):
        settings = devkit.template_settings({'template': {'memory_mb': 4096}})
        self.assertEqual(settings['memory_mb'], 4096)
        self.assertEqual(settings['cores'], devkit.TEMPLATE_DEFAULTS['cores'])
        self.assertEqual(devkit.template_settings({}), devkit.TEMPLATE_DEFAULTS)

    def test_a_new_container_is_unprivileged_and_carries_only_the_public_key(self):
        params = devkit.new_container_params(120, 'devkit-build-base', 'devkit', devkit.TEMPLATE_DEFAULTS, 'ssh-ed25519 AAA devkit')
        self.assertEqual(params['unprivileged'], 1)
        self.assertEqual(params['rootfs'], 'local-lvm:20')
        self.assertEqual(params['net0'], 'name=eth0,bridge=vmbr0,ip=dhcp,type=veth')
        self.assertEqual(params['ssh-public-keys'], 'ssh-ed25519 AAA devkit')

    def test_build_settings_keep_every_key_and_no_value(self):
        text = '# c\nA=secret\nB="x y"\n\nC=\n'
        self.assertEqual(devkit.blank_env(text), 'A=\nB=\nC=\n')
        self.assertNotIn('secret', devkit.blank_env(text))

    def test_environment_size_comes_from_the_project_first(self):
        server = {'environment': {'memory_mb': 3072, 'cores': 4}}
        self.assertEqual(devkit.environment_size(server, {'environment': {'memory_mb': 6144}}), {'memory': 6144, 'cores': 4})
        self.assertEqual(devkit.environment_size({}, {}), {})
        self.assertEqual(devkit.environment_size({}, None), {})

    def test_provisioning_is_for_the_configured_user_and_node(self):
        script = devkit.provision_script('builder', 20)
        self.assertIn('useradd -m -s /bin/bash builder', script)
        self.assertIn('setup_20.x', script)
        self.assertIn('grep -q "^v20\\."', script)
        self.assertNotIn('{', script.replace('${', ''))

    def test_the_root_step_names_the_container(self):
        commands = devkit.root_step_commands(121)
        self.assertIn('/etc/pve/lxc/121.conf', commands[0])
        self.assertEqual(commands[2], 'pct reboot 121')

    def run_shell(self, script, home):
        import os
        import subprocess
        tools = os.path.join(home, '.test-bin')
        os.makedirs(tools, exist_ok=True)
        for name in ('rm', 'sh', 'echo', '['):
            link = os.path.join(tools, name)
            if not os.path.exists(link):
                os.symlink(os.path.join('/usr/bin', name), link)
        return subprocess.run(['/bin/sh', '-c', script], env={'HOME': home, 'PATH': tools},
                              capture_output=True, text=True)

    def test_the_leftover_check_only_says_clean_when_nothing_is_there(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            spaced = os.path.join(home, 'my project', '.env')
            check = devkit.leftover_check((spaced, '~/.claude', '~/.claude.json'))
            self.assertEqual(self.run_shell(check, home).stdout.strip(), 'clean')
            os.makedirs(os.path.dirname(spaced))
            open(spaced, 'w').close()
            self.assertEqual(self.run_shell(check, home).stdout.strip(), '')
            os.remove(spaced)
            os.makedirs(os.path.join(home, '.claude'))
            self.assertEqual(self.run_shell(check, home).stdout.strip(), '')

    def test_cleanup_removes_the_settings_file_and_everything_the_mirror_wrote(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            workdir = os.path.join(home, 'my project')
            os.makedirs(workdir)
            target = os.path.join(workdir, '.env')
            os.makedirs(os.path.join(home, '.claude'))
            files = [target, os.path.join(home, '.claude.json'), os.path.join(home, '.devkit-secrets')]
            files += [os.path.join(home, '.claude', item) for item in devkit.MIRRORED]
            for path in files:
                with open(path, 'w') as handle:
                    handle.write('A=secret\n')
            script = devkit.remove_files_script((target, *devkit.CLAUDE_STATE))
            self.assertTrue(script.startswith('rm -rf '))
            for forbidden in ('docker', 'git', ';', '|', '&'):
                self.assertNotIn(forbidden, script)
            self.run_shell(script, home)
            self.assertFalse(os.path.exists(target))
            for leftover in ('.claude', '.claude.json', '.devkit-secrets'):
                self.assertFalse(os.path.exists(os.path.join(home, leftover)), leftover)
            check = devkit.leftover_check((target, *devkit.CLAUDE_STATE))
            self.assertEqual(self.run_shell(check, home).stdout.strip(), 'clean')

    def test_every_mirrored_item_lives_under_a_path_the_cleanup_removes(self):
        self.assertIn('~/.claude', devkit.CLAUDE_STATE)

    def test_prepare_must_be_a_list_of_commands(self):
        self.assertEqual(devkit.prepare_commands(None, 'x'), [])
        self.assertEqual(devkit.prepare_commands(['make deps'], 'x'), ['make deps'])
        for wrong in ('make deps', ['ok', 3], ['']):
            with self.assertRaises(devkit.DevkitError):
                devkit.prepare_commands(wrong, 'x')

    def test_sudo_works_for_a_user_name_with_a_dot(self):
        script = devkit.provision_script('john.doe', 22)
        self.assertIn('> /etc/sudoers.d/devkit', script)
        self.assertNotIn('/etc/sudoers.d/john.doe', script)

    def test_an_update_upgrades_what_is_already_installed(self):
        script = devkit.provision_script('dev', 22)
        self.assertIn('apt-get -y -qq upgrade', script)
        self.assertIn('uv self update', script)
        self.assertIn('claude update', devkit.CLAUDE_INSTALL)

    def test_a_template_remembers_the_repository_it_was_built_for(self):
        description = devkit.encode_template_description('git@github.com:o/app.git')
        self.assertEqual(devkit.decode_template_repo(description), 'git@github.com:o/app.git')
        self.assertEqual(devkit.decode_template_repo('devkit%20template%20repo%3Dgit%40h%3Ao%2Fa.git%0A'), 'git@h:o/a.git')
        self.assertIsNone(devkit.decode_template_repo(devkit.encode_template_description()))
        self.assertIsNone(devkit.decode_template_repo(None))


class FakeProxmox:

    def __init__(self, containers):
        self.state = {c['vmid']: dict(c) for c in containers}
        self.calls = []

    def containers(self):
        return [dict(c) for c in self.state.values()]

    def named(self, name):
        return [c for c in self.containers() if c.get('name') == name]

    def shutdown(self, vmid):
        self.calls.append(('shutdown', vmid))

    def to_template(self, vmid):
        self.calls.append(('to_template', vmid))
        self.state[vmid]['template'] = 1

    def delete(self, vmid):
        self.calls.append(('delete', vmid))
        del self.state[vmid]

    def set_config(self, vmid, **params):
        self.calls.append(('rename', vmid))
        self.state[vmid]['name'] = params['hostname']

    def config(self, vmid):
        return {'description': self.state[vmid].get('description', '')}


class FakeRemote:

    def run(self, script, stdin=None):
        return ''


class TestFreezing(unittest.TestCase):

    def build(self, containers):
        kit = devkit.Devkit.__new__(devkit.Devkit)
        kit.proxmox = FakeProxmox(containers)
        return kit

    def setUp(self):
        import contextlib
        import unittest.mock
        patcher = unittest.mock.patch.object(devkit, 'clone_lock', lambda wait_seconds=0: contextlib.nullcontext())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_old_template_goes_only_after_the_new_one_is_frozen(self):
        kit = self.build([
            {'vmid': 104, 'name': 'devkit-base', 'template': 1},
            {'vmid': 110, 'name': 'devkit-build-base', 'template': 0},
        ])
        kit._freeze(110, FakeRemote(), 'devkit-base')
        order = [name for name, _ in kit.proxmox.calls]
        self.assertLess(order.index('to_template'), order.index('delete'))
        self.assertLess(order.index('delete'), order.index('rename'))
        self.assertEqual(kit.proxmox.containers(), [{'vmid': 110, 'name': 'devkit-base', 'template': 1}])

    def test_a_first_build_has_nothing_to_replace(self):
        kit = self.build([{'vmid': 110, 'name': 'devkit-build-base', 'template': 0}])
        kit._freeze(110, FakeRemote(), 'devkit-base')
        self.assertNotIn('delete', [name for name, _ in kit.proxmox.calls])

    def test_a_build_interrupted_after_freezing_is_finished_by_the_next_one(self):
        kit = self.build([{'vmid': 110, 'name': 'devkit-build-base', 'template': 1}])
        kit._recover_or_discard('devkit-build-base', 'devkit-base')
        self.assertEqual(kit.proxmox.containers(), [{'vmid': 110, 'name': 'devkit-base', 'template': 1}])

    def test_a_stale_frozen_build_is_dropped_when_the_real_template_exists(self):
        kit = self.build([
            {'vmid': 104, 'name': 'devkit-base', 'template': 1},
            {'vmid': 110, 'name': 'devkit-build-base', 'template': 1},
        ])
        kit._recover_or_discard('devkit-build-base', 'devkit-base')
        self.assertEqual([c['vmid'] for c in kit.proxmox.containers()], [104])

    def test_a_template_built_for_another_repository_is_refused(self):
        kit = self.build([{'vmid': 111, 'name': 'devkit-tpl-app', 'template': 1,
                           'description': devkit.encode_template_description('git@h:other/app.git')}])
        kit.project = {'repo': 'git@h:mine/app.git'}
        with self.assertRaises(devkit.DevkitError):
            kit._require_same_repo(111, 'devkit-tpl-app')
        kit.project = {'repo': 'git@h:other/app.git'}
        kit._require_same_repo(111, 'devkit-tpl-app')

    def test_fresh_only_makes_sense_for_the_base(self):
        kit = self.build([])
        kit.project = {'repo': 'x'}
        kit.slug = 'app'
        with self.assertRaises(devkit.DevkitError):
            kit.template_build(base=False, fresh=True)

    def test_only_unfrozen_build_containers_count_as_leftovers(self):
        kit = self.build([
            {'vmid': 104, 'name': 'devkit-base', 'template': 1},
            {'vmid': 105, 'name': 'devkit-build-tpl-app', 'template': 0},
            {'vmid': 106, 'name': 'devkit-build-base', 'template': 1},
            {'vmid': 107, 'name': 'env-fix', 'template': 0},
        ])
        self.assertEqual([c['vmid'] for c in kit._leftover_builds()], [105])

    def test_a_project_named_base_does_not_share_the_base_build_container(self):
        self.assertNotEqual(devkit.BUILD_PREFIX + 'tpl-' + 'base', devkit.BUILD_PREFIX + 'base')



class TestNames(unittest.TestCase):

    def test_accepts_branch_like_names(self):
        self.assertEqual(devkit.validate_name('fix-orders-2'), 'fix-orders-2')

    def test_rejects_names_proxmox_or_a_shell_would_choke_on(self):
        for bad in ('', 'Fix', 'a b', 'a/b', '-a', 'a;rm', 'x' * 31):
            with self.assertRaises(devkit.DevkitError):
                devkit.validate_name(bad)


class TestDescription(unittest.TestCase):

    def test_branch_round_trips(self):
        self.assertEqual(
            devkit.decode_description(devkit.encode_description('fix/orders-net')), 'fix/orders-net'
        )

    def test_proxmox_url_encoded_description_is_decoded(self):
        self.assertEqual(devkit.decode_description('devkit%20branch%3Dfeature%2Fx%0A'), 'feature/x')

    def test_foreign_description_yields_no_branch(self):
        self.assertEqual(devkit.decode_description('something else'), '')
        self.assertEqual(devkit.decode_description(None), '')


class TestAge(unittest.TestCase):

    def test_formats(self):
        self.assertEqual(devkit.format_age(125), '2m')
        self.assertEqual(devkit.format_age(3725), '1h02m')
        self.assertEqual(devkit.format_age(90000), '1d1h')


if __name__ == '__main__':
    unittest.main()


class TestRunningLimit(unittest.TestCase):

    ENVIRONMENTS = [
        {'name': 'env-a', 'status': 'running'},
        {'name': 'env-b', 'status': 'stopped'},
        {'name': 'env-c', 'status': 'running'},
    ]

    def test_stopped_environments_do_not_count(self):
        self.assertEqual(devkit.running_names(self.ENVIRONMENTS), ['a', 'c'])

    def test_the_limit_is_reached_by_running_ones_only(self):
        self.assertIsNone(devkit.limit_reached(self.ENVIRONMENTS, 3))
        self.assertEqual(devkit.limit_reached(self.ENVIRONMENTS, 2), ['a', 'c'])


class TestWorkerTimeLimit(unittest.TestCase):

    def test_the_worker_is_bounded_when_a_limit_is_set(self):
        script = devkit.worker_script('/home/dev/app', '20260101-000000', ['--permission-mode', 'auto'], timeout=3600)
        self.assertIn('timeout 3600 claude -p', script)

    def test_no_limit_leaves_the_worker_unbounded(self):
        script = devkit.worker_script('/home/dev/app', '20260101-000000', [])
        self.assertNotIn('timeout', script)

    def test_a_worker_stopped_at_the_limit_says_so(self):
        self.assertIn('time limit', devkit.format_report('r1', '124', None))


class TestReportData(unittest.TestCase):

    RESULT = {'type': 'result', 'result': ' done \n', 'is_error': False,
              'session_id': 'abc', 'num_turns': 4, 'duration_ms': 9500}

    def test_states(self):
        self.assertEqual(devkit.report_data(None, 'none', None)['state'], 'none')
        self.assertEqual(devkit.report_data('r1', 'running', None)['state'], 'running')
        self.assertEqual(devkit.report_data('r1', '0', self.RESULT)['state'], 'finished')
        self.assertEqual(devkit.report_data('r1', '1', dict(self.RESULT, is_error=True))['state'], 'failed')
        self.assertEqual(devkit.report_data('r1', '1', None)['state'], 'failed')
        self.assertEqual(devkit.report_data('r1', '124', None)['state'], 'timeout')

    def test_a_finished_worker_carries_its_report_and_session(self):
        data = devkit.report_data('r1', '0', self.RESULT)
        self.assertEqual(data['report'], 'done')
        self.assertEqual(data['session_id'], 'abc')
        self.assertEqual(data['run_id'], 'r1')
        self.assertEqual(data['seconds'], 9)


class TestShip(unittest.TestCase):

    def test_the_message_and_identity_are_shell_quoted(self):
        script = devkit.ship_script('/home/dev/app', "fix: it's done", 'Ana Li', 'ana@example.com')
        self.assertIn("""'fix: it'"'"'s done'""", script)
        self.assertIn("user.name='Ana Li'", script)
        self.assertIn('user.email=ana@example.com', script)

    def test_a_clean_checkout_ships_nothing(self):
        script = devkit.ship_script('/home/dev/app', 'm', 'n', 'e@x')
        self.assertIn('nothing to ship', script)

    def test_the_current_branch_is_pushed(self):
        script = devkit.ship_script('/home/dev/app', 'm', 'n', 'e@x')
        self.assertIn('-u origin HEAD', script)


class TestListData(unittest.TestCase):

    def test_an_environment_is_named_without_the_prefix(self):
        container = {'name': 'env-gt-10071', 'status': 'stopped'}
        self.assertEqual(
            devkit.environment_summary(container, 'fix/glitchtip-10071'),
            {'name': 'gt-10071', 'branch': 'fix/glitchtip-10071', 'state': 'stopped'},
        )


class TestSharedHostKey(unittest.TestCase):

    def test_the_key_arrives_on_standard_input_never_in_the_script(self):
        script = devkit.host_key_script()
        self.assertIn('cat >', script)
        self.assertNotIn('PRIVATE KEY', script)

    def test_only_the_shared_key_is_offered_afterwards(self):
        script = devkit.host_key_script()
        self.assertIn('HostKey /etc/ssh/ssh_host_ed25519_key', script)
        self.assertIn('sshd_config.d', script)
        self.assertIn('restart ssh', script)
