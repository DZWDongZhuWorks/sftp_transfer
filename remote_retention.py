#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""岸端 SFTP log 的保留政策：刪掉遠端 sftp_logs 底下超過 N 天的傳輸紀錄。

為什麼需要這支
--------------
岸端 `/fleet/wanhai_nssms_deploy/sftp_logs/` 只增不減 —— 各船靠 `upload_log=true` 一直往上
推，而**沒有任何自動刪除者**。2026-09-22 實測的形狀是：`upload/` 從 2026-08-25 起 29 天沒
被碰過、`download/` 的地板線停在 2026-09-15 01:38:53。那兩條線是**人工**清除留下的（兩次
事件相隔 21 天、範圍還不一樣、地板線是零碎的時間點而非日界），不是任何排程的形狀。

分工（與另外兩個刪除者劃清界線，三者管的是三個不同的地方）：

| 管哪裡 | 誰 | 節奏 |
|---|---|---|
| 船上本機 `logs/D_*.csv`、`U_*.csv` | scheduler 的 `cleanup_old_files.py` | 每天 |
| 岸端本機鏡像 `fleet_logs/` | 同上（規則 `sftp-fleet-reports`，30 天） | 每天 |
| **岸端 SFTP 上的 `sftp_logs/`** | **本程式** | 每天 |

為什麼不直接用 cleanup_old_files.py
-----------------------------------
1. **反向依賴**：`share/scheduler` 本身就是 sftp_transfer 下載下來的
   （`config/scheduler_download_settings.json`）。傳輸層去依賴自己的載荷，scheduler 壞掉
   或還沒下載時就起不來。
2. **語意對不上**：那一支是 `os.walk` + `os.unlink`，全是本地檔案系統語意；這裡只有
   `SFTPAttributes` 可用。
3. **schema 對不上**：`cleanup_rules.json` 是已上線、20 條全 enabled 的生產檔，還有測試
   釘住逐條宣告清單；在這裡放一個同名不同 schema 的檔會害死維運。

共用的只有 `pattern` 的**語意** —— 而且是直接共用程式碼：判定沿用
`SFTPBase._delete_source_kept_reason`（fnmatch 比對 basename、Linux 上區分大小寫、
`*` 含隱藏檔、不支援 `{a,b}`、清單是 any）。全船隊三個刪除工具因此同一個心智模型。

為什麼不是 delete_source
------------------------
`delete_source` 是「傳完就刪」，沒有保留窗，等於把遠端壓到 0 天 —— 岸端本機那份鏡像就成為
唯一副本。本程式是「放久了才刪」，遠端留 N 天當中繼緩衝，這是完全不同的問題。

安全設計
--------
1. **預設只預覽。** 不給 `--apply` 就只掃描、只統計、把「將刪除」清單寫進 log。預覽與實刪
   走**完全相同的掃描與判定程式碼**，差別只在最後要不要呼叫 `sftp.remove` —— 分成兩條路徑
   遲早會出現「預覽看起來對、實刪刪錯」。

2. **同步不新鮮就整趟放棄（fail-closed，exit 2）。** 這是最重要的一道，擋的是
   「同步壞掉幾天沒人發現，而這支還在照時間刪」。真實案例：09-15 那次人工清除的地板線
   `01:38:53` **正好是我們那趟同步收工的時刻**，只差五分鐘就會永久少掉 20 天的 download
   log；而當時這台機器上根本沒有任何排程在同步。現在靠的是 tmux 裡那個
   `log_monitor --watch 6000` 的 pane —— 而它正是會卡住的那一個，所以不能假設它活著。

3. **逐檔的「本地確實有」只驗得到一半，這是 30 天窗自己的限制。** 本地鏡像的清掃也是 30 天
   且用同一個時間戳（下載時 `os.utime` 把遠端 mtime 鏡射到本地檔），所以遠端檔滿 30 天的
   那一刻，本地那份也正在同一天被刪 —— 誰先跑誰贏。若把「本地必須還在」當成硬門檻，本地
   先跑的日子遠端就永遠刪不掉（漏水）。而 manifest 也救不了：2026-09-22 實測
   `.sftp_download_manifest.json` 的 17,275 筆是**遠端現況的子集**，對本地還在、遠端已消失
   的 23,472 個檔**一筆紀錄都沒有**（紀錄只回溯到 09-15 那次清除），它不是「我們曾經收到過」
   的耐久證據。所以逐檔只做**降級版**的檢查：
     * 本地那份不在 → 放行（它已經超過本地保留窗，本地政策自己刪掉是預期行為）
     * 本地那份在、大小相符 → 放行
     * 本地那份在、**大小不符** → 保留（我們手上不是遠端現在這一版）
   要拿回完整的逐檔保證，遠端窗必須**嚴格短於**本地窗（例如遠端 21 天、本地 30 天）。

4. **空目錄是選配（`--remove-empty-dirs`），而且永遠不動最上面兩層。** 清空的葉目錄移掉之後
   船再上傳時 `_ensure_remote_dir` 會自己建回來，不會失去可見性；順帶讓每輪同步的目錄走訪
   少幾次往返（實測效益很小：30 天窗會清空 0 個葉目錄、7 天窗 41 個／2.7%，約 2～4 秒，
   所以這不是做這件事的理由）。

5. **開發機守門刻意沒有。** `script/_dev_guard.sh` 擋的是「在 CLINK 發佈源頭跑下載會覆蓋
   未發佈的修改」，本程式不下載任何東西，而且岸端監控機**就是** CLINK —— 加守門等於讓它
   永遠不能跑。

離開碼：`0` 正常；`1` 有刪除失敗；`2` fail-closed（設定檔缺席、同步不新鮮、連不上）。
"""
from typing import List, Optional, Tuple

import argparse
import fnmatch
import logging
import logging.handlers
import stat
import sys
import time
from pathlib import Path

import paramiko

from downloader import SFTPBase, format_exception, format_size, diagnostic_message
from settings import PlaceholderError, load_settings

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_RETENTION_DAYS = 30
# 預設樣式與 cleanup_rules.json 的 `sftp-transfer-csv-logs` 一字不差：只吃逐次傳輸紀錄。
# 刻意不用 `*.csv` 也不用 `*` —— 遠端那棵樹現在只有這兩種前綴的檔，但哪天多了別的東西
# （設定、報表、誰放上去的暫存檔），用寬樣式就會一起刪掉。
DEFAULT_PATTERNS = ["D_*.csv", "U_*.csv"]
# 同步多久沒動就視為壞掉（見檔頭安全設計第 2 點）。同步每 100 分鐘一輪，48 小時＝連續
# 漏掉約 28 輪，那已經不是抖動而是壞了。
DEFAULT_REQUIRE_LOCAL_SYNC_HOURS = 48
LOG_FILENAME = "remote_retention.log"
# 與專案既有慣例相同的輪替（2 MiB × 3）：這份 log 不被任何 cleanup_rules.json 的規則涵蓋，
# 自己輪替才不會變成下一個只增不減的東西。
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


def build_logger(log_dir: Path, quiet: bool = False) -> logging.Logger:
    """檔案（輪替）＋ stdout。刻意**不用** downloader.create_logger：

    那一支寫的是 `D_*.csv`／`U_*.csv`，而 monitor/log_monitor.py 會把 logs/ 底下這種檔名
    當成一次傳輸來解析 —— 保留工作不是傳輸，混進去會在船隊畫面上長出一台假裝置。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sftp_transfer.remote_retention")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        str(log_dir / LOG_FILENAME), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if not quiet:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


def newest_local_mtime(local_root: Path, patterns: List[str]) -> Optional[float]:
    """本地鏡像裡最新一份紀錄的 mtime；一個都沒有回 None。

    只看符合 patterns 的檔，與刪除判定用同一組樣式 —— 拿 log_monitor.html 或 manifest
    的 mtime 當「同步很新鮮」的證據是假的，那兩個檔每輪都會被改寫，即使一個 log 都沒下載到。
    """
    newest = None
    for path in local_root.rglob("*"):
        name = path.name
        if not any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


class RemoteRetention(SFTPBase):
    """走訪遠端樹，刪掉超過保留窗的紀錄。

    沿用 SFTPBase 的連線／重試／等網路／ignore／樣式與年紀判定；保留窗透過
    `delete_source_min_age_minutes = 天數 × 1440` 餵進既有的
    `_delete_source_kept_reason`，所以「樣式 + 年紀」的語意與 delete_source 完全同一份。
    """

    def __init__(self, *args, retention_days=DEFAULT_RETENTION_DAYS, apply=False,
                 remove_empty_dirs=False, verbose=False, **kwargs):
        kwargs["delete_source_min_age_minutes"] = max(0.0, float(retention_days)) * 24 * 60
        super().__init__(*args, **kwargs)
        self.retention_days = retention_days
        self.apply = apply
        self.remove_empty_dirs = remove_empty_dirs
        self.verbose = verbose

    # ---- 走訪 ----------------------------------------------------------
    def _walk(self, root: str) -> Tuple[list, list]:
        """回傳 (files, dirs)。files 為 [(遠端絕對路徑, rel_path, attr)]，dirs 由深到淺。

        自己走而不沿用 SFTPDownloader._walk_remote_dir：那一支會 `mkdir` 本地目錄（下載要先
        把目的地準備好），保留工作不該在本機留下任何空目錄；而且它不回報目錄清單，
        而移除清空的目錄必須由深到淺處理。

        符號連結一律跳過（與 cleanup_old_files.py 的「跳過：符號連結」同一個選擇）：
        `sftp.remove` 刪的是連結本身，而我們判斷年紀用的是連結的 lstat，兩者對不上。
        """
        files, dirs = [], []
        stack = [("", root)]
        while stack:
            rel_dir, remote_dir = stack.pop()
            try:
                entries = self.sftp.listdir_attr(remote_dir)
            except (OSError, paramiko.SSHException) as exc:
                self.logger.warning(f"列不出遠端目錄，略過整棵: {remote_dir}（{format_exception(exc)}）")
                continue
            for entry in entries:
                rel = f"{rel_dir}/{entry.filename}" if rel_dir else entry.filename
                path = remote_dir.rstrip("/") + "/" + entry.filename
                mode = getattr(entry, "st_mode", None)
                if mode is None:
                    continue
                if stat.S_ISLNK(mode):
                    self.stats["symlinks"] += 1
                    continue
                if stat.S_ISDIR(mode):
                    if self._is_ignored(rel + "/"):
                        continue
                    dirs.append(rel)
                    stack.append((rel, path))
                elif not self._is_ignored(rel):
                    files.append((path, rel, entry))
        dirs.sort(key=lambda p: p.count("/"), reverse=True)
        return files, dirs

    # ---- 逐檔判定 ------------------------------------------------------
    def _local_copy_verdict(self, rel_path: str, remote_size) -> Optional[str]:
        """本地鏡像的狀態。回 None 代表可以刪，回字串代表保留的理由代碼。

        降級版檢查，理由見檔頭安全設計第 3 點：本地那份不在是**預期**的（本地也是 30 天窗、
        同一個時間戳），把它當硬門檻會讓遠端永遠刪不掉。
        """
        local_file = Path(self.local_path) / Path(*rel_path.split("/"))
        try:
            local_size = local_file.stat().st_size
        except OSError:
            return None
        if remote_size is not None and local_size != remote_size:
            return "local_size_mismatch"
        return None

    # ---- 主流程 --------------------------------------------------------
    def run(self) -> int:
        self.stats = {
            "scanned": 0, "deleted": 0, "bytes": 0, "errors": 0, "symlinks": 0,
            "dirs_removed": 0,
        }
        kept = {}
        roots = self.remote_path if isinstance(self.remote_path, (list, tuple)) else [self.remote_path]

        if self.wait_for_network:
            self._wait_for_network()
        try:
            self._connect_with_retry()
        except Exception as exc:
            self.logger.error(f"=== 任務中止：連不上 {self.host}（{format_exception(exc)}） ===")
            return 2
        self._ignore_spec = self._load_ignore_spec()

        cutoff = time.time() - self.delete_source_min_age
        mode_label = "實際刪除" if self.apply else "預覽（未加 --apply，不會刪任何東西）"
        self.logger.info(diagnostic_message(
            "RETENTION_CONTEXT", f"岸端 log 保留政策：{mode_label}",
            host=self.host, remote_path=roots, local_path=str(self.local_path),
            retention_days=self.retention_days,
            cutoff=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)),
            pattern=self.delete_source_pattern,
            remove_empty_dirs=self.remove_empty_dirs,
        ))

        try:
            for root in roots:
                files, dirs = self._walk(root)
                self.stats["scanned"] += len(files)
                self.logger.info(f"掃描 {root}：{len(files)} 個檔、{len(dirs)} 個目錄")
                for remote_file, rel_path, attr in files:
                    size = getattr(attr, "st_size", None)
                    reason = self._delete_source_kept_reason(
                        rel_path, lambda a=attr: getattr(a, "st_mtime", None)
                    )
                    if reason is not None:
                        code, message = reason
                        kept[code] = kept.get(code, 0) + 1
                        if self.verbose:
                            self.logger.info("  保留：" + message)
                        continue
                    code = self._local_copy_verdict(rel_path, size)
                    if code is not None:
                        kept[code] = kept.get(code, 0) + 1
                        self.logger.warning(
                            f"  保留（本地那份與遠端大小不符，我們手上不是這一版）: {rel_path}"
                        )
                        continue
                    if not self.apply:
                        self.stats["deleted"] += 1
                        self.stats["bytes"] += size or 0
                        if self.verbose or self.stats["deleted"] <= 20:
                            self.logger.info(f"  將刪除：{rel_path}（{format_size(size or 0)}）")
                        continue
                    try:
                        self.sftp.remove(remote_file)
                    except (OSError, paramiko.SSHException) as exc:
                        self.stats["errors"] += 1
                        self.logger.warning(f"  刪不掉 {rel_path}：{format_exception(exc)}")
                        continue
                    self.stats["deleted"] += 1
                    self.stats["bytes"] += size or 0
                    if self.verbose or self.stats["deleted"] <= 20:
                        self.logger.info(f"  已刪除：{rel_path}（{format_size(size or 0)}）")
                if self.remove_empty_dirs:
                    self._sweep_empty_dirs(root, dirs)
        finally:
            self._close()

        self.logger.info("───────── 總結 ─────────")
        self.logger.info(f"  模式     : {mode_label}")
        self.logger.info(f"  掃描     : {self.stats['scanned']} 個檔"
                         + (f"（跳過符號連結 {self.stats['symlinks']}）" if self.stats["symlinks"] else ""))
        verb = "已刪除" if self.apply else "將刪除"
        self.logger.info(f"  {verb}   : {self.stats['deleted']} 個檔、{format_size(self.stats['bytes'])}")
        if self.remove_empty_dirs:
            self.logger.info(f"  空目錄   : {self.stats['dirs_removed']} 個")
        for code, count in sorted(kept.items()):
            self.logger.info(f"  保留     : {count} 個（{code}）")
        if self.stats["errors"]:
            self.logger.warning(f"  錯誤     : {self.stats['errors']}")
        return 1 if self.stats["errors"] else 0

    def _sweep_empty_dirs(self, root: str, dirs: List[str]) -> None:
        """由深到淺移除清空的目錄。永遠不動最上面兩層（`sftp_logs` 自己與 download/upload）。

        `rmdir` 對非空目錄會失敗，所以「是不是真的空了」交給伺服器判斷，不自己數 —— 我們
        手上那份清單是走訪當下的快照，這中間可能有船剛好上傳了新檔。
        """
        for rel in dirs:
            if "/" not in rel:
                continue          # download / upload 這一層不動
            try:
                self.sftp.rmdir(root.rstrip("/") + "/" + rel)
            except (OSError, paramiko.SSHException):
                continue          # 還有東西（或沒權限）＝ 本來就不該動
            self.stats["dirs_removed"] += 1
            if self.verbose:
                self.logger.info(f"  已移除空目錄：{rel}/")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="刪掉岸端 SFTP sftp_logs 底下超過保留窗的傳輸紀錄（預設只預覽）。")
    parser.add_argument("--config", required=True,
                        help="download 設定檔：host/port/帳密與 remote_path 沿用它，"
                             "local_path 則是用來比對的本地鏡像")
    parser.add_argument("--retention-days", type=float, default=DEFAULT_RETENTION_DAYS,
                        help=f"保留窗（天），預設 {DEFAULT_RETENTION_DAYS}")
    parser.add_argument("--pattern", action="append", default=None,
                        help=f"只刪檔名符合的（可重複），預設 {' '.join(DEFAULT_PATTERNS)}")
    parser.add_argument("--apply", action="store_true",
                        help="真的刪；不給就只預覽")
    parser.add_argument("--remove-empty-dirs", action="store_true",
                        help="順便移除被清空的目錄（不動最上面兩層）")
    parser.add_argument("--require-local-sync-hours", type=float,
                        default=DEFAULT_REQUIRE_LOCAL_SYNC_HOURS,
                        help="本地鏡像最新一份紀錄必須新於這麼多小時，否則整趟放棄"
                             f"（預設 {DEFAULT_REQUIRE_LOCAL_SYNC_HOURS}；0 = 不檢查）")
    parser.add_argument("--verbose", action="store_true", help="逐檔列出，不只前 20 筆")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"錯誤：找不到設定檔 {config_path}", file=sys.stderr)
        return 2
    try:
        settings = load_settings(config_path)
    except (PlaceholderError, ValueError, OSError) as exc:
        print(f"錯誤：設定檔解析失敗 {config_path}：{exc}", file=sys.stderr)
        return 2

    local_path = settings.get("local_path")
    remote_path = settings.get("remote_path")
    if not local_path or not remote_path:
        print("錯誤：設定檔缺少 local_path 或 remote_path", file=sys.stderr)
        return 2
    local_root = Path(local_path)
    if not local_root.is_absolute():
        local_root = BASE_DIR / local_root

    patterns = args.pattern or list(DEFAULT_PATTERNS)
    logger = build_logger(BASE_DIR / (settings.get("log_dir") or "logs"))

    # fail-closed：同步不新鮮就一個檔都不刪（見檔頭安全設計第 2 點）
    if args.require_local_sync_hours > 0:
        newest = newest_local_mtime(local_root, patterns)
        if newest is None:
            logger.error(f"=== 任務中止：本地鏡像 {local_root} 裡找不到任何符合 "
                         f"{patterns} 的紀錄，無法確認同步是否正常，不刪任何東西 ===")
            return 2
        age_hours = (time.time() - newest) / 3600.0
        if age_hours > args.require_local_sync_hours:
            logger.error(
                f"=== 任務中止：本地鏡像最新一份紀錄已經 {age_hours:.1f} 小時前"
                f"（門檻 {args.require_local_sync_hours} 小時），同步可能壞了，不刪任何東西 ==="
            )
            return 2
        logger.info(f"同步新鮮度檢查通過：本地最新一份紀錄 {age_hours:.1f} 小時前")

    task = RemoteRetention(
        host=settings["host"],
        port=settings.get("port", 22),
        username=settings["username"],
        password=settings.get("password") or None,
        key_file=settings.get("key_file") or None,
        remote_path=remote_path,
        local_path=str(local_root),
        wait_for_network=bool(settings.get("wait_for_network", True)),
        retry_count=settings.get("retry_count"),
        retry_delay=settings.get("retry_delay", 10),
        ignore_file=settings.get("ignore_file") or None,
        resume=False,                      # 不牽涉 manifest，別去讀寫它
        delete_source_pattern=patterns,
        logger=logger,
        retention_days=args.retention_days,
        apply=args.apply,
        remove_empty_dirs=args.remove_empty_dirs,
        verbose=args.verbose,
    )
    return task.run()


if __name__ == "__main__":
    sys.exit(main())
