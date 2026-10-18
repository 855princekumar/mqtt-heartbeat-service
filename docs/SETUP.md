# Setup guide

This guide takes a fleet from nothing to heartbeats on screen. Pick the path
that matches your setup:

| Path | Use it when |
|---|---|
| [A. With EdgeShell](#a-with-edgeshell) | EdgeShell manages your nodes: it installs on many nodes for you |
| [B. On the node with a join token](#b-on-the-node-with-a-join-token) | you run SenseHive on its own: one command on each node (recommended without EdgeShell) |
| [C. By hand, with the master key](#c-by-hand-with-the-master-key) | you want to understand or script each step |
| [D. Many nodes at once](#d-many-nodes-at-once-bulk-ssh-without-edgeshell) | tens or hundreds of nodes, from your computer over SSH, without EdgeShell |

Every path ends the same way: each node has **its own** identity, so a node
nobody set up (a crawler, a fake agent on the broker) can never appear in
SenseHive. See also [the broker login](#the-broker-login).

![Architecture](assets/architecture.svg)

## Before you start

- **A receiver:** [SenseHive](https://github.com/855princekumar/sense-hive)
  1.2 or newer (the EdgeShell stack already includes it).
- **Nodes:** Linux with Python 3.7+ (Raspberry Pi OS, Debian, Ubuntu; amd64,
  arm64, armv7). On Debian-like systems `install.sh` installs the three OS
  packages it needs with apt.
- **Network:** nodes and SenseHive need **outbound** TCP 8883 (MQTT over TLS)
  to the broker. No inbound port anywhere.
- **Clocks:** nodes keep time with NTP (messages more than 2 minutes off are
  refused).

## A. With EdgeShell

1. In EdgeShell's `.env`, set `HEARTBEAT_MASTER_KEY` (64 hex characters) and
   `SENSEHIVE_API_TOKEN` (32+ characters), then restart the stack. SenseHive
   gets both automatically.
2. **Node Fleet, Fleet Setup, Heartbeat agent:** tick a few test nodes first,
   then **Install on selected**. EdgeShell connects over SSH, writes each node's
   own `agent.env`, and runs `install.sh`; progress shows in the job window.
3. After about 10 seconds the nodes show a heartbeat badge on their cards, and
   administrators see them on SenseHive's **Heartbeats** page.
4. Install on the rest of the fleet the same way.

Changing settings for every node (broker, interval) is a push of one
`config.yaml` from EdgeShell; the agents pick it up without a restart.

## B. On the node with a join token

Needs SenseHive 1.8 or newer with its heartbeat receiver on
(`HEARTBEAT_MASTER_KEY=auto` is enough: a private fleet key for that SenseHive).

1. **In SenseHive:** **Heartbeats**, **Add node**, a name for the node (letters,
   digits, dots, dashes, underscores), **Create token**. Copy the token.
2. **On the node** (over SSH or at its own screen):

   ```bash
   git clone https://github.com/855princekumar/mqtt-heartbeat-service
   cd mqtt-heartbeat-service
   sudo python3 setup.py
   ```

   Paste the token when asked, then the broker username and password (none of
   them is shown or kept in your shell history). `setup.py`:

   - checks the token: a cut, wrapped-and-damaged or edited paste is refused
     with a plain reason, and nothing is installed;
   - logs in to the broker, TLS first ([the broker login](#the-broker-login));
   - writes `config.yaml` with the broker SenseHive listens on (an `interval`
     set before is kept) and `mqtt.env` with the login;
   - writes this node's `agent.env` (its name and keys, never the master key)
     and runs `install.sh` (the hardened systemd service, see step 3 of path C);
   - leaves no copy of the token or the keys anywhere but `/opt/edgeshell-heartbeat`.

3. **Check:** the node shows **online** on SenseHive's Heartbeats page within
   seconds. On the node: `sudo python3 setup.py --status` names the node and
   its **fleet**; SenseHive's **Add node** shows the same fleet.

For scripts: `HB_JOIN_TOKEN=... sudo -E python3 setup.py`, or
`sudo python3 setup.py --token-file token.txt` (the file is deleted after a
good install). `sudo python3 setup.py --check TOKEN` only checks a token.

**Isolation.** Every SenseHive has its own fleet master key, so its tokens only
work with it. Nodes of another SenseHive (another person or company) on the
same public broker use other topics and other keys: they never appear on your
dashboard, and yours never appear on theirs.

## C. By hand, with the master key

### 1. Make the fleet master key and turn the receiver on

On a trusted machine (not a node):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"      # the master key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # an API token for other tools
```

Give both to SenseHive (in its `.env`, never in a file you commit):

```bash
HEARTBEAT_MASTER_KEY=<the master key>
HEARTBEAT_API_TOKEN=<the API token>
```

Restart SenseHive. Its **Heartbeats** page now says `connected to the
heartbeat broker`.

### 2. Make each node's identity (`agent.env`)

Still on the trusted machine, in a clone of this repository, once per node
name:

```bash
export HEARTBEAT_MASTER_KEY=<the master key>
python3 -c "import os, heartbeat_protocol as hp; p = hp.provision(bytes.fromhex(os.environ['HEARTBEAT_MASTER_KEY']), 'pi-node-01'); print('\n'.join(f'{k}={v}' for k, v in p.items()))" > agent.env
```

`agent.env` holds `HB_NODE`, `HB_KID`, `HB_NODE_KEY` and `HB_TOPIC_KEY`: this
node's name and keys, **not** the master key. It is a secret for that node
only: copy it to that node and nowhere else, and delete your local copy after
installing.

### 3. Install on the node

```bash
scp heartbeat_agent.py heartbeat_protocol.py install.sh agent.env pi@pi-node-01:/tmp/hb/
ssh pi@pi-node-01 'cd /tmp/hb && sudo bash install.sh agent.env && rm -rf /tmp/hb'
```

`install.sh`:

- checks Python 3.7+ and installs `python3-paho-mqtt`, `python3-yaml` and
  `python3-cryptography` from the OS packages if they are missing;
- creates the system user `hbagent` (no login, no home);
- copies the files to `/opt/edgeshell-heartbeat` (`agent.env` as
  `root:hbagent 0640`) and writes a default `config.yaml` only if none exists;
- installs and starts `edgeshell-heartbeat.service` with its sandbox
  (read-only system, no capabilities, `MemoryMax=64M`, `CPUQuota=10%`); without
  systemd it starts a background process as `hbagent` instead.

Run it again to update the agent; a pushed `config.yaml` is kept.

### 4. Check it

On the node:

```bash
systemctl status edgeshell-heartbeat
journalctl -u edgeshell-heartbeat -n 20
# heartbeat: agent 1.2.0 for pi-node-01: broker.hivemq.com:8883 tls=on every 10s (config: ...)
```

In SenseHive: the node appears on the **Heartbeats** page as `online` within
seconds. There is nothing to add in SenseHive: it found the node by itself.

From any tool, with the API token:

```bash
curl -s -H "Authorization: Bearer $HEARTBEAT_API_TOKEN" http://<sensehive>:5000/api/heartbeat/nodes
```

### 5. Test the last will

Stop the node's network (or pull its power). Within about half a minute the
broker publishes its will and SenseHive shows it as `offline`. Bring it back:
it reconnects, sends `birth`, and is `online` again.

## D. Many nodes at once (bulk SSH, without EdgeShell)

`tools/bulk_setup.py` runs on your own computer (Windows, Linux or macOS) and
sets up, re-logs-in or tests any number of nodes over SSH. EdgeShell's
**Fleet Setup** does the same for you; this tool is for SenseHive on its own.

1. **Tokens:** in SenseHive, **Heartbeats, Add node, Many nodes (CSV)**: paste
   the node names (or a prefix and a range, e.g. `pi-node-01` to `pi-node-100`),
   **Create tokens**, **Download CSV** (`node,token`, one row per node, shown only
   once: keep it private).
2. **Nodes:** a CSV of where to reach them over SSH (example:
   [`tools/nodes.example.csv`](../tools/nodes.example.csv)):

   ```csv
   node,host,user,port
   pi-node-01,192.168.1.21,pi,22
   pi-node-02,192.168.1.22,pi,22
   ```

   `node` must match the tokens CSV; `user` and `port` may be left empty (the
   defaults from `bulk.yaml` apply).
3. **Settings:** copy [`tools/bulk.example.yaml`](../tools/bulk.example.yaml) to
   `tools/bulk.yaml` (ignored by git: it holds passwords) and set the **broker
   login** every node uses (the example is `admin` / `admin123`), your SSH key,
   and optionally SenseHive's address and API token to confirm the nodes come
   online.
4. **Run:**

   ```bash
   pip install pyyaml                     # paramiko too, only if you log in to nodes with SSH passwords
   python tools/bulk_setup.py
   ```

   It explains what it will do for 5 seconds, asks what to do (set up many
   nodes, one node, a new broker login on every node, or test), and opens a
   file window for the tokens CSV and the nodes CSV (on a server without a
   screen it asks for the paths instead).

For each node it checks SSH and Python, copies the agent over SSH (the node
needs no git and no internet besides the broker), installs it with the node's
own token and the broker login, then runs the node's own test: service
running and enabled at boot, key and login files private, the broker reachable
(TLS, or plain MQTT when TLS cannot be reached) and the login accepted.
Tokens and passwords travel only inside the SSH session: never on a command
line, and nothing is left in a temp folder. Every run writes a CSV to
`tools/results/` (node, host, ssh, action, broker, test, sensehive, detail; no
secrets) and prints a summary; the exit code is 1 when any node failed.

Without questions (cron, CI):

```bash
python tools/bulk_setup.py --mode setup --tokens tokens.csv --nodes nodes.csv --yes
python tools/bulk_setup.py --mode login --nodes nodes.csv --yes     # new username/password from bulk.yaml
python tools/bulk_setup.py --mode test  --nodes nodes.csv --yes
```

## The broker login

The agent logs in to the broker with a username and password, kept in
`/opt/edgeshell-heartbeat/mqtt.env` (`HB_MQTT_USERNAME`, `HB_MQTT_PASSWORD`;
`root:hbagent 0640`), never in `config.yaml`. `setup.py` asks for it (hidden),
or reads it from the environment or `--login-file`, and checks it against the
broker with a real MQTT login before saving anything: a wrong login is
refused and nothing changes.

**TLS first.** `setup.py` connects as the join token says (TLS on 8883 by
default). Only when the TLS port cannot be reached at all does it use plain
MQTT on 1883 (`--plain-port`), and it says so; a refused login never falls
back. `--require-tls` (or `tls.require: true` in `bulk.yaml`) never falls back.
The heartbeats themselves are encrypted either way; TLS also protects the
login.

**A new password** for every node: change it on the broker, set it in
`bulk.yaml`, and run `python tools/bulk_setup.py --mode login`. On one node:
`sudo python3 /opt/edgeshell-heartbeat/setup.py --set-credentials`. The agent
picks the new login up within one interval and reconnects, with no restart.
Roll a password out before the broker stops accepting the old one, or for a
short while accept both.

## Change settings on every node

Edit one `config.yaml` and copy it to `/opt/edgeshell-heartbeat/config.yaml` on
each node, atomically:

```bash
sudo install -m 0644 config.yaml /opt/edgeshell-heartbeat/config.yaml.new
sudo mv /opt/edgeshell-heartbeat/config.yaml.new /opt/edgeshell-heartbeat/config.yaml
```

The agent applies it on its next beat, with no restart. An invalid file is
ignored (with a log line) and the agent keeps its last good settings.

| Setting | Default | Range |
|---|---|---|
| `broker` | `broker.hivemq.com` | any MQTT 3.1.1 broker |
| `port` | `8883` | 1 to 65535 |
| `tls` | `true` | keep `true` on public brokers |
| `interval` | `10` | 5 to 3600 seconds |

## Uninstall

```bash
sudo bash install.sh --uninstall     # stops the service, removes the files and the hbagent user
```

## Rotate keys

- **One node compromised:** re-provision it under a new name (a new key id and
  node key); the old identity stops counting.
- **The whole fleet:** set a new master key on SenseHive (and EdgeShell), then
  re-install the agent on every node (EdgeShell: Fleet Setup).

## Troubleshooting

| Symptom | Check |
|---|---|
| `agent.env not found` | pass the path: `sudo bash install.sh ./agent.env` |
| `HB_... missing in agent.env` | the file was cut or edited; make it again (step 2) |
| Log says `broker not reachable` | outbound TCP 8883 from the node; DNS; the `broker` setting |
| Running, but the node never shows in SenseHive | the node's clock (`timedatectl`); SenseHive has the **same** master key the node was provisioned from; SenseHive's page says `connected` |
| Shows `silent` | the broker or the internet path, not the node: the agent keeps retrying (2 s up to 60 s) |
| Shows `offline` but the node is up | the agent stopped: `systemctl status edgeshell-heartbeat` |
| SenseHive `rejected` count grows | normal on a public broker (other people's traffic); a steady climb with a missing node points at a wrong key or clock |
