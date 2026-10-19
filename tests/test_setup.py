"""setup.py: reading a SenseHive join token safely, and what it writes."""
import base64
import hashlib
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('agent_setup', os.path.join(HERE, 'setup.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

import heartbeat_protocol as hp  # noqa: E402

MASTER = os.urandom(32)


def token(node='pi-node-01', broker='broker.hivemq.com', port=8883, tls=True, **change):
    """A token as SenseHive makes it (sense-hive/version-1.0/heartbeat.py join_token)."""
    p = hp.provision(MASTER, node)
    body = {'v': 1, 'node': node, 'kid': p['HB_KID'], 'nk': p['HB_NODE_KEY'], 'tk': p['HB_TOPIC_KEY'],
            'broker': broker, 'port': port, 'tls': tls}
    body.update(change)
    body['c'] = hashlib.sha256(json.dumps(body, separators=(',', ':'), sort_keys=True).encode()).hexdigest()[:8]
    return 'hbj1.' + base64.urlsafe_b64encode(json.dumps(body, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')


def test_a_token_gives_exactly_the_nodes_agent_env_and_never_the_master_key():
    t = setup.read_token(token())
    env = dict(line.split('=', 1) for line in setup.agent_env(t).splitlines())
    assert env == hp.provision(MASTER, 'pi-node-01')
    assert MASTER.hex() not in setup.agent_env(t) + setup.config_yaml(t, 10)


def test_a_pasted_token_may_wrap_or_carry_spaces():
    raw = token()
    assert setup.read_token('  ' + raw[:30] + '\n' + raw[30:60] + ' ' + raw[60:] + '\n')['node'] == 'pi-node-01'


@pytest.mark.parametrize('bad, reason', [
    ('', 'not a SenseHive join token'),
    ('hello', 'not a SenseHive join token'),
    ('hbj1.!!!', 'cut or changed'),
    ('hbj1.' + base64.urlsafe_b64encode(b'[1]').decode(), 'newer agent'),
])
def test_garbage_is_refused_with_a_plain_reason(bad, reason):
    with pytest.raises(setup.BadToken, match=reason):
        setup.read_token(bad)


def test_a_cut_or_edited_token_is_refused():
    raw = token()
    with pytest.raises(setup.BadToken, match='cut or changed'):
        setup.read_token(raw[:-4])
    body = json.loads(base64.urlsafe_b64decode(raw[5:] + '=' * (-len(raw[5:]) % 4)))
    body['broker'] = 'evil.example.com'                     # edited without fixing the check
    edited = 'hbj1.' + base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
    with pytest.raises(setup.BadToken, match='cut or changed'):
        setup.read_token(edited)


@pytest.mark.parametrize('change, reason', [
    ({'v': 2}, 'newer agent'),
    ({'node': 'a b'}, 'node name'),
    ({'node': '-x'}, 'node name'),
    ({'kid': 'zz' * 8}, 'keys'),
    ({'nk': 'ab'}, 'keys'),
    ({'broker': 'a b'}, 'broker'),
    ({'broker': 'x;rm -rf /'}, 'broker'),
    ({'port': 0}, 'broker'),
    ({'port': True}, 'broker'),
    ({'tls': 'yes'}, 'broker'),
])
def test_a_token_with_bad_contents_is_refused(change, reason):
    with pytest.raises(setup.BadToken, match=reason):
        setup.read_token(token(**change))


def test_config_yaml_points_the_agent_at_sensehives_broker():
    t = setup.read_token(token(broker='10.0.0.5', port=1883, tls=False))
    text = setup.config_yaml(t, 7)
    assert 'broker: 10.0.0.5\n' in text and 'port: 1883\n' in text and 'tls: false\n' in text and 'interval: 7\n' in text
    import heartbeat_agent as ha
    parsed = ha.parse_settings(text)
    assert (parsed['broker'], parsed['port'], parsed['tls'], parsed['interval']) == ('10.0.0.5', 1883, False, 7)


def test_the_fleet_id_matches_sensehive():
    # sense-hive/version-1.0/heartbeat.py fleet_id: sha256("fleet id|" + topic key)[:12]
    tk = hp.topic_key(MASTER)
    assert setup.fleet_id(tk.hex()) == hashlib.sha256(b'fleet id|' + tk).hexdigest()[:12]


def test_check_only_changes_nothing(capsys):
    assert setup.main(['--check', token()]) == 0
    assert 'Valid: node pi-node-01' in capsys.readouterr().out
    assert setup.main(['--check', 'nope']) == 1


@pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() == 0, reason='checks the refusal as a normal user')
def test_installing_needs_root(capsys):
    assert setup.main([]) == 1
    assert 'sudo' in capsys.readouterr().out


# ---------------------------------------------------------------- broker login and TLS fallback

class FakeBroker:
    """Answers one MQTT CONNECT per connection: 0 for the right login, 4 otherwise."""

    def __init__(self, user='admin', password='admin123'):
        import socket
        import threading
        self.user, self.password = user, password
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(1024)
                fields, i = [], 12                         # after fixed header (2), protocol name (6), level, flags, keepalive
                while i + 2 <= len(data):
                    n = int.from_bytes(data[i:i + 2], 'big')
                    fields.append(data[i + 2:i + 2 + n].decode())
                    i += 2 + n
                ok = fields[1:3] == [self.user, self.password]
                conn.sendall(bytes([0x20, 2, 0, 0 if ok else 4]))

    def close(self):
        self.sock.close()


def closed_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_login_is_checked_against_a_real_mqtt_handshake():
    b = FakeBroker()
    try:
        assert setup.mqtt_login('127.0.0.1', b.port, False, 'admin', 'admin123')[0] == 0
        code, text = setup.mqtt_login('127.0.0.1', b.port, False, 'admin', 'wrong')
        assert code == 4 and 'wrong username or password' in text
        assert setup.mqtt_login('127.0.0.1', closed_port(), False, 'a', 'b')[0] == -1
    finally:
        b.close()


def test_tls_first_then_plain_only_when_tls_cannot_be_reached():
    b = FakeBroker()
    try:
        t = setup.read_token(token(broker='127.0.0.1', port=closed_port(), tls=True))
        settings, how = setup.find_transport(t, 'admin', 'admin123', plain_port=b.port)
        assert settings['prefer'] == 'tcp' and settings['ports']['tcp'] == b.port and 'over tcp' in how
        assert 'login is sent unencrypted' in how                                  # said plainly
        with pytest.raises(SystemExit, match='require-tls'):                      # never with --require-tls
            setup.find_transport(t, 'admin', 'admin123', require_tls=True, plain_port=b.port)
        plain = setup.read_token(token(broker='127.0.0.1', port=b.port, tls=False))
        settings, how = setup.find_transport(plain, 'admin', 'admin123')
        assert settings['prefer'] == 'tcp' and settings['port'] == b.port and 'login accepted' in how
        import heartbeat_agent as ha
        parsed = ha.parse_settings(setup.config_yaml(settings, 10))            # the agent reads what setup.py writes
        assert parsed['prefer'] == 'tcp' and parsed['fallback'] is True and parsed['ports']['tcp'] == b.port
        with pytest.raises(SystemExit, match='wrong username or password'):     # a refused login never falls back
            setup.find_transport(plain, 'admin', 'nope')
    finally:
        b.close()


@pytest.mark.parametrize('user, password', [('', 'x'), ('x', ''), ('a\nb', 'x'), ('x', 'p' * 300)])
def test_a_missing_or_odd_login_is_refused(user, password):
    assert setup.valid_login(user, password)
    assert setup.valid_login('admin', 'admin123') is None


def test_the_login_file_is_private_and_never_in_config_yaml():
    t = setup.read_token(token())
    assert 'admin123' not in setup.config_yaml(t, 10)
    assert setup.login_env('admin', 'admin123') == 'HB_MQTT_USERNAME=admin\nHB_MQTT_PASSWORD=admin123\n'


def test_merge_config_keeps_the_way_in_unless_the_broker_changed(tmp_path, monkeypatch):
    import heartbeat_agent as ha
    monkeypatch.setattr(setup, 'DIR', str(tmp_path))
    t = setup.read_token(token(broker='mqtt.example.com', port=8883, tls=True))
    found = dict(setup.settings_for(t), prefer='ws-tls')
    (tmp_path / 'config.yaml').write_text(setup.config_yaml(found, 10))
    new = tmp_path / 'base.yaml'
    new.write_text('broker: mqtt.example.com\nport: 8883\ntls: true\ninterval: 30\n')
    assert setup.merge_config(str(new)) == 0 and not new.exists()
    s = ha.parse_settings((tmp_path / 'config.yaml').read_text())
    assert s['interval'] == 30 and s['prefer'] == 'ws-tls'                  # same broker: the way in stays
    new.write_text('broker: other.example.com\nport: 1883\ntls: false\ninterval: 30\n')
    assert setup.merge_config(str(new)) == 0
    s = ha.parse_settings((tmp_path / 'config.yaml').read_text())
    assert s['broker'] == 'other.example.com' and s['prefer'] == '' and s['ports']['tcp'] == 1883
    new.write_text('broker: [bad\n')
    assert setup.merge_config(str(new)) == 1                                  # a bad file changes nothing
    assert ha.parse_settings((tmp_path / 'config.yaml').read_text())['broker'] == 'other.example.com'


def test_merge_config_says_when_the_agent_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'DIR', str(tmp_path))
    new = tmp_path / 'base.yaml'
    new.write_text('broker: a.example.com\nport: 8883\ntls: true\ninterval: 10\n')
    assert setup.merge_config(str(new)) == 3 and 'NOT_INSTALLED' in capsys.readouterr().out


def test_the_install_path_writes_the_tokens_identity_and_the_chosen_broker(tmp_path, monkeypatch):
    """main() end to end on this machine: the node identity from the token, the way in from the
    checkpoints, both written where the agent reads them (install.sh itself is not run)."""
    import heartbeat_agent as ha
    monkeypatch.setattr(setup, 'DIR', str(tmp_path))
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(setup, 'find_transport',
                        lambda t, u, p, r, port: (dict(setup.settings_for(t), prefer='ws-tls'), 'x over ws-tls on port 8884'))
    seen = {}

    def fake_run(args, **kw):
        env = dict(line.split('=', 1) for line in open(args[2]).read().splitlines())
        seen.update(env=env, config=ha.parse_settings((tmp_path / 'config.yaml').read_text()))

        class R:
            returncode, stdout = 0, 'HEARTBEAT_OK 1.2.0'
        return R()
    monkeypatch.setattr(setup.subprocess, 'run', fake_run)
    monkeypatch.setenv('HB_JOIN_TOKEN', token(node='pi-node-09'))
    monkeypatch.setenv('HB_MQTT_USERNAME', 'admin')
    monkeypatch.setenv('HB_MQTT_PASSWORD', 'admin123')
    assert setup.main(['--interval', '30']) == 0
    assert seen['env'] == hp.provision(MASTER, 'pi-node-09')
    assert seen['config']['prefer'] == 'ws-tls' and seen['config']['interval'] == 30
    assert 'admin123' in (tmp_path / 'mqtt.env').read_text() and 'admin123' not in (tmp_path / 'config.yaml').read_text()


@pytest.mark.skipif(os.name != 'posix', reason='file modes')
def test_config_yaml_stays_readable_by_the_agent_under_a_strict_umask(tmp_path, monkeypatch):
    """bulk_setup.py runs setup.py with umask 077: the agent's user must still read config.yaml."""
    monkeypatch.setattr(setup, 'DIR', str(tmp_path))
    old = os.umask(0o077)
    try:
        (tmp_path / 'config.yaml').write_text(setup.config_yaml(setup.settings_for(setup.read_token(token())), 10))
        new = tmp_path / 'base.yaml'
        new.write_text('broker: a.example.com\nport: 8883\ntls: true\ninterval: 20\n')
        assert setup.merge_config(str(new)) == 0
    finally:
        os.umask(old)
    assert (tmp_path / 'config.yaml').stat().st_mode & 0o007 == 0                 # never readable by other users


def test_files_only_writes_the_settings_and_installs_nothing(tmp_path, monkeypatch, capsys):
    """EdgeShell's esapp installs the code, its venv and its service; setup.py only writes
    the node's settings into the app's data folder (HB_HOME)."""
    import heartbeat_agent as ha
    monkeypatch.setattr(setup, 'DIR', str(tmp_path))
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(setup, 'find_transport',
                        lambda t, u, p, r, port: (dict(setup.settings_for(t), prefer='tcp'), 'x over tcp on port 1883'))
    monkeypatch.setattr(setup.subprocess, 'run', lambda *a, **k: pytest.fail('install.sh must not run'))
    monkeypatch.setenv('HB_JOIN_TOKEN', token(node='pi-node-11'))
    monkeypatch.setenv('HB_MQTT_USERNAME', 'admin')
    monkeypatch.setenv('HB_MQTT_PASSWORD', 'admin123')
    assert setup.main(['--files-only', '--interval', '20']) == 0
    out = capsys.readouterr().out
    assert 'HEARTBEAT_FILES_OK' in out and 'Done.' in out
    env = dict(line.split('=', 1) for line in (tmp_path / 'agent.env').read_text().splitlines())
    assert env == hp.provision(MASTER, 'pi-node-11')
    assert ha.parse_settings((tmp_path / 'config.yaml').read_text())['interval'] == 20
    assert not [f for f in os.listdir(tmp_path) if f.endswith('.new')]


def test_the_agent_finds_its_folder_from_hb_home_or_app_data(monkeypatch):
    import importlib
    import heartbeat_agent as ha
    monkeypatch.delenv('HB_HOME', raising=False)
    monkeypatch.setenv('APP_DATA', '/opt/edgeshell/apps/heartbeat/data')
    assert importlib.reload(ha).IDENTITY == os.path.join('/opt/edgeshell/apps/heartbeat/data', 'agent.env')
    monkeypatch.setenv('HB_HOME', '/srv/hb')
    assert importlib.reload(ha).SETTINGS == os.path.join('/srv/hb', 'config.yaml')
    monkeypatch.delenv('HB_HOME')
    monkeypatch.delenv('APP_DATA')
    assert importlib.reload(ha).HOME == '/opt/edgeshell-heartbeat'
