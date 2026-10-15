"""End-to-end check against a real MQTT broker: the agent and a receiver side
by side, with a throwaway fleet key. Exit code 0 only if every check passes.

    python tests/live_check.py --broker broker.hivemq.com              (TLS, port 8883)
    python tests/live_check.py --broker 127.0.0.1 --port 1883 --no-tls  (CI, local Mosquitto)

Checks: birth and heartbeats are accepted, nothing is rejected, a forged
message from a third party is rejected, and after the agent's connection is
cut without a clean disconnect the broker delivers the signed last will.
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import heartbeat_agent as ha  # noqa: E402
import heartbeat_protocol as hp  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--broker', default='broker.hivemq.com')
ap.add_argument('--port', type=int, default=8883)
ap.add_argument('--no-tls', action='store_true')
ap.add_argument('--seconds', type=int, default=25)
args = ap.parse_args()
tls = not args.no_tls

master = os.urandom(32)                            # throwaway: nothing real is ever used here
p = hp.provision(master, 'live-check-node')
identity = {'node': p['HB_NODE'], 'kid': bytes.fromhex(p['HB_KID']),
            'node_key': bytes.fromhex(p['HB_NODE_KEY']), 'topic_key': bytes.fromhex(p['HB_TOPIC_KEY'])}
tracker, accepted, rejected = hp.Tracker(), [], []
tkey = hp.topic_key(master)


def on_message(client, userdata, msg):
    try:
        body = hp.open_message(master, msg.topic, msg.payload)
        if tracker.accept(body):
            accepted.append(body['k'])
    except hp.Invalid as e:
        rejected.append(str(e))


def client(name):
    c = ha.new_client(name + os.urandom(4).hex())
    if tls:
        c.tls_set()
    c.connect(args.broker, args.port, 30)
    c.loop_start()
    return c


rx = client('rx-')
rx.on_message = on_message
w = hp.window()
for win in (w - 1, w, w + 1):
    rx.subscribe(hp.topic(tkey, win), qos=1)
time.sleep(2)

home = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.live')
os.makedirs(home, exist_ok=True)
settings = os.path.join(home, 'config.yaml')
with open(settings, 'w') as fh:
    fh.write(f'broker: {args.broker}\nport: {args.port}\ntls: {"true" if tls else "false"}\ninterval: 5\n')
agent = ha.Agent(identity, settings_file=ha.SettingsFile(settings))
threading.Thread(target=agent.run, daemon=True).start()
time.sleep(args.seconds)

# Someone else on the broker publishes junk and a message sealed with another key.
intruder = client('x-')
time.sleep(3)                                      # connected before it publishes
tpc = hp.topic(tkey, hp.window())
intruder.publish(tpc, b'{"n":"live-check-node","k":"lwt"}', qos=1)
other = os.urandom(32)
kid = hp.kid_for(other, 'live-check-node')
intruder.publish(tpc, hp.seal(hp.node_key(other, kid), kid, tpc,
                              {'n': 'live-check-node', 'k': 'lwt', 't': time.time(), 's': time.time(), 'q': 1}), qos=1)
time.sleep(3)
status_live = tracker.status('live-check-node')

agent.running = False                              # cut without DISCONNECT: the broker publishes the will
sock = agent.client.socket() if agent.client else None
if sock:
    sock.close()
deadline = time.time() + 90
while time.time() < deadline and tracker.status('live-check-node') != 'offline':
    time.sleep(1)

checks = {
    'birth accepted': 'birth' in accepted,
    'heartbeats accepted': accepted.count('hb') >= 3,
    'forged messages rejected': len(rejected) >= 2,
    'still online after forged last wills': status_live == 'online',
    'signed last will after a cut': tracker.status('live-check-node') == 'offline' and accepted[-1] == 'lwt',
}
for name, ok in checks.items():
    print(('PASS  ' if ok else 'FAIL  ') + name)
print('accepted:', accepted, '| rejected:', rejected)
rx.disconnect()
intruder.disconnect()
sys.exit(0 if all(checks.values()) else 1)
