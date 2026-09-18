#!/usr/bin/env bash
# CPU equivalent of src/iplayerdl/transcode.py
# Usage: transcode_cpu.sh -i <input> -o <output>
# Hardcoded (from config defaults + cpu branch):
#   encoder=libsvtav1, quality=20, crop=true, samples=20,
#   cropdetect=24:16:0, audio=aac 192k, subs=copy, map=0
set -euo pipefail

QUALITY=20
CROP_SAMPLES=20
CROPDETECT="24:16:0"

usage() {
  echo "Usage: $0 -i <input_file> -o <output_file>" >&2
  exit "${1:-0}"
}

INPUT=""
OUTPUT=""
while getopts ":i:o:h" opt; do
  case "$opt" in
    i) INPUT="$OPTARG" ;;
    o) OUTPUT="$OPTARG" ;;
    h) usage 0 ;;
    *) usage 2 ;;
  esac
done
[[ -n "$INPUT" && -n "$OUTPUT" ]] || usage 2
[[ -f "$INPUT" ]] || { echo "error: input not found: $INPUT" >&2; exit 1; }

mkdir -p "$(dirname "$OUTPUT")"

# get_video_duration(): ffprobe format=duration
DURATION="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$INPUT")"
INTERVAL="$(awk -v d="$DURATION" -v n="$CROP_SAMPLES" 'BEGIN { i = d / n; if (i < 1.0) i = 1.0; printf "%.3f", i }')"

# get_crop_region(): most common cropdetect hit, empty if none
CROP_VAL="$(ffmpeg -hide_banner -nostats -i "$INPUT" -an -sn -dn \
  -vf "fps=1/${INTERVAL},cropdetect=${CROPDETECT}" -f null - 2>&1 \
  | grep -oE 'crop=[0-9]+:[0-9]+:[0-9]+:[0-9]+' | cut -d= -f2 \
  | sort | uniq -c | sort -nr | awk 'NR==1 {print $2}' || true)"

VF_ARGS=()
if [[ -n "${CROP_VAL:-}" ]]; then
  VF_ARGS=(-vf "crop=${CROP_VAL}")
  echo "crop: $CROP_VAL" >&2
else
  echo "crop: none detected, skipping" >&2
fi

# get_params() cpu branch: no hwaccel + libsvtav1 + map/aac/subs
if ! ffmpeg -y -hide_banner -loglevel error -stats \
  -i "$INPUT" \
  "${VF_ARGS[@]}" \
  -c:v libsvtav1 -global_quality "$QUALITY" \
  -map 0 -c:a aac -b:a 192k -c:s copy \
  "$OUTPUT"; then
  echo "error: transcode failed, removing partial output" >&2
  rm -f "$OUTPUT"
  exit 1
fi

echo "Transcoded: $OUTPUT" >&2
