#!/bin/bash
set -e

echo "=========================================="
echo "        SysHealer-AI - Installation       "
echo "=========================================="

if [ "$EUID" -ne 0 ]; then
  echo "[-] Please run this installer as root: sudo bash install.sh"
  exit 1
fi

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR="/opt/syshealer"

mkdir -p "$INSTALL_DIR"
cp -r "$PROJECT_DIR/"* "$INSTALL_DIR/"

echo "[*] Creating syshealer user and setting permissions..."
if ! id "syshealer" &>/dev/null; then
    useradd -r -s /usr/sbin/nologin syshealer
fi
usermod -aG systemd-journal syshealer

mkdir -p "$INSTALL_DIR/data/scripts"
chown -R syshealer:syshealer "$INSTALL_DIR"
chmod -R 750 "$INSTALL_DIR"

echo "[*] Configuring sudoers for syshealer..."
cat << EOF > /etc/sudoers.d/syshealer
syshealer ALL=(ALL) NOPASSWD: $INSTALL_DIR/data/scripts/*.sh
EOF
chmod 0440 /etc/sudoers.d/syshealer

echo "[*] Preparing local Python environment..."
sudo -u syshealer python3 -m venv "$INSTALL_DIR/ai_env"
sudo -u syshealer "$INSTALL_DIR/ai_env/bin/pip" install --upgrade pip -q
sudo -u syshealer "$INSTALL_DIR/ai_env/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "[*] Creating global CLI command 'syshealer'..."
cat << EOF > /usr/local/bin/syshealer
#!/bin/bash
cd "$INSTALL_DIR"
exec sudo -u syshealer "$INSTALL_DIR/ai_env/bin/python3" "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x /usr/local/bin/syshealer

echo "[*] Setting up Systemd Background Daemon..."
cat << EOF > /etc/systemd/system/syshealer.service
[Unit]
Description=SysHealer-AI Background AI Daemon
After=network.target

[Service]
Type=simple
User=syshealer
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/ai_env/bin/python3 $INSTALL_DIR/src/daemon.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now syshealer.service

echo "[+] Success! SysHealer-AI is ready to use."
