#!/bin/bash
set -uo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

# Prefer raw ALSA card for mic if present
CARD=$(arecord -l 2>/dev/null | awk -F: '/Razer Seiren Mini/{gsub(/card /,"",$1); print $1+0; exit}')
echo "seiren_card=${CARD:-none}"

# Make PortAudio see hardware: temporarily suspend pulse capture of seiren by setting
# default source away and using explicit hw device in butler.
if [ -n "${CARD:-}" ]; then
  DEV="hw:${CARD},0"
  echo "pin BUTLER_WAKE_DEVICE=$DEV"
  # update systemd unit
  sudo sed -i '/Environment=BUTLER_WAKE_DEVICE=/d' /etc/systemd/system/antigravity-butler-voice.service
  sudo sed -i "/Environment=BUTLER_WAKE_THRESHOLD=/a Environment=BUTLER_WAKE_DEVICE=$DEV" /etc/systemd/system/antigravity-butler-voice.service
fi

# Ensure TTS route is bt
sudo sed -i 's/BUTLER_TTS_ROUTE=.*/BUTLER_TTS_ROUTE=bt/' /etc/systemd/system/antigravity-butler-voice.service
grep -E 'WAKE_DEVICE|TTS_ROUTE|BT_SINK' /etc/systemd/system/antigravity-butler-voice.service

# Verify hw open works
if [ -n "${CARD:-}" ]; then
  /home/past/antigravity-butler/wake/.venv/bin/python - <<PY
import sounddevice as sd
dev = "hw:${CARD},0"
print("trying", dev)
try:
    with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=1280, device=dev) as s:
        a,_=s.read(1280)
        import numpy as np
        rms=float((a**2).mean()**0.5)
        print("hw_ok rms=", rms)
except Exception as e:
    print("hw_fail", e)
PY
fi

sudo systemctl daemon-reload
sudo systemctl restart antigravity-butler-voice
sleep 8
systemctl is-active antigravity-butler-voice
journalctl -u antigravity-butler-voice -n 20 --no-pager

# One more BT 在
bash /tmp/_play_zai_bt.sh
