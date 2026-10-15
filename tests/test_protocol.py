"""Heartbeat protocol: authenticity, confidentiality, freshness and the
LWT session rule. The vector test pins the derivations, so the copies of
heartbeat_protocol.py in SenseHive and EdgeShell must give the same values."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import heartbeat_protocol as hp  # noqa: E402

MASTER = bytes(range(32))
NOW = 1_800_000_000.0


def msg(node='pi-01', kind='hb', t=NOW, s=NOW - 50, q=1, master=MASTER, tpc=None):
    kid = hp.kid_for(master, node)
    tpc = tpc or hp.topic(hp.topic_key(master), hp.window(t))
    return tpc, hp.seal(hp.node_key(master, kid), kid, tpc, {'n': node, 'k': kind, 't': t, 's': s, 'q': q, 'v': '1'})


# Fixed vectors: every copy of heartbeat_protocol.py (agent, SenseHive,
# EdgeShell) must produce exactly these values, or nodes and receiver disagree.
VECTORS = {
    'kid': 'ebb13644074fabc3',
    'node_key': '493726126c479bc52142d4f3f63850c0f5ca956ef4466bc3161fa7b0d6233735',
    'topic_key': '714f19029722c7ce079b14df24552ae0f154762e040f188d79dece5ef5208ade',
    'topic_3000000': '3abd08/4a0e86dbcd37698b1225a11a',
}


def test_vectors_pin_the_derivations():
    kid = hp.kid_for(MASTER, 'pi-01')
    assert kid.hex() == VECTORS['kid']
    assert hp.node_key(MASTER, kid).hex() == VECTORS['node_key']
    assert hp.topic_key(MASTER).hex() == VECTORS['topic_key']
    assert hp.topic(hp.topic_key(MASTER), 3_000_000) == VECTORS['topic_3000000']
    p = hp.provision(MASTER, 'pi-01')
    assert set(p) == {'HB_NODE', 'HB_KID', 'HB_NODE_KEY', 'HB_TOPIC_KEY'}
    assert MASTER.hex() not in json.dumps(p)                      # the master key never reaches a node


def test_a_message_round_trips_and_reveals_nothing_in_clear():
    tpc, raw = msg()
    body = hp.open_message(MASTER, tpc, raw)
    assert body['n'] == 'pi-01' and body['k'] == 'hb'
    assert b'pi-01' not in raw and b'hb' not in raw[21:]         # encrypted
    assert 'pi-01' not in tpc                                     # nothing readable in the topic


def test_topics_change_every_window_and_look_random():
    tk = hp.topic_key(MASTER)
    seen = {hp.topic(tk, w) for w in range(100)}
    assert len(seen) == 100
    assert all(len(t) == 31 for t in seen)


@pytest.mark.parametrize('mutate', [
    lambda r: r[:-1] + bytes([r[-1] ^ 1]),                       # ciphertext changed
    lambda r: r[:5] + bytes([r[5] ^ 1]) + r[6:],                  # kid changed
    lambda r: bytes([2]) + r[1:],                                 # version changed
    lambda r: r[:20],                                             # cut
    lambda r: b'',
    lambda r: os.urandom(len(r)),                                 # noise from someone on the broker
])
def test_tampered_or_forged_messages_are_rejected(mutate):
    tpc, raw = msg()
    with pytest.raises(hp.Invalid):
        hp.open_message(MASTER, tpc, mutate(raw))


def test_a_message_cannot_be_moved_to_another_topic():
    tpc, raw = msg()
    with pytest.raises(hp.Invalid):
        hp.open_message(MASTER, hp.topic(hp.topic_key(MASTER), hp.window(NOW) + 1), raw)


def test_another_fleet_or_a_guessed_key_is_rejected():
    tpc, raw = msg(master=os.urandom(32))
    with pytest.raises(hp.Invalid):
        hp.open_message(MASTER, tpc, raw)


def test_a_node_cannot_speak_for_another_node():
    """pi-02 holds only its own key: a message claiming to be pi-01, sealed with
    pi-02's key, fails the name binding."""
    kid2 = hp.kid_for(MASTER, 'pi-02')
    tpc = hp.topic(hp.topic_key(MASTER), hp.window(NOW))
    raw = hp.seal(hp.node_key(MASTER, kid2), kid2, tpc, {'n': 'pi-01', 'k': 'hb', 't': NOW, 's': NOW, 'q': 1})
    with pytest.raises(hp.Invalid):
        hp.open_message(MASTER, tpc, raw)


def test_replayed_stale_and_out_of_order_heartbeats_are_ignored():
    tr = hp.Tracker()
    tpc, raw = msg(t=NOW)
    body = hp.open_message(MASTER, tpc, raw)
    assert tr.accept(body, now=NOW)
    assert not tr.accept(body, now=NOW + 1)                       # the same message again
    old = hp.open_message(MASTER, *msg(t=NOW - 5))
    assert not tr.accept(old, now=NOW + 1)                        # older than the last one
    stale = hp.open_message(MASTER, *msg(t=NOW - 600, s=NOW - 700))
    assert not tr.accept(stale, now=NOW + 1)                      # recorded long ago, replayed now
    future = hp.open_message(MASTER, *msg(t=NOW + 600, s=NOW + 500))
    assert not tr.accept(future, now=NOW + 1)


def test_the_last_will_counts_only_for_the_current_session():
    tr = hp.Tracker()
    s1 = NOW - 100
    assert tr.accept(hp.open_message(MASTER, *msg(kind='birth', t=s1, s=s1)), now=s1)
    lwt1 = hp.open_message(MASTER, *msg(kind='lwt', t=s1, s=s1))
    s2 = NOW
    assert tr.accept(hp.open_message(MASTER, *msg(kind='birth', t=s2, s=s2)), now=s2)
    assert not tr.accept(lwt1, now=s2 + 5)                        # an old will cannot take a live node down
    assert tr.status('pi-01', now=s2 + 5) == 'online'
    lwt2 = hp.open_message(MASTER, *msg(kind='lwt', t=s2, s=s2))
    assert tr.accept(lwt2, now=s2 + 400)                          # the will may arrive late: still valid
    assert tr.status('pi-01', now=s2 + 400) == 'offline'


def test_silence_is_reported_when_heartbeats_stop_without_a_will():
    tr = hp.Tracker()
    tr.accept(hp.open_message(MASTER, *msg(t=NOW)), now=NOW)
    assert tr.status('pi-01', now=NOW + 10) == 'online'
    assert tr.status('pi-01', now=NOW + 60) == 'silent'
    assert tr.status('unknown', now=NOW) is None
