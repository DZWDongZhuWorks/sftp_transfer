#!/usr/bin/env bash
# 下載並驗證原始碼(在主機上跑,不進容器)。
set -euo pipefail
W="$(cd "$(dirname "$0")/.." && pwd)"
FFV=8.1.3
X264_COMMIT=b35605ace3ddf7c1a5d67a2eb553f034aef41d55
cd "$W/src"

curl -fsSLO "https://ffmpeg.org/releases/ffmpeg-$FFV.tar.xz"
curl -fsSLO "https://ffmpeg.org/releases/ffmpeg-$FFV.tar.xz.asc"
curl -fsSL -o ffmpeg-devel.asc https://ffmpeg.org/ffmpeg-devel.asc

export GNUPGHOME="$W/src/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
gpg --batch --quiet --import ffmpeg-devel.asc
gpg --batch --fingerprint
gpg --batch --verify "ffmpeg-$FFV.tar.xz.asc" "ffmpeg-$FFV.tar.xz"

rm -rf x264
git clone --quiet -c core.autocrlf=false -c core.eol=lf https://code.videolan.org/videolan/x264.git x264
git -C x264 -c advice.detachedHead=false checkout --quiet "$X264_COMMIT"
if grep -rlI $'\r$' x264 --exclude-dir=.git | grep -q .; then echo "x264 checkout 有 CRLF(core.autocrlf?)" >&2; exit 1; fi
git -C x264 log -1 --format='x264 %H %cI'
sha256sum "ffmpeg-$FFV.tar.xz"
