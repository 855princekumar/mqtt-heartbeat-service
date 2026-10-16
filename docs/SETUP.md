# Setup guide

This guide takes a fleet from nothing to heartbeats on screen. Pick the path
that matches your setup:

| Path | Use it when |
|---|---|
| [A. With EdgeShell](#a-with-edgeshell) | EdgeShell manages your nodes (recommended: it does every step for you) |
| [B. By hand, with SenseHive](#b-by-hand-with-sensehive) | you run SenseHive on its own, or want to understand each step |

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

## B. By hand, with SenseHive

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
# heartbeat: agent 1.1.0 for pi-node-01: broker.hivemq.com:8883 tls=on every 10s (config: ...)
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
