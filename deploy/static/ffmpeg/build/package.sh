#!/usr/bin/env bash
# 把 build_in_container.sh 的產出(<W>/out)包成 deploy/static/ffmpeg/ 的離線包 + MANIFEST.txt。
# 用法:package.sh <W> <目的目錄(sftp_transfer 的 deploy/static/ffmpeg)>
# tar 以固定排序 / mtime / owner 打包:同一份 out/ 每次包出位元相同的壓縮檔。
set -euo pipefail
W="${1:?建置工作目錄}"; DEST="${2:?目的目錄}"
FFV=8.1.3
X264_COMMIT=b35605ace3ddf7c1a5d67a2eb553f034aef41d55
REV=nssms1
NAME="ffmpeg-${FFV}-${REV}-linux-arm64-static"
SRC_SHA="$(sha256sum "$W/src/ffmpeg-$FFV.tar.xz" | awk '{print $1}')"

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/$NAME"; mkdir -p "$D/bin"
install -m 0755 "$W/out/bin/ffmpeg" "$W/out/bin/ffprobe" "$D/bin/"
install -m 0644 "$W/out/COPYING.GPLv2" "$D/COPYING.GPLv2"
{
  echo "version: ffmpeg $FFV ($REV) + x264 ${X264_COMMIT:0:7},musl 完全靜態,linux arm64"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "toolchain: docker alpine:3.20(musl),$(cat "$W/out/gcc.txt")"
  echo "source: https://ffmpeg.org/releases/ffmpeg-$FFV.tar.xz"
  echo "source_sha256: $SRC_SHA"
  echo "source_signature: ffmpeg-$FFV.tar.xz.asc,FFmpeg release key FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8(Good signature)"
  echo "x264: https://code.videolan.org/videolan/x264.git @ $X264_COMMIT(stable 分支)"
  echo "license: GPL-2.0-or-later(--enable-gpl + libx264);全文見 COPYING.GPLv2"
  echo "rebuild: sftp_transfer 的 deploy/static/ffmpeg/build/README.md"
  echo
  echo "--- ffmpeg -version"
  cat "$W/out/version.txt"
  echo
  echo "--- ffmpeg -buildconf"
  cat "$W/out/buildconf.txt"
} > "$D/BUILDINFO.txt"
chmod 0644 "$D/BUILDINFO.txt"
(cd "$D" && sha256sum bin/ffmpeg bin/ffprobe > SHA256SUMS)

mkdir -p "$DEST"
rm -f "$DEST"/*.tar.xz
tar --sort=name --mtime='2026-01-01 00:00:00Z' --owner=0 --group=0 --numeric-owner \
    -C "$STAGE" -cf - "$NAME" | xz -9e -T1 > "$DEST/$NAME.tar.xz"
{
  echo "# 靜態 ffmpeg / ffprobe 離線包(linux arm64,musl 完全靜態;Ubuntu 18.04 / 22.04 共用)。"
  echo "# 由 build/package.sh 產生;怎麼重編見 build/README.md。"
  (cd "$DEST" && sha256sum "$NAME.tar.xz")
} > "$DEST/MANIFEST.txt"
ls -l "$DEST"
cat "$DEST/MANIFEST.txt"
