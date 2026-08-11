#!/bin/bash
# Keep PulseAudio + Nest Hub A2DP ready for butler-voice
set -uo pipefail
export XDG_RUNTIME_DIR=/run/user/1000
MAC=D8:EB:46:C5:23:27
# Do NOT force 100% — leave Nest/user volume alone after connect
BT_VOLUME="${BUTLER_BT_VOLUME:-55%}"

if ! pactl info >/dev/null 2>&1; then
  pulseaudio --start --exit-idle-time=-1 || true
  sleep 1
fi
pactl load-module module-bluetooth-discover 2>/dev/null || true
pactl load-module module-switch-on-connect 2>/dev/null || true

if ! bluetoothctl info "$MAC" 2>/dev/null | grep -q 'Connected: yes'; then
  bluetoothctl connect "$MAC" >/dev/null 2>&1 || true
  sleep 2
fi

if pactl list short sinks 2>/dev/null | grep -q bluez; then
  SINK=$(pactl list short sinks | awk '/bluez/{print $2; exit}')
  pactl set-card-profile bluez_card.D8_EB_46_C5_23_27 a2dp_sink 2>/dev/null || true
  pactl set-default-sink "$SINK" 2>/dev/null || true
  pactl set-sink-mute "$SINK" 0 2>/dev/null || true
  # Only set volume if flag file missing (once per boot)
  FLAG=/tmp/butler_bt_vol_set
  if [ ! -f "$FLAG" ]; then
    pactl set-sink-volume "$SINK" "$BT_VOLUME" 2>/dev/null || true
    touch "$FLAG"
    echo "ok sink=$SINK vol=$BT_VOLUME (once)"
  else
    echo "ok sink=$SINK (volume untouched)"
  fi
  exit 0
fi

echo "no bluez sink yet"
exit 1
