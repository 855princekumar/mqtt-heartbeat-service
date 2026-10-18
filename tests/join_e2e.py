"""End to end, with no EdgeShell: SenseHive issues a join token, setup.py
installs the agent from it as a real systemd service on a Debian node, and
the node appears in SenseHive by itself. A second SenseHive with its own fleet
on the same broker never sees it. Stopping the agent shows the node offline.

    python tests/join_e2e.py --sensehive-dir ../sense-hive/version-1.0 \\
        --node NODE_CONTAINER --broker 127.0.0.1:1883 --node-broker host.docker.internal:1883

NODE_CONTAINER is a running systemd container (jrei/systemd-debian:12) with
python3; this repository is copied into it. The broker must accept anonymous
clients; --broker is how SenseHive (here) reaches it, --node-broker how the node does.
Prints one JSON line of measured times (E2E-STATS).
"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.cookiejar import CookieJar

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ('setup.py', 'install.sh', 'heartbeat_agent.py', 'heartbeat_protocol.py')


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SenseHive:
    """One SenseHive install from source, with its own data folder and fleet (auto)."""

    def __init__(self, src, broker, port, node_broker, label):
        self.data = tempfile.mkdtemp(prefix=f'sh-{label}-')
        self.port, self.password, self.label = free_port(), 'e2e-' + secrets.token_hex(8), label
        env = dict(os.environ, DB_PATH=os.path.join(self.data, 'sh.db'), HOST='127.0.0.1', PORT=str(self.port),
                   SENSEHIVE_ADMIN_PASSWORD=self.password, HEARTBEAT_MASTER_KEY='auto', HEARTBEAT_BROKER=broker,
                   HEARTBEAT_PORT=str(port), HEARTBEAT_TLS='false', SENSEHIVE_SECURE_TOPICS='false',
                   HEARTBEAT_NODE_BROKER=node_broker,
                   PYTHONUNBUFFERED='1')
        self.log = open(os.path.join(self.data, 'log.txt'), 'w')
        self.proc = subprocess.Popen([sys.executable, 'app.py'], cwd=src, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        wait(lambda: self._up(), 40, f'SenseHive {label} to start')
        self.call('/api/login', {'username': 'admin', 'password': self.password})
        wait(lambda: self.call('/api/heartbeat/status')['connected'], 30, f'SenseHive {label} to reach the broker')

    def _up(self):
        try:
            return urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/health', timeout=2).status == 200
        except OSError:
            return False

    def call(self, path, body=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        return json.load(self.opener.open(req, timeout=10))

    def node(self, name):
        return next((n for n in self.call('/api/heartbeat/nodes')['nodes'] if n['name'] == name), None)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def wait(fn, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:                       # noqa: BLE001 - still starting
            v = None
        if v:
            return v
        time.sleep(0.5)
    raise SystemExit(f'FAIL: timed out waiting for {what}')


def node_run(container, cmd, env=None, check=True):
    args = ['docker', 'exec']
    for k, v in (env or {}).items():
        args += ['-e', f'{k}={v}']
    out = subprocess.run(args + [container, 'bash', '-c', cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True)
    if check and out.returncode:
        print(out.stdout)
        raise SystemExit(f'FAIL: {cmd} exited {out.returncode}')
    return out.stdout


def check(ok, what, detail=''):
    print(('ok    ' if ok else 'FAIL  ') + what + (f'  ({detail})' if detail and not ok else ''))
    if not ok:
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sensehive-dir', required=True)
    ap.add_argument('--node', required=True, help='a running systemd container')
    ap.add_argument('--broker', default='127.0.0.1:1883', help='the broker as SenseHive reaches it')
    ap.add_argument('--node-broker', default='host.docker.internal:1883', help='the same broker as the node reaches it')
    a = ap.parse_args()
    host, port = a.broker.split(':')
    node_host = a.node_broker.split(':')[0]
    stats = {}

    a_hive = SenseHive(a.sensehive_dir, host, int(port), a.node_broker, 'company-a')
    b_hive = SenseHive(a.sensehive_dir, host, int(port), a.node_broker, 'company-b')
    try:
        info_a, info_b = a_hive.call('/api/heartbeat/join'), b_hive.call('/api/heartbeat/join')
        check(info_a['enabled'] and info_a['fleet'] != info_b['fleet'], 'two SenseHives, each with its own fleet (auto)')
        name = 'e2e-' + secrets.token_hex(3)
        made = a_hive.call('/api/heartbeat/join', {'node': name})
        check(made['success'] and made['token'].startswith('hbj1.'), 'company A issues a join token')

        node_run(a.node, 'rm -rf /root/mqtt-heartbeat-service && mkdir -p /root/mqtt-heartbeat-service')
        for f in FILES:
            subprocess.run(['docker', 'cp', os.path.join(REPO, f), f'{a.node}:/root/mqtt-heartbeat-service/{f}'], check=True)
        out = node_run(a.node, 'cd /root/mqtt-heartbeat-service && python3 setup.py --check "$T"', {'T': made['token']})
        check(f"node {name}, fleet {made['fleet']}" in out, 'setup.py checks the token and shows the same fleet')
        bad = node_run(a.node, 'cd /root/mqtt-heartbeat-service && HB_JOIN_TOKEN="${T%???}" python3 setup.py',
                       {'T': made['token'], 'HB_MQTT_USERNAME': 'admin', 'HB_MQTT_PASSWORD': 'admin123'}, check=False)
        check('Not set up' in bad and not node_run(a.node, 'ls /opt/edgeshell-heartbeat/agent.env 2>/dev/null', check=False).strip(),
              'a cut token is refused and installs nothing')

        t0 = time.time()
        login = {'HB_MQTT_USERNAME': 'admin', 'HB_MQTT_PASSWORD': 'admin123'}       # this test broker accepts any login
        out = node_run(a.node, 'cd /root/mqtt-heartbeat-service && HB_JOIN_TOKEN="$T" python3 setup.py',
                       dict(login, T=made['token']))
        stats['setup_s'] = round(time.time() - t0, 1)
        check('HEARTBEAT_OK' in out and 'Done.' in out, 'setup.py installs the agent', out[-400:])
        unit = node_run(a.node, 'systemctl is-active edgeshell-heartbeat; systemctl is-enabled edgeshell-heartbeat; '
                                'systemctl show edgeshell-heartbeat -p User --value; stat -c %a:%U:%G /opt/edgeshell-heartbeat/agent.env; '
                                'grep -c "^broker: ' + node_host + '$" /opt/edgeshell-heartbeat/config.yaml')
        check(unit.split() == ['active', 'enabled', 'hbagent', '640:root:hbagent', '1'],
              'a hardened systemd service, enabled at boot, key file private, broker from the token', unit)
        leftovers = node_run(a.node, 'grep -rl "$K" /root /tmp 2>/dev/null | head -3', {'K': made['token'][5:40]}, check=False).strip()
        check(not leftovers, 'no copy of the token or the keys left outside /opt', leftovers)

        n = wait(lambda: (x := a_hive.node(name)) and x['status'] == 'online' and x, 40, 'the node online in company A')
        stats['online_after_setup_s'] = round(time.time() - t0, 1)
        check(n['agent_version'] != '', f'company A shows {name} online by itself, with nothing added in SenseHive')
        time.sleep(15)
        check(b_hive.node(name) is None and b_hive.call('/api/heartbeat/nodes')['nodes'] == [],
              "company B, on the same broker, never sees company A's node")
        status = node_run(a.node, 'cd /root/mqtt-heartbeat-service && python3 setup.py --status', check=False)
        check(f'Node:  {name}' in status and f"Fleet: {made['fleet']}" in status, 'setup.py --status names the node and fleet')

        t1 = time.time()
        node_run(a.node, 'systemctl stop edgeshell-heartbeat')
        wait(lambda: a_hive.node(name)['status'] in ('offline', 'silent'), 60, 'the node offline after the agent stopped')
        stats['offline_after_stop_s'] = round(time.time() - t1, 1)
        check(True, 'stopping the agent: no heartbeats, SenseHive marks it ' + a_hive.node(name)['status'])
        out = node_run(a.node, 'cd /root/mqtt-heartbeat-service && python3 setup.py --uninstall')
        check('HEARTBEAT_UNINSTALLED' in out and not node_run(a.node, 'ls /opt/edgeshell-heartbeat 2>/dev/null', check=False).strip(),
              'setup.py --uninstall removes everything')
        print('E2E-STATS ' + json.dumps(stats, sort_keys=True))
    finally:
        a_hive.stop()
        b_hive.stop()


if __name__ == '__main__':
    main()
