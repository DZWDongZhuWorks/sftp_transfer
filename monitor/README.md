# log_monitor — SFTP 傳輸 Log 監視分析工具

解析 `sftp_transfer` 車隊各設備產生的 CSV 傳輸 log，彙整成**每裝置最新更新狀態**，
輸出**終端機彩色分群列表**與**自包含 HTML 報告**，供全覽與排查各設備的更新狀況。

- 獨立、**唯讀**、僅依賴 Python 標準函式庫（分析本身不需 `paramiko`）。
- 位於 `monitor/`（與 `deploy/` 同層），可 `python monitor/log_monitor.py` 直接執行，
  亦可 `from monitor.log_monitor import ...` 匯入。

---

## 運作概念

每次下載/上傳，`sftp_transfer` 本體會產生一份 CSV log（`utf-8-sig`），內含
`timestamp,device_name,version_info,level,message`，並在 `upload_log=True` 時上傳到遠端
`log_remote_dir`（基底 `/fleet/wanhai_nssms_deploy/sftp_logs/`）。

本工具**只分析本地目錄**：先由 `sftp_transfer` 本體把遠端 `sftp_logs` 下載到本地
（或直接分析本機自產的 `logs/`），再由本工具遞迴掃描、解析、分群呈現。

裝置身分取自 `device_name`，慣例為 `{vsl_name}_{ipc}_{project}`（如 `CLINK_IPC-1_ecdis`）；
無法解析者（靜態名如 `RADAR_UPLOADER`、舊命名）歸入「（未分類）」桶。

### 狀態判定（每份 log）

| 狀態 | 條件 |
|------|------|
| `success` 正常 | 有「任務結束」行且失敗數為 0 |
| `partial` 部分失敗 | 有「任務結束」行且失敗數 > 0 |
| `aborted` 中止 | 出現「任務中止：…」 |
| `incomplete` 未完成 | 無結束行（中斷/仍在執行） |
| `stale` 過期 | 最新一次為 `success`，但距今超過 `--stale-hours`（預設 24h） |

過期只套用在原本正常的裝置；`partial/aborted/incomplete` 一律以異常優先呈現。

---

## 呈現：階層分群

層級為 **方向（下載/上傳）＞ vessel ＞ IPC ＞ project**，可折疊。
**預設：正常群組收合、異常群組（含過期/失敗/中止）自動展開**，方便上百艘船時一眼定位問題。

- **CLI**：縮排階層 + 每層摘要徽章（`裝置 N｜正常 x｜過期 y｜異常 z`）。
  健康群組只印摘要行；問題群組展開列出各 project。
- **HTML**：原生 `<details>` 巢狀折疊 + 控制列（搜尋 / 方向 / 船 / IPC / 元件 / 狀態下拉、
  「只看異常」、「全部展開 / 全部收合」）。過濾即時重算各群「（符合 N）」並隱藏空群組。
  支援淺/深色，點列可展開該裝置的錯誤/警告與近期歷史。

---

### 報告是「靜態快照」

所有計算（讀 CSV、判狀態、過期、分群、每群計數）都在 **Python 產生報告的當下**完成，
結果直接寫死進 HTML。HTML 內嵌的 JS **只做畫面互動**（搜尋 / 過濾 / 折疊 / 重算「符合 N」），
**不會讀取 `fleet_logs`、也不會重算狀態**——瀏覽器開的是某一時刻的凍結快照。
要看新資料就重跑一次工具重新產生（`--watch` 會自動每 N 秒重產並覆寫同一份 `log_monitor.html`）。

> 註：來源 CSV 會隨每次傳輸持續累積（本體是「一次執行一檔」），但監視器依
> `(device_name, mode)` **只取最新一筆**呈現，畫面每台裝置仍只有一列。

---

## 使用方式

### A) 只分析（下載已由本體/排程完成）

```bash
python monitor/log_monitor.py --log-dir logs --html
```

### TUI 互動式終端機介面（`--tui`）

想要「接近 HTML 的控制能力」但留在終端機時，加上 `--tui`（stdlib `curses`，零新依賴）：

```bash
python monitor/log_monitor.py --log-dir fleet_logs --tui
# 搭配即時監視（每 60 秒重抓遠端 log 再刷新，保留展開/選取狀態）
python monitor/log_monitor.py --sync-config config/log_monitor_sync.json \
    --log-dir fleet_logs --tui --watch 60
```

**非互動式終端機（cron／管線／`| grep`）會自動退回靜態分群輸出**，不會卡住或崩潰。

鍵位：

| 鍵 | 動作 |
|----|------|
| `↑`/`↓` 或 `k`/`j`、`PgUp`/`PgDn`、`Home`/`End` | 移動選取 |
| `Enter` / `Space` | 群組開合；在裝置列開明細 |
| `→`/`l`、`←`/`h` | 展開 / 收合（裝置列 `←` 跳回父群） |
| `E` / `C` | 全部展開 / 全部收合 |
| `/` | 搜尋（`Esc` 清除） |
| `m` / `s` / `p` | 循環方向 / 循環狀態 / 只看異常 |
| `r` | 立即重載 | 
| `?` / `q` | 說明 / 離開 |

### B) 一鍵即時監視（工具自行觸發本體下載遠端 sftp_logs 再分析）

需先備一份 download 設定檔（`remote_path` 指向遠端 `sftp_logs`、`local_path` 等於 `--log-dir`）。
本倉庫已提供 `config/log_monitor_sync.json`（`local_path=fleet_logs`）。

```bash
python monitor/log_monitor.py \
    --sync-config config/log_monitor_sync.json \
    --log-dir fleet_logs --watch 60 --html
```

每 60 秒重新下載遠端 log 再刷新彩色列表。`--watch` 若不搭配 `--sync-config`，僅重讀本地目錄。

---

## 參數

| 參數 | 說明 |
|------|------|
| `--log-dir PATH` | 要分析的本地 log 目錄（預設 `logs`）。遞迴掃描 `*.csv`，涵蓋巢狀結構。 |
| `--mode {download,upload,all}` | 只看某方向（預設 `all`）。 |
| `--stale-hours N` | 逾期告警門檻（小時，預設 24）。 |
| `--html [PATH]` | 另存 HTML 報告；不接路徑則**覆寫** `<log-dir>/log_monitor.html`（固定檔名，`--watch` 為原地刷新、不堆檔）。需保留歷史快照時改接明確路徑。 |
| `--sync-config PATH` | 分析前先用此 download 設定檔觸發本體下載遠端 log。 |
| `--watch SECONDS` | 每 N 秒清畫面刷新（搭配 `--sync-config` 才會重新下載）。 |
| `--vessel NAME` | 只顯示指定船名。 |
| `--ipc NAME` | 只顯示指定 IPC。 |
| `--component NAME` | 只顯示指定元件（project）。 |
| `--status {ok,stale,problem,all}` | 只顯示某狀態（`problem`=失敗/中止/未完成）。 |
| `--flat` | 改用舊的平面表格（不分群，方便 grep）。 |
| `--tui` | 互動式終端機介面（curses，見上節）；非 TTY 自動退回靜態輸出。 |
| `--expand-all` / `--collapsed` | 分群時強制全展開 / 全收合（覆寫預設折疊）。 |
| `--no-color` | 停用 ANSI 顏色。 |

離開碼：全部正常 → `0`；有任何非 `success`（含過期）→ `1`。

---

## 測試

```bash
pytest -q tests/test_log_monitor.py
```

測試沿用 `tests/conftest.py` 的 `tmp_path` 風格、於暫存目錄寫入含 BOM 的合成 CSV，
不需網路；`sync_logs` 以 mock `subprocess.run` 驗證。

---

## 檔案

| 檔案 | 說明 |
|------|------|
| `monitor/log_monitor.py` | 主程式（解析、分群、CLI/HTML 呈現、選配觸發下載、`--tui` 進入點）。 |
| `monitor/tui.py` | curses 互動式 TUI（`--tui`）；資料層沿用 `log_monitor`。 |
| `monitor/__init__.py` | 使 `import monitor.log_monitor` 於測試中可用。 |
| `config/log_monitor_sync.json` | 抓取遠端 `sftp_logs` 的 download 設定檔（依慣例不納入版控）。 |
| `tests/test_log_monitor.py` | 解析/分群/呈現的單元測試。 |
| `tests/test_tui.py` | TUI 純邏輯測試（flatten/reducer/按鍵映射）。 |
