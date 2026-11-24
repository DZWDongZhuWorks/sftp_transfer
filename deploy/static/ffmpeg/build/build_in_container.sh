#!/bin/sh
# 在 alpine(musl)容器內跑:/src 原始碼(唯讀)、/out 產出、/scripts 本腳本。
# 產出完全靜態的 ffmpeg / ffprobe:不依賴 glibc,Ubuntu 18.04 / 22.04 arm64 都能跑。
set -eu
FFV=8.1.3
JOBS=10

command -v nasm >/dev/null || apk add --no-cache bash build-base nasm perl pkgconf linux-headers xz zlib-dev zlib-static >/dev/null

B=/build; mkdir -p $B; cd $B

# ---- x264(只要靜態函式庫)
cp -a /src/x264 x264; cd x264
./configure --prefix=/opt/x264 --enable-static --disable-cli --disable-opencl --enable-pic
make -j$JOBS && make install
cd $B

# ---- ffmpeg
tar xf /src/ffmpeg-$FFV.tar.xz; cd ffmpeg-$FFV
PKG_CONFIG_PATH=/opt/x264/lib/pkgconfig ./configure \
  --prefix=/opt/ffmpeg \
  --pkg-config-flags=--static \
  --extra-cflags="-I/opt/x264/include" \
  --extra-ldflags="-L/opt/x264/lib -static" \
  --extra-ldexeflags="-static" \
  --extra-version="nssms-static" \
  --disable-autodetect \
  --enable-gpl --enable-libx264 --enable-zlib \
  --enable-static --disable-shared \
  --disable-ffplay --disable-doc --disable-debug
make -j$JOBS
strip ffmpeg ffprobe

mkdir -p /out/bin
cp ffmpeg ffprobe /out/bin/
cp COPYING.GPLv2 /out/
./ffmpeg -hide_banner -version > /out/version.txt
./ffmpeg -hide_banner -buildconf > /out/buildconf.txt
gcc --version | head -n 1 > /out/gcc.txt
ls -l /out/bin
