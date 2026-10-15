#!/usr/bin/env bash
# Install (or update) the MQTT heartbeat agent as a systemd service.
#
#   sudo bash install.sh [path/to/agent.env]      install or update
#   sudo bash install.sh --uninstall              remove everything
#
# Layout
#   /opt/edgeshell-heartbeat/heartbeat_agent.py      root:root 0644
#   /opt/edgeshell-heartbeat/heartbeat_protocol.py   root:root 0644
#   /opt/edgeshell-heartbeat/agent.env               root:hbagent 0640 (this node's key)
#   /opt/edgeshell-heartbeat/config.yaml             root:root 0644 (fleet settings, hot reloaded;
#                                                    created with defaults only if missing)
#   /etc/systemd/system/edgeshell-heartbeat.service
# The service runs as the unprivileged user hbagent with a read-only system.
set -euo pipefail

DIR=/opt/edgeshell-heartbeat
UNIT=/etc/systemd/system/edgeshell-heartbeat.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now edgeshell-heartbeat 2>/dev/null || true
    rm -f "$UNIT"; systemctl daemon-reload
    rm -rf "$DIR"
    id hbagent >/dev/null 2>&1 && userdel hbagent 2>/dev/null || true
    echo "HEARTBEAT_UNINSTALLED"
    exit 0
fi

ENV_SRC="${1:-$DIR/agent.env}"
[ -f "$ENV_SRC" ] || { echo "agent.env not found: $ENV_SRC"; exit 1; }

python3 - <<'PY' || { echo "python 3.7 or newer is required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)
PY

# Dependencies from the OS packages (no pip, nothing downloaded at run time).
DEPS='import paho.mqtt.client, yaml, cryptography.hazmat.primitives.ciphers.aead'
if ! python3 -c "$DEPS" 2>/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3-paho-mqtt python3-yaml python3-cryptography ca-certificates >/dev/null
fi
python3 -c "$DEPS" || { echo "python3-paho-mqtt, python3-yaml or python3-cryptography could not be installed"; exit 1; }

id hbagent >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hbagent

install -d -m 0755 -o root -g root "$DIR"
install -m 0644 -o root -g root "$SRC/heartbeat_agent.py" "$SRC/heartbeat_protocol.py" "$DIR/"
if [ "$(readlink -f "$ENV_SRC")" != "$(readlink -f "$DIR/agent.env")" ]; then
    install -m 0640 -o root -g hbagent "$ENV_SRC" "$DIR/agent.env"
else
    chown root:hbagent "$DIR/agent.env"; chmod 0640 "$DIR/agent.env"
fi

# Fleet settings: never overwritten here, so a pushed config.yaml survives updates.
if [ ! -f "$DIR/config.yaml" ]; then
    cat > "$DIR/config.yaml" <<'YAML'
# MQTT heartbeat agent settings. Hot reloaded: save a new version (or push the
# same file to every node) and the agent applies it within one interval, with
# no restart. An invalid file is ignored and the last good settings stay.
broker: broker.hivemq.com   # MQTT broker host
port: 8883                  # 8883 = MQTT over TLS
tls: true                   # verify the broker certificate (keep true on public brokers)
interval: 10                # seconds between heartbeats (5 to 3600)
YAML
    chmod 0644 "$DIR/config.yaml"
fi

cat > "$UNIT" <<'UNIT'
[Unit]
Description=EdgeShell MQTT heartbeat agent
Documentation=https://github.com/855princekumar/mqtt-heartbeat-service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hbagent
Group=hbagent
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /opt/edgeshell-heartbeat/heartbeat_agent.py
Restart=always
RestartSec=5
# Hardening: no privileges, read-only system, nothing but outbound network.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
CapabilityBoundingSet=
MemoryMax=64M
CPUQuota=10%

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable edgeshell-heartbeat >/dev/null 2>&1
systemctl restart edgeshell-heartbeat
sleep 3
if systemctl is-active --quiet edgeshell-heartbeat; then
    echo "HEARTBEAT_OK $(PYTHONDONTWRITEBYTECODE=1 python3 -c 'import sys; sys.path.insert(0, "/opt/edgeshell-heartbeat"); import heartbeat_agent as a; print(a.__version__)')"
else
    journalctl -u edgeshell-heartbeat -n 20 --no-pager || true
    echo "HEARTBEAT_FAILED"
    exit 1
fi
