"""The agent against a fake MQTT client: what it publishes, where, that a
receiver holding the master key accepts every message, and that config.yaml
is hot reloaded safely."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import heartbeat_agent as ha  # noqa: E402
import heartbeat_protocol as hp  # noqa: E402

MASTER = os.urandom(32)
NL = '\n'


class FakeClient:
    instances = []

    def __init__(self, client_id):
        self.client_id, self.published, self.will, self.tls, self.disconnected = client_id, [], None, False, False
        FakeClient.instances.append(self)

    def tls_set_context(self, ctx):
        import ssl
        assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2 and ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname                               # never an unverified or old TLS
        self.tls = True

    def will_set(self, topic, payload, qos, retain):
        assert retain is False
        self.will = (topic, payload, qos)

    def reconnect_delay_set(self, **kw):
        pass

    def connect(self, host, port, keepalive):
        self.host, self.port = host, port

    def loop_start(self):
        on_connect = getattr(self, 'on_connect', None)            # the broker accepted the connection
        if on_connect:
            on_connect(self, None, {}, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos, retain):
        assert retain is False                                    # nothing lingers on a public broker
        self.published.append((topic, payload))


def reachable(*a, **k):
    return 0, 'login accepted'


def make(clock, settings_path=None, tmp=None):
    p = hp.provision(MASTER, 'pi-07')
    identity = {'node': p['HB_NODE'], 'kid': bytes.fromhex(p['HB_KID']),
                'node_key': bytes.fromhex(p['HB_NODE_KEY']), 'topic_key': bytes.fromhex(p['HB_TOPIC_KEY'])}
    FakeClient.instances.clear()
    sf = ha.SettingsFile(settings_path or os.path.join(tmp or '.', 'absent-config.yaml'))
    return ha.Agent(identity, settings_file=sf, client_factory=FakeClient, clock=clock, prober=reachable)


def write(path, lines):
    """Like a bulk push: a new file replaces the old one."""
    tmp = str(path) + '.new'
    with open(tmp, 'w') as fh:
        fh.write(NL.join(lines) + NL)
    os.replace(tmp, path)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # coarse clocks: force a new stamp


def test_birth_heartbeats_and_will_are_authentic_and_private():
    now = [1_800_000_000.0]
    agent = make(lambda: now[0])
    tr = hp.Tracker()
    for _ in range(4):
        agent.beat()
        now[0] += 10
    c = FakeClient.instances[0]
    assert c.tls and c.port == 8883 and c.host == 'broker.hivemq.com' and 'pi-07' not in c.client_id
    kinds = []
    for tpc, raw in c.published:
        body = hp.open_message(MASTER, tpc, raw)
        assert set(body) == {'n', 'k', 't', 's', 'q', 'v'}         # nothing else is ever sent
        assert tr.accept(body, now=body['t'])
        kinds.append(body['k'])
    assert kinds == ['birth', 'hb', 'hb', 'hb']
    will = hp.open_message(MASTER, c.will[0], c.will[1])
    assert will['k'] == 'lwt' and will['s'] == will['t'] and c.will[2] == 1


def test_a_new_topic_window_reconnects_cleanly_with_a_new_will():
    now = [hp.WINDOW * 3_000_000 - 5.0]
    agent = make(lambda: now[0])
    agent.beat()
    first = FakeClient.instances[0]
    now[0] += 10                                                   # crosses into the next window
    agent.beat()
    assert first.disconnected                                      # clean: the old will is not published
    second = FakeClient.instances[1]
    assert second.will[0] != first.will[0]
    assert hp.open_message(MASTER, second.will[0], second.will[1])['k'] == 'lwt'


def test_load_identity_requires_every_key(tmp_path):
    p = tmp_path / 'agent.env'
    p.write_text('HB_NODE=pi-01' + NL + 'HB_KID=00' + NL)
    with pytest.raises(SystemExit) as e:
        ha.load_identity(str(p))
    assert 'HB_NODE_KEY' in str(e.value)


def test_a_new_broker_in_config_yaml_is_applied_without_a_restart(tmp_path):
    cfg = tmp_path / 'config.yaml'
    write(cfg, ['broker: broker.hivemq.com', 'port: 8883', 'tls: true', 'interval: 10'])
    now = [1_800_000_000.0]
    agent = make(lambda: now[0], settings_path=str(cfg))
    agent.beat()
    agent.beat()
    first = FakeClient.instances[0]
    write(cfg, ['broker: mqtt.example.org', 'port: 8884', 'tls: true', 'interval: 10'])
    now[0] += 10
    agent.beat()
    second = FakeClient.instances[-1]
    assert first.disconnected and second is not first              # clean switch, no last will
    assert (second.host, second.port) == ('mqtt.example.org', 8884)
    assert hp.open_message(MASTER, *second.published[0])['k'] == 'birth'


def test_an_interval_change_applies_without_reconnecting(tmp_path):
    cfg = tmp_path / 'config.yaml'
    write(cfg, ['interval: 10'])
    agent = make(lambda: 1_800_000_000.0, settings_path=str(cfg))
    agent.beat()
    write(cfg, ['interval: 30'])
    agent.beat()
    assert agent.settings['interval'] == 30 and len(FakeClient.instances) == 1


@pytest.mark.parametrize('bad', [
    'broker: [unclosed', 'port: 99999', 'port: "8883"', 'tls: maybe', 'interval: 1',
    'broker: evil.example/topic', 'broker: user@host', 'unknown_key: 1', '- a list', 'interval: true'])
def test_a_broken_config_yaml_never_stops_the_heartbeat(tmp_path, bad):
    cfg = tmp_path / 'config.yaml'
    write(cfg, ['broker: broker.hivemq.com', 'interval: 10'])
    now = [1_800_000_000.0]
    agent = make(lambda: now[0], settings_path=str(cfg))
    agent.beat()
    write(cfg, [bad])
    now[0] += 10
    agent.beat()
    c = FakeClient.instances[-1]
    assert len(FakeClient.instances) == 1 and agent.settings['broker'] == 'broker.hivemq.com'
    assert hp.open_message(MASTER, *c.published[-1])['k'] == 'hb'  # still beating


def test_a_missing_config_yaml_means_the_safe_defaults(tmp_path):
    agent = make(lambda: 1_800_000_000.0, tmp=str(tmp_path))
    assert agent.settings == ha.DEFAULTS and agent.settings['tls'] is True


def test_the_broker_login_is_used_and_a_new_one_reconnects(tmp_path):
    login = tmp_path / 'mqtt.env'
    write(login, ['HB_MQTT_USERNAME=admin', 'HB_MQTT_PASSWORD=admin123'])
    seen = []

    class Client(FakeClient):
        def username_pw_set(self, user, password):
            seen.append((user, password))

    p = hp.provision(MASTER, 'pi-07')
    identity = {'node': p['HB_NODE'], 'kid': bytes.fromhex(p['HB_KID']),
                'node_key': bytes.fromhex(p['HB_NODE_KEY']), 'topic_key': bytes.fromhex(p['HB_TOPIC_KEY'])}
    agent = ha.Agent(identity, settings_file=ha.SettingsFile(str(tmp_path / 'none.yaml')), client_factory=Client,
                     credentials=ha.CredentialsFile(str(login)), prober=reachable)
    agent.beat()
    assert seen == [('admin', 'admin123')]
    write(login, ['HB_MQTT_USERNAME=admin', 'HB_MQTT_PASSWORD=rotated-1'])
    agent.beat()                                   # notices the change: clean disconnect
    agent.beat()                                   # and connects with the new login
    assert seen[-1] == ('admin', 'rotated-1')


def test_no_login_file_means_no_login():
    creds = ha.CredentialsFile('/nonexistent/mqtt.env')
    creds.check()
    assert creds.current == (None, None)


# ---------------------------------------------------------------- four ways to the broker

def identity():
    p = hp.provision(MASTER, 'pi-07')
    return {'node': p['HB_NODE'], 'kid': bytes.fromhex(p['HB_KID']),
            'node_key': bytes.fromhex(p['HB_NODE_KEY']), 'topic_key': bytes.fromhex(p['HB_TOPIC_KEY'])}


class WsClient(FakeClient):
    def __init__(self, client_id, transport='tcp'):
        super().__init__(client_id)
        self.transport, self.ws_path, self.connected = transport, None, True

    def ws_set_options(self, path):
        self.ws_path = path

    def is_connected(self):
        return self.connected


def test_the_four_ways_secure_first_and_require_tls_keeps_only_tls():
    s = dict(ha.DEFAULTS)
    assert [c[0] for c in ha.candidates(s)] == ['tcp-tls', 'ws-tls', 'tcp', 'ws']
    assert [c[0] for c in ha.candidates(dict(s, require_tls=True))] == ['tcp-tls', 'ws-tls']
    assert [c[0] for c in ha.candidates(dict(s, fallback=False))] == ['tcp-tls']
    plain = dict(s, tls=False, port=1883)
    assert [c[0] for c in ha.candidates(plain)] == ['tcp', 'tcp-tls', 'ws-tls', 'ws']


@pytest.mark.parametrize('text, err', [
    ('ports: {tcp: 0}', 'ports must be'), ('ports: {quic: 1}', 'ports may set'), ('ws_path: mqtt', 'ws_path'),
    ('fallback: maybe', 'fallback'), ('tls: false' + NL + 'require_tls: true', 'require_tls needs tls'),
])
def test_bad_transport_settings_are_refused(text, err):
    with pytest.raises(ValueError, match=err):
        ha.parse_settings(text)


def test_tls_blocked_falls_back_to_websocket_tls_then_returns_to_tls_next_window(tmp_path):
    now = [1_800_000_000.0]
    blocked = {'tcp-tls'}
    seen = []

    def prober(host, port, tls, ws, path, user, password):
        name = ('ws-' if ws else 'tcp') + ('tls' if tls else '')
        name = {'ws-tls': 'ws-tls', 'tcptls': 'tcp-tls', 'tcp': 'tcp', 'ws-': 'ws'}[name]
        seen.append(name)
        return (-2, 'TLS failed') if name in blocked else (0, 'login accepted')

    agent = ha.Agent(identity(), settings_file=ha.SettingsFile(str(tmp_path / 'none.yaml')), client_factory=WsClient,
                     clock=lambda: now[0], prober=prober)
    agent.beat()
    c = FakeClient.instances[-1]
    assert agent.transport == 'ws-tls' and c.transport == 'websockets' and c.ws_path == '/mqtt' and c.tls
    assert c.port == 8884 and seen == ['tcp-tls', 'ws-tls']
    blocked.clear()
    now[0] += 600                                   # a new topic window: the preferred way is tried first again
    agent.beat()
    assert agent.transport == 'tcp-tls' and FakeClient.instances[-1].port == 8883


def test_a_refused_login_stops_the_search_and_nothing_connects(tmp_path):
    calls = []

    def prober(*a):
        calls.append(a)
        return 5, 'refused: not authorised'
    agent = ha.Agent(identity(), settings_file=ha.SettingsFile(str(tmp_path / 'none.yaml')), client_factory=WsClient,
                     prober=prober)
    with pytest.raises(ha.Unreachable):
        agent.beat()
    assert len(calls) == 1 and agent.client is None


def test_nothing_reachable_raises_and_run_keeps_going(tmp_path, monkeypatch):
    agent = ha.Agent(identity(), settings_file=ha.SettingsFile(str(tmp_path / 'none.yaml')), client_factory=WsClient,
                     prober=lambda *a: (-1, 'not reachable (ConnectionRefusedError)'))
    with pytest.raises(ha.Unreachable, match='tcp-tls 8883.*ws-tls 8884.*tcp 1883.*ws 8000'):
        agent.connect()
    sleeps = []

    def sleep(n):
        sleeps.append(n)
        if len(sleeps) == 3:
            agent.running = False
    monkeypatch.setattr(ha.time, 'sleep', sleep)
    agent.run()                                      # no exception escapes; it backs off and retries
    assert sleeps == [2, 4, 8]


def test_a_connection_stuck_in_retries_finds_another_way(tmp_path):
    ways = iter([(0, 'ok')] + [(-1, 'down')] + [(0, 'ok')] * 5)

    def prober(*a):
        return next(ways)
    agent = ha.Agent(identity(), settings_file=ha.SettingsFile(str(tmp_path / 'none.yaml')), client_factory=WsClient,
                     clock=lambda: 1_800_000_000.0, prober=prober)
    agent.beat()
    assert agent.transport == 'tcp-tls'
    FakeClient.instances[-1].connected = False          # paho is stuck reconnecting the same way
    for _ in range(ha.Agent.STUCK_BEATS):
        agent.beat()
    assert agent.transport == 'ws-tls'                  # tcp-tls failed the new probe: the next way works
