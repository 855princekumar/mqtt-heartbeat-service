#!/usr/bin/env python3
"""Set up this node with a join token from SenseHive: one command, no EdgeShell.

On the node (Raspberry Pi OS, Debian, Ubuntu; Python 3.7+):

    git clone https://github.com/855princekumar/mqtt-heartbeat-service
    cd mqtt-heartbeat-service
    sudo python3 setup.py                 # paste the join token when asked

The token comes from SenseHive: Heartbeats, Add node, a name for this node.
It holds this node's name and keys and the broker SenseHive listens on, never
the fleet master key. setup.py checks it, writes agent.env and config.yaml, and
runs install.sh (a system user, /opt/edgeshell-heartbeat, the sandboxed systemd
service edgeshell-heartbeat, started and enabled at boot). Within seconds the
node appears on SenseHive's Heartbeats page by itself.

    sudo python3 setup.py --token-file FILE   read the token from a file (deleted after use)
    sudo python3 setup.py --check TOKEN       only check a token, change nothing
    sudo python3 setup.py --status            is the agent running, and for which node and fleet
    sudo python3 setup.py --uninstall         remove the agent

The token is a secret for this node: paste it, do not save it in shell history
(setup.py asks for it hidden when it is not given).
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = '/opt/edgeshell-heartbeat'
PREFIX = 'hbj1.'
HEX = re.compile(r'^[0-9a-f]+$')
NODE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')
HOST = re.compile(r'^[A-Za-z0-9.-]{1,253}$')


class BadToken(ValueError):
    pass


def read_token(token):
    """The checked contents of a join token, or BadToken with a plain reason."""
    token = ''.join(token.split())                       # a paste may wrap or carry spaces
    if not token.startswith(PREFIX):
        raise BadToken('not a SenseHive join token (it starts with "hbj1.")')
    raw = token[len(PREFIX):]
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    except (ValueError, TypeError):
        raise BadToken('the token is cut or changed: copy it again from SenseHive') from None
    if not isinstance(body, dict) or body.get('v') != 1:
        raise BadToken('this token is for a newer agent: update this repository (git pull)')
    check = body.pop('c', None)
    text = json.dumps(body, separators=(',', ':'), sort_keys=True)
    if check != hashlib.sha256(text.encode()).hexdigest()[:8]:
        raise BadToken('the token is cut or changed: copy it again from SenseHive')
    node, kid, nk, tk = body.get('node'), body.get('kid'), body.get('nk'), body.get('tk')
    if not (isinstance(node, str) and NODE.match(node)):
        raise BadToken('the node name in the token is not valid')
    for value, length in ((kid, 16), (nk, 64), (tk, 64)):
        if not (isinstance(value, str) and len(value) == length and HEX.match(value)):
            raise BadToken('the keys in the token are not valid')
    broker, port, tls = body.get('broker'), body.get('port'), body.get('tls')
    if not (isinstance(broker, str) and HOST.match(broker)) or not isinstance(port, int) or isinstance(port, bool) \
            or not 1 <= port <= 65535 or not isinstance(tls, bool):
        raise BadToken('the broker settings in the token are not valid')
    return body


def fleet_id(tk_hex):
    """The same short fleet name SenseHive shows (Heartbeats, Add node)."""
    return hashlib.sha256(b'fleet id|' + bytes.fromhex(tk_hex)).hexdigest()[:12]


def agent_env(t):
    return (f"HB_NODE={t['node']}\nHB_KID={t['kid']}\nHB_NODE_KEY={t['nk']}\nHB_TOPIC_KEY={t['tk']}\n")


def config_yaml(t, interval):
    return ('# MQTT heartbeat agent settings, written by setup.py from the SenseHive join token.\n'
            '# Hot reloaded: save a new version and the agent applies it within one interval.\n'
            f"broker: {t['broker']}\nport: {t['port']}\ntls: {'true' if t['tls'] else 'false'}\n"
            f'interval: {interval}\n')


def current_interval():
    """Keep an interval set before (e.g. pushed by EdgeShell); 10 otherwise."""
    try:
        with open(os.path.join(DIR, 'config.yaml')) as f:
            m = re.search(r'^interval:\s*(\d+)', f.read(), re.M)
        if m and 5 <= int(m.group(1)) <= 3600:
            return int(m.group(1))
    except OSError:
        pass
    return 10


def write_private(path, text, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(text)


def install(t):
    os.makedirs(DIR, mode=0o755, exist_ok=True)
    # config.yaml first, so the agent's very first connection goes to SenseHive's broker
    tmp = os.path.join(DIR, 'config.yaml.new')
    write_private(tmp, config_yaml(t, current_interval()), 0o644)
    os.replace(tmp, os.path.join(DIR, 'config.yaml'))
    with tempfile.TemporaryDirectory() as work:            # agent.env never lands anywhere but /opt
        env = os.path.join(work, 'agent.env')
        write_private(env, agent_env(t), 0o600)
        out = subprocess.run(['bash', os.path.join(HERE, 'install.sh'), env],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    print(out.stdout.rstrip())
    return out.returncode == 0 and 'HEARTBEAT_OK' in out.stdout


def status():
    try:
        with open(os.path.join(DIR, 'agent.env')) as f:
            env = dict(line.strip().split('=', 1) for line in f if '=' in line)
    except OSError:
        print('The agent is not installed on this node.')
        return 1
    print(f"Node:  {env.get('HB_NODE', '?')}")
    if HEX.match(env.get('HB_TOPIC_KEY', '')):
        print(f"Fleet: {fleet_id(env['HB_TOPIC_KEY'])}  (SenseHive shows the same under Heartbeats, Add node)")
    if os.path.isdir('/run/systemd/system'):
        return subprocess.call(['systemctl', '--no-pager', 'status', 'edgeshell-heartbeat'])
    return subprocess.call(['pgrep', '-a', '-f', DIR + '/heartbeat_agent.py'])


def main(argv=None):
    ap = argparse.ArgumentParser(description='Set up the MQTT heartbeat agent on this node with a SenseHive join token.')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--token-file', help='read the join token from this file, and delete the file after a good install')
    g.add_argument('--check', metavar='TOKEN', help='only check a join token')
    g.add_argument('--status', action='store_true', help='show whether the agent runs, for which node and fleet')
    g.add_argument('--uninstall', action='store_true', help='remove the agent from this node')
    a = ap.parse_args(argv)

    if a.check:
        try:
            t = read_token(a.check)
        except BadToken as e:
            print(f'Not valid: {e}')
            return 1
        print(f"Valid: node {t['node']}, fleet {fleet_id(t['tk'])}, broker {t['broker']}:{t['port']} "
              f"tls={'on' if t['tls'] else 'off'}")
        return 0
    if a.status:
        return status()
    if os.geteuid() != 0:
        print('Run it with sudo: sudo python3 setup.py')
        return 1
    if a.uninstall:
        return subprocess.call(['bash', os.path.join(HERE, 'install.sh'), '--uninstall'])

    if a.token_file:
        with open(a.token_file) as f:
            raw = f.read()
    elif os.getenv('HB_JOIN_TOKEN'):
        raw = os.environ['HB_JOIN_TOKEN']
    else:
        print('Paste the join token from SenseHive (Heartbeats, Add node). It is not shown while you paste.')
        raw = getpass.getpass('Join token: ')
    try:
        t = read_token(raw)
    except BadToken as e:
        print(f'Not set up: {e}')
        return 1
    print(f"Setting up node {t['node']} (fleet {fleet_id(t['tk'])}, broker {t['broker']}:{t['port']} "
          f"tls={'on' if t['tls'] else 'off'})")
    if not install(t):
        print('Not set up: see the messages above (docs/SETUP.md, Troubleshooting).')
        return 1
    if a.token_file:
        os.remove(a.token_file)
    print(f"Done. {t['node']} appears on SenseHive's Heartbeats page within a few seconds.")
    print('Check this node any time with: sudo python3 setup.py --status')
    return 0


if __name__ == '__main__':
    sys.exit(main())
