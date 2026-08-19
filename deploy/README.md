# 離線部署包 (Offline Deployment Bundle)

供 **完全無對外網路** 的環境安裝 `sftp_transfer` 及其相依套件之用。

## 目標平台

**Python 相依與 tmux 都走 profile**，依 `/etc/os-release` 自動選擇，兩個平台各自獨立：

| Profile | Arch | glibc | 船端 Python | tmux | wheelhouse 標籤 |
|---|---|---:|---|---|---|
| `ubuntu-18.04-arm64` | ARM64 | 2.27 | CPython 3.6 (`cp36`) | 2.6 | cp36 / manylinux2014 |
| `ubuntu-22.04-arm64` | ARM64 | 2.35 | CPython 3.10 (`cp310`) | 3.2a | cp310 / manylinux_2_34 |

其他 OS、Ubuntu 版本或架構會在**任何持久變更之前**停止，不會猜測 profile。

兩個平台的 wheelhouse 內容完全不同，這不是疏漏而是必然：

| 套件 | Jammy (py3.10) | Bionic (py3.6) | 為什麼不能共用 |
|---|---|---|---|
| paramiko | 5.0.0 | 3.5.1 | paramiko 5 要求 `>=3.9` |
| cryptography | 49.0.0 | 40.0.2 | 49 要求 `>=3.9` 且 `glibc>=2.34` |
| cffi | 2.1.0 | 1.15.1 | cffi 2.x 要求 `>=3.10` |
| pytest | 9.1.1 | 7.0.1 | pytest 8+ 要求 `>=3.8` |

Bionic 也沒有任何真的 `exceptiongroup`（backport 需要 `>=3.7`，PyPI 上給 3.6 的只有一個
`0.0.0a0` 佔位套件）。測試堆疊因此按**實際存在**的檔案挑選，缺項只 warn 不中止 ——
它不是船上跑服務的必要條件；執行期相依則相反，缺一個就在 preflight 擋下。

> ⚠️ **Bionic 的 crypto 堆疊是死路。** `cryptography 40.0.2` 是最後一個支援 3.6 的版本，
> 匯入時自己就會警告下一版將移除 3.6。那些船拿不到 SSH/crypto 的後續安全更新 ——
> 要脫離只能把船端 Python 升上去，而本部署包**刻意不攜帶 Python runtime**
> （`tests/test_offline_deploy.py` 有測試釘住這個契約），所以那是另一個決定。

Python runtime 本身一律沿用船端映像既有的預安裝版本：不下載、不安裝、不限制。

## 內容

| 檔案 / 目錄 | 說明 |
|-------------|------|
| `platforms/<profile>/wheelhouse/` | **該平台**的 `.whl` + 同目錄的 `MANIFEST.txt`（Jammy 17 個 / Bionic 19 個） |
| `platforms/<profile>/debs/` | 該平台的 tmux 及其依賴 `.deb`（各 4 個）+ 同目錄的 `MANIFEST.txt` |
| `platforms/<profile>/profile.env` | 該 profile 的 OS / 架構 / glibc 下限 / 套件與版本鎖定 |
| `virtualenv_wheels/` | 建立 venv 用的 `virtualenv` 及其相依 `.whl`；**共用**（見下方說明） |
| `install_virtualenv_offline.sh` | 為船端 Python 離線安裝 `virtualenv`（deploy 需要時自動呼叫） |
| `lib/offline_common.sh` | profile 偵測與 manifest 校驗的共用函式 |
| `lib/wheel_compat.py` | preflight 守門：wheelhouse 的 wheel 標籤 vs 船端直譯器／glibc／架構 |
| `install_tmux_offline.sh` | 依偵測到的 profile 離線安裝 tmux（deploy 需要時自動呼叫） |
| `build/collect_tmux_debs_online.sh` | **僅建置機**：有網路時重新蒐集該平台的 `.deb` 並更新 manifest |
| `requirements-lock.txt` | Jammy 的版本鎖定清單（可重現安裝） |
| `wheelhouse/` `MANIFEST.txt` | **legacy 過渡**：已派送到船上的舊離線包形狀，仍可用但會 warn |
| `deploy_offline.sh` | 離線安裝腳本（全程 `--no-index`，不連外網） |
| `health_check.py` | 安裝後能力測試 + SFTP 連線測試 + 產生健康報告 |
| `automation_health_check.py` | systemd user service、timer、linger、sudoers、heartbeat、tmux 與 unit 同步狀態的一鍵唯讀巡檢 |

```text
deploy/
├── platforms/
│   ├── ubuntu-18.04-arm64/
│   │   ├── profile.env
│   │   ├── debs/             Bionic tmux closure + MANIFEST.txt
│   │   └── wheelhouse/       cp36 輪子        + MANIFEST.txt
│   └── ubuntu-22.04-arm64/
│       ├── profile.env
│       ├── debs/             Jammy tmux closure + MANIFEST.txt
│       └── wheelhouse/       cp310 輪子       + MANIFEST.txt
├── virtualenv_wheels/        共用的 bootstrap（全部 Requires-Python >=3.6）
├── lib/{offline_common.sh,wheel_compat.py}
├── build/collect_tmux_debs_online.sh
├── install_tmux_offline.sh
└── deploy_offline.sh
```

`wheelhouse` 與 `debs` 刻意同構：**各 profile 一份，manifest 放在該目錄裡面**。
校驗的基準目錄就是 wheelhouse 自己，共用一份 manifest 在換平台時必然對不上。

`virtualenv_wheels/` 目前**共用**：那一組（virtualenv 20.17.1 / filelock 3.4.1 /
importlib_metadata 4.8.3 / zipp 3.6.0 …）全部宣告 `Requires-Python >=3.6`，剛好兩個平台
都吃得下 —— 這是刻意選的釘法，不是巧合，升版時要保持。哪天某個 profile 需要自己一份，
放 `platforms/<profile>/virtualenv_wheels/` 就會被自動挑走（`resolve_wheelhouse`）。

> `wheelhouse/` 與 `virtualenv_wheels/` 內的 `.whl` 因體積較大且與平台綁定，
> 不納入 git 版控（見 `.gitignore` 的 `*.whl`），須隨部署包一併實體派送到船機。
> 但**各 profile 的 `wheelhouse/MANIFEST.txt` 納入版控** —— 它就是「這個平台該有哪些
> 輪子、sha256 是什麼」的權威清單，派送漏檔時由它抓出來。
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

每個 profile **各自一份**。不需要真的有一台該平台的機器 —— `pip download` 的
`--python-version` / `--abi` / `--platform` 可以在任何有網路的機器上解出目標平台的閉包
（`--only-binary=:all:` 是必要的：它強迫 pip 只挑 wheel，不會退回在本機編譯 sdist 而
產生一個只能在本機用的產物）。

Jammy（cp310 / glibc ≥ 2.34）：

```bash
PROF=deploy/platforms/ubuntu-22.04-arm64/wheelhouse
pip3 download --only-binary=:all: \
  --python-version 3.10 --implementation cp --abi cp310 --platform manylinux_2_34_aarch64 \
  -d "$PROF" paramiko pytest pytest-cov
```

Bionic（cp36 / glibc ≥ 2.17，Bionic 實際是 2.27）：

```bash
PROF=deploy/platforms/ubuntu-18.04-arm64/wheelhouse
pip3 download --only-binary=:all: \
  --python-version 3.6 --implementation cp --abi cp36m --platform manylinux2014_aarch64 \
  -d "$PROF" paramiko pytest pytest-cov
```

兩者都要重新產生同目錄的 manifest：

```bash
cd "$PROF" && sha256sum *.whl > MANIFEST.txt   # 檔頭註解請保留/補上
```

驗收：直接問守門，不要靠肉眼看檔名。

```bash
python3 deploy/lib/wheel_compat.py --py 3.6 --glibc 2.27 --arch aarch64 \
  deploy/platforms/ubuntu-18.04-arm64/wheelhouse \
  paramiko bcrypt cryptography pynacl cffi pycparser
# rc=0 相容／4 缺漏或缺必要套件／6 有輪子與目標平台不相容
```

> ⚠️ **`--python-version` 不會改變 environment marker 的求值** —— 這是 Bionic 那份
> wheelhouse 少掉 `importlib-metadata` 的真正原因（WHA03 IPC-3，2026-08-19）。
> `pip download --python-version 3.6` 只換掉「挑 wheel 的 tag」，相依的 marker 仍然用
> **執行 pip 的那個直譯器**去算：建置機是 3.10，於是
>
> ```
> importlib-metadata>=0.12 ; python_version < "3.8"      ← pytest 7.0.1 / pluggy 1.0.0
> ```
>
> 這一條被判成「不需要」，整包就這樣少了兩顆（`importlib-metadata`、它要的 `zipp`）。
> 在建置機上完全看不出來，一路要到船上真的跑 `pip install` 才爆，而那時階段 A 已經改完
> systemd / sudoers / tmux。（實測：pip 22.0.2；`pip download --no-index` 對著**已經帶了**
> `importlib_metadata` 的 wheelhouse 解 3.6 的相依，仍然不會挑它。）
>
> 所以 Bionic 那份**必須**明列這兩顆（版本是各自最後支援 3.6 的）：
>
> ```bash
> pip download 'importlib-metadata==4.8.3' 'zipp==3.6.0' --no-deps \
>   --only-binary=:all: --python-version 36 -d "$PROF"
> ```
>
> 驗收不要只靠 `wheel_compat.py`（它看的是 tag 與 glibc，看不見缺哪一顆間接相依）：
>
> ```bash
> python3 -m pytest tests/test_offline_deploy.py::WheelhouseClosureTests -q
> ```
>
> 那一組會把每個 wheel 的 `METADATA` 讀出來、用該 profile 的 Python 版本評估 marker、
> 一路展開到不動點，缺件與版本不合都會紅。它是唯一在**開發機上**就能看見這個形狀的檢查。

> ⚠️ Bionic 那份**不要**用 `pip download exceptiongroup`。真正的 backport 需要 `>=3.7`，
> pip 在 3.6 下會挑到一個 `0.0.0a0` 佔位套件，還會把 trio / outcome / sniffio 一整串
> 拖進 wheelhouse。它本來就不該在 Bionic 的清單裡。

> ⚠️ Bionic 那份**必須**包含 `dataclasses`（`0.8`，純 python）。`dataclasses` 是 3.7 才
> 進標準庫，而 `monitor/log_monitor.py`、`monitor/tui.py`、`run_selected_transfers.py`、
> `pack_upload.py` 都用 `@dataclass` —— 少了它，那四支人工工具在 Bionic 的 3.6 venv 上
> 一律 `ModuleNotFoundError`（在 Bionic 開發機實測確認）。重建時記得帶上：
>
> ```bash
> pip download 'dataclasses==0.8' --no-deps --only-binary=:all: \
>   --python-version 36 -d "$PROF"
> ```
>
> Jammy 那份**不要**放它：3.10 已內建，而 `dataclasses==0.8` 的 `python_requires` 是
> `>=3.6,<3.7`，pip 在 3.10 上本來就會拒絕。`deploy_offline.sh` 對它走「wheelhouse 有
> 才裝」（`BACKPORT_PKGS`），所以同一份安裝清單在兩個 profile 上都正確。
> `tests/test_offline_deploy.py` 有一條斷言把「程式碼用了 backport」與「Bionic 的
> MANIFEST 有那個輪子」綁在一起 —— 漏帶就會紅。

### `virtualenv_wheels/`（兩個 profile 共用）

這一組是「在主環境（非 venv）離線補上 `virtualenv`」用的，由
`install_virtualenv_offline.sh` 以 `pip install --user --no-index --find-links` 安裝 ——
它是船上**每一個** venv 的前提。目前一份共用即可：全部宣告 `Requires-Python >=3.6`，
Bionic 的 3.6.9 與 Jammy 的 3.10 都吃得下（要為某個 profile 另備一份就放
`platforms/<profile>/virtualenv_wheels/`，會自動被挑走）。

```bash
pip download virtualenv --no-deps --only-binary=:all: --python-version 36 \
  -d deploy/virtualenv_wheels     # 相依（distlib / filelock / platformdirs /
                                  # importlib-metadata / importlib-resources /
                                  # zipp / typing-extensions）也要一併抓
cd deploy/virtualenv_wheels && sha256sum *.whl > MANIFEST.txt   # 檔頭註解請保留/補上
```

> `MANIFEST.txt` 不是可選的。OTA 走 SFTP，少送或截斷一個檔的話，失敗會晚到 pip 解析相依
> 那一刻才以「找不到相依」浮出來 —— 而那時 `deploy_offline.sh` 已經動過機器了。preflight
> 會在**確定需要 bootstrap 時**校驗它（已經有 `virtualenv` 的機器走略過，不會被卡住），
> 用的是與 tmux debs 同一支 `nssms_verify_flat_manifest`。舊離線包沒有這份 manifest 時只
> 會 warn，不會讓那些包一次全部失效。

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

## preflight 為什麼要驗 wheel 標籤

`lib/wheel_compat.py` 在**階段 A 之前**比對 wheelhouse 與船端直譯器／glibc／架構。
這一項不是「多一層保險」，它擋的是一個具體且已在實機重現過的故障：

一台 Bionic（py3.6 / glibc 2.27）對著 cp310 的 wheelhouse 跑 `--check-only`，
舊版會回 **EXIT=0**、印出「全部通過」——`deploy_offline.sh` 一直都算得出 `cp36` 這個標籤，
但只印出來、從不比對；wheelhouse 是空的也不會有人吭聲。於是：

1. 階段 A 全部成功 —— systemd unit、sudoers、docker 群組、tmux 都已落地
2. 腳本印出「以下不再需要任何輸入，可以離開終端機」，操作者離開
3. 階段 B 的 `pip` 才失敗，`exit $PIP_RC`，階段 C 完全不執行

最糟的是第 3 步留下的東西：**venv 建好了、套件沒裝完**。兩支 OTA 腳本
（`run_sftp_self_update.sh` / `run_scheduler_download.sh`）都以這個 venv 的 python 執行，
它們的守門是 `[ ! -x "$VENV_PY" ]` —— venv 在、python 可執行，守門過得去，
然後在 `import paramiko` 炸掉。而 `update_booster.sh` 註解寫得很清楚：
`run_scheduler_download.sh` 是 `share/scheduler` 的**唯一** OTA 路徑。

結果是那條船每次開機都忠實地重試、永遠補不起來，岸上推的任何修正都到不了它，
只能派人帶正確的 wheelhouse 上船。

擋在階段 A 之前，最壞情況就只是「還沒部署」——而不是「部署到一半且失去自救能力」。
這兩個狀態的救援成本差一個量級。

## 失敗原則

- wheelhouse 的 wheel 與船端 Python／glibc／架構不符：停止（**在任何持久變更之前**）。
- wheelhouse 缺少任一執行期相依（paramiko 堆疊）：停止。測試堆疊缺項只 warn。
- `.deb` 缺漏、多出、hash 不符、package/version/architecture 不符：停止。
- OS / architecture / glibc 不符任何 profile：停止，不猜測。
- tmux 缺少且無 root/sudo：停止，不留下假的 rootless 安裝。
- `--check-only` 不寫 HOME、不安裝套件、不修改 systemd 或 dpkg。
