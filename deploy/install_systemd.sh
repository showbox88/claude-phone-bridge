#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

sudo install -m 644 notion-sync.service /etc/systemd/system/notion-sync.service
sudo install -m 644 notion-sync.timer   /etc/systemd/system/notion-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now notion-sync.timer

echo
echo "Installed. Status:"
systemctl status notion-sync.timer --no-pager
echo
echo "Next 3 wake-ups:"
systemctl list-timers notion-sync.timer --no-pager
