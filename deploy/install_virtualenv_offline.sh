#!/usr/bin/env bash
set -euo pipefail

# install_virtualenv_offline — 為 deploy_offline 選定的系統 Python 離線補 virtualenv
#
# 在主環境（非虛擬環境）安裝 virtualenv，供 sftp_transfer 與後續專案建立各自環境。
# 呼叫端以 PYTHON_BIN 傳入本次 profile 選定的精確直譯器；Bionic IPC3 可能是 Python 3.6，
# Jammy 則通常是 Python 3.10，不能在本檔另外從 PATH 猜成另一支。
#
# wheels 來源優先序: VENV_WHEELS_DIR 指定 → 腳本同層 virtualenv_wheels/ → 當前目錄 ./virtualenv_wheels

# 腳本自身所在目錄(無論從何處呼叫,皆能定位到隨附的 virtualenv_wheels)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WHEELS_DIR="${VENV_WHEELS_DIR:-$SCRIPT_DIR/virtualenv_wheels}"

echo "================================================="
echo "   開始在主環境中安裝 virtualenv (離線模式)      "
echo "================================================="

# 目標解譯器必須與 deploy_offline.sh 完全相同。優先採用呼叫端傳入的 PYTHON_BIN，
# 否則先找固定的系統路徑；不能只用 PATH，因為操作者可能正處於另一個已啟用的 venv，
# 此時 `python3 -m pip install --user` 會直接拒絕（user site 在 venv 內不可見）。
if [ -n "${PYTHON_BIN:-}" ]; then
    PY="$PYTHON_BIN"
elif [ -x /usr/bin/python3.10 ]; then
    PY=/usr/bin/python3.10
elif [ -x /usr/bin/python3 ]; then
    PY=/usr/bin/python3
elif command -v python3.10 >/dev/null 2>&1; then
    PY="$(command -v python3.10)"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
    echo "警告: 找不到 python3.10,改用 python3;若下游 install_env.sh 報 No module named virtualenv,代表 virtualenv 裝到了非 python3.10 的解譯器。"
else
    echo "錯誤: 找不到 python3.10 或 python3，請先確認系統已安裝 Python 3.10。"
    exit 1
fi
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "錯誤: 找不到指定的 Python：$PY"
    exit 1
fi
PY="$(command -v "$PY")"

# --user 安裝在 venv 內必定不可見；在進入 pip 前就用可理解的訊息停止。
if ! "$PY" -c 'import sys; raise SystemExit(0 if getattr(sys, "base_prefix", sys.prefix) == sys.prefix and not hasattr(sys, "real_prefix") else 1)' >/dev/null 2>&1; then
    echo "錯誤: 指定的 Python 位於虛擬環境中：$PY"
    echo "請改用系統直譯器（例如 /usr/bin/python3），或由 deploy_offline.sh 呼叫本腳本。"
    exit 1
fi
echo "目標 Python: $("$PY" --version 2>&1) ($PY)"

# 檢查 PIP 是否存在
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "警告: 找不到 $PY 的 pip 模組。"
    echo "嘗試使用 $PY -m ensurepip 建立 pip..."
    "$PY" -m ensurepip --default-pip || true
fi

# 檢查 virtualenv_wheels 資料夾是否存在(同層找不到再退而找當前目錄)
if [ ! -d "$WHEELS_DIR" ]; then
    if [ -d "./virtualenv_wheels" ]; then
        WHEELS_DIR="./virtualenv_wheels"
    else
        echo "錯誤: 找不到 $WHEELS_DIR 或是當前目錄下的 virtualenv_wheels。"
        echo "請確保本安裝服務(radar-shm-install)隨附的 virtualenv_wheels 資料夾完整。"
        exit 1
    fi
fi

echo "使用 wheels 目錄: $WHEELS_DIR"

# 安裝 virtualenv(裝到上面選定的 $PY,確保與下游 python3.10 -m virtualenv 一致)
"$PY" -m pip install \
    --user \
    --no-index \
    --find-links="$WHEELS_DIR" \
    virtualenv

echo "================================================="
echo "virtualenv 安裝完成！"
echo "您現在可以繼續執行各專案的 install_env.sh 進行後續環境建置。"
echo "================================================="
