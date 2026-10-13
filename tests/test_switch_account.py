import base64
from contextlib import nullcontext, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('switch_account', Path(__file__).parents[1] / 'assets/switch_account.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def record(user='user-one', workspace='workspace-one', **overrides):
    payload = {'iss': 'https://auth.openai.com', 'exp': 100,
               'https://api.openai.com/auth': {'chatgpt_user_id': user, 'chatgpt_account_id': workspace}}
    payload.update(overrides.pop('claims', {}))
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return {'id': 'saved-session', 'name': 'Example Account',
            'auth_data': {'type': 'chat_g_p_t', 'account_id': workspace,
                          'id_token': 'header.' + encoded + '.signature',
                          'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh'}, **overrides}


class IdentityTests(unittest.TestCase):
    def test_rotated_sessions_and_labels_match(self):
        first = record()
        second = record(id='different-local-id', name='Renamed', claims={'exp': 200, 'iss': 'https://auth.example.invalid'})
        second['auth_data']['access_token'] = 'new-synthetic-access'
        self.assertEqual(helper.auth_identity(first)['identity'], helper.auth_identity(second)['identity'])

    def test_workspace_and_user_are_both_required(self):
        base = helper.auth_identity(record())['identity']
        self.assertNotEqual(base, helper.auth_identity(record(user='user-two'))['identity'])
        self.assertNotEqual(base, helper.auth_identity(record(workspace='workspace-two'))['identity'])
        self.assertIsNone(helper.auth_identity(record(user=None)))
        self.assertIsNone(helper.auth_identity(record(workspace=None)))

    def test_conflicting_stored_workspace_is_rejected(self):
        value = record()
        value['auth_data']['account_id'] = 'different-workspace'
        self.assertIsNone(helper.auth_identity(value))

    def test_user_id_fallback_and_precedence(self):
        original = helper.auth_identity(record())['identity']
        fallback = record(claims={'https://api.openai.com/auth': {'user_id': 'user-one', 'chatgpt_account_id': 'workspace-one'}})
        preferred = record(claims={'https://api.openai.com/auth': {'user_id': 'user-two', 'chatgpt_user_id': 'user-one', 'chatgpt_account_id': 'workspace-one'}})
        self.assertEqual(original, helper.auth_identity(fallback)['identity'])
        self.assertEqual(original, helper.auth_identity(preferred)['identity'])

    def test_credentials_and_raw_ids_are_not_returned(self):
        value = record()
        serialized = json.dumps(helper.auth_identity(value))
        for private in [value['auth_data']['id_token'], 'synthetic-access', 'synthetic-refresh', 'user-one', 'workspace-one']:
            self.assertNotIn(private, serialized)
        self.assertEqual(len(helper.auth_identity(value)['identity']), 64)

    def test_malformed_token_does_not_fall_back_to_name(self):
        value = record()
        value['auth_data']['id_token'] = 'not-a-jwt'
        self.assertIsNone(helper.auth_identity(value))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {'id': 'first-session', 'name': 'Personal account', 'is_active': False},
            {'id': 'second-session', 'name': 'Work account', 'is_active': True},
        ]

    def test_missing_id_never_falls_back_to_account_label(self):
        for account_id in (None, '', 'personal', 'work', 'unknown-session'):
            with self.subTest(account_id=account_id):
                with self.assertRaisesRegex(RuntimeError, 'missing or ambiguous'):
                    helper.select_account(self.accounts, account_id)

    def test_exact_id_selects_even_with_duplicate_labels(self):
        self.accounts[1]['name'] = self.accounts[0]['name']
        self.assertIs(helper.select_account(self.accounts, 'second-session'), self.accounts[1])

    def test_missing_id_aborts_before_closing_or_switching(self):
        api = unittest.mock.Mock()
        api.request.return_value = self.accounts
        with patch.object(helper, 'MACHINES', {'laptop': {'name': 'Test Laptop'}}), \
             patch.object(helper, 'verify_target', return_value='laptop'), \
             patch.object(helper, 'operation_lock', return_value=nullcontext()), \
             patch.object(helper, 'api_server', return_value=nullcontext(api)), \
             patch.object(helper, 'reopen_codex') as reopen, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'missing or ambiguous'):
                helper.operate('laptop', None)
        api.request.assert_called_once_with('list_accounts')
        reopen.assert_not_called()

    def test_cli_rejects_legacy_positional_account(self):
        with patch.object(helper.sys, 'argv', ['switch_account.py', 'local', 'personal']), \
             patch.object(helper, 'load_devices') as load, \
             patch.object(helper.sys, 'stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                helper.main()
        self.assertEqual(failure.exception.code, 2)
        load.assert_not_called()


class AllDevicesTests(unittest.TestCase):
    def setUp(self):
        self.identity = helper.auth_identity(record())['identity']
        self.devices = {key: {'name': key, 'identity': str(index) * 64, 'ssh': key}
                        for index, key in enumerate(['laptop', 'desktop', 'work-mac'], 1)}
        self.snapshot = {key: [{'id': key + '-session', 'name': 'Example (Personal)', 'identity': self.identity,
                               'is_active': False, 'token_expiry': 100, 'last_used_at': ''}] for key in self.devices}
        self.patch = patch.object(helper, 'MACHINES', self.devices)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_duplicate_credentials_form_one_common_account(self):
        self.snapshot['desktop'].append({**self.snapshot['desktop'][0], 'id': 'active-older-session',
                                         'is_active': True, 'token_expiry': 1})
        common = helper.common_accounts(self.snapshot)
        self.assertEqual(len(common), 1)
        self.assertEqual(common[0]['active_count'], 1)
        self.assertEqual(helper.all_plan(self.snapshot, self.identity)['desktop']['id'], 'active-older-session')

    def test_same_workspace_with_different_member_is_not_common(self):
        self.snapshot['work-mac'][0]['identity'] = helper.auth_identity(record(user='user-two'))['identity']
        self.assertEqual(helper.common_accounts(self.snapshot), [])

    def test_failed_availability_preflight_changes_nothing(self):
        with patch.object(helper, 'operation_lock', return_value=nullcontext()), \
             patch.object(helper, 'gather_devices', side_effect=RuntimeError('offline')), \
             patch.object(helper, 'switch_target') as switch:
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                helper.operate_all(self.identity)
            switch.assert_not_called()

    def test_missing_identity_preflight_changes_nothing(self):
        self.snapshot['work-mac'] = []
        with patch.object(helper, 'operation_lock', return_value=nullcontext()), \
             patch.object(helper, 'gather_devices', return_value=self.snapshot), \
             patch.object(helper, 'switch_target') as switch:
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                helper.operate_all(self.identity)
            switch.assert_not_called()

    def test_switches_are_concurrent(self):
        barrier = threading.Barrier(3)
        completed = []
        def switch(key, account, identity):
            barrier.wait(timeout=2)
            completed.append(key)
        with patch.object(helper, 'operation_lock', return_value=nullcontext()), \
             patch.object(helper, 'gather_devices', return_value=self.snapshot), \
             patch.object(helper, 'switch_target', side_effect=switch), redirect_stdout(io.StringIO()):
            helper.operate_all(self.identity)
        self.assertCountEqual(completed, self.devices)

    def test_partial_failure_reports_individual_outcomes(self):
        def switch(key, account, identity):
            if key == 'desktop':
                raise RuntimeError('reopening failed')
        output = io.StringIO()
        with patch.object(helper, 'operation_lock', return_value=nullcontext()), \
             patch.object(helper, 'gather_devices', return_value=self.snapshot), \
             patch.object(helper, 'switch_target', side_effect=switch), redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'desktop'):
                helper.operate_all(self.identity)
        self.assertIn('desktop: failed', output.getvalue())
        self.assertIn('laptop: Example (Personal)', output.getvalue())
        self.assertNotIn('All configured Macs now use', output.getvalue())

    def test_local_and_ssh_failures_preserve_both_output_streams(self):
        progress = 'Account switch verified.\n'
        failure_detail = 'Error: The account changed, but Codex desktop reopening could not be verified.\n'
        for target in ('laptop', 'desktop'):
            with self.subTest(target=target):
                result = helper.subprocess.CompletedProcess([], 1, stdout=progress, stderr=failure_detail)
                with patch.object(helper, 'machine_key', return_value='laptop'), \
                     patch.object(helper.subprocess, 'run', return_value=result):
                    with self.assertRaises(helper.RequestFailure) as failure:
                        helper.switch_target(target, self.snapshot[target][0], self.identity)
                self.assertEqual(failure.exception.stdout, progress)
                self.assertEqual(failure.exception.stderr, failure_detail)
                self.assertIn(progress.strip(), str(failure.exception))
                self.assertIn(failure_detail.strip(), str(failure.exception))

    def test_all_outcomes_show_verified_switch_before_local_or_ssh_reopen_failure(self):
        for failed_target in ('laptop', 'desktop'):
            with self.subTest(failed_target=failed_target):
                def run(command, **kwargs):
                    target = command[-2] if command[0] == '/usr/bin/ssh' else command[-1]
                    if target == failed_target:
                        return helper.subprocess.CompletedProcess(
                            command, 1, stdout='Account switch verified.\n',
                            stderr='Error: The account changed, but Codex desktop reopening could not be verified.\n')
                    return helper.subprocess.CompletedProcess(command, 0, stdout='Codex desktop is running.\n', stderr='')
                outcome = io.StringIO()
                with patch.object(helper, 'operation_lock', return_value=nullcontext()), \
                     patch.object(helper, 'gather_devices', return_value=self.snapshot), \
                     patch.object(helper, 'machine_key', return_value='laptop'), \
                     patch.object(helper.subprocess, 'run', side_effect=run), redirect_stdout(outcome):
                    with self.assertRaisesRegex(RuntimeError, failed_target):
                        helper.operate_all(self.identity)
                text = outcome.getvalue()
                self.assertIn(failed_target + ': failed', text)
                self.assertIn('Account switch verified.', text)
                self.assertIn('The account changed, but Codex desktop reopening could not be verified.', text)
                self.assertNotIn('All configured Macs now use', text)
                for target in self.devices:
                    if target != failed_target:
                        self.assertIn(target + ': Example (Personal) — Codex is running.', text)

    def test_identity_change_aborts_before_close_or_switch(self):
        api = unittest.mock.Mock()
        api.request.return_value = self.snapshot['laptop']
        wrong_record = record(user='user-two')
        with patch.object(helper, 'verify_target', return_value='laptop'), \
             patch.object(helper, 'operation_lock', return_value=nullcontext()), \
             patch.object(helper, 'api_server', return_value=nullcontext(api)), \
             patch.object(helper, 'read_saved_records', return_value={'laptop-session': wrong_record}), \
             patch.object(helper, 'reopen_codex') as reopen, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                helper.operate('laptop', 'laptop-session', expect_identity=self.identity)
        api.request.assert_called_once_with('list_accounts')
        reopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
