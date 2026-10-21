# 雜湊基準回填方案(force_redownload)

> 2026-10-21 撰。數據來源:岸端 `/fleet/wanhai_nssms_deploy/STANDARD` 的唯讀 SFTP 列表(1,547 個檔)、
> 岸端歸檔的下載 log `fleet_logs/download`(2026-09-01 起,103 艘船)、`scheduler/README.md` 的 warm 預算實測。

## 結論

**只回填第 1 批(B1:程式碼、設定、小型靜態檔),每台 IPC 約 31 MB,中位數的船一輪 warm 就抓完。**
模型、wheels、venv 包(B2/B3,每台 IPC 約 1.7 GB)**不要**用 `force_redownload` 回填:中位數的船要
吃掉 15~28 天的空閒預算,而且這段期間排在大檔後面的檔(包括真正的改版)都得等。這些檔的基準應改走
「岸端參考雜湊」(見文末〈後續〉),那個方案每艘船只需要幾 KB。

| | 範圍 | 每台 IPC | 每艘船 | 中位數的船要幾天 |
|---|---|---|---|---|
| **B1(下發)** | 十份下載設定的程式碼/設定/小型 assets | 30.8 MB(IPC-3 為 7.7 MB) | ≈ 69 MB | **0.8 天** |
| B2(不下發) | radar `YOLOv7_MODEL/` `OCR_MODEL/`、ecdis `assets/models/` | 607 MB | ≈ 1.2 GB | 15 天 |
| B3(不下發) | radar/SHM 的 wheels、ecdis venv 包與字型、sftp `deploy/` 的 wheels | 1,114 MB | ≈ 2.3 GB | 28 天 |

## 回填 ≠ 驗證(先講清楚)

強制重抓下來的內容是**岸端此刻的內容**。sha256 是照收到的位元組算的:岸端那份如果本來就壞,船上
的基準就跟著壞,之後 `--verify` 只會證明「船上跟那份壞掉的一樣」。回填做的是讓**以後**的 bit rot
抓得到,不是證明**現在**的檔是對的。

而且回填會直接**覆蓋**船上的舊檔,不會先比對。船上那份如果已經壞了,我們不會知道,證據也跟著被
蓋掉。對 B1 來說這可以接受(程式碼壞了多半會大聲失敗,量也小)。這也是 B2/B3 不走這條路的第二個
理由。

## 頻寬與時間的依據

**鏈路速率**:每艘船取「≥200 KB 的檔,單檔平均速率」的中位數,再看全船隊的分布:

| p10 | p25 | p50 | p75 | p90 | 最慢 |
|---|---|---|---|---|---|
| 12.3 KB/s | 14.8 KB/s | 16.9 KB/s | 64.4 KB/s | 153.4 KB/s | WHA06 1.9 KB/s |

**可用時間**:warm 一天兩輪,每輪上限 1680 秒,所以每台 IPC 一天最多 3,360 秒。日常下載本來就要
用掉一部分(中位數每天 1,039 秒,p75 1,432 秒),剩下約 **2,300 秒/天**能拿來回填。原本就只有 24%
的 machine-day 跑得完一整輪 update cycle,能撥出來的預算並不多。

| 批次 | p10 船 | p50 船 | p90 船 | WHA06 |
|---|---|---|---|---|
| B1 30.8 MB | 0.7 h / 1.1 天 | 0.5 h / 0.8 天 | 0.1 h | 4.4 h / 6.8 天 |
| B2 607 MB | 13.4 h / 21 天 | 9.7 h / 15 天 | 1.1 h / 1.7 天 | 87 h / 135 天 |
| B3 1,114 MB | 24.6 h / 38 天 | 17.9 h / 28 天 | 2.0 h / 3.1 天 | 159 h / 247 天 |

(格式:實際傳輸時數 / 換算成空閒預算的天數。一艘船的 IPC-1 與 IPC-2 走同一條衛星鏈路、各抓一份;
上表算的是單台,兩台同時抓的時候實際速率可能更低。)

**大檔還有一個結構性問題**:warm 用完 1680 秒會停在當下那個 task,下一輪從**下一個專案**開始,所以
一個專案一個 cycle 只拿得到一段時間。radar 一顆 146 MB 的 onnx 用 16.9 KB/s 要傳 2.3 小時,也就是
5 段 radar 時段;ecdis 那包 624 MB 的 venv zip 要 10 小時、21 段。強制重下雖然能從檢查點接續,不會
白抓,但那幾週裡,同一個專案排在後面的檔都在排隊。

## 每棵樹的判斷

| 樹 | 大小/台 | 現有的偵測 | 判斷 |
|---|---|---|---|
| 各專案程式碼與設定 | ≈ 12 MB | 無(壞了多半會大聲失敗) | **B1**。量小;本來就常改版,不回填也會慢慢累積,回填只是讓 verify 第一天就有東西可驗 |
| IPC1_share(News_ticker_SDK、alarm 音檔) | 19.0 MB | 無 | **B1**。幾乎不變動的靜態檔,正是自然累積永遠輪不到的那類,而且量小 |
| radar `YOLOv7_MODEL/` `OCR_MODEL/` | 452 MB | **無**,權重壞了只會默默推論錯誤 | 最值得有基準,但不適合用重抓來回填 → 後續的岸端參考雜湊,第一優先 |
| ecdis `assets/models/` | 155 MB | 無(ecdis 沒有擋門) | 同上 |
| radar `wheels/` `virtualenv_wheels/`、SHM `wheels/` | 409 MB | `install_env.sh` 的 CRC32 擋門會發現並改名成 `.corrupt` | 已有閉環,優先度低 |
| ecdis venv zip | 624 MB | zip 自帶 CRC,解壓時會大聲失敗(只是沒有自動修復) | 優先度低 |
| ecdis `assets/fonts/` | 54 MB | 無,影響小 | 優先度低 |
| sftp `deploy/` 的 wheels | 27 MB × 3 台 | 只在離線重裝時用到 | 不回填 |

per-vessel 的 `UNIQUE/{vsl}` 樹每艘船只有 2 個小檔(實測 WH359、WH101),忽略不計。

## 要下發的設定

批次檔:[`docs/force_redownload_b1.json`](force_redownload_b1.json),批次號 `hash-baseline-b1`。以
radar 為例,套用之後設定檔會多出:

```json
"force_redownload": ["*", "!YOLOv7_MODEL/", "!YOLOv7_MODEL/**", "!OCR_MODEL/", "!OCR_MODEL/**",
                     "!wheels/", "!wheels/**", "!virtualenv_wheels/", "!virtualenv_wheels/**"],
"force_redownload_id": "hash-baseline-b1"
```

套用的是十份設定:radar、ecdis、shm、sftp、scheduler、device_monitor、IPC1_share、IPC2_nsms、
IPC3_nsms、IPC3_share。`fleet_status`、`device_monitor_reports`、`web_ota_gpg_prefix` 是資料或單一
檔,不在範圍內。

每份樣式都拿岸端的實際清單,用 `SFTPDownloader._force_matches` 本身算過:

| 設定 | B1 命中 | 不強制(留給 B2/B3) |
|---|---|---|
| radar | 119 檔 / 2.50 MB | 78 檔 |
| ecdis | 203 檔 / 2.16 MB | 13 檔 |
| shm | 95 檔 / 0.82 MB | 6 檔 |
| sftp | 122 檔 / 1.39 MB | 62 檔 |
| scheduler / device_monitor / IPC1_share / IPC2_nsms / IPC3_nsms / IPC3_share | 全部 | 0 |

**寫樣式的陷阱(已經踩過)**:`!` 排除只對**第一層**資料夾有效。`_force_matches` 會由外往內檢查
祖先資料夾,只要有一層命中就整棵算進去,這跟 git「整個資料夾被排除,底下的檔就救不回來」是同一個
行為。所以 `"*"` 配 `"!rt_enc_ias/assets/"` 完全沒有作用,因為 `rt_enc_ias/` 這一層已經被 `*` 吃掉。
ecdis 只好改用正向列舉,`tests/test_force_redownload_batch.py` 把每棵大樹都釘住了。正向列舉漏掉
之後才新增的檔也沒關係,新檔第一次下載本來就會留下基準。

## 執行步驟

1. **前提:發佈的程式碼必須 ≥ v0.27.0。** v0.26.x 會強制重抓,但略過分支會把 sha256 洗掉,只留下
   `force_id`:檔案抓了、基準沒留下,而且這個批次號已經用掉了。現在船隊全部 ≤ 0.25.4(240 台裡
   183 台是 0.25.4、42 台是 0.25.0),不認得這兩個 key,會直接忽略,不會出事。CLINK 上的
   `share/sftp_transfer` 目前落後 origin/main 8 個 commit,要先 `git pull` 到 v0.27.1。
2. 預覽,再套用(只改本機的 `config/`,密碼那幾行不會印出來):
   ```bash
   python script/force_redownload_batch.py docs/force_redownload_b1.json
   python script/force_redownload_batch.py docs/force_redownload_b1.json --apply
   ```
   `IPC3_share_download_settings.json` 原本有一處 `"..." ,`(逗號前多一個空白),寫回時會被正規化,
   所以差異會多出那一行。這是預期的。
3. 照平常的方式發佈 sftp_transfer:`script/run_sftp_upload.sh`。config 跟程式碼走同一個 task,所以不管
   這個 task 中斷在哪,船上只會落在「舊碼 + 新設定」或「新碼 + 舊設定」,兩種都不會出事。
4. 生效時間:sftp_transfer 的 self-update 排在 warm queue 第一個,所以 12 小時內各船就會拿到新設定;
   各專案的強制重抓在它們各自輪到時才發生,幾天內收斂。
5. 觀察:
   - 岸端 `sftp_logs/download/<船>/<IPC>/<專案>/D_*.csv` 裡的 `[FORCE_REDOWNLOAD]` 行,
     `force_id="hash-baseline-b1"`。
   - `nssms-verify-downloads`(scheduler v0.68.0)每天的 `[VERIFY_SUMMARY]`。B1 收斂後,`no_baseline`
     應該只剩上表「不強制」那一欄(radar ≈ 78、ecdis ≈ 13、shm ≈ 6、sftp ≈ 62,其餘 0)。實際數字
     會再多一點:manifest 不剪枝,遠端已經刪掉的檔永遠留在 `no_baseline` 裡。

## 收尾:誰拿掉、什麼時候

有批次號,這份設定留在船上不會再產生流量;每個檔抓完就蓋上 `force_id`,之後照常略過。不過它還是
要拿掉,原因有二:

- 有人照 README 的說法刪掉某台的 `.sftp_download_manifest.json` 來「重置」的話,那台的 B1 範圍會
  整份再抓一次(≤ 31 MB,不嚴重,但很意外)。
- 下一批要換新的批次號跟樣式。同一份設定裡留著舊批次,很容易被誤當成「還在跑」。

**負責人:發佈 sftp_transfer 的人(CLINK 上跑 `run_sftp_upload.sh` 的那位)。** 時機取兩者先到:
發佈後 4 週,或 ≥ 90% 已升到 v0.27 的機器 `no_baseline` 降到預期殘量。做法:

```bash
python script/force_redownload_batch.py docs/force_redownload_b1.json --clear --apply
```

然後照常發佈。忘了拿掉也只是上面兩點,不會變成全船隊重抓。

## 已知限制

- **升不上 v0.27 的機器拿不到基準。** 停在 0.25.0 的那 42 台,如果就是 self-update 永久失敗的那批
  (Ubuntu 18.04,待確認),它們會忽略這份設定,verify 那邊也一直是 `no_baseline`。
- **同大小的船上手改會被蓋掉。** B1 範圍包含各專案的 `config/`。一般 OTA 只會蓋掉「大小不同」的
  本地改動;強制重下連同大小的也蓋。船上本來就不該手改這些檔,但如果有,這次就會消失。
- **跑起來的服務不受影響。** 寫入方式是先寫 `.part` 再 `os.replace`,跟一般 OTA 相同。wheels 的指紋
  只看檔名加大小,B1 也不碰 wheels,所以不會觸發 env 重建。

## 後續:B2/B3 改走岸端參考雜湊(另開任務)

拿岸端那份的 sha256 跟船上的檔比對,不搬檔案:

1. 在 CLINK(辦公室鏈路)對**岸端 STANDARD 的實際內容**算 sha256,不是對 CLINK 的工作樹。產出一份
   參考清單(B2 + B3 約 90 筆,10 KB 上下),隨 sftp_transfer 發佈。
2. 船端:相符的 → 寫進 manifest 當基準(讀一次本地檔,零傳輸);不符的 → 這才是**真的抓到**壞檔,
   只對那幾個檔強制重下。

跟 B1 的強制重抓比,這個做法每艘船省下 1.2~2.3 GB,而且**會發現**船上現有的壞檔,不是默默蓋掉。
「岸端本身壞了就抓不到」的限制一樣存在。

另一條路是現成的:`VERSION.stamp.json` 已經帶著每個程式碼檔在發佈端算好的 sha256,只是 radar 的
`stamp_exclude` 把 `wheels/`、`YOLOv7_MODEL/` 排除了。排除的理由寫的是「船上每次開機驗證要重讀
好幾百 MB」,但目前船上讀 stamp 的只有版號顯示(`start_radar.sh`、dashboard),找不到開機驗證。
如果拿掉排除,這兩棵樹的雜湊就免費跟著上船。缺點是它代表的是 CLINK 工作樹,不是岸端的位元組。
