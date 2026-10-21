# -*- coding: utf-8 -*-
"""把一批強制重下（force_redownload / force_redownload_id）套進 config/ 的下載設定檔，或拿掉。

為什麼要有這支
--------------
一批回填要改十份 `config/*_download_settings.json`，而那些檔帶著 SFTP 密碼、不進版控。
手改十份容易漏一份或打錯一個逗號（壞掉的 JSON 會被 load_settings 整份忽略 → 那艘船那個
專案這一輪直接沒設定可用）；這支只讀進來、改兩個 key、照原本的格式（indent=2）寫回，
其餘內容一個字元都不碰。

用法（預設只印差異，不寫檔）：

    python script/force_redownload_batch.py docs/force_redownload_b1.json            # 預覽
    python script/force_redownload_batch.py docs/force_redownload_b1.json --apply    # 寫入
    python script/force_redownload_batch.py docs/force_redownload_b1.json --clear --apply  # 收尾拿掉

批次檔格式見 docs/force_redownload_b1.json；方案本身見 docs/hash-baseline-backfill.md。
寫入的是**本機**的 config/。要上船還得照平常的方式發佈 sftp_transfer（run_sftp_upload.sh）。
"""

import argparse
import difflib
import json
import sys
from pathlib import Path

KEYS = ("force_redownload", "force_redownload_id")
SUFFIX = "_download_settings.json"


def load_batch(path):
    batch = json.loads(Path(path).read_text(encoding="utf-8"))
    batch_id = str(batch.get("force_redownload_id") or "").strip()
    configs = batch.get("configs") or {}
    if not batch_id:
        # 沒有批次號就是「全船隊每一輪都重抓」，見 README【強制重新下載】。這支只做一次性的批次。
        raise ValueError("批次檔缺 force_redownload_id")
    for name, patterns in configs.items():
        if not isinstance(patterns, list) or not patterns or any(not isinstance(p, str) for p in patterns):
            raise ValueError("%s 的樣式必須是非空字串陣列" % name)
    return batch_id, configs


def render(settings, batch_id, patterns, clear):
    updated = dict(settings)
    if clear:
        for key in KEYS:
            updated.pop(key, None)
    else:
        updated["force_redownload"] = list(patterns)
        updated["force_redownload_id"] = batch_id
    return json.dumps(updated, ensure_ascii=False, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("batch", help="批次檔（例：docs/force_redownload_b1.json）")
    parser.add_argument("--config-dir", default=str(Path(__file__).resolve().parent.parent / "config"))
    parser.add_argument("--clear", action="store_true", help="拿掉兩個 key（批次收尾）")
    parser.add_argument("--apply", action="store_true", help="真的寫檔；沒帶只印差異")
    args = parser.parse_args(argv)

    batch_id, configs = load_batch(args.batch)
    config_dir = Path(args.config_dir)
    missing = [name for name in configs if not (config_dir / (name + SUFFIX)).is_file()]
    if missing:
        # 一份都不寫：半套的批次比沒有更難收尾。
        print("找不到設定檔，整批不套用: %s" % ", ".join(m + SUFFIX for m in missing), file=sys.stderr)
        return 2

    changed = 0
    for name, patterns in configs.items():
        path = config_dir / (name + SUFFIX)
        before = path.read_text(encoding="utf-8")
        after = render(json.loads(before), batch_id, patterns, args.clear)
        if after == before:
            continue
        changed += 1
        # 差異只印這兩個 key 附近的行 —— 整份檔有密碼，不該出現在終端機或 log 裡。
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                         path.name, path.name, lineterm="", n=0):
            if '"password"' not in line:
                print(line)
        if args.apply:
            path.write_text(after, encoding="utf-8")

    action = "已寫入" if args.apply else "預覽（加 --apply 才寫入）"
    print("%s：%d / %d 份設定檔有變動，批次號 %s%s"
          % (action, changed, len(configs), batch_id, "（拿掉）" if args.clear else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
