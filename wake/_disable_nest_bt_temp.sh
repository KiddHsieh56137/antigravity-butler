#!/bin/bash
# Temporarily disable Nest Hub BT audio. Code kept.
MAC="${BUTLER_BT_MAC:-D8:EB:46:C5:23:27}"

sudo systemctl daemon-reload
sudo systemctl stop antigravity-butler-voice.service || true
sudo systemctl disable antigravity-butler-voice.service || true
sudo systemctl stop keep-bt-audio.timer keep-bt-audio.service 2>/dev/null || true
sudo systemctl disable keep-bt-audio.timer 2>/dev/null || true

bluetoothctl disconnect "$MAC" 2>/dev/null || true
sleep 1
bluetoothctl info "$MAC" 2>/dev/null | grep -E 'Name|Connected' || true
echo "Nest Hub BT temporarily disabled. Pi voice service stopped."
