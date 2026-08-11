#!/bin/bash
set -uo pipefail
export XDG_RUNTIME_DIR=/run/user/1000

pactl info >/dev/null 2>&1 || pulseaudio --start --exit-idle-time=-1
sleep 1
pactl load-module module-bluetooth-discover 2>/dev/null || true
pactl load-module module-switch-on-connect 2>/dev/null || true

bluetoothctl power on
bluetoothctl trust D8:EB:46:C5:23:27 || true
bluetoothctl disconnect D8:EB:46:C5:23:27 2>/dev/null || true
sleep 1

connected=0
for i in 1 2 3 4 5 6; do
  echo "ATTEMPT $i"
  bluetoothctl connect D8:EB:46:C5:23:27 || true
  sleep 4
  if bluetoothctl info D8:EB:46:C5:23:27 | grep -q 'Connected: yes'; then
    echo CONNECTED
    connected=1
    break
  fi
  sleep 2
done

bluetoothctl info D8:EB:46:C5:23:27 | grep -E 'Connected|Name|UUID' || true

if [ "$connected" != 1 ]; then
  echo "FAILED to connect BT"
  exit 1
fi

SINK=""
for i in $(seq 1 12); do
  SINK=$(pactl list short sinks | awk '/bluez/{print $2; exit}')
  if [ -n "$SINK" ]; then
    echo "SINK_OK $SINK"
    break
  fi
  echo "waiting_sink_$i"
  # nudge profile discovery
  pactl list cards >/dev/null 2>&1 || true
  sleep 1
done

pactl list short sinks || true
pactl list short cards || true

if [ -z "$SINK" ]; then
  echo NO_SINK
  exit 2
fi

pactl set-card-profile bluez_card.D8_EB_46_C5_23_27 a2dp_sink || true
pactl set-default-sink "$SINK"
pactl set-sink-volume "$SINK" 100%
pactl set-sink-mute "$SINK" 0

curl -sS 'http://127.0.0.1:8788/api/tts?text=%E8%97%8D%E7%89%99%E5%B7%B2%E9%80%A3%E7%B7%9A%EF%BC%8C%E9%80%99%E6%98%AF%E4%B8%BB%E8%87%A5%E5%AE%A4%E5%96%AE%E5%93%A1' -o /tmp/bt3.mp3
ffmpeg -y -loglevel error -i /tmp/bt3.mp3 -ac 2 -ar 44100 -filter:a volume=2.5 /tmp/bt3.wav

echo "PLAYING_TO_$SINK"
PULSE_SINK="$SINK" paplay /tmp/bt3.wav
echo "DONE exit=$?"
