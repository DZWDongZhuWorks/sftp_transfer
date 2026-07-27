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

## 內容

| 檔案 / 目錄 | 說明 |
|-------------|------|
| `wheelhouse/` | 17 個預先下載的 `.whl`（paramiko 執行期堆疊 + pytest 測試工具） |
| `virtualenv_wheels/` | 建立 venv 用的 `virtualenv` 及其相依 `.whl`（供離線安裝 virtualenv） |
| `install_virtualenv_offline.sh` | 在主環境為 `python3.10` 離線安裝 `virtualenv`（deploy 需要時自動呼叫） |
| `requirements-lock.txt` | 版本鎖定清單（可重現安裝） |
| `MANIFEST.txt` | 各 wheel 的 sha256 與建置平台資訊（安裝前完整性校驗用） |
| `deploy_offline.sh` | 離線安裝腳本（全程 `--no-index`，不連外網） |
| `health_check.py` | 安裝後能力測試 + SFTP 連線測試 + 產生健康報告 |
| `automation_health_check.py` | systemd user service、timer、linger、sudoers、heartbeat、tmux 與 unit 同步狀態的一鍵唯讀巡檢 |

> `wheelhouse/` 與 `virtualenv_wheels/` 內的 `.whl` 因體積較大且與平台綁定，
> 不納入 git 版控（見 `.gitignore`），須隨部署包一併實體派送到船機。

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
