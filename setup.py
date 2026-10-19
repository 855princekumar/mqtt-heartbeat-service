#!/usr/bin/env python3
"""Set up this node with a join token from SenseHive: one command, no EdgeShell.

On the node (Raspberry Pi OS, Debian, Ubuntu; Python 3.7+):

    git clone https://github.com/855princekumar/mqtt-heartbeat-service
    cd mqtt-heartbeat-service
    sudo python3 setup.py                 # paste the join token, then the broker login

The token comes from SenseHive: Heartbeats, Add node, a name for this node.
It holds this node's name and keys and the broker SenseHive listens on, never
the fleet master key. setup.py checks it, finds how to reach the broker (TLS
first, then WebSocket over TLS, plain MQTT and plain WebSocket, each with a real
login: a checkpoint per node), keeps the best way that works as the preferred
one (the agent switches to another by itself when it stops working), checks the
broker login, writes agent.env, mqtt.env and config.yaml, and runs install.sh (a system
user, /opt/edgeshell-heartbeat, the sandboxed systemd service
edgeshell-heartbeat, started and enabled at boot). Within seconds the node
appears on SenseHive's Heartbeats page by itself.

    sudo python3 setup.py --token-file FILE    read the token from a file (deleted after use)
    sudo python3 setup.py --login-file FILE    read HB_MQTT_USERNAME / HB_MQTT_PASSWORD from a file (deleted after use)
    sudo python3 setup.py --require-tls        never fall back to plain MQTT
    sudo python3 setup.py --set-credentials    only change the broker login (no token needed)
    sudo python3 setup.py --test               check the service, the broker and the login
    sudo python3 setup.py --check TOKEN        only check a token, change nothing
    sudo python3 setup.py --status             is the agent running, and for which node and fleet
    sudo python3 setup.py --uninstall          remove the agent

After an install, the same tool is at /opt/edgeshell-heartbeat/setup.py, so
--status, --test and --set-credentials work without the clone.

The token and the password are secrets: setup.py asks for them hidden when they
are not given, and they never appear on a command line (HB_JOIN_TOKEN,
HB_MQTT_USERNAME and HB_MQTT_PASSWORD in the environment work too, for scripts).
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
# The agent's folder, its group and its service. Defaults: the classic install by install.sh.
# EdgeShell's esapp install sets HB_HOME=/opt/edgeshell/apps/heartbeat/data, HB_GROUP and HB_UNIT=es-heartbeat.
DIR = os.environ.get('HB_HOME') or '/opt/edgeshell-heartbeat'
GROUP = os.environ.get('HB_GROUP') or 'hbagent'
UNIT = os.environ.get('HB_UNIT') or 'edgeshell-heartbeat'
PREFIX = 'hbj1.'
HEX = re.compile(r'^[0-9a-f]+$')
NODE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')
HOST = re.compile(r'^[A-Za-z0-9.-]{1,253}$')
PLAIN_PORT = 1883

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


def login_env(user, password):
    return f'HB_MQTT_USERNAME={user}\nHB_MQTT_PASSWORD={password}\n'


def config_yaml(t, interval):
    """config.yaml from the token, or from settings_for()/find_transport() (with prefer and ports)."""
    lines = ['# MQTT heartbeat agent settings, written by setup.py from the SenseHive join token.',
             '# Hot reloaded: save a new version and the agent applies it within one interval.',
             f"broker: {t['broker']}", f"port: {t['port']}", f"tls: {'true' if t['tls'] else 'false'}",
             f'interval: {interval}']
    if 'prefer' in t:
        ports = ', '.join(f'{k}: {v}' for k, v in t['ports'].items())
        lines += ['# the four ways in (the agent switches by itself when one stops working)',
                  'prefer: ' + (t['prefer'] or "''"), 'fallback: true',
                  f"require_tls: {'true' if t.get('require_tls') else 'false'}",
                  f"ws_path: {t.get('ws_path', '/mqtt')}", f'ports: {{{ports}}}']
    return '\n'.join(lines) + '\n'


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


def read_env_file(path):
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    return out


def share_with_agent(path):
    """root:GROUP 0640: the agent's user may read it, nobody else. Before the first
    install hbagent does not exist yet; install.sh sets it then."""
    try:
        import grp
        os.chown(path, 0, grp.getgrnam(GROUP).gr_gid)
        os.chmod(path, 0o640)
    except (KeyError, ImportError, PermissionError):
        pass


def write_private(path, text, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as f:
        f.write(text)


def valid_login(user, password):
    if not user or not password:
        return 'a broker username and password are required'
    for v in (user, password):
        if len(v) > 256 or any(c in v for c in '\r\n\0'):
            return 'the username or password has a line break or is over 256 characters'
    return None


# ---------------------------------------------------------------- the broker: four ways in

sys.path.insert(0, HERE)
import heartbeat_agent as ha  # noqa: E402  (same folder: the clone, or /opt/edgeshell-heartbeat)


def mqtt_login(host, port, tls, user, password, ws=False, ws_path='/mqtt', timeout=8):
    """One real MQTT login (the agent's own probe). (code, text): 0 accepted,
    1 to 5 refused by the broker, -1 not reachable, -2 TLS failed."""
    return ha.probe(host, port, tls, ws, ws_path, user, password, timeout)


def settings_for(t, require_tls=False, plain_port=PLAIN_PORT):
    s = dict(ha.DEFAULTS, broker=t['broker'], port=t['port'], tls=t['tls'], require_tls=require_tls and t['tls'])
    s['ports'] = dict(ha.DEFAULTS['ports'], tcp=plain_port)
    s['ports']['tcp-tls' if t['tls'] else 'tcp'] = t['port']        # the token's own port for its own way
    return s


def checkpoints(s, user, password):
    """Every way in, tried with a real login: [(name, port, code, text)], the preferred first."""
    return [(name, port, *mqtt_login(s['broker'], port, tls, user, password, ws, s['ws_path']))
            for name, port, ws, tls in ha.candidates(s)]


def print_checkpoints(rows):
    for name, port, code, text in rows:
        print(f"  {'ok  ' if code == 0 else 'FAIL'}  {name:<8} port {port:<6} {text}")


def find_transport(t, user, password, require_tls=False, plain_port=PLAIN_PORT):
    """Tries all four ways (TLS first) and keeps the first that logs in as the preferred
    one; the agent falls back to the others by itself later. Returns (settings, message)
    or raises SystemExit with a plain reason."""
    s = settings_for(t, require_tls, plain_port)
    rows = checkpoints(s, user, password)
    print('Ways to the broker (a real login on each):')
    print_checkpoints(rows)
    good = [r for r in rows if r[2] == 0]
    if good:
        name, port = good[0][0], good[0][1]
        s['prefer'] = name
        secure = ha.TRANSPORTS[name][1]
        note = '' if secure else ' (no TLS way works here: the login is sent unencrypted, the heartbeats stay encrypted)'
        return s, f"{s['broker']} over {name} on port {port}, login accepted{note}"
    refused = [r for r in rows if r[2] > 0]
    if refused:
        raise SystemExit(f'Not set up: the broker {refused[0][3]}. Check HB_MQTT_USERNAME and HB_MQTT_PASSWORD.')
    raise SystemExit('Not set up: no way to the broker works from this node' +
                     (' with TLS (--require-tls)' if s['require_tls'] else '') + ': check the network, firewall and DNS.')


# ---------------------------------------------------------------- actions

def install(t, cfg, user, password, interval=None, files_only=False):
    """t: the token (this node's identity); cfg: the broker settings find_transport() chose.
    files_only: write agent.env, mqtt.env and config.yaml into DIR and stop there (EdgeShell's
    esapp installs the code, its venv and its service)."""
    os.makedirs(DIR, mode=0o755, exist_ok=True)
    # config.yaml and the login first, so the agent's very first connection works
    tmp = os.path.join(DIR, 'config.yaml.new')
    write_private(tmp, config_yaml(cfg, interval or current_interval()), 0o600)
    share_with_agent(tmp)                                # the agent's user reads it, whatever the umask
    os.replace(tmp, os.path.join(DIR, 'config.yaml'))
    write_login(user, password)
    if files_only:
        path = os.path.join(DIR, 'agent.env')
        write_private(path + '.new', agent_env(t), 0o600)
        share_with_agent(path + '.new')
        os.replace(path + '.new', path)
        print('HEARTBEAT_FILES_OK %s' % DIR)
        return True
    with tempfile.TemporaryDirectory() as work:            # agent.env never lands anywhere but /opt
        env = os.path.join(work, 'agent.env')
        write_private(env, agent_env(t), 0o600)
        out = subprocess.run(['bash', os.path.join(HERE, 'install.sh'), env],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    print(out.stdout.rstrip())
    return out.returncode == 0 and 'HEARTBEAT_OK' in out.stdout


def write_login(user, password):
    """mqtt.env, root:hbagent 0640 (hbagent may not exist yet before the first install)."""
    path = os.path.join(DIR, 'mqtt.env')
    tmp = path + '.new'
    write_private(tmp, login_env(user, password), 0o600)
    share_with_agent(tmp)
    os.replace(tmp, path)


def current_settings():
    """The agent's own view of config.yaml (None when missing or not valid)."""
    try:
        with open(os.path.join(DIR, 'config.yaml')) as f:
            return ha.parse_settings(f.read())
    except Exception:                                  # noqa: BLE001 - missing, unreadable or not valid
        return None


def merge_config(path):
    """New fleet values (broker, port, tls, interval) into config.yaml, keeping this
    node's preferred way in and ports unless the broker itself changed."""
    try:
        with open(path) as f:
            new = ha.parse_settings(f.read())
    except Exception as e:                               # noqa: BLE001 - say why, change nothing
        print(f'Not changed: the new settings are not valid ({e})')
        return 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    old = current_settings()
    if old is None:
        print('NOT_INSTALLED')
        return 3
    merged = dict(old, broker=new['broker'], port=new['port'], tls=new['tls'], interval=new['interval'])
    if (old['broker'], old['port'], old['tls']) != (new['broker'], new['port'], new['tls']):
        merged['prefer'] = ''                            # another broker: the agent finds its way in again
        merged['ports'] = dict(merged['ports'], **{('tcp-tls' if new['tls'] else 'tcp'): new['port']})
    tmp = os.path.join(DIR, 'config.yaml.new')
    write_private(tmp, config_yaml(merged, merged['interval']), 0o600)
    ha.parse_settings(open(tmp).read())                  # never leave a file the agent would refuse
    share_with_agent(tmp)                                # the agent's user reads it, whatever the umask
    os.replace(tmp, os.path.join(DIR, 'config.yaml'))
    print(f"CONFIG_PUSHED broker {merged['broker']}:{merged['port']} tls={'on' if merged['tls'] else 'off'} "
          f"every {merged['interval']}s, way in {merged['prefer'] or 'found by the agent'}")
    return 0


def status():
    try:
        env = read_env_file(os.path.join(DIR, 'agent.env'))
    except OSError:
        print('The agent is not installed on this node.')
        return 1
    print(f"Node:  {env.get('HB_NODE', '?')}")
    if HEX.match(env.get('HB_TOPIC_KEY', '')):
        print(f"Fleet: {fleet_id(env['HB_TOPIC_KEY'])}  (SenseHive shows the same under Heartbeats, Add node)")
    if os.path.isdir('/run/systemd/system'):
        return subprocess.call(['systemctl', '--no-pager', 'status', UNIT])
    return subprocess.call(['pgrep', '-a', '-f', DIR + '/heartbeat_agent.py'])


def test():
    """Prints one line per check and HEARTBEAT_TEST ok|fail (read by tools/bulk_setup.py)."""
    results = []

    def check(ok, what):
        results.append(ok)
        print(('ok    ' if ok else 'FAIL  ') + what)

    try:
        env = read_env_file(os.path.join(DIR, 'agent.env'))
        fleet = fleet_id(env['HB_TOPIC_KEY']) if HEX.match(env.get('HB_TOPIC_KEY', '')) else '?'
        check(all(env.get(k) for k in ('HB_NODE', 'HB_KID', 'HB_NODE_KEY', 'HB_TOPIC_KEY')),
              f"agent.env for node {env.get('HB_NODE', '?')}, fleet {fleet}")
    except OSError:
        check(False, 'agent.env present (the agent is not installed)')
        print('HEARTBEAT_TEST fail')
        return 1
    if os.path.isdir('/run/systemd/system'):
        active = subprocess.call(['systemctl', 'is-active', '--quiet', UNIT]) == 0
        enabled = subprocess.call(['systemctl', 'is-enabled', '--quiet', UNIT]) == 0
        check(active and enabled, f'service {UNIT} running and enabled at boot')
    else:
        check(subprocess.call(['pgrep', '-f', DIR + '/heartbeat_agent.py'], stdout=subprocess.DEVNULL) == 0,
              'agent running (background process, no systemd)')
    st = os.stat(os.path.join(DIR, 'agent.env'))
    check(st.st_mode & 0o007 == 0, 'agent.env not readable by other users')
    try:
        login = read_env_file(os.path.join(DIR, 'mqtt.env'))
        user, password = login.get('HB_MQTT_USERNAME'), login.get('HB_MQTT_PASSWORD')
        check(bool(user and password) and os.stat(os.path.join(DIR, 'mqtt.env')).st_mode & 0o007 == 0,
              f'broker login set for user {user!r}, file private')
    except OSError:
        user = password = None
        check(False, 'broker login set (mqtt.env missing: sudo python3 setup.py --set-credentials)')
    s = current_settings()
    if s:
        rows = checkpoints(s, user, password)
        for name, port, code, text in rows:
            print(f"{'ok    ' if code == 0 else 'info  '}way {name:<8} port {port:<6} {text}")
        working = [r for r in rows if r[2] == 0]
        check(bool(working), f"broker {s['broker']}: " + (f'reached over {working[0][0]}' if working else 'no way in works'))
    else:
        check(False, 'config.yaml readable')
    ok = all(results)
    print('HEARTBEAT_TEST ' + ('ok' if ok else 'fail'))
    return 0 if ok else 1


def ask_login(args):
    """The broker login: a file, the environment, or asked (the password hidden)."""
    if args.login_file:
        env = read_env_file(args.login_file)
        user, password = env.get('HB_MQTT_USERNAME', ''), env.get('HB_MQTT_PASSWORD', '')
    else:
        user, password = os.getenv('HB_MQTT_USERNAME', ''), os.getenv('HB_MQTT_PASSWORD', '')
        if not user and sys.stdin.isatty():
            user = input('Broker username: ').strip()
        if not password and sys.stdin.isatty():
            password = getpass.getpass('Broker password (not shown): ')
    problem = valid_login(user, password)
    if problem:
        raise SystemExit(f'Not set up: {problem} (HB_MQTT_USERNAME and HB_MQTT_PASSWORD, or --login-file).')
    return user, password


def main(argv=None):
    ap = argparse.ArgumentParser(description='Set up the MQTT heartbeat agent on this node with a SenseHive join token.')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--check', metavar='TOKEN', help='only check a join token')
    g.add_argument('--status', action='store_true', help='show whether the agent runs, for which node and fleet')
    g.add_argument('--test', action='store_true', help='check the service, the broker and the login')
    g.add_argument('--set-credentials', action='store_true', help='only change the broker username and password')
    g.add_argument('--uninstall', action='store_true', help='remove the agent from this node')
    g.add_argument('--merge-config', metavar='FILE',
                   help='apply broker, port, tls and interval from FILE (EdgeShell), keeping the way in found here')
    ap.add_argument('--token-file', help='read the join token from this file, deleted after a good install')
    ap.add_argument('--login-file', help='read HB_MQTT_USERNAME and HB_MQTT_PASSWORD from this file, deleted after use')
    ap.add_argument('--require-tls', action='store_true', help='never fall back to plain MQTT')
    ap.add_argument('--plain-port', type=int, default=PLAIN_PORT, help='plain MQTT port for the fallback (1883)')
    ap.add_argument('--interval', type=int, help='seconds between heartbeats (5 to 3600; default: keep, else 10)')
    ap.add_argument('--files-only', action='store_true',
                    help='write the settings into HB_HOME and stop (EdgeShell esapp installs the code and service)')
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
    if a.test:
        return test()
    if a.uninstall:
        script = os.path.join(HERE, 'install.sh')
        if not os.path.isfile(script):
            print('Run --uninstall from the clone of the repository (it has install.sh).')
            return 1
        return subprocess.call(['bash', script, '--uninstall'])
    if a.merge_config:
        return merge_config(a.merge_config)
    if a.set_credentials:
        user, password = ask_login(a)
        s = current_settings()
        if not s:
            print('Not changed: the agent is not installed here (no config.yaml).')
            return 1
        rows = checkpoints(s, user, password)
        if not any(r[2] == 0 for r in rows):
            print_checkpoints(rows)
            refused = next((r[3] for r in rows if r[2] > 0), None)
            why = f'the broker {refused}' if refused else f"{s['broker']} cannot be reached on any way in"
            print(f'Not changed: {why}. The old login stays.')
            return 1
        write_login(user, password)
        if a.login_file:
            os.remove(a.login_file)
        print(f'HEARTBEAT_LOGIN_OK: login for {user!r} accepted and saved; the agent reconnects with it within one interval.')
        return 0

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
    user, password = ask_login(a)
    print(f"Setting up node {t['node']} (fleet {fleet_id(t['tk'])})")
    cfg, how = find_transport(t, user, password, a.require_tls, a.plain_port)
    print(f'Broker: {how}')
    if a.interval is not None and not 5 <= a.interval <= 3600:
        print('Not set up: --interval must be 5 to 3600 seconds')
        return 1
    if not install(t, cfg, user, password, a.interval, a.files_only):
        print('Not set up: see the messages above (docs/SETUP.md, Troubleshooting).')
        return 1
    for f in (a.token_file, a.login_file):
        if f:
            os.remove(f)
    print(f"Done. {t['node']} appears on SenseHive's Heartbeats page within a few seconds.")
    if not a.files_only:
        print(f'Check this node any time with: sudo python3 {DIR}/setup.py --test')
    return 0


if __name__ == '__main__':
    sys.exit(main())
