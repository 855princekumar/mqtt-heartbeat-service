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
    assert ha.parse_settings(text) == {'broker': '10.0.0.5', 'port': 1883, 'tls': False, 'interval': 7}


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
