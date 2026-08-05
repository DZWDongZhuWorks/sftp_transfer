# 離線部署包 (Offline Deployment Bundle)

供 **完全無對外網路** 的環境安裝 `sftp_transfer` 及其相依套件之用。

## 目標平台

| 項目 | 值 |
|------|----|
| 作業系統 | Linux (NVIDIA Tegra, mic-733ao) |
| 架構 | `aarch64` |
| Python | CPython 3.10 (`cp310`) |
| glibc | ≥ 2.34（目標機為 2.35） |

> ⚠️ wheel 檔案與平台綁定。此包**只適用**上述平台。若要部署到不同架構
> （x86_64）或不同 Python 版本，需在對應平台重新以 `pip download` 產生 wheelhouse。
>
> ⚠️ `debs/` 內的 `.deb`（tmux 及其依賴）同樣平台綁定，目標是 Ubuntu 22.04
> (jammy) / arm64。換架構或換發行版需重新蒐集，見下方「未來如何更新 / 重建 debs」。

## 內容

| 檔案 / 目錄 | 說明 |
|-------------|------|
| `wheelhouse/` | 17 個預先下載的 `.whl`（paramiko 執行期堆疊 + pytest 測試工具） |
| `virtualenv_wheels/` | 建立 venv 用的 `virtualenv` 及其相依 `.whl`（供離線安裝 virtualenv） |
| `install_virtualenv_offline.sh` | 在主環境為 `python3.10` 離線安裝 `virtualenv`（deploy 需要時自動呼叫） |
| `debs/` | tmux 及其依賴的 `.deb`（4 個，約 620 KB）+ `MANIFEST.txt` |
| `install_tmux_offline.sh` | 以 `debs/` 離線安裝 tmux（deploy 需要時自動呼叫） |
| `requirements-lock.txt` | 版本鎖定清單（可重現安裝） |
| `MANIFEST.txt` | 各 wheel 的 sha256 與建置平台資訊（安裝前完整性校驗用） |
| `deploy_offline.sh` | 離線安裝腳本（全程 `--no-index`，不連外網） |
| `health_check.py` | 安裝後能力測試 + SFTP 連線測試 + 產生健康報告 |
| `automation_health_check.py` | systemd user service、timer、linger、sudoers、heartbeat、tmux 與 unit 同步狀態的一鍵唯讀巡檢 |

> `wheelhouse/` 與 `virtualenv_wheels/` 內的 `.whl` 因體積較大且與平台綁定，
> 不納入 git 版控（見 `.gitignore`），須隨部署包一併實體派送到船機。
>
> `debs/` 內的 `.deb` 相反，**納入 git 版控**。它們一共只有約 620 KB，而缺 tmux
> 是「船上無法自救」的故障（沒有網路可以 `apt install`），所以讓它跟著 repo 與 OTA
> 一起走，比省下這點體積重要 —— 派送時漏掉的機率降到零。

## tmux：另一個離線前置

`scheduler` 的整個開機服務模型建立在 tmux 之上：每一支
`reboot_script/start_*.sh` 以 `tmux new-session` 啟動專案，`reboot_launcher.sh` 以
`tmux has-session` 做差異對帳，`automation_health_check.py` 以「角色 → 預期 session」
清單驗收。缺 tmux 時的症狀特別難判讀：各 `start_*.sh` 分別 `exit 2`、啟動器「記錄並
繼續」、每 30 分鐘重試同一批卻永遠補不起來 —— 要交叉三份 log 才會發現原因是少了一個
指令。而船上沒有對外網路，`apt install tmux` 不成立。

所以 tmux 用與 wheelhouse 完全同構的方式處理：預先蒐集的 `.deb` 隨包派送，
`dpkg -i` 安裝，全程不連網。

```bash
# 只回報現況，不做任何變更
./deploy/install_tmux_offline.sh --check-only

# 需要時安裝（deploy_offline.sh 的階段 A 會自動呼叫，不必手動跑）
./deploy/install_tmux_offline.sh
```

兩條安裝路徑：

- **主路徑** `sudo dpkg -i` —— 正式登錄進 dpkg 資料庫，之後 apt 也認得。
- **後備路徑** `dpkg-deb -x` 解到 `~/.local` —— 免 root，用於沒有密碼可輸入的場合
  （非互動終端機）。systemd user manager 的預設 PATH 已含 `~/.local/bin`
  （systemd ≥ 248），所以 `nssms-boot` 找得到它，不需要改任何 unit 檔。

離開碼：`0` 已就緒／`1` 安裝失敗／`2` 參數錯誤／`3` 已以免 root 方式裝好／
`4` 離線包不完整（未做任何變更）／`5` （`--check-only`）待安裝。

安裝器只裝**真正缺的**套件：`libtinfo6` 這類核心程式庫在機上幾乎一定已是同版本，
對運行中的機器做無謂 reinstall 只是白白製造風險。例外是 tmux 本身 —— 若它在 dpkg
資料庫裡卻跑不起來，就會被強制重裝。

## 安裝目標：專屬 venv

sftp_transfer 使用**專屬虛擬環境**（與 radar / SHM 等其他專案慣例一致），
預設路徑：

```
~/venv/wanhai_nssms/share/sftp_transfer
```

此 venv 與系統 site-packages 隔離（`include-system-site-packages = false`），
不會污染主環境，也不受主環境套件版本影響。

## 使用方式

```bash
# 1) 建立/更新專屬 venv 並離線安裝（執行期相依）
./deploy/deploy_offline.sh

#    一併安裝測試工具（pytest 等）
./deploy/deploy_offline.sh --with-tests

#    砍掉重建 venv（乾淨安裝）
./deploy/deploy_offline.sh --recreate --with-tests

#    只校驗 wheel 與環境、不安裝
./deploy/deploy_offline.sh --check-only

#    自訂 venv 路徑
./deploy/deploy_offline.sh --venv /path/to/venv

# 2) 安裝後健康檢查（能力測試 + SFTP 連線 + 健康報告）
#    務必用 venv 內的 python 執行：
~/venv/wanhai_nssms/share/sftp_transfer/bin/python deploy/health_check.py
#    報告會寫到  logs/health_report_<時間>.md

# 3) 巡檢開機自動化、timer、heartbeat 等隱性設定（使用系統 python3 即可）
python3 deploy/automation_health_check.py
#    報告會寫到 logs/automation_health_report_<時間>.md
```

### 自動化存活巡檢

`automation_health_check.py` 是唯讀工具，不會啟停服務或修改設定。它會檢查：

- `systemctl --user` 與 linger 是否可用；
- `nssms-boot`、`nssms-heartbeat`、所有 `nssms-*` timer 的實際狀態；
- timer 對應 service 的最近執行結果與 `ExecStart` 目標是否存在；
- repo unit 母體與 `~/.config/systemd/user/` 實際安裝版本是否一致；
- sudoers 權限及 reboot / TeamViewer 的精確 NOPASSWD 白名單；
- heartbeat 實際 TCP 回應、failover 狀態與角色；
- 依 IPC 角色預期存在的 tmux sessions。

wave 目前可空置，因此預設把 wave 腳本缺少或 service 失敗列為 `SKIP`，不影響整體
健康。需要將 wave 納入正式驗收時，可使用嚴格模式：

```bash
python3 deploy/automation_health_check.py --strict-wave
```

其他選項：

```bash
python3 deploy/automation_health_check.py --no-report     # 不寫 Markdown 報告
python3 deploy/automation_health_check.py --compact       # 只顯示 WARN/FAIL 與總結
python3 deploy/automation_health_check.py --fail-on-warn  # 有 WARN 時回傳 exit 2
```

離開碼：`0` 表示沒有 FAIL；`1` 表示至少一項 FAIL；搭配 `--fail-on-warn` 時，
只有 WARN 而無 FAIL 會回傳 `2`。

`deploy_offline.sh` 會在部署流程最後自動以 `--compact --fail-on-warn` 執行本
巡檢，並把 HEALTHY／DEGRADED／UNHEALTHY 加入部署總結；完整明細仍會寫入
Markdown 報告。指定 `deploy_offline.sh
--no-health-check` 時，能力健康檢查與自動化存活巡檢都會略過。

## 執行本工具

```bash
# 啟用 venv 後執行
source ~/venv/wanhai_nssms/share/sftp_transfer/bin/activate
python main.py --cli

# 或不啟用、直接用 venv 的絕對路徑（適合排程 crontab）
~/venv/wanhai_nssms/share/sftp_transfer/bin/python \
    /home/mic-733ao/Documents/wanhai_nssms/share/sftp_transfer/main.py --cli
```

> 離線建立 venv 改用 `python3.10 -m virtualenv`（與 radar / SHM 一致），
> 不再依賴系統的 `python3-venv` / `ensurepip`。若 `python3.10` 尚未安裝
> `virtualenv`，`deploy_offline.sh` 會自動呼叫隨附的 `install_virtualenv_offline.sh`
> 以 `virtualenv_wheels/` 離線補齊，全程不需連網。

## 未來如何更新 / 重建 wheelhouse

須在**具網路且與目標同平台**的機器上執行：

```bash
pip3 download -r requirements.txt      --only-binary=:all: -d deploy/wheelhouse
pip3 download "pytest>=7.4" "pytest-cov>=4.1" --only-binary=:all: -d deploy/wheelhouse
# 重新產生 MANIFEST.txt：
cd deploy/wheelhouse && sha256sum *.whl > ../MANIFEST.txt   # （檔頭註解可自行補上）
```

## 未來如何更新 / 重建 debs

同樣須在**具網路且與目標同平台**（Ubuntu 22.04 / arm64）的機器上執行：

```bash
cd deploy/debs && apt-get download tmux libevent-core-2.1-7 libtinfo6 libutempter0
sha256sum *.deb >> MANIFEST.txt   # 檔頭註解保留，只換掉 hash 那幾行
```

清單就是 tmux 的 `Depends` 減去 `libc6`：

```bash
dpkg -I deploy/debs/tmux_*.deb | grep Depends
```

`libc6` 刻意**不打包**：tmux 只要求 `libc6 >= 2.34`，那是基底系統的東西，而在免 root
路徑上用 `LD_LIBRARY_PATH` 蓋掉 libc 是會把機器弄壞的操作。若換到 libc 更舊的映像，
正確做法是換映像或改用靜態編譯的 tmux，不是把 libc 塞進這個目錄。

`MANIFEST.txt` 刻意放在 `debs/` **裡面**，而不是併進 `deploy/MANIFEST.txt`：後者的
校驗基準目錄是 `wheelhouse/`（見 `deploy_offline.sh` 裡的 `cd "$WHEELHOUSE"`），
混進來會壞掉。
