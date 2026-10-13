#!/usr/bin/python3
"""Switch saved Codex accounts locally or over SSH using Codex Switcher's API."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT = '/Users/Shared/Codex Account Switcher/switch-codex-account.py'
SERVER = '/Applications/Codex Switcher.app/Contents/MacOS/codex-web'
CONFIG = Path(os.environ.get('CODEX_ACCOUNT_SWITCHER_CONFIG',
                             str(Path.home() / '.config/codex-account-switcher/devices.json')))
MACHINES = {}


def hardware_identity():
    output = subprocess.check_output(
        ['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'], text=True)
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
    if not match:
        raise RuntimeError('Cannot verify this Mac\'s identity.')
    return hashlib.sha256((match.group(1) + '\n').encode()).hexdigest()


def load_devices():
    data = json.loads(CONFIG.read_text()) if CONFIG.exists() else {'devices': []}
    devices = data.get('devices')
    if not isinstance(devices, list):
        raise RuntimeError('Device configuration needs a devices array.')
    result = {}
    identities = set()
    for device in devices:
        key, name = device.get('id'), device.get('name')
        host, identity = device.get('ssh'), device.get('identity')
        if (not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', key)
                or key in {'local', 'all', '_local'} or key in result):
            raise RuntimeError('Device IDs must be unique; local and all are reserved.')
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError('Each device needs a display name.')
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', host):
            raise RuntimeError('Each device needs a valid SSH alias.')
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity):
            raise RuntimeError('Each device needs the identity hash from --identify.')
        if identity in identities:
            raise RuntimeError('Each configured device must have a different identity hash.')
        identities.add(identity)
        result[key] = {'name': name.strip(), 'ssh': host, 'identity': identity}
    return result


def machine_key():
    identity = hardware_identity()
    for key, device in MACHINES.items():
        if identity == device['identity']:
            return key
    MACHINES['_local'] = {'name': 'This Mac', 'ssh': None, 'identity': identity}
    return '_local'


def verify_target(expected):
    if expected not in MACHINES:
        raise RuntimeError('The selected device is not configured.')
    actual = machine_key()
    if actual != expected:
        raise RuntimeError('SSH reached %s; expected %s. No account changes made.'
                           % (MACHINES[actual]['name'], MACHINES[expected]['name']))
    return actual


@contextlib.contextmanager
def operation_lock(name):
    folder = Path.home() / '.local/share/codex-account-switch'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (name + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another account switch is already running. Retry after it finishes.')
        yield


class SwitcherAPI:
    def __init__(self, port):
        self.base = 'http://127.0.0.1:%d' % port
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, command=None, payload=None):
        url = self.base + ('/api/invoke/' + command if command else '/api/health')
        body = json.dumps(payload or {}).encode() if command else None
        request = urllib.request.Request(url, data=body,
                                         headers={'Content-Type': 'application/json'})
        try:
            with self.http.open(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = json.loads(error.read()).get('error', 'HTTP %s' % error.code)
            raise RuntimeError(str(detail)) from None


@contextlib.contextmanager
def api_server():
    if not Path(SERVER).is_file():
        raise RuntimeError('Install Codex Switcher with its codex-web helper on this Mac.')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, CODEX_SWITCHER_WEB_HOST='127.0.0.1',
               CODEX_SWITCHER_WEB_PORT=str(port))
    process = subprocess.Popen([SERVER], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    api = SwitcherAPI(port)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Codex Switcher HTTP helper exited before becoming ready.')
            try:
                if api.request() == {'ok': True}:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError('Codex Switcher HTTP helper did not become ready.')
        yield api
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def read_saved_records():
    path = Path.home() / '.codex-switcher/accounts.json'
    for attempt in range(3):
        try:
            records = json.loads(path.read_text()).get('accounts', [])
            if not isinstance(records, list):
                raise RuntimeError('Codex Switcher saved-account data is invalid.')
            return {record['id']: record for record in records}
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            if attempt == 2:
                raise RuntimeError('Saved-account data is being updated. Retry shortly.') from None
            time.sleep(0.1)


def auth_identity(record):
    """Return opaque user+workspace identity, never a credential or raw claim ID."""
    auth = record.get('auth_data') or {}
    if auth.get('type') != 'chat_g_p_t':
        return None
    try:
        token = auth.get('id_token') or ''
        parts = token.split('.')
        if len(parts) != 3:
            return None
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        details = claims.get('https://api.openai.com/auth') or {}
        user = details.get('chatgpt_user_id') or details.get('user_id')
        workspace = details.get('chatgpt_account_id')
        if not all(isinstance(value, str) and value for value in (user, workspace)):
            return None
        if auth.get('account_id') and auth['account_id'] != workspace:
            return None
        # The opaque OpenAI IDs survive token rotation, display-name and issuer changes.
        canonical = json.dumps(['chatgpt-user-workspace-v1', user, workspace], separators=(',', ':'))
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        expiry = claims.get('exp')
        expiry = expiry if isinstance(expiry, int) and not isinstance(expiry, bool) else 0
        return {'identity': fingerprint, 'token_expiry': expiry}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def list_accounts(expected):
    verify_target(expected)
    with api_server() as api:
        accounts = api.request('list_accounts')
        records = read_saved_records()
        result = []
        for item in accounts:
            record = records.get(item['id'], {})
            identity = auth_identity(record) or {'identity': None, 'token_expiry': 0}
            result.append({**{key: item.get(key) for key in ('id', 'name', 'is_active')},
                           **identity, 'last_used_at': record.get('last_used_at') or ''})
        return result


def choose_equivalent(accounts, identity):
    matches = [item for item in accounts if item.get('identity') == identity]
    if not matches:
        raise RuntimeError('The selected ChatGPT account and workspace are no longer available.')
    return max(matches, key=lambda item: (bool(item.get('is_active')),
                                         item.get('token_expiry') or 0,
                                         item.get('last_used_at') or '', item['id']))


def select_account(accounts, account_id):
    matches = [item for item in accounts if account_id and item['id'] == account_id]
    if len(matches) != 1:
        raise RuntimeError('The selected saved account is missing or ambiguous. Refresh and choose it again.')
    return matches[0]


def reopen_codex():
    subprocess.run(['/usr/bin/osascript', '-e',
                    'tell application id "com.openai.codex" to launch'],
                   check=True, timeout=30, stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        running = subprocess.run(['/usr/bin/pgrep', '-f',
                                  '^/Applications/ChatGPT[.]app/Contents/MacOS/ChatGPT($| )'],
                                 stdout=subprocess.DEVNULL)
        if running.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError('The account changed, but Codex desktop reopening could not be verified.')


def operate(expected, account_id, check_only=False, expect_identity=None):
    actual = verify_target(expected)
    label = MACHINES[actual]['name']
    print('Verified target: %s' % label, flush=True)
    with operation_lock('operation'), api_server() as api:
        selected = select_account(api.request('list_accounts'), account_id)
        if expect_identity:
            identity = auth_identity(read_saved_records().get(selected['id'], {}))
            if not identity or identity['identity'] != expect_identity:
                raise RuntimeError('Account identity changed after preflight. No account changes made.')
        active = api.request('get_active_account_info')
        print('Selected account: %s' % selected['name'], flush=True)
        if check_only:
            print('Target, HTTP API and account checks passed. No account changes or restart performed.')
            return
        closed = False
        reopened = False
        try:
            if not active or active['id'] != selected['id']:
                if not api.request('check_codex_processes')['can_switch']:
                    print('Closing Codex gracefully...', flush=True)
                    closed = True
                    result = api.request('kill_codex_processes',
                                         {'reopenDesktop': False, 'forceClose': False})
                    if result.get('failed_pids'):
                        raise RuntimeError('Codex could not close gracefully. Finish its sessions and retry.')
                deadline = time.monotonic() + 15
                while not api.request('check_codex_processes')['can_switch']:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Codex is still running; no switch performed.')
                    time.sleep(0.25)
                api.request('switch_account', {'accountId': selected['id']})
                verified = api.request('get_active_account_info')
                if not verified or verified['id'] != selected['id']:
                    raise RuntimeError('The requested active account could not be verified.')
                if expect_identity:
                    identity = auth_identity(read_saved_records().get(selected['id'], {}))
                    if not identity or identity['identity'] != expect_identity:
                        raise RuntimeError('The switched account identity could not be verified.')
                print('Account switch verified.', flush=True)
            else:
                print('Requested account is already active.', flush=True)
            reopen_codex()
            reopened = True
            print('Codex desktop is running on %s.' % label, flush=True)
        finally:
            if closed and not reopened:
                try:
                    reopen_codex()
                    print('Codex desktop reopened after the failed switch.', flush=True)
                except Exception:
                    print('Codex desktop could not reopen; open it manually on the target Mac.', file=sys.stderr)


def rename_account(expected, account_id, new_name):
    verify_target(expected)
    if not new_name or not new_name.strip():
        raise RuntimeError('An account label cannot be empty.')
    with operation_lock('operation'), api_server() as api:
        select_account(api.request('list_accounts'), account_id)
        api.request('rename_account', {'accountId': account_id, 'newName': new_name.strip()})
    print('Saved account label updated.')


class RequestFailure(RuntimeError):
    """Keep verified switch progress when a later subprocess step fails."""
    def __init__(self, message, stdout=None, stderr=None):
        self.stdout = stdout or ''
        self.stderr = stderr or ''
        details = [message, self.stdout.strip(), self.stderr.strip()]
        super().__init__('\n'.join(detail for detail in details if detail))


def ssh_command(target, arguments, capture=False):
    command = ['/usr/bin/python3', SCRIPT, 'local'] + arguments + ['--run-on', target]
    result = subprocess.run(['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                             '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
                             MACHINES[target]['ssh'], ' '.join(map(shlex.quote, command))],
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, text=True)
    if result.returncode:
        raise RequestFailure('%s could not complete the request over SSH. No local fallback was used.'
                             % MACHINES[target]['name'], result.stdout, result.stderr)
    return result.stdout


def query_target(target):
    if target == machine_key():
        return list_accounts(target)
    return json.loads(ssh_command(target, ['--list'], capture=True))


def gather_devices():
    targets = [key for key in MACHINES if key != '_local']
    if len(targets) < 2:
        raise RuntimeError('Configure at least two Macs to use All devices.')
    result, errors = {}, []
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        pending = {pool.submit(query_target, key): key for key in targets}
        for future in as_completed(pending):
            key = pending[future]
            try:
                result[key] = future.result()
            except Exception as error:
                errors.append('%s: %s' % (MACHINES[key]['name'], error))
    if errors:
        raise RuntimeError('All devices preflight failed. No account changes made.\n' + '\n'.join(errors))
    return result


def common_accounts(snapshot):
    shared = set.intersection(*[{item['identity'] for item in accounts if item.get('identity')}
                                for accounts in snapshot.values()])
    result = []
    reference = next(iter(snapshot))
    for identity in shared:
        selected = {key: choose_equivalent(accounts, identity) for key, accounts in snapshot.items()}
        active_count = sum(bool(item.get('is_active')) for item in selected.values())
        result.append({'id': identity, 'identity': identity,
                       'name': selected[reference]['name'],
                       'is_active': active_count == len(snapshot),
                       'active_count': active_count, 'device_count': len(snapshot)})
    return sorted(result, key=lambda item: item['name'])


def all_plan(snapshot, identity):
    if not identity or not re.fullmatch(r'[0-9a-f]{64}', identity):
        raise RuntimeError('Choose a shared account in All devices first.')
    selected = {}
    for key, accounts in snapshot.items():
        try:
            selected[key] = choose_equivalent(accounts, identity)
        except RuntimeError:
            raise RuntimeError('The selected account and workspace are missing on %s. No account changes made.'
                               % MACHINES[key]['name']) from None
    return selected


def switch_target(target, selected, identity):
    arguments = ['--account-id', selected['id'], '--expect-identity', identity]
    if target == machine_key():
        result = subprocess.run(['/usr/bin/python3', SCRIPT, 'local', *arguments, '--run-on', target],
                                capture_output=True, text=True)
        if result.returncode:
            raise RequestFailure('Switch could not finish.', result.stdout, result.stderr)
        return result.stdout
    return ssh_command(target, arguments, capture=True)


def operate_all(identity, check_only=False):
    with operation_lock('all'):
        selected = all_plan(gather_devices(), identity)
        if check_only:
            for key, account in selected.items():
                print('%s: %s — available and identity matched.' % (MACHINES[key]['name'], account['name']))
            print('All devices preflight passed. No account changes or restart performed.')
            return
        print('Preflight passed. Switching all configured Macs concurrently...', flush=True)
        failures = []
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            pending = {pool.submit(switch_target, key, account, identity): key
                       for key, account in selected.items()}
            for future in as_completed(pending):
                key = pending[future]
                try:
                    future.result()
                    print('%s: %s — Codex is running.' % (MACHINES[key]['name'], selected[key]['name']), flush=True)
                except Exception as error:
                    failures.append(MACHINES[key]['name'])
                    print('%s: failed — %s' % (MACHINES[key]['name'], error), flush=True)
        if failures:
            raise RuntimeError('Some devices could not finish: %s. See the per-device results above.'
                               % ', '.join(failures))
        print('All configured Macs now use the selected ChatGPT account and workspace.', flush=True)


def main():
    global MACHINES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('machine', nargs='?', default='local')
    parser.add_argument('--account-id')
    parser.add_argument('--identity')
    parser.add_argument('--expect-identity')
    parser.add_argument('--run-on')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--devices', action='store_true')
    parser.add_argument('--identify', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rename')
    args = parser.parse_args()
    if args.identify:
        print(json.dumps({'identity': hardware_identity(), 'hostname': socket.gethostname()}))
        return
    MACHINES = load_devices()
    current = machine_key()
    if args.devices:
        print(json.dumps({'devices': [{'id': key, 'name': value['name']}
                                      for key, value in MACHINES.items() if key != '_local'],
                          'current_id': current}))
        return
    if args.run_on:
        verify_target(args.run_on)
        if args.list:
            print(json.dumps(list_accounts(args.run_on)))
        elif args.rename:
            rename_account(args.run_on, args.account_id, args.rename)
        elif args.account_id:
            operate(args.run_on, args.account_id, args.check, args.expect_identity)
        else:
            raise RuntimeError('No account selected; no account changes made.')
        return
    if args.machine == 'all':
        if args.list:
            print(json.dumps(common_accounts(gather_devices())))
        else:
            operate_all(args.identity, args.check)
        return
    target = current if args.machine == 'local' else args.machine
    if target not in MACHINES:
        raise RuntimeError('The selected Mac is not configured.')
    if args.list:
        print(json.dumps(query_target(target)))
        return
    if target == current:
        if args.rename:
            rename_account(target, args.account_id, args.rename)
        elif args.account_id:
            operate(target, args.account_id, args.check, args.expect_identity)
        else:
            raise RuntimeError('Choose a saved account in Raycast first.')
        return
    if not args.account_id:
        raise RuntimeError('Choose a saved account in Raycast first.')
    arguments = ['--account-id', args.account_id]
    if args.expect_identity:
        arguments += ['--expect-identity', args.expect_identity]
    if args.check:
        arguments.append('--check')
    if args.rename:
        arguments += ['--rename', args.rename]
    ssh_command(target, arguments)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise InterruptedError('Request interrupted; temporary HTTP helper stopped.')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as error:
        print('Error: %s' % error, file=sys.stderr, flush=True)
        sys.exit(1)
