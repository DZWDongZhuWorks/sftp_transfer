"""船上更新用腳本：遍歷 config/ 內所有「下載」設定檔並依序執行 SFTP 下載。

只挑選檔名符合 `*_download_settings.json` 的設定檔，避免把上傳設定檔
（`*_upload_settings.json`，由 run_all_uploads.py 負責）也一併跑成下載。
每份設定檔各跑一次 `main.py --cli --mode download --config <設定檔>`，前一個專案
下載結束（成功或失敗）後才會執行下一個，最後彙總各專案結果。

使用方式：
    python run_all_downloads.py
    python run_all_downloads.py --config-dir other_config
    python run_all_downloads.py --force-redownload 'wheels/' --force-redownload-id 20261020
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
MAIN_SCRIPT = BASE_DIR / "main.py"


def is_dev_machine():
    """CLINK 開發機（STANDARD 發佈源頭）判定，與 script/_dev_guard.sh 對稱。

    所有下載設定檔皆為 duplicate_mode=overwrite，且打進開發端工作區；在 CLINK 上跑下載
    會清掉尚未發佈的開發修改，故一鍵全跑時也需擋住。失效方向安全：讀不到船舶資訊檔／
    解析失敗一律回 False（當一般船照常下載）。船舶資訊檔路徑與 settings.py 一致
    （可用 VESSEL_INFO_PATH 覆蓋，預設 share/.env/）。"""
    vinfo = os.environ.get("VESSEL_INFO_PATH") or (BASE_DIR.parent / ".env" / "vessel_basic_info.json")
    try:
        with open(vinfo, encoding="utf-8") as f:
            return str(json.load(f).get("vsl_name", "")).upper() == "CLINK"
    except Exception:
        return False


def find_setting_files(config_dir: Path):
    """回傳 config 目錄下所有「下載」設定檔（*_download_settings.json，排序後）。"""
    return sorted(config_dir.glob("*_download_settings.json"))


def force_args(args):
    """把強制重下參數轉成 main.py 的命令列片段；沒指定就是空串列（與改版前的指令一字不差）。"""
    extra = []
    for pattern in args.force_redownload:
        extra += ["--force-redownload", pattern]
    if args.force_redownload_id:
        extra += ["--force-redownload-id", args.force_redownload_id]
    return extra


def main():
    parser = argparse.ArgumentParser(description="依序執行所有設定檔的 SFTP 下載")
    parser.add_argument("--config-dir", default=str(CONFIG_DIR), help="設定檔資料夾（預設 ./config）")
    # 原樣轉給每一個專案的 main.py（見 main.py 的同名參數）。樣式是相對於各專案根目錄比對的，
    # 所以 wheels/ 會一次涵蓋所有帶離線 wheels 的專案；* 則是全部專案整份重抓，船上頻寬慎用。
    parser.add_argument("--force-redownload", action="append", default=[],
                        help="強制重新下載符合此樣式（gitignore 格式）的檔案，可重複指定；套用到每一份下載設定")
    parser.add_argument("--force-redownload-id", help="強制重下的批次號：有填時每個命中的檔案只重抓一次")
    args = parser.parse_args()

    if is_dev_machine():
        print("偵測到開發機 (vsl_name=CLINK)，略過所有下載，避免覆蓋未提交的修改。", file=sys.stderr)
        return 0

    files = find_setting_files(Path(args.config_dir))
    if not files:
        print(f"錯誤：{args.config_dir} 內找不到任何下載設定檔（*_download_settings.json）", file=sys.stderr)
        return 1

    results = []
    for i, path in enumerate(files, 1):
        print(f"\n===== [{i}/{len(files)}] 開始下載：{path.name} =====")
        proc = subprocess.run(
            [sys.executable, str(MAIN_SCRIPT), "--cli", "--mode", "download", "--config", str(path)]
            + force_args(args),
            cwd=str(BASE_DIR),
        )
        ok = proc.returncode == 0
        results.append((path.name, ok))
        print(f"===== [{i}/{len(files)}] {path.name} {'完成' if ok else '失敗'} =====")

    print("\n========== 下載結果彙總 ==========")
    for name, ok in results:
        print(f"  {'[成功]' if ok else '[失敗]'} {name}")
    failed = sum(1 for _, ok in results if not ok)
    print(f"共 {len(results)} 個專案，成功 {len(results) - failed}，失敗 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
