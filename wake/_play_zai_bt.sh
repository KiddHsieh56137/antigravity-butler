#!/bin/bash
set -uo pipefail
export XDG_RUNTIME_DIR=/run/user/1000
# Simulate butler BT TTS path
curl -sS 'http://127.0.0.1:8788/api/tts?text=%E5%9C%A8' -o /tmp/zai2.mp3
ffmpeg -y -loglevel error -i /tmp/zai2.mp3 -ac 2 -ar 44100 -filter:a volume=2.5 /tmp/zai2.wav
bash /home/past/antigravity-butler/wake/keep_bt_audio.sh
SINK=$(pactl list short sinks | awk '/bluez/{print $2; exit}')
echo "SINK=$SINK"
pactl set-sink-volume "$SINK" 100%
pactl set-sink-mute "$SINK" 0
echo "PLAY 在 now — listen to Nest Hub"
PULSE_SINK="$SINK" paplay /tmp/zai2.wav
echo exit=$?
