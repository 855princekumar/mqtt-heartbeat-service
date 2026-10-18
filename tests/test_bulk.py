"""tools/bulk_setup.py without SSH: reading the CSVs and bulk.yaml, what it sends to
a node, and that no secret reaches a command line or the results file."""
import importlib.util
import io
import os
import tarfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('bulk_setup', os.path.join(HERE, 'tools', 'bulk_setup.py'))
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)


def write(path, text):
    path.write_text(text, encoding='utf-8')
    return str(path)


@pytest.fixture
def cfg(tmp_path):
    return bulk.load_config(write(tmp_path / 'bulk.yaml', 'broker_login:\n  username: admin\n  password: admin123\n'
                                                           'ssh:\n  user: pi\n'))


def test_the_example_config_loads_with_the_example_login(capsys):
    c = bulk.load_config(os.path.join(HERE, 'tools', 'bulk.example.yaml'))
    assert c['broker_login'] == {'username': 'admin', 'password': 'admin123'}
    assert 'example login' in capsys.readouterr().out                       # a reminder to change it


def test_a_login_is_required(tmp_path):
    with pytest.raises(SystemExit, match='username and a password'):
        bulk.load_config(write(tmp_path / 'b.yaml', 'broker_login:\n  username: admin\n'))


def test_nodes_and_tokens_are_matched_and_problems_found_before_any_ssh(tmp_path, cfg):
    nodes = write(tmp_path / 'nodes.csv', 'node,host,user,port\npi-1,10.0.0.1,,\npi-2,10.0.0.2,admin,2222\n'
                                          'pi-3,10.0.0.3,,\nbad name,10.0.0.4,,\npi-1,10.0.0.5,,\npi-5,10.0.0.6,,x\n')
    tokens = write(tmp_path / 'tokens.csv', 'node,token\npi-1,hbj1.a\npi-2,hbj1.b\npi-5,hbj1.e\n')
    todo, problems = bulk.plan(nodes, tokens, cfg, need_tokens=True)
    assert [(n['node'], n['user'], n['port'], n['token']) for n in todo] == [('pi-1', 'pi', 22, 'hbj1.a'),
                                                                            ('pi-2', 'admin', 2222, 'hbj1.b')]
    assert any('pi-3: no token' in p for p in problems) and any('bad name' in p for p in problems)
    assert any('listed twice' in p for p in problems) and any('port is not a number' in p for p in problems)
    todo, _ = bulk.plan(nodes, None, cfg, need_tokens=False)                 # login and test need no tokens
    assert {n['node'] for n in todo} == {'pi-1', 'pi-2', 'pi-3'}


def test_a_spreadsheet_saved_csv_with_a_bom_still_reads(tmp_path):
    path = tmp_path / 'n.csv'
    path.write_bytes('﻿node,host\npi-1,10.0.0.1\n'.encode('utf-8'))
    assert bulk.read_csv(str(path), ('node', 'host'))[0]['node'] == 'pi-1'


class Recorder:
    def __init__(self):
        self.calls = []

    def run(self, n, command, stdin=b'', timeout=300):
        self.calls.append((command, stdin))
        return 0, 'HB_SSH_OK\nok\nHEARTBEAT_OK 1.2.0\nBroker: x over tcp-tls on port 8883, login accepted\nDone.\nHEARTBEAT_TEST ok\n'


def test_secrets_travel_only_in_the_bundle_never_on_a_command_line(cfg):
    ssh = Recorder()
    node = {'node': 'pi-1', 'host': 'h', 'user': 'pi', 'port': 22, 'token': 'hbj1.SECRET-TOKEN'}
    row = bulk.work_on(ssh, cfg, node, 'setup', {'setup.py': b'#'})
    assert row['action'] == row['test'] == 'ok' and row['broker'] == 'tcp-tls:8883'
    for command, _stdin in ssh.calls:
        assert 'SECRET-TOKEN' not in command and 'admin123' not in command
    names = tarfile.open(fileobj=io.BytesIO(ssh.calls[1][1])).getnames()
    assert {'token', 'login', 'setup.py'} <= set(names)
    members = tarfile.open(fileobj=io.BytesIO(ssh.calls[1][1])).getmembers()
    assert all(m.mode == 0o600 for m in members)


def test_root_logins_need_no_sudo_and_others_use_it(cfg):
    ssh = Recorder()
    bulk.run_bundle(ssh, cfg, {'node': 'n', 'host': 'h', 'user': 'root', 'port': 22}, {}, '--test')
    command = ssh.calls[0][0]
    assert 'id -u' in command and 'sudo -n python3 setup.py --test' in command
    cfg['ssh']['sudo_password'] = 'pw'
    bulk.run_bundle(ssh, cfg, {'node': 'n', 'host': 'h', 'user': 'pi', 'port': 22}, {}, '--test')
    command, stdin = ssh.calls[1]
    assert 'sudo -S' in command and '< sudopw' in command and 'pw' not in command.replace('sudopw', '')
    assert 'sudopw' in tarfile.open(fileobj=io.BytesIO(stdin)).getnames()


def test_a_refused_login_is_reported_with_its_reason(cfg):
    class Refused(Recorder):
        def run(self, n, command, stdin=b'', timeout=300):
            self.calls.append((command, stdin))
            if len(self.calls) == 1:
                return 0, 'HB_SSH_OK\n'
            return 1, 'Setting up node pi-1\nNot set up: the broker refused: wrong username or password.\n'
    row = bulk.work_on(Refused(), cfg, {'node': 'pi-1', 'host': 'h', 'user': 'pi', 'port': 22, 'token': 't'}, 'setup', {})
    assert row['action'] == 'fail' and 'wrong username or password' in row['detail'] and row['test'] == ''


def test_results_csv_escapes_formulas_but_not_empty_cells(tmp_path, cfg, monkeypatch):
    rows = [{'node': 'pi-1', 'host': '=cmd', 'ssh': 'ok', 'action': '', 'broker': '', 'test': 'ok', 'sensehive': '',
             'detail': '-bad'}]
    cfg['results_dir'] = str(tmp_path)
    monkeypatch.setattr(bulk, 'load_config', lambda path: cfg)
    monkeypatch.setattr(bulk, 'plan', lambda *a, **k: ([{'node': 'pi-1', 'host': '=cmd', 'user': 'pi', 'port': 22,
                                                          'token': ''}], []))
    monkeypatch.setattr(bulk, 'work_on', lambda *a: rows[0])
    monkeypatch.setattr(bulk, 'agent_files', lambda: {})
    bulk.main(['--mode', 'test', '--nodes', 'x.csv', '--yes'])
    text = next(tmp_path.glob('bulk-test-*.csv')).read_text()
    assert "'=cmd" in text and "'-bad" in text and "pi-1,'=cmd,ok,,,ok,,'-bad" in text
