#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "[-] Please run this uninstaller as root: sudo bash uninstall.sh"
  exit 1
fi

echo "[*] 1. Stopping and removing systemd daemon..."
systemctl stop syshealer.service 2>/dev/null || true
systemctl disable syshealer.service 2>/dev/null || true
rm -f /etc/systemd/system/syshealer.service
systemctl daemon-reload

echo "[*] 2. Removing global CLI command and sudoers..."
rm -f /usr/local/bin/syshealer
rm -f /etc/sudoers.d/syshealer

echo "[*] 3. Cleaning up system user and installation directory..."
userdel syshealer 2>/dev/null || true
rm -rf /opt/syshealer

echo "[+] SysHealer-AI system components have been removed."
