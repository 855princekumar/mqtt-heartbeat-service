#!/usr/bin/env python3
"""Set up, re-login or test the heartbeat agent on many nodes over SSH, without
EdgeShell (EdgeShell's Fleet Setup does all of this for you).

    pip install pyyaml                       # paramiko too, only for SSH passwords
    cp tools/bulk.example.yaml tools/bulk.yaml   # then set the broker login in it
    python tools/bulk_setup.py

It explains what it will do, then asks what to do and lets you pick the files:

  1  set up nodes      the tokens CSV from SenseHive (Heartbeats, Add node, bulk,
                       Download CSV) and a nodes CSV (node,host,user,port)
  2  set up one node   one host and its token
  3  new broker login  the username and password in bulk.yaml, pushed to every node
  4  test             SSH, the service, the broker (TLS or plain) and the login

For each node it copies the agent over SSH (no git or internet needed on the
node beyond the broker), installs it with the node's own token and the broker
login, then runs the node's own test. Tokens and passwords travel only inside
the SSH session (never on a command line, never in a file left behind). The
results go to a CSV in results/ (no secrets in it).

Without a screen (or with --yes) everything comes from the command line:

    python tools/bulk_setup.py --mode setup --tokens tokens.csv --nodes nodes.csv --yes
    python tools/bulk_setup.py --mode login --nodes nodes.csv --yes
    python tools/bulk_setup.py --mode test  --nodes nodes.csv --yes
"""
import argparse
import base64
import concurrent.futures
import csv
import getpass
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AGENT_FILES = ('heartbeat_agent.py', 'heartbeat_protocol.py', 'install.sh', 'setup.py')
NODE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')
DEFAULTS = {'broker_login': {'username': '', 'password': ''},
            'tls': {'require': False, 'plain_port': 1883},
            'ssh': {'user': 'pi', 'port': 22, 'key_file': '', 'password': '', 'sudo_password': '', 'connect_timeout': 10,
                    'known_hosts_file': ''},
            'parallel': 10, 'sensehive': {'url': '', 'api_token': ''}, 'results_dir': 'results'}
INTRO = """
Heartbeat agent, bulk setup over SSH
------------------------------------
For each node: copy the agent over SSH, install it with the node's own join
token and the broker login from bulk.yaml (TLS first, plain MQTT only if TLS
cannot be reached), then test SSH, the service, the broker and the login.
Nothing is changed on a node that fails a step before the install.
"""


# ---------------------------------------------------------------- settings and files

def load_config(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit('Needs PyYAML: pip install pyyaml') from None
    if not os.path.isfile(path):
        raise SystemExit(f'{path} not found: copy tools/bulk.example.yaml to tools/bulk.yaml and set the broker login.')
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    login = cfg['broker_login']
    login['username'], login['password'] = str(login.get('username') or ''), str(login.get('password') or '')
    if not login['username'] or not login['password']:
        raise SystemExit(f'{path}: broker_login needs a username and a password.')
    if (login['username'], login['password']) == ('admin', 'admin123'):
        print('NOTE: bulk.yaml uses the example login admin / admin123. Make sure your broker has this user,\n'
              '      and change it on the broker and in bulk.yaml (then run "new broker login") for production.\n')
    return cfg


def read_csv(path, required):
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f'{path} is empty')
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise SystemExit(f'{path} needs the columns {", ".join(required)} (missing: {", ".join(missing)})')
    return [{k.strip(): (v or '').strip() for k, v in r.items() if k} for r in rows]


def plan(nodes_csv, tokens_csv, cfg, need_tokens):
    """The nodes to work on: node, host, user, port, token. Problems found before any SSH."""
    nodes = read_csv(nodes_csv, ('node', 'host'))
    tokens = {r['node']: r['token'] for r in read_csv(tokens_csv, ('node', 'token'))} if tokens_csv else {}
    out, problems, seen = [], [], set()
    for r in nodes:
        name = r['node']
        if not NODE.match(name):
            problems.append(f'{name or "(empty)"}: not a valid node name')
            continue
        if name in seen:
            problems.append(f'{name}: listed twice')
            continue
        seen.add(name)
        if need_tokens and name not in tokens:
            problems.append(f'{name}: no token for it in the tokens CSV')
            continue
        try:
            port = int(r.get('port') or cfg['ssh']['port'])
        except ValueError:
            problems.append(f'{name}: SSH port is not a number')
            continue
        out.append({'node': name, 'host': r['host'], 'user': r.get('user') or cfg['ssh']['user'], 'port': port,
                    'token': tokens.get(name, '')})
    return out, problems


# ---------------------------------------------------------------- SSH

class SystemSSH:
    """The ssh client of this machine (Windows 10+, Linux, macOS): keys or an ssh agent."""

    def __init__(self, cfg):
        self.cfg = cfg['ssh']

    def run(self, n, command, stdin=b'', timeout=300):
        args = ['ssh', '-p', str(n['port']), '-o', 'BatchMode=yes', '-o', f"ConnectTimeout={self.cfg['connect_timeout']}",
                '-o', 'StrictHostKeyChecking=accept-new']
        if self.cfg.get('key_file'):
            args += ['-i', os.path.expanduser(self.cfg['key_file'])]
        if self.cfg.get('known_hosts_file'):
            args += ['-o', f"UserKnownHostsFile={os.path.expanduser(self.cfg['known_hosts_file'])}"]
        args += [f"{n['user']}@{n['host']}", command]
        try:
            p = subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, 'timed out'
        return p.returncode, p.stdout.decode('utf-8', 'replace')


class ParamikoSSH:
    """SSH with a password (ssh.password in bulk.yaml): needs pip install paramiko."""

    def __init__(self, cfg):
        try:
            import paramiko
        except ImportError:
            raise SystemExit('ssh.password is set: pip install paramiko (or use SSH keys)') from None
        self.paramiko, self.cfg = paramiko, cfg['ssh']

    def _policy(self):
        """Like ssh -o StrictHostKeyChecking=accept-new: a node seen for the first time is
        remembered in the known hosts file; a node whose key changed is refused (paramiko
        raises BadHostKeyException for a known host with another key)."""
        known = os.path.expanduser(self.cfg.get('known_hosts_file') or '~/.ssh/known_hosts')
        paramiko = self.paramiko

        class AcceptNew(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                client.get_host_keys().add(hostname, key.get_name(), key)
                os.makedirs(os.path.dirname(known) or '.', exist_ok=True)
                client.save_host_keys(known)
        return known, AcceptNew()

    def run(self, n, command, stdin=b'', timeout=300):
        c = self.paramiko.SSHClient()
        known, policy = self._policy()
        if os.path.isfile(known):
            c.load_host_keys(known)
        c.set_missing_host_key_policy(policy)
        try:
            c.connect(n['host'], port=n['port'], username=n['user'], password=self.cfg['password'],
                      key_filename=os.path.expanduser(self.cfg['key_file']) if self.cfg.get('key_file') else None,
                      timeout=self.cfg['connect_timeout'], allow_agent=True, look_for_keys=True)
            # Bandit B601 reviewed: `command` is built in this file from fixed text and
            # shlex.quote(); node names, tokens and passwords travel on stdin, never in it
            i, o, _ = c.exec_command(command, timeout=timeout, get_pty=False)  # nosec B601
            if stdin:
                i.write(stdin)
            i.channel.shutdown_write()
            text = o.read().decode('utf-8', 'replace')
            return o.channel.recv_exit_status(), text
        except Exception as e:                                       # noqa: BLE001 - reported per node
            return 255, f'{type(e).__name__}: {e}'
        finally:
            c.close()


def bundle(files):
    """A tar of {name: bytes}, every file 0600."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def agent_files():
    out = {}
    for name in AGENT_FILES:
        with open(os.path.join(REPO, name), 'rb') as f:
            out[name] = f.read()
    return out


def run_bundle(ssh, cfg, n, payload, args):
    """Sends a private bundle (the agent files, secrets as files) on stdin, unpacks it in
    a temp folder only the SSH user can read, runs setup.py there as root with `args`,
    and always removes the folder. With ssh.sudo_password, sudo reads it from the bundle."""
    payload = dict(payload)
    if cfg['ssh'].get('sudo_password'):
        payload['sudopw'] = (cfg['ssh']['sudo_password'] + '\n').encode()
        as_root = f'sudo -S -p "" python3 setup.py {args} < sudopw'
    else:
        as_root = f'sudo -n python3 setup.py {args}'
    # logged in as root (minimal systems have no sudo): run it directly
    run = f'if [ "$(id -u)" = 0 ]; then python3 setup.py {args}; else {as_root}; fi'
    script = (f'umask 077; d=$(mktemp -d) && chmod 700 "$d" && tar -xf - -C "$d" || exit 90; '
              f'cd "$d" && {run}; rc=$?; cd /; rm -rf "$d" 2>/dev/null || sudo -n rm -rf "$d"; exit $rc')
    return ssh.run(n, f'sh -c {shlex.quote(script)}', stdin=bundle(payload))


def work_on(ssh, cfg, n, mode, files):
    """One node: returns a result row."""
    row = {'node': n['node'], 'host': n['host'], 'ssh': '', 'action': '', 'broker': '', 'test': '', 'sensehive': '',
           'detail': ''}
    rc, out = ssh.run(n, 'command -v python3 >/dev/null && python3 -c "import sys; sys.exit(sys.version_info < (3, 7))" '
                         '&& echo HB_SSH_OK', timeout=60)
    if 'HB_SSH_OK' not in out:
        row['ssh'] = 'fail'
        row['detail'] = ('no Python 3.7+ on the node' if rc in (0, 1) and 'HB_SSH_OK' not in out and 'denied' not in out
                         else out.strip().splitlines()[-1] if out.strip() else f'ssh exit {rc}')
        return row
    row['ssh'] = 'ok'
    login = (f"HB_MQTT_USERNAME={cfg['broker_login']['username']}\n"
             f"HB_MQTT_PASSWORD={cfg['broker_login']['password']}\n").encode()
    extra = (' --require-tls' if cfg['tls'].get('require') else '') + f" --plain-port {int(cfg['tls'].get('plain_port', 1883))}"

    if mode in ('setup', 'login'):
        if mode == 'setup':
            rc, out = run_bundle(ssh, cfg, n, dict(files, login=login, token=n['token'].encode()),
                                 f'--token-file token --login-file login{extra}')
        else:
            rc, out = run_bundle(ssh, cfg, n, dict(files, login=login), '--set-credentials --login-file login')
        ok = ('Done.' in out and 'HEARTBEAT_OK' in out) if mode == 'setup' else 'HEARTBEAT_LOGIN_OK' in out
        row['action'] = 'ok' if ok else 'fail'
        m = re.search(r'^Broker: \S+ over (\S+) on port (\d+)', out, re.M)
        if m:
            row['broker'] = f'{m.group(1)}:{m.group(2)}'
        if not ok:
            lines = [x for x in out.strip().splitlines() if x.strip()]
            row['detail'] = next((x for x in reversed(lines) if x.startswith(('Not set up', 'Not changed', 'sudo'))),
                                 lines[-1] if lines else f'exit {rc}')
            return row

    rc, out = run_bundle(ssh, cfg, n, files, '--test')
    row['test'] = 'ok' if 'HEARTBEAT_TEST ok' in out else 'fail'
    m = re.search(r'reached over (\S+)', out)
    if m and not row['broker']:
        row['broker'] = m.group(1)
    if row['test'] != 'ok':
        fails = [x[6:] for x in out.splitlines() if x.startswith('FAIL  ')]
        row['detail'] = '; '.join(fails) or (out.strip().splitlines()[-1] if out.strip() else f'exit {rc}')
    return row


# ---------------------------------------------------------------- SenseHive (optional)

def sensehive_online(cfg, names, wait=90):
    url, token = cfg['sensehive'].get('url', '').rstrip('/'), cfg['sensehive'].get('api_token', '')
    if not url or not token:
        return None
    if not url.startswith(('http://', 'https://')):
        print(f'sensehive.url must start with http:// or https:// (got {url[:40]!r}); not checked')
        return None
    req = urllib.request.Request(url + '/api/heartbeat/nodes', headers={'Authorization': f'Bearer {token}'})
    end, online = time.time() + wait, set()
    while time.time() < end:
        try:
            # Bandit B310 reviewed: the URL is SenseHive's address from bulk.yaml, http(s) only (checked above)
            nodes = json.load(urllib.request.urlopen(req, timeout=10)).get('nodes', [])  # nosec B310
            online = {x['name'] for x in nodes if x.get('status') == 'online'}
        except Exception:                                            # noqa: BLE001 - keep trying
            pass
        if set(names) <= online:
            break
        time.sleep(3)
    return online


# ---------------------------------------------------------------- the conversation

def pick_file(title, typed_prompt, use_gui):
    if use_gui:
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askopenfilename(title=title, filetypes=[('CSV files', '*.csv'), ('All files', '*.*')])
            root.destroy()
            if path:
                return path
        except Exception:                                            # noqa: BLE001 - no screen: ask instead
            pass
    path = input(f'{typed_prompt}: ').strip().strip('"')
    if not os.path.isfile(path):
        raise SystemExit(f'{path} not found')
    return path


def choose(args):
    if args.mode:
        return args.mode
    print('What to do?\n  1  set up nodes (bulk: tokens CSV and nodes CSV)\n  2  set up one node\n'
          '  3  new broker login on every node (username and password from bulk.yaml)\n  4  test nodes')
    pick = input('Choose 1, 2, 3 or 4: ').strip()
    return {'1': 'setup', '2': 'one', '3': 'login', '4': 'test'}.get(pick) or sys.exit('Nothing chosen.')


def main(argv=None):
    ap = argparse.ArgumentParser(description='Heartbeat agent on many nodes over SSH (without EdgeShell).')
    ap.add_argument('--config', default=os.path.join(HERE, 'bulk.yaml'))
    ap.add_argument('--mode', choices=('setup', 'one', 'login', 'test'))
    ap.add_argument('--tokens', help='tokens CSV from SenseHive (node,token)')
    ap.add_argument('--nodes', help='nodes CSV (node,host,user,port)')
    ap.add_argument('--yes', action='store_true', help='no pause, no questions, no file windows')
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    if not a.yes:
        print(INTRO)
        for left in range(5, 0, -1):
            print(f'\rStarting in {left} s (Ctrl+C to stop) ', end='', flush=True)
            time.sleep(1)
        print('\n')
    mode = choose(a)
    gui = not a.yes

    if mode == 'one':
        host = input('Node address (host or IP): ').strip()
        user = input(f"SSH user [{cfg['ssh']['user']}]: ").strip() or cfg['ssh']['user']
        token = getpass.getpass('Join token from SenseHive (not shown): ').strip()
        name = re.sub(r'[^A-Za-z0-9._-]', '-', host)
        try:
            body = json.loads(base64.urlsafe_b64decode(token[5:] + '=' * (-len(token[5:]) % 4)))
            name = body.get('node') or name
        except Exception:                                            # noqa: BLE001 - setup.py says what is wrong
            pass
        todo, problems, mode = [{'node': name, 'host': host, 'user': user, 'port': int(cfg['ssh']['port']), 'token': token}], [], 'setup'
    else:
        nodes_csv = a.nodes or pick_file('Nodes CSV (node,host,user,port)', 'Nodes CSV path', gui)
        tokens_csv = None
        if mode == 'setup':
            tokens_csv = a.tokens or pick_file('Tokens CSV from SenseHive (node,token)', 'Tokens CSV path', gui)
        todo, problems = plan(nodes_csv, tokens_csv, cfg, need_tokens=mode == 'setup')
    for p in problems:
        print('SKIP  ' + p)
    if not todo:
        raise SystemExit('No node to work on.')
    if not a.yes and input(f'{mode} on {len(todo)} node(s)? [y/N] ').strip().lower() != 'y':
        raise SystemExit('Stopped, nothing changed.')

    ssh = ParamikoSSH(cfg) if cfg['ssh'].get('password') else SystemSSH(cfg)
    files = agent_files()
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(cfg.get('parallel') or 10))) as pool:
        futures = {pool.submit(work_on, ssh, cfg, n, mode, files): n for n in todo}
        for fut in concurrent.futures.as_completed(futures):
            row = fut.result()
            rows.append(row)
            good = row['ssh'] == 'ok' and row['action'] in ('', 'ok') and row['test'] == 'ok'
            print(f"{'OK  ' if good else 'FAIL'}  {row['node']:<24} {row['host']:<20} "
                  f"{row['broker'] or '-':<32} {row['detail']}", flush=True)

    passed = [r['node'] for r in rows if r['ssh'] == 'ok' and r['action'] in ('', 'ok') and r['test'] == 'ok']
    online = sensehive_online(cfg, passed) if mode in ('setup', 'test') and passed else None
    for r in rows:
        if online is not None and r['node'] in passed:
            r['sensehive'] = 'online' if r['node'] in online else 'not online yet'

    out_dir = os.path.join(HERE, cfg.get('results_dir') or 'results') if not os.path.isabs(str(cfg.get('results_dir'))) \
        else cfg['results_dir']
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"bulk-{mode}-{time.strftime('%Y%m%d-%H%M%S')}.csv")
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['node', 'host', 'ssh', 'action', 'broker', 'test', 'sensehive', 'detail'])
        w.writeheader()
        for r in sorted(rows, key=lambda x: x['node']):
            w.writerow({k: ("'" + v if isinstance(v, str) and v and v[0] in '=+-@\t\r' else v) for k, v in r.items()})
    failed = len(rows) - len(passed)
    print(f'\n{len(passed)} ok, {failed} failed' + (f", {sum(1 for r in rows if r['sensehive'] == 'online')} online in SenseHive"
                                                    if online is not None else '') + f'. Results: {path}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
