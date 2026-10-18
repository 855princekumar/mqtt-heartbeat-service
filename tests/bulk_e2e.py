"""tools/bulk_setup.py end to end: SenseHive makes tokens in bulk, the tool sets
up real systemd nodes over SSH with them and a broker login, and SenseHive shows
the nodes online. Then a wrong password is refused and the old login stays, and
a new password is rolled out to every node. A node SSH cannot reach is reported.

    python tests/bulk_e2e.py --sensehive-dir ../sense-hive/version-1.0

Needs Docker (it starts the broker and three Debian 12 systemd nodes with SSH,
and removes them after), ssh and ssh-keygen. Prints one JSON line of measured
times (BULK-STATS).
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from join_e2e import SenseHive, check, wait  # noqa: E402

NODES = ['bulk-01', 'bulk-02', 'bulk-03']
BROKER_PORT = 18841
NODE_IMAGE = 'jrei/systemd-debian:12'


def docker(*args, check_rc=True):
    out = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check_rc and out.returncode:
        raise SystemExit(f"FAIL: docker {' '.join(args[:3])}: {out.stdout[-500:]}")
    return out.stdout


def start(work):
    """The broker (login required) and the nodes. Returns {node: ssh port}."""
    script = ('mosquitto_passwd -b -c /tmp/passwd admin admin123 && chown mosquitto:mosquitto /tmp/passwd && '
              'printf "listener 1883\\nallow_anonymous false\\npassword_file /tmp/passwd\\n" > /tmp/m.conf && '
              'exec mosquitto -c /tmp/m.conf')
    docker('run', '-d', '--name', 'bulk-mq', '-p', f'{BROKER_PORT}:1883', 'eclipse-mosquitto:2', 'sh', '-c', script)
    key = os.path.join(work, 'id_ed25519')
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], check=True)
    with open(key + '.pub') as f:
        pub = f.read().strip()
    ports = {}
    for i, name in enumerate(NODES):
        port = 2240 + i
        docker('run', '-d', '--name', name, '--hostname', name, '--privileged', '--cgroupns=host',
               '-v', '/sys/fs/cgroup:/sys/fs/cgroup:rw', '--add-host', 'host.docker.internal:host-gateway',
               '-p', f'{port}:22', NODE_IMAGE)
        ports[name] = port
    for name in NODES:
        wait(lambda n=name: 'running' in docker('exec', n, 'systemctl', 'is-system-running', check_rc=False)
             or 'degraded' in docker('exec', n, 'systemctl', 'is-system-running', check_rc=False), 60, f'{name} to boot')
        docker('exec', name, 'bash', '-c',
               'apt-get update -qq && apt-get install -y -qq python3 openssh-server >/dev/null && '
               f'mkdir -p /root/.ssh && echo "{pub}" > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys '
               '&& systemctl enable --now ssh >/dev/null 2>&1')
    return key, ports


def stop():
    docker('rm', '-f', 'bulk-mq', *NODES, check_rc=False)


def bulk(work, mode, cfg_extra=None, tokens=True):
    args = [sys.executable, os.path.join(REPO, 'tools', 'bulk_setup.py'), '--config', os.path.join(work, 'bulk.yaml'),
            '--mode', mode, '--nodes', os.path.join(work, 'nodes.csv'), '--yes']
    if tokens:
        args += ['--tokens', os.path.join(work, 'tokens.csv')]
    out = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    print(out.stdout)
    path = out.stdout.strip().rsplit('Results: ', 1)[-1].strip()
    with open(path, newline='') as f:
        rows = {r['node']: r for r in csv.DictReader(f)}
    return out.returncode, rows


def write_config(work, key, password, sensehive=None):
    lines = ['broker_login:', '  username: admin', f'  password: {password}', 'ssh:', '  user: root',
             f"  key_file: '{key}'", f"  known_hosts_file: '{os.path.join(work, 'known_hosts')}'", 'parallel: 4',
             f"results_dir: '{os.path.join(work, 'results')}'"]
    if sensehive:
        lines += ['sensehive:', f"  url: http://127.0.0.1:{sensehive.port}", f'  api_token: {sensehive.api_token}']
    with open(os.path.join(work, 'bulk.yaml'), 'w') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sensehive-dir', required=True)
    a = ap.parse_args()
    work = tempfile.mkdtemp(prefix='bulk-e2e-')
    stats = {}
    stop()
    try:
        key, ports = start(work)
        api_token = 'bulk-' + os.urandom(16).hex()
        os.environ['HEARTBEAT_API_TOKEN'] = api_token
        os.environ['HEARTBEAT_BROKER_USERNAME'], os.environ['HEARTBEAT_BROKER_PASSWORD'] = 'admin', 'admin123'
        hive = SenseHive(a.sensehive_dir, '127.0.0.1', BROKER_PORT, f'host.docker.internal:{BROKER_PORT}', 'bulk')
        hive.api_token = api_token
        try:
            made = hive.call('/api/heartbeat/join', {'nodes': NODES + ['unreachable-01']})
            check(made['success'] and len(made['tokens']) == 4, 'SenseHive makes 4 tokens in bulk')
            with open(os.path.join(work, 'tokens.csv'), 'w', newline='') as f:
                f.write('node,token\n' + ''.join(f"{t['node']},{t['token']}\n" for t in made['tokens']))
            with open(os.path.join(work, 'nodes.csv'), 'w', newline='') as f:
                f.write('node,host,user,port\n' + ''.join(f'{n},127.0.0.1,root,{p}\n' for n, p in ports.items())
                        + 'unreachable-01,127.0.0.1,root,2299\n')
            write_config(work, key, 'admin123', hive)

            t0 = time.time()
            rc, rows = bulk(work, 'setup')
            stats['setup_3_nodes_s'] = round(time.time() - t0, 1)
            check(all(rows[n]['ssh'] == rows[n]['action'] == rows[n]['test'] == 'ok' for n in NODES),
                  'the tool sets up every reachable node, and each passes its own test', json.dumps(rows)[:400])
            if not all(rows[n]['sensehive'] == 'online' for n in NODES):          # say why before failing
                print('SenseHive nodes:', json.dumps(hive.call('/api/heartbeat/nodes'))[:600])
                print('SenseHive status:', json.dumps(hive.call('/api/heartbeat/status')))
                for n in NODES:
                    print(n, docker('exec', n, 'journalctl', '-u', 'edgeshell-heartbeat', '--no-pager', '-n', '8',
                                    check_rc=False))
            check(all(rows[n]['sensehive'] == 'online' for n in NODES), 'SenseHive shows all of them online')
            check(rows['unreachable-01']['ssh'] == 'fail' and rc == 1, 'a node SSH cannot reach is reported, the run says so')
            check(all(rows[n]['broker'].startswith('tcp:') for n in NODES), 'plain MQTT found and kept (the only way in here)',
                  [rows[n]['broker'] for n in NODES])
            leftovers = docker('exec', NODES[0], 'bash', '-c', 'ls /tmp; grep -rl admin123 /root /tmp /home 2>/dev/null | head -3',
                               check_rc=False)
            check('tmp.' not in leftovers and 'admin123' not in leftovers, 'no bundle, token or password left in temp folders')
            perms = docker('exec', NODES[0], 'stat', '-c', '%a:%U:%G', '/opt/edgeshell-heartbeat/mqtt.env').strip()
            cfg_mode = docker('exec', NODES[0], 'stat', '-c', '%a', '/opt/edgeshell-heartbeat/config.yaml').strip()
            check(cfg_mode == '644', 'config.yaml readable by the agent (whatever the umask)', cfg_mode)
            check(perms == '640:root:hbagent', 'the broker login is private on the node', perms)

            write_config(work, key, 'wrong-password', hive)
            rc, rows = bulk(work, 'login', tokens=False)
            check(all(rows[n]['action'] == 'fail' and 'refused' in rows[n]['detail'] for n in NODES),
                  'a wrong password is refused before it is saved', json.dumps(rows)[:300])
            time.sleep(12)
            check(all((hive.node(n) or {}).get('status') == 'online' for n in NODES), 'the nodes keep the old login and stay online')

            docker('exec', 'bulk-mq', 'sh', '-c',
           'mosquitto_passwd -b /tmp/passwd admin rotated-2 && chown mosquitto:mosquitto /tmp/passwd && kill -HUP 1')
            write_config(work, key, 'rotated-2', hive)
            t1 = time.time()
            rc, rows = bulk(work, 'login', tokens=False)
            stats['new_login_3_nodes_s'] = round(time.time() - t1, 1)
            check(all(rows[n]['action'] == rows[n]['test'] == 'ok' for n in NODES), 'a new password is rolled out to every node')
            wait(lambda: 'broker login changed: reconnecting' in docker(            # picked up within one beat
                'exec', NODES[1], 'journalctl', '-u', 'edgeshell-heartbeat', '--no-pager', check_rc=False), 40,
                 'the agent to pick up the new login')
            check(True, 'the agent reconnects with it, no restart')
            wait(lambda: all((hive.node(n) or {}).get('status') == 'online' for n in NODES), 60, 'online with the new login')
            check(True, 'the nodes stay online with the new login')
            rc, rows = bulk(work, 'test', tokens=False)
            check(all(rows[n]['test'] == 'ok' and rows[n]['sensehive'] == 'online' for n in NODES),
                  'test mode: every node passes, SenseHive shows them online')
            print('BULK-STATS ' + json.dumps(stats, sort_keys=True))
        finally:
            hive.stop()
    finally:
        stop()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
