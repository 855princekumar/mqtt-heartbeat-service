#!/usr/bin/env python3
"""MQTT heartbeat agent: tells the receiver "this node is alive" every few
seconds over a public MQTT broker, independent of any VPN.

Runs as a systemd service (edgeshell-heartbeat). Two files, both under
/opt/edgeshell-heartbeat:

  agent.env    this node's identity and keys (written once per node by
               EdgeShell; root:hbagent 0640). Never bulk-pushed.
  config.yaml  fleet-wide settings: broker, port, TLS, interval. No secrets,
               so the same file can be pushed to every node. Hot reloaded:
               the agent checks it on every beat and applies a change without
               a restart (a new broker means a clean reconnect). A broken or
               half-written file is ignored and the last good settings stay.

Each message is encrypted and signed with this node's own key and goes to a
topic that changes every ten minutes (heartbeat_protocol.py). Nothing but the
node name, a time and a counter is ever sent.
"""
import os
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heartbeat_protocol as hp  # noqa: E402

__version__ = '1.1.0'
HOME = os.environ.get('HB_HOME', '/opt/edgeshell-heartbeat')
IDENTITY = os.path.join(HOME, 'agent.env')
SETTINGS = os.path.join(HOME, 'config.yaml')

DEFAULTS = {'broker': 'broker.hivemq.com', 'port': 8883, 'tls': True, 'interval': 10}
CONNECTION_KEYS = ('broker', 'port', 'tls')


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


def new_client(client_id):
    import paho.mqtt.client as mqtt
    if hasattr(mqtt, 'CallbackAPIVersion'):                       # paho-mqtt 2.x
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    return mqtt.Client(client_id=client_id)                        # paho-mqtt 1.x


class Agent:
    def __init__(self, identity, settings_file=None, client_factory=new_client, clock=time.time):
        self.id, self.factory, self.clock = identity, client_factory, clock
        self.settings_file = settings_file or SettingsFile()
        self.settings_file.check()
        self.settings = dict(self.settings_file.current)
        self.client, self.win, self.session, self.seq, self.topic = None, None, None, 0, None
        self.running = True

    def _msg(self, kind, tpc, t=None):
        self.seq += 1
        body = {'n': self.id['node'], 'k': kind, 't': round(t if t is not None else self.clock(), 3),
                's': self.session, 'q': self.seq, 'v': __version__}
        return hp.seal(self.id['node_key'], self.id['kid'], tpc, body)

    def connect(self):
        """(Re)connect for the current topic window, with a signed last will."""
        self.disconnect()
        s = self.settings
        self.win = hp.window(self.clock())
        tpc = hp.topic(self.id['topic_key'], self.win)
        self.session = round(self.clock(), 3)
        c = self.factory('hb-' + os.urandom(6).hex())      # random id: the broker never learns the node name
        if s['tls']:
            c.tls_set()                                     # system CA store, certificate verified
        c.will_set(tpc, self._msg('lwt', tpc, t=self.session), qos=1, retain=False)
        c.reconnect_delay_set(min_delay=2, max_delay=60)

        def on_connect(client, userdata, flags, rc, *rest):          # paho 1.x and 2.x signatures
            # Announce only once the broker accepted the connection (also after
            # an automatic reconnect), or the message would be dropped.
            if (getattr(rc, 'value', rc) or 0) == 0:
                client.publish(tpc, self._msg('birth', tpc), qos=1, retain=False)
        c.on_connect = on_connect
        c.connect(s['broker'], s['port'], keepalive=max(15, s['interval'] * 2))
        c.loop_start()
        self.client, self.topic = c, tpc

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()                    # a clean disconnect: no last will
                self.client.loop_stop()
            except Exception:
                pass
            self.client = None

    def reload(self):
        """Apply a changed config.yaml without stopping."""
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
        self.client.publish(self.topic, self._msg('hb', self.topic), qos=0, retain=False)

    def run(self):
        backoff = 2
        while self.running:
            try:
                self.beat()
                backoff = 2
                time.sleep(self.settings['interval'])
            except (OSError, socket.error) as e:
                log(f'broker not reachable ({type(e).__name__}); retry in {backoff}s')
                self.client = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.disconnect()


def main():
    agent = Agent(load_identity())
    # systemd stop: finish the current beat, then disconnect cleanly (no last will).
    signal.signal(signal.SIGTERM, lambda *_: setattr(agent, 'running', False))
    s = agent.settings
    log(f"agent {__version__} for {agent.id['node']}: {s['broker']}:{s['port']} "
        f"tls={'on' if s['tls'] else 'off'} every {s['interval']}s (config: {SETTINGS})")
    agent.run()


if __name__ == '__main__':
    main()
