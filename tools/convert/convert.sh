#!/bin/bash
# usage: convert.sh <kind> <source> [output folder]   -> <stem>.castcouch.mp4 (beside the source by default)
set -e
kind=$1; src=$2; dir=${3:-$(dirname "$src")}; mkdir -p "$dir"; base=$(basename "${src%.*}"); out="$dir/$base.castcouch.mp4"; tmp="$dir/$base.castcouch.part.mp4"
common=(-nostdin -hide_banner -loglevel warning -y)
t=$(date +%s)
case $kind in
  video) nice ffmpeg "${common[@]}" -fflags +genpts -i "$src" -map 0:v:0 -map 0:a:0 \
           -c:v h264_nvenc -preset p6 -tune hq -rc vbr -cq 21 -b:v 0 -maxrate 6M -bufsize 12M -profile:v high -pix_fmt yuv420p \
           -c:a aac -b:a 160k -ac 2 -movflags +faststart -f mp4 "$tmp" ;;
  sound) nice ffmpeg "${common[@]}" -i "$src" -map 0:v:0 -map 0:a:0 -map 0:a \
           -c:v copy -c:a copy -c:a:0 aac -b:a:0 192k -ac:a:0 2 \
           -disposition:a 0 -disposition:a:0 default -metadata:s:a:0 title="Stereo (AAC)" \
           -movflags +faststart -f mp4 "$tmp" ;;
  container) nice ffmpeg "${common[@]}" -fflags +genpts -i "$src" -map 0:v:0 -map 0:a:0 -c copy -movflags +faststart -f mp4 "$tmp" ;;
esac
mv "$tmp" "$out"
echo "$kind done in $(( $(date +%s) - t )) s: $(du -h "$out" | cut -f1)  $out"
