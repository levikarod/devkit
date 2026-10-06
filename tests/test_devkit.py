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
