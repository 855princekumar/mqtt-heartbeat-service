#!/usr/bin/env python3
"""MQTT heartbeat agent: tells the receiver "this node is alive" every few
seconds over a public MQTT broker, independent of any VPN.

Runs as a systemd service (edgeshell-heartbeat). Three files, all under
/opt/edgeshell-heartbeat:

  agent.env    this node's identity and keys (written once per node by
               EdgeShell or setup.py; root:hbagent 0640). Never bulk-pushed.
  mqtt.env     the broker username and password (HB_MQTT_USERNAME,
               HB_MQTT_PASSWORD; root:hbagent 0640). The same for every node
               of a fleet; a change is applied with a clean reconnect.
  config.yaml  fleet-wide settings: broker, port, TLS, interval. No secrets,
               so the same file can be pushed to every node. Hot reloaded:
               the agent checks it on every beat and applies a change without
               a restart (a new broker means a clean reconnect). A broken or
               half-written file is ignored and the last good settings stay.

Each message is encrypted and signed with this node's own key and goes to a
topic that changes every ten minutes (heartbeat_protocol.py). Nothing but the
node name, a time and a counter is ever sent.
"""
import base64
import os
import signal
import socket
import ssl
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heartbeat_protocol as hp  # noqa: E402

__version__ = '1.2.0'
HOME = os.environ.get('HB_HOME', '/opt/edgeshell-heartbeat')
IDENTITY = os.path.join(HOME, 'agent.env')
SETTINGS = os.path.join(HOME, 'config.yaml')
CREDENTIALS = os.path.join(HOME, 'mqtt.env')

DEFAULTS = {'broker': 'broker.hivemq.com', 'port': 8883, 'tls': True, 'interval': 10,
            'fallback': True, 'require_tls': False, 'ws_path': '/mqtt', 'prefer': '',
            'ports': {'tcp-tls': 8883, 'ws-tls': 8884, 'tcp': 1883, 'ws': 8000}}
CONNECTION_KEYS = ('broker', 'port', 'tls', 'fallback', 'require_tls', 'ws_path', 'ports', 'prefer')

# The four ways an MQTT client reaches a broker, most secure first: (websocket, tls)
TRANSPORTS = {'tcp-tls': (False, True), 'ws-tls': (True, True), 'tcp': (False, False), 'ws': (True, False)}
ORDER = ('tcp-tls', 'ws-tls', 'tcp', 'ws')
CONNACK = {0: 'login accepted', 1: 'refused: protocol version', 2: 'refused: client id',
           3: 'refused: broker unavailable', 4: 'refused: wrong username or password', 5: 'refused: not authorised'}
PROBE_TIMEOUT = 8


def log(msg):
    print(f'heartbeat: {msg}', flush=True)


def load_identity(path=IDENTITY):
    """This node's name and keys (agent.env)."""
    cfg = {}
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
    for key in ('HB_NODE', 'HB_KID', 'HB_NODE_KEY', 'HB_TOPIC_KEY'):
        if not cfg.get(key):
            raise SystemExit(f'{key} missing in {path}')
    return {'node': cfg['HB_NODE'], 'kid': bytes.fromhex(cfg['HB_KID']),
            'node_key': bytes.fromhex(cfg['HB_NODE_KEY']), 'topic_key': bytes.fromhex(cfg['HB_TOPIC_KEY'])}


def read_env(path):
    out = {}
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    return out


class CredentialsFile:
    """mqtt.env with change detection: (username, password), or (None, None)
    when there is none (a broker without logins)."""

    def __init__(self, path=CREDENTIALS):
        self.path, self.stamp, self.current = path, False, (None, None)

    def check(self):
        """True when the credentials changed since the last check."""
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            stamp = None
        if stamp == self.stamp:
            return False
        self.stamp = stamp
        new = (None, None)
        if stamp is not None:
            try:
                env = read_env(self.path)
                new = (env.get('HB_MQTT_USERNAME') or None, env.get('HB_MQTT_PASSWORD') or None)
            except OSError as e:
                log(f'mqtt.env not readable ({type(e).__name__}); keeping the current login')
                return False
        changed, self.current = new != self.current, new
        return changed


# ---------------------------------------------------------------- reaching the broker

def tls_context():
    """The system CA store, the certificate and host name verified, TLS 1.2 or newer."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def candidates(s):
    """(name, port, websocket, tls) to try, in order: `prefer` (the way setup.py found
    working), else the configured one (port and tls), then, with fallback on, the
    others most secure first; with require_tls only TLS ones."""
    base = 'tcp-tls' if s['tls'] else 'tcp'
    port = lambda name: s['port'] if name == base else s['ports'][name]   # noqa: E731
    first = s.get('prefer') or base
    names = [first] + ([n for n in ORDER if n != first] if s['fallback'] else [])
    if first != base and s['fallback'] and base in names:
        names.remove(base)
        names.insert(1, base)                       # the configured way right after the preferred one
    return [(n, port(n), *TRANSPORTS[n]) for n in names if TRANSPORTS[n][1] or not s['require_tls']]


def _str(text):
    b = text.encode('utf-8')
    return len(b).to_bytes(2, 'big') + b


def _remaining(n):
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ws_open(sock, host, path):
    """WebSocket handshake for MQTT (RFC 6455, subprotocol "mqtt"). Returns bytes read past it."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (f'GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: mqtt\r\n\r\n')
    sock.sendall(request.encode())
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(1024)
        if not chunk or len(data) > 8192:
            raise OSError('closed during the WebSocket handshake')
        data += chunk
    head, _, rest = data.partition(b'\r\n\r\n')
    if not head.startswith(b'HTTP/1.1 101'):
        raise OSError('no WebSocket here (' + head.split(b'\r\n', 1)[0].decode(errors='replace')[:60] + ')')
    return rest


def _ws_frame(data):
    """One masked binary frame (clients must mask)."""
    mask = os.urandom(4)
    n = len(data)
    head = bytes([0x82]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, 'big'))
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _ws_payload(sock, buf):
    """The payload of the first frame from the server (not masked)."""
    def need(count):
        nonlocal buf
        while len(buf) < count:
            chunk = sock.recv(1024)
            if not chunk:
                raise OSError('closed')
            buf += chunk
    need(2)
    n, at = buf[1] & 0x7F, 2
    if n == 126:
        need(4)
        n, at = int.from_bytes(buf[2:4], 'big'), 4
    need(at + n)
    return buf[at:at + n]


def probe(host, port, tls, ws=False, ws_path='/mqtt', user=None, password=None, timeout=PROBE_TIMEOUT):
    """One real MQTT 3.1.1 login (CONNECT, CONNACK, DISCONNECT) over TCP or WebSocket,
    with or without TLS. Returns (code, text): 0 accepted, 1 to 5 refused by the broker,
    -1 not reachable, -2 TLS failed. Never raises, never waits longer than `timeout`."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return -1, f'not reachable ({type(e).__name__})'
    try:
        sock.settimeout(timeout)
        if tls:
            try:
                sock = tls_context().wrap_socket(sock, server_hostname=host)
            except (ssl.SSLError, OSError) as e:
                return -2, f'TLS failed ({type(e).__name__})'
        rest = _ws_open(sock, host, ws_path) if ws else b''
        flags = 0x02 | (0x80 if user else 0) | (0x40 if password else 0)
        body = _str('MQTT') + bytes([4, flags]) + (30).to_bytes(2, 'big') + _str('hb-probe-' + os.urandom(4).hex())
        if user:
            body += _str(user)
        if password:
            body += _str(password)
        packet = bytes([0x10]) + _remaining(len(body)) + body
        if ws:
            sock.sendall(_ws_frame(packet))
            reply = _ws_payload(sock, rest)
        else:
            sock.sendall(packet)
            reply = sock.recv(4)
        if len(reply) < 4 or reply[0] != 0x20:
            return -1, 'did not answer as an MQTT broker'
        code = reply[3]
        if code == 0:
            sock.sendall(_ws_frame(b'\xe0\x00') if ws else b'\xe0\x00')
        return code, CONNACK.get(code, f'refused (code {code})')
    except (OSError, ValueError) as e:
        return -1, f'{type(e).__name__}: {e}'[:120]
    finally:
        try:
            sock.close()
        except OSError:
            pass


def parse_settings(text):
    """Validate config.yaml. Returns a full settings dict or raises ValueError;
    a file that does not validate is never applied."""
    import yaml
    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('the file must be a mapping of settings')
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValueError(f'unknown settings: {", ".join(sorted(unknown))}')
    out = dict(DEFAULTS, **data)
    host = out['broker']
    if not isinstance(host, str) or not host or len(host) > 253 or any(c in host for c in ' /\\@:'):
        raise ValueError('broker must be a host name')
    if not isinstance(out['port'], int) or isinstance(out['port'], bool) or not 1 <= out['port'] <= 65535:
        raise ValueError('port must be 1 to 65535')
    if not isinstance(out['tls'], bool):
        raise ValueError('tls must be true or false')
    if not isinstance(out['interval'], int) or isinstance(out['interval'], bool) or not 5 <= out['interval'] <= 3600:
        raise ValueError('interval must be 5 to 3600 seconds')
    for key in ('fallback', 'require_tls'):
        if not isinstance(out[key], bool):
            raise ValueError(f'{key} must be true or false')
    path = out['ws_path']
    if not isinstance(path, str) or not path.startswith('/') or any(c in path for c in ' \r\n'):
        raise ValueError('ws_path must start with /')
    given = out['ports'] if out['ports'] is not None else {}
    if not isinstance(given, dict) or not set(given) <= set(ORDER):
        raise ValueError('ports may set tcp-tls, ws-tls, tcp and ws')
    ports = dict(DEFAULTS['ports'], **given)
    if not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535 for v in ports.values()):
        raise ValueError('ports must be 1 to 65535')
    out['ports'] = ports
    if out['require_tls'] and not out['tls']:
        raise ValueError('require_tls needs tls: true')
    if out['prefer'] is None:                        # "prefer:" with nothing after it
        out['prefer'] = ''
    if out['prefer'] not in ('',) + ORDER:
        raise ValueError('prefer must be empty or one of tcp-tls, ws-tls, tcp, ws')
    return out


class SettingsFile:
    """config.yaml with change detection. Missing file: defaults."""

    def __init__(self, path=SETTINGS):
        self.path, self.stamp, self.current = path, None, dict(DEFAULTS)

    def _stamp(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return None

    def check(self):
        """Returns the new settings when the file changed and is valid, else None."""
        stamp = self._stamp()
        if stamp == self.stamp:
            return None
        self.stamp = stamp
        if stamp is None:
            new = dict(DEFAULTS)
        else:
            try:
                with open(self.path, encoding='utf-8') as fh:
                    new = parse_settings(fh.read(64 * 1024))
            except Exception as e:                       # broken or half-written: keep what works
                log(f'config.yaml not applied ({e}); keeping the current settings')
                return None
        if new == self.current:
            return None
        self.current = new
        return new


def new_client(client_id, transport='tcp'):
    import paho.mqtt.client as mqtt
    if hasattr(mqtt, 'CallbackAPIVersion'):                       # paho-mqtt 2.x
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, transport=transport)
    return mqtt.Client(client_id=client_id, transport=transport)  # paho-mqtt 1.x


class Unreachable(OSError):
    """No way to the broker worked this time (run() retries with a backoff)."""


class Agent:
    STUCK_BEATS = 3          # beats without a live connection before looking for another way in

    def __init__(self, identity, settings_file=None, client_factory=new_client, clock=time.time, credentials=None,
                 prober=probe):
        self.id, self.factory, self.clock, self.prober = identity, client_factory, clock, prober
        self.settings_file = settings_file or SettingsFile()
        self.settings_file.check()
        self.credentials = credentials or CredentialsFile()
        self.credentials.check()
        self.refused_logged = False
        self.settings = dict(self.settings_file.current)
        self.client, self.win, self.session, self.seq, self.topic = None, None, None, 0, None
        self.transport, self.down_beats = None, 0
        self.running = True

    def _msg(self, kind, tpc, t=None):
        self.seq += 1
        body = {'n': self.id['node'], 'k': kind, 't': round(t if t is not None else self.clock(), 3),
                's': self.session, 'q': self.seq, 'v': __version__}
        return hp.seal(self.id['node_key'], self.id['kid'], tpc, body)

    def find_way(self):
        """The first of the four ways in (configured first, then the fallbacks) where a real
        login works. Refused logins stop the search: every way uses the same login."""
        s, (user, password) = self.settings, self.credentials.current
        tried = []
        for name, port, ws, tls in candidates(s):
            code, text = self.prober(s['broker'], port, tls, ws, s['ws_path'], user, password)
            if code == 0:
                if tried:
                    log(f"broker reached over {name} (port {port}) after: {'; '.join(tried)}")
                return name, port, ws, tls
            if code > 0:
                if not self.refused_logged:
                    self.refused_logged = True
                    log(f'broker {text} over {name}: check the username and password in mqtt.env')
                raise Unreachable(text)
            tried.append(f'{name} {port}: {text}')
        raise Unreachable('; '.join(tried))

    def connect(self):
        """(Re)connect for the current topic window, with a signed last will, over the
        best way that works now (a new window always tries the preferred way first)."""
        self.disconnect()
        s = self.settings
        name, port, ws, tls = self.find_way()
        self.win = hp.window(self.clock())
        tpc = hp.topic(self.id['topic_key'], self.win)
        self.session = round(self.clock(), 3)
        cid = 'hb-' + os.urandom(6).hex()                   # random id: the broker never learns the node name
        c = self.factory(cid, transport='websockets') if ws else self.factory(cid)
        if ws:
            c.ws_set_options(path=s['ws_path'])
        if tls:
            c.tls_set_context(tls_context())                # system CA store, certificate verified, TLS 1.2+
        user, password = self.credentials.current
        if user:
            c.username_pw_set(user, password)
        c.will_set(tpc, self._msg('lwt', tpc, t=self.session), qos=1, retain=False)
        c.reconnect_delay_set(min_delay=2, max_delay=60)

        def on_connect(client, userdata, flags, rc, *rest):          # paho 1.x and 2.x signatures
            # Announce only once the broker accepted the connection (also after
            # an automatic reconnect), or the message would be dropped.
            code = getattr(rc, 'value', rc) or 0
            if code == 0:
                self.refused_logged = False
                client.publish(tpc, self._msg('birth', tpc), qos=1, retain=False)
            elif not self.refused_logged:                            # once, not on every retry
                self.refused_logged = True
                log(f'broker refused the connection (code {code}): check the username and password in mqtt.env')
        c.on_connect = on_connect
        c.connect(s['broker'], port, keepalive=max(15, s['interval'] * 2))
        c.loop_start()
        if name != self.transport:
            log(f'connected over {name} ({s["broker"]}:{port})')
        self.client, self.topic, self.transport, self.down_beats = c, tpc, name, 0

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()                    # a clean disconnect: no last will
                self.client.loop_stop()
            except Exception:
                pass
            self.client = None

    def reload(self):
        """Apply a changed config.yaml or mqtt.env without stopping."""
        if self.credentials.check():
            log('broker login changed: reconnecting')
            self.refused_logged = False
            self.disconnect()
        new = self.settings_file.check()
        if not new:
            return
        reconnect = any(new[k] != self.settings[k] for k in CONNECTION_KEYS)
        log('settings reloaded: ' + ', '.join(f'{k}={new[k]}' for k in sorted(new) if new[k] != self.settings[k]))
        self.settings = new
        if reconnect:
            self.disconnect()                               # the next beat connects to the new broker

    def beat(self):
        self.reload()
        if self.client is None or hp.window(self.clock()) != self.win:
            self.connect()                                  # new window (or broker): new topic and last will
            return
        connected = getattr(self.client, 'is_connected', lambda: True)()
        self.down_beats = 0 if connected else self.down_beats + 1
        if self.down_beats >= self.STUCK_BEATS:             # paho keeps retrying the same way: find another
            log(f'no connection for {self.down_beats} beats over {self.transport}; looking for another way in')
            self.connect()
            return
        self.client.publish(self.topic, self._msg('hb', self.topic), qos=0, retain=False)

    def run(self):
        backoff = 2
        while self.running:
            try:
                self.beat()
                backoff = 2
                time.sleep(self.settings['interval'])
            except Exception as e:                          # never die: log, wait, try again
                log(f'broker not reachable ({type(e).__name__}: {str(e)[:200]}); retry in {backoff}s')
                self.disconnect()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.disconnect()


def main():
    agent = Agent(load_identity())
    # systemd stop: finish the current beat, then disconnect cleanly (no last will).
    signal.signal(signal.SIGTERM, lambda *_: setattr(agent, 'running', False))
    s = agent.settings
    log(f"agent {__version__} for {agent.id['node']}: {s['broker']}:{s['port']} "
        f"tls={'on' if s['tls'] else 'off'} login={'yes' if agent.credentials.current[0] else 'no'} "
        f"every {s['interval']}s (config: {SETTINGS})")
    agent.run()


if __name__ == '__main__':
    main()
