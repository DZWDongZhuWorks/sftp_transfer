# 離線部署包 (Offline Deployment Bundle)

供 **完全無對外網路** 的環境安裝 `sftp_transfer` 及其相依套件之用。

## 目標平台

wheelhouse（Python 相依）與 tmux 的 `.deb` 綁定範圍**不同**，要分開看。

Python 側：

| 項目 | 值 |
|------|----|
| 作業系統 | Linux (NVIDIA Tegra, mic-733ao) |
| 架構 | `aarch64` |
| Python | CPython 3.10 (`cp310`) |
| glibc | ≥ 2.34（目標機為 2.35） |

> ⚠️ wheel 檔案與平台綁定。此包**只適用**上述平台。若要部署到不同架構
> （x86_64）或不同 Python 版本，需在對應平台重新以 `pip download` 產生 wheelhouse。

tmux 側走**雙平台 profile**，依 `/etc/os-release` 自動選擇：

| Profile | Architecture | glibc baseline | tmux payload |
|---|---:|---:|---|
| Ubuntu 18.04 Bionic | ARM64 | 2.27 | Bionic `.deb`（tmux 2.6） |
| Ubuntu 22.04 Jammy | ARM64 | 2.35 | Jammy `.deb`（tmux 3.2a） |

其他 OS、Ubuntu 版本或架構會在**任何持久變更之前**停止，不會猜測 profile。
tmux 相容層只負責選擇、驗證並安裝 tmux 的 dependency closure；不下載、不安裝、
也不限制 Python runtime —— Python 一律沿用船端映像既有的預安裝版本。

## 內容

| 檔案 / 目錄 | 說明 |
|-------------|------|
| `wheelhouse/` | 17 個預先下載的 `.whl`（paramiko 執行期堆疊 + pytest 測試工具） |
| `virtualenv_wheels/` | 建立 venv 用的 `virtualenv` 及其相依 `.whl`（供離線安裝 virtualenv） |
| `install_virtualenv_offline.sh` | 在主環境為 `python3.10` 離線安裝 `virtualenv`（deploy 需要時自動呼叫） |
| `platforms/<profile>/debs/` | 該平台的 tmux 及其依賴 `.deb`（各 4 個）+ 同目錄的 `MANIFEST.txt` |
| `platforms/<profile>/profile.env` | 該 profile 的 OS / 架構 / glibc 下限 / 套件與版本鎖定 |
| `lib/offline_common.sh` | profile 偵測與 manifest 校驗的共用函式（兩支安裝器共用） |
| `install_tmux_offline.sh` | 依偵測到的 profile 離線安裝 tmux（deploy 需要時自動呼叫） |
| `build/collect_tmux_debs_online.sh` | **僅建置機**：有網路時重新蒐集該平台的 `.deb` 並更新 manifest |
| `requirements-lock.txt` | 版本鎖定清單（可重現安裝） |
| `MANIFEST.txt` | 各 wheel 的 sha256 與建置平台資訊（安裝前完整性校驗用） |
| `deploy_offline.sh` | 離線安裝腳本（全程 `--no-index`，不連外網） |
| `health_check.py` | 安裝後能力測試 + SFTP 連線測試 + 產生健康報告 |
| `automation_health_check.py` | systemd user service、timer、linger、sudoers、heartbeat、tmux 與 unit 同步狀態的一鍵唯讀巡檢 |

```text
deploy/
├── platforms/
│   ├── ubuntu-18.04-arm64/
│   │   ├── profile.env
│   │   └── debs/             Bionic tmux closure + MANIFEST.txt
│   └── ubuntu-22.04-arm64/
│       ├── profile.env
│       └── debs/             Jammy tmux closure + MANIFEST.txt
├── lib/offline_common.sh
├── build/collect_tmux_debs_online.sh
├── install_tmux_offline.sh
└── deploy_offline.sh
```

> `wheelhouse/` 與 `virtualenv_wheels/` 內的 `.whl` 因體積較大且與平台綁定，
> 不納入 git 版控（見 `.gitignore`），須隨部署包一併實體派送到船機。
>
> `platforms/*/debs/` 內的 `.deb` 相反，**納入 git 版控**。兩個 profile 加起來
> 也只有約 1 MB，而缺 tmux 是「船上無法自救」的故障（沒有網路可以 `apt install`），
> 所以讓它跟著 repo 與 OTA 一起走，比省下這點體積重要 —— 派送時漏掉的機率降到零。
> **兩個 profile 的 `debs/` 都要隨 release 派送**，因為船機映像不保證是哪一版。

## tmux：另一個離線前置

`scheduler` 的整個開機服務模型建立在 tmux 之上：每一支
`reboot_script/start_*.sh` 以 `tmux new-session` 啟動專案，`reboot_launcher.sh` 以
`tmux has-session` 做差異對帳，`automation_health_check.py` 以「角色 → 預期 session」
清單驗收。缺 tmux 時的症狀特別難判讀：各 `start_*.sh` 分別 `exit 2`、啟動器「記錄並
繼續」、每 30 分鐘重試同一批卻永遠補不起來 —— 要交叉三份 log 才會發現原因是少了一個
指令。而船上沒有對外網路，`apt install tmux` 不成立。

所以 tmux 用與 wheelhouse 完全同構的方式處理：預先蒐集的 `.deb` 隨包派送，
`dpkg -i` 安裝，全程不連網。正式安裝路徑不呼叫 `apt update`、`apt install`、
`curl` 或 `wget`。

```bash
# 只回報現況，不做任何變更
./deploy/install_tmux_offline.sh --check-only

# 需要時安裝（deploy_offline.sh 的階段 A 會自動呼叫，不必手動跑）
./deploy/install_tmux_offline.sh

# 強制指定 profile（跳過 /etc/os-release 偵測，測試用）
./deploy/install_tmux_offline.sh --profile-dir deploy/platforms/ubuntu-18.04-arm64
```

安裝器先依 `/etc/os-release` 選出 profile，然後在動任何東西之前驗證：

- manifest 條目與實際 `.deb` inventory 完全一致；
- 每個 `.deb` 的 SHA256；
- package set、architecture 與 profile 鎖定的 tmux 版本；
- `.deb` 宣告的 glibc 下限；
- 所有 `.deb` 解到 `/tmp` 之後，該 tmux 二進位真的能建立、查詢與刪除測試 session。

payload / ABI 驗證全過之後才輪到既有 tmux：功能正常就沿用，**不強制升級**；
缺少或已壞掉才以 `dpkg -i` 安裝鎖定的 closure。

離開碼：`0` 已就緒／`1` 安裝失敗／`2` 參數錯誤／`4` 離線包不完整（未做任何變更）／
`5`（`--check-only`）待安裝／`6` 平台或 ABI 不相容。

> ⚠️ **沒有 root/sudo 時是停止，不是降級。** 舊版曾以 `dpkg-deb -x` 解到 `~/.local`
> 當後備路徑，現已移除：那條路徑會留下一個沒登錄進 dpkg 資料庫的假安裝，
> 之後的巡檢與 apt 都看不到它。缺權限請補 sudo 後重跑，不要繞過。

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

#    只校驗平台、tmux payload 與 wheel，不安裝、不修改 HOME/systemd/dpkg
./deploy/deploy_offline.sh --check-only

#    自訂 venv 路徑
./deploy/deploy_offline.sh --venv /path/to/venv

#    指定船端既有的 Python（只是選直譯器，不會安裝 Python runtime）
./deploy/deploy_offline.sh --python /absolute/path/to/python

# 2) 安裝後健康檢查（能力測試 + SFTP 連線 + 健康報告）
#    務必用 venv 內的 python 執行：
~/venv/wanhai_nssms/share/sftp_transfer/bin/python deploy/health_check.py
#    報告會寫到  logs/health_report_<時間>.md

# 3) 巡檢開機自動化、timer、heartbeat 等隱性設定（使用系統 python3 即可）
python3 deploy/automation_health_check.py
#    報告會寫到 logs/automation_health_report_<時間>.md
```

`deploy_offline.sh` 會優先使用船端的 `python3.10`，沒有時使用 `python3`。

### 部署會留下哪些檔案

`logs/` 下每次部署會多三份，三份的用途不同：

| 檔案 | 內容 |
| --- | --- |
| `deploy_offline_<時間>.log` | 部署**過程**逐字記錄（stdout + stderr 合流，已去掉顏色碼） |
| `health_report_<時間>.md` | 能力健康檢查的**結果**（`health_check.py`） |
| `automation_health_report_<時間>.md` | 自動化存活巡檢的**結果**（`automation_health_check.py`） |

兩份 Markdown 是部署完成後的狀態快照；要查「哪一步失敗、操作者選了什麼、pip 卡在哪個
wheel」只能看 `.log` 那一份，船上回報問題時寄它。路徑會印在部署總結的最後一行。
指定 `--no-health-check` 時只會少兩份 Markdown，逐字記錄照留；`--help` 與參數錯誤不留檔。
三種檔案都帶時間戳、不覆蓋、也不自動清理。

### 自動化存活巡檢

`automation_health_check.py` 是唯讀工具，不會啟停服務或修改設定。它會檢查：

- `systemctl --user` 與 linger 是否可用；
- `nssms-boot`、`nssms-heartbeat`、所有 `nssms-*` timer 的實際狀態；
- timer 對應 service 的最近執行結果與 `ExecStart` 目標是否存在；
- repo unit 母體與 `~/.config/systemd/user/` 實際安裝版本是否一致；
- sudoers 權限及 reboot / TeamViewer 的精確 NOPASSWD 白名單；
- heartbeat 實際 TCP 回應、failover 狀態與角色；
- 依 IPC 角色預期存在的 tmux sessions。

部署當下若機器剛開機（或有人另開視窗手動 `systemctl --user start nssms-boot`），
`nssms-boot` 這個 oneshot 可能還停在 `activating/start` —— 那是「這一輪還在跑」而不是
壞掉（`ExecStartPre` 有 `sleep 20`，之後還有 OTA 與全相位起服務）。巡檢會記成 `WARN`
並提示等它收工後重跑；只有卡在啟動中超過 15 分鐘才升為 `FAIL`，此時看
`scheduler/logs/launcher.log` 最後一行。**不要因為這一項就去 disable／重裝 unit**，
那動到的是全船唯一的開機入口。

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

分別在 **Bionic ARM64** 與 **Jammy ARM64** 的建置環境（有對外網路）各跑一次：

```bash
./deploy/build/collect_tmux_debs_online.sh --allow-network-build
```

腳本會自動偵測所在 profile、以當前 Ubuntu repository 下載鎖定套件、驗證名稱／
版本／架構，然後更新對應的 `platforms/<profile>/debs/` 與其 manifest。
**這支腳本只供建置機使用，不應在船機執行**（它是這整包唯一會連網的路徑，
`--allow-network-build` 就是要你按下去之前先確認自己在哪台機器上）。

兩個 profile 的產物合併進同一份 release bundle。每種目標映像至少跑一次
`--check-only`；正式 release 還應在「預先移除 tmux」的測試機上驗證
`sudo dpkg -i` 分支真的走得通。

清單就是 tmux 的 `Depends` 減去 `libc6`：

```bash
dpkg -I deploy/platforms/ubuntu-22.04-arm64/debs/tmux_*.deb | grep Depends
```

`libc6` 刻意**不打包**：tmux 只要求特定 glibc 下限，那是基底系統的東西，而用
`LD_LIBRARY_PATH` 蓋掉 libc 是會把機器弄壞的操作。若換到 libc 更舊的映像，
正確做法是新增一個 profile 或改用靜態編譯的 tmux，不是把 libc 塞進這個目錄。

`MANIFEST.txt` 刻意放在各 profile 的 `debs/` **裡面**，而不是併進
`deploy/MANIFEST.txt`：後者的校驗基準目錄是 `wheelhouse/`（見 `deploy_offline.sh`
裡的 `cd "$WHEELHOUSE"`），混進來會壞掉。

## 失敗原則

- `.deb` 缺漏、多出、hash 不符、package/version/architecture 不符：停止。
- OS / architecture / glibc 不符任何 profile：停止，不猜測。
- tmux 缺少且無 root/sudo：停止，不留下假的 rootless 安裝。
- `--check-only` 不寫 HOME、不安裝套件、不修改 systemd 或 dpkg。
