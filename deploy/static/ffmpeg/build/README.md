# 靜態 ffmpeg 離線包怎麼編

`deploy/static/ffmpeg/*.tar.xz` 是在開發機(CLINK,Jetson AGX Orin,aarch64)上用這三支腳本從**官方原始碼**編出來的,
不是第三方預編檔。由 `deploy/install_ffmpeg_offline.sh`(`deploy_offline.sh` 的 A8d)裝到船上的 `/usr/local/bin`。

| 項目 | 內容 |
|---|---|
| FFmpeg | 8.1.3(2026-09-21),`https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz`,以 FFmpeg release key `FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8` 驗 GPG 簽章 |
| x264 | `https://code.videolan.org/videolan/x264.git` stable 分支 `b35605ace3ddf7c1a5d67a2eb553f034aef41d55`(2025-06-08) |
| 工具鏈 | docker `alpine:3.20`(musl),gcc 13.2 |
| 連結 | **完全靜態**(`-static`,沒有 PT_INTERP):不依賴 glibc,Ubuntu 18.04(glibc 2.27)與 22.04 同一支 |
| 功能 | FFmpeg 內建的全部編解碼器 / 封裝 / 濾鏡 / 網路協定(RTSP 等)+ `libx264`(含 `libx264rgb`)+ zlib;`--disable-autodetect`,沒有其他外部函式庫(x265、vpx、硬體加速都沒有);不含 ffplay |
| 授權 | GPL-2.0-or-later(`--enable-gpl` + libx264)。壓縮檔內附 `COPYING.GPLv2` 與 `BUILDINFO.txt`(原始碼 URL / sha256 / configure 參數);裝到船上後在 `/usr/local/share/nssms-ffmpeg/` |

**為什麼用 musl 而不是 glibc 靜態連結**:glibc 的靜態執行檔做名稱解析(`getaddrinfo`,連 `/etc/hosts` 都算)時仍會
dlopen 系統上**同版本**的 NSS 函式庫,版本不同可能崩潰 —— 船隊同時有 glibc 2.27 與 2.35。musl 自己讀
`/etc/hosts` / `resolv.conf`,沒有這個問題。

## 重編

```bash
W=<NVMe 上的工作目錄>      # 根目錄(eMMC)空間不夠,原始碼與建置都放 NVMe
mkdir -p $W/scripts $W/src $W/out
cp fetch.sh build_in_container.sh package.sh $W/scripts/

# 1) 下載原始碼、驗 GPG 簽章、clone x264 到固定 commit(要網路)
bash $W/scripts/fetch.sh

# 2) 在 alpine 容器裡編(Orin 12 核約 10 分鐘;apk 從 Alpine CDN 下載工具鏈另計)
docker run --rm -v $W/src:/src:ro -v $W/out:/out -v $W/scripts:/scripts:ro \
    alpine:3.20 sh /scripts/build_in_container.sh
sudo chown -R "$(id -u):$(id -g)" $W/out

# 3) 打包到這個目錄(覆寫舊的 .tar.xz 與 MANIFEST.txt)
bash $W/scripts/package.sh $W <sftp_transfer>/deploy/static/ffmpeg

# 4) 驗證:真的裝一次到暫存 prefix、1 px 線條逐位元無損、靜態連結
python3 -m pytest tests/test_ffmpeg_offline.py -v
```

注意:

- **開發機的 `core.autocrlf=true`**:`fetch.sh` 用 `git clone -c core.autocrlf=false`,並檢查 x264 checkout 沒有
  CRLF。少了這道,x264 的 `configure` 會是 `#!/bin/bash\r`,容器裡報 `./configure: not found`(實際踩過)。
- 升 FFmpeg / x264 版本時改三支腳本開頭的版號 / commit;`package.sh` 的 `REV` 從 `nssms1` 重新起算,同一個上游版本
  重編(例如改 configure)才 +1。
- tar 以固定排序 / mtime / owner 打包,同一份 `out/` 包出來位元相同;但 gcc 的輸出不保證跨次建置位元相同,
  所以重編後 sha256 會變 —— 那就是一次新版,照常隨 sftp_transfer 升版派送。
- 船上判斷「已是同一版」看的是壓縮檔內 `SHA256SUMS` 與已裝執行檔的 sha256,所以換版後每台船會在下一次
  部署時自動換上新的(一次 OTA 約一個壓縮檔的大小)。
