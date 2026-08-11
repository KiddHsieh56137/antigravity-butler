#!/bin/bash
set -uo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

# Free Razer Seiren from Pulse so PortAudio can open hw:X,0 directly.
# Keep Bluetooth sink modules intact.
MID=$(pactl list modules full | awk '
  BEGIN{id=""}
  /^Module:/{id=$2}
  /alsa.card_name = "Razer Seiren Mini"/{print id; exit}
  /device.description = "Razer Seiren Mini"/{print id; exit}
  /usb-Razer_Inc_Razer_Seiren_Mini/{print id; exit}
')
echo "seiren_module=${MID:-none}"
if [ -n "${MID:-}" ]; then
  pactl unload-module "$MID" && echo unloaded || echo unload_failed
fi
sleep 1
echo "=== sources after ==="
pactl list short sources
echo "=== portaudio ==="
/home/past/antigravity-butler/wake/.venv/bin/python - <<'PY'
import sounddevice as sd
devs=sd.query_devices()
print(devs)
for i,d in enumerate(devs):
    if d['max_input_channels']>0:
        print(i, d['name'], 'in=', d['max_input_channels'])
PY
