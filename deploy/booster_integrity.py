# -*- coding: utf-8 -*-
"""booster 完整性檢查:拿發佈端的 VERSION.stamp.json 驗 sftp_transfer / scheduler 的本地檔。

用法:
    python3 deploy/booster_integrity.py <專案根目錄> [<專案根目錄> ...] [--dev]
                                        [--state <狀態檔>] [--persist-days <天數>]

    deploy_offline.sh 的 stage_booster_integrity 會帶上 share/sftp_transfer 與 share/scheduler。
    --dev:CLINK 開發機。工作樹與發佈版本來就不同,「不一致」只列為 INFO,也不拿下載 manifest
           當證據(開發機的 manifest 是陳年的);只有「確定壞掉」(空檔、語法壞掉)仍算損毀。

    --state:記住每個「不一致」第一次出現的時間(見下方〈持續不一致〉)。deploy_offline.sh 傳
           share/.env/booster_integrity_state.json(不在 OTA 範圍內)。不給就不判持續。
    --persist-days:同一個不一致持續幾天才升級為 FAIL(預設 3)。

離開碼:0 = OK、1 = FAIL(有損毀或持續不一致)、2 = WARN(有不一致 / 缺檔 / 無法驗證)、3 = 參數錯誤。

【為什麼要有】sftp_transfer 與 scheduler 是 OTA 的控制面(scheduler 稱為 booster):其他專案
都靠它們下載,而它們也靠自己下載自己。實測過:main.py 被截成 0 byte 時 ——
  * `python main.py ...` 執行空檔 → 什麼都不做、rc=0,run_all_downloads 回報「全部成功」;
  * health_check 的 `import main` 照樣 PASS;
  * 下載器就是 main.py,所以永遠抓不回好的 main.py。
同大小的內容損壞(bit rot)更糟:下載的略過分支只比 size/mtime,每一輪都當成「已完整下載」。
於是那台船安靜地停止所有 OTA 與上傳,沒有任何一層會報警。

【基準為什麼是 VERSION.stamp.json】它是**發佈端**上傳前算的每檔 sha256(見 version_stamp.py),
隨鏡像一起上船。船上自己算的雜湊只能證明「跟當下一樣」,證明不了「跟發佈的一樣」。

【不一致 ≠ 損毀 —— 判定規則】鏡像更新不是原子的:warm 的時間預算用完時會停在半途,樹上會
有「有些檔新、有些檔舊」的時段,這時 stamp 不符是正常的。所以只有 stamp 不符**再加上**下列
任一項證據,才判為損毀(FAIL):
  1. 空檔:本地 0 byte,而發佈版不是空檔(空的 __init__.py 在 stamp 裡就是空檔的雜湊,不會誤判);
  2. 結構壞掉:.py 編譯不過、.sh 過不了 `bash -n`、.json 解析不了 —— 截斷與 bit rot 的典型症狀;
  3. 下載後變質:下載 manifest 記著這個檔「完整下載當時」的整檔 sha256(見 downloader.py 的
     whole_file_digest),而現在的內容或大小已經跟它不同。下載是 .part + 原子換名,所以船上
     的檔不會停在半截;下載完之後還會變的,只有損壞或有人手改。
其餘的 stamp 不符:
  * 版本交錯(skew):內容跟 manifest 的整檔雜湊相符 —— 是一次完整的下載,只是版本跟 stamp
    不同(更新進行到一半)。WARN。
  * 不一致(mismatch):沒有任何證據能分辨是哪一種(船上多數檔還沒有整檔雜湊)。WARN。
  * 缺少(missing):stamp 有、本地沒有。WARN —— 可能是漏送,也可能是新版還沒抓到。

【持續不一致 → FAIL】上面三種 WARN 的共同前提是「更新進行到一半」。warm 一天兩輪,booster 的
檔又小,同一個不一致若**好幾天都一模一樣**(本地內容的 sha 與發佈版的 sha 都沒變),就不可能是
更新進行中。--state 記下每一筆的簽名 (本地 sha, 發佈版 sha) 與第一次出現的時間,持續滿
--persist-days 天就升級為 FAIL(persistent),並依原本的類別給出判讀:
  * 不一致(沒有下載證據)→ 內容很可能已損毀:大小沒變的 bit rot、截在語法仍完整之處的截斷 ——
    這兩種結構驗證抓不到,下載的略過分支也永遠不會重抓。
  * 版本交錯(內容是一次完整的下載)→ 船上忠實地抓下了岸上的內容,它卻一直不是發佈時的樣子:
    岸端 STANDARD 上那份很可能本身就壞了(或發佈後被改過、stamp 沒有跟著重產)。
  * 缺少 → 不是新版還沒抓到,是漏送或被刪。
簽名任一邊變了就重新計時:岸上發佈新版(stamp 的 sha 變了)時,換版期間的不一致不會被誤判。

【一定要印結論行】最後一行固定是 `BOOSTER_INTEGRITY status=...`。呼叫端**必須**以這一行為準,
不能只看離開碼:這支檔本身也可能被截成 0 byte,而空的 .py 執行起來就是 rc=0 —— 那正是本檢查
要抓的問題。沒有結論行 = 檢查器自己壞了。

只讀:不改、不刪、不改名任何檔案,也不連網路。不印任何檔案內容(config/ 裡有密碼檔)。
Python 3.6 相容、只用標準庫:由 deploy_offline.sh 的 $PYTHON_BIN(系統 python3)執行,
而且刻意不 import 本專案任何模組 —— 被檢查的對象不能同時是檢查器的相依。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict

STAMP_NAME = "VERSION.stamp.json"
DOWNLOAD_MANIFEST_NAME = ".sftp_download_manifest.json"
VERDICT_PREFIX = "BOOSTER_INTEGRITY"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_HASH_CHUNK = 1024 * 1024
# 每一類最多列出幾個檔:樹整個換掉時不該洗出幾百行,總數另外印。
LIST_LIMIT = 20

EXIT_OK, EXIT_FAIL, EXIT_WARN, EXIT_USAGE = 0, 1, 2, 3
DEFAULT_PERSIST_DAYS = 3
STATE_VERSION = 1

CORRUPT, PERSISTENT = "corrupt", "persistent"
SKEW, MISMATCH, MISSING = "skew", "mismatch", "missing"
CATEGORY_LABEL = OrderedDict((
    (CORRUPT, "損毀"),
    (PERSISTENT, "持續不一致"),
    (MISMATCH, "與發佈版不一致"),
    (SKEW, "版本交錯(完整下載,但不是這一版)"),
    (MISSING, "缺少"),
))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def whole_file_digest(entry):
    """與 downloader.whole_file_digest 同一套規則(刻意複製而不 import,理由見檔頭)。

    只有 local_bytes == size 的那筆才是整檔雜湊;local_bytes < size 是傳到一半的檢查點。
    回傳 (sha256, bytes) 或 None。
    """
    if not isinstance(entry, dict):
        return None
    size = entry.get("size")
    offset = entry.get("local_bytes")
    digest = entry.get("local_sha256")
    if (isinstance(size, int) and not isinstance(size, bool)
            and isinstance(offset, int) and not isinstance(offset, bool)
            and offset == size and size >= 0
            and isinstance(digest, str) and len(digest) == 64):
        return digest, size
    return None


def structure_error(path, bash):
    """檢查檔案結構;壞掉時回傳一句說明,其餘(含不認得的副檔名)回 None。"""
    lower = path.lower()
    if lower.endswith(".py"):
        try:
            with open(path, "rb") as handle:
                source = handle.read()
            compile(source, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:  # ValueError:原始碼含 NUL byte
            return "Python 編譯失敗(python {}.{}):{}".format(
                sys.version_info[0], sys.version_info[1], _one_line(exc))
        return None
    if lower.endswith(".sh"):
        if not bash:
            return None
        proc = subprocess.run([bash, "-n", path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            return "bash -n 失敗:{}".format(_one_line(detail[-1] if detail else "rc={}".format(
                proc.returncode)))
        return None
    if lower.endswith(".json"):
        try:
            with open(path, "rb") as handle:
                json.loads(handle.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return "JSON 解析失敗:{}".format(_one_line(exc))
        return None
    return None


def _one_line(value):
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= 160 else text[:157] + "..."


def _safe_rel(rel):
    """stamp 是磁碟上的 JSON,可能被手改或損毀:絕對路徑與 .. 一律不碰(不能讀到根目錄外)。"""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        return False
    return ".." not in rel.split("/")


def _load_json_dict(path):
    """回傳 (dict 或 None, 錯誤說明或 None)。檔案不存在時回 (None, None)。"""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, _one_line(exc)
    if not isinstance(data, dict):
        return None, "根節點不是 JSON 物件"
    return data, None


class RootReport(object):
    def __init__(self, root):
        self.root = root
        self.name = os.path.basename(os.path.normpath(root)) or root
        self.version = None
        self.total = 0
        self.ok = 0
        self.findings = OrderedDict((key, []) for key in CATEGORY_LABEL)
        self.unverified = None   # 整個根目錄無法驗證時的原因
        self.stamp_broken = False
        # 不一致的簽名:rel → (本地 sha 或 "missing", 發佈版 sha)。給 apply_persistence 用。
        self.signatures = {}


def check_root(root, dev=False, bash=None):
    report = RootReport(root)
    if not os.path.isdir(root):
        report.unverified = "目錄不存在"
        return report

    stamp, error = _load_json_dict(os.path.join(root, STAMP_NAME))
    if stamp is None:
        if error is None:
            report.unverified = "沒有 {}(這一版沒有發佈端標記,無從比對)".format(STAMP_NAME)
        else:
            # stamp 存在卻讀不出來:它本身就是隨鏡像上船的檔,壞掉一樣是損毀。
            report.unverified = "{} 讀取失敗:{}".format(STAMP_NAME, error)
            report.stamp_broken = True
        return report
    files = stamp.get("files")
    if not isinstance(files, dict) or not files:
        report.unverified = "{} 沒有 files 清單".format(STAMP_NAME)
        report.stamp_broken = True
        return report
    report.version = "{}({})".format(stamp.get("version") or "?", stamp.get("commit") or "?")

    manifest = {}
    if not dev:
        loaded, _error = _load_json_dict(os.path.join(root, DOWNLOAD_MANIFEST_NAME))
        manifest = loaded or {}   # 讀不到就當沒有證據:只會少判損毀,不會多判

    for rel in sorted(files):
        expected = files[rel]
        if not _safe_rel(rel) or not isinstance(expected, str) or len(expected) != 64:
            continue
        report.total += 1
        path = os.path.join(root, *rel.split("/"))
        try:
            size = os.path.getsize(path)
            actual = sha256_file(path)
        except FileNotFoundError:
            report.findings[MISSING].append((rel, ""))
            report.signatures[rel] = ("missing", expected)
            continue
        except OSError as exc:
            report.findings[CORRUPT].append((rel, "無法讀取:{}".format(_one_line(exc))))
            continue
        if actual == expected:
            report.ok += 1
            continue

        reasons = []
        if size == 0 and expected != EMPTY_SHA256:
            reasons.append("0 byte(發佈版不是空檔)")
        broken = structure_error(path, bash)
        if broken:
            reasons.append(broken)
        baseline = whole_file_digest(manifest.get(rel))
        if baseline is not None and size != 0:
            base_sha, base_bytes = baseline
            if base_bytes != size:
                reasons.append("大小與下載完成時不同({} → {} byte)".format(base_bytes, size))
            elif base_sha != actual:
                reasons.append("內容與下載完成時的整檔 sha256 不同(下載後變質)")
        if reasons:
            report.findings[CORRUPT].append((rel, ";".join(reasons)))
        elif baseline is not None:
            report.findings[SKEW].append((rel, ""))
            report.signatures[rel] = (actual, expected)
        else:
            report.findings[MISMATCH].append((rel, ""))
            report.signatures[rel] = (actual, expected)
    return report


PERSISTENT_HINT = {
    MISMATCH: ("與發佈版不同、內容一直沒變 —— warm 早該補齊,內容很可能已損毀(大小沒變的 bit rot、"
               "截在語法仍完整之處的截斷),下載的略過分支也不會自己重抓。用 main.py --force-redownload 重抓"),
    SKEW: ("內容是一次完整的下載,卻一直不是發佈時的樣子 —— 岸端 STANDARD 上那份很可能本身就壞了"
           "(或發佈後被改過、stamp 沒有重產)。先比對岸端的檔,重抓只會抓回同一份"),
    MISSING: "一直缺少 —— 不是新版還沒抓到,是漏送或被刪",
}


def apply_persistence(report, previous, now, persist_days):
    """把持續滿 persist_days 天、簽名沒變的不一致升級為 PERSISTENT。回傳這個根目錄的新狀態。

    previous:上一次的狀態 {rel: {"category", "local", "expected", "first_seen"}};簽名變了就重新計時。
    消失的不一致(修好了、或換版完成)自然不會出現在新狀態裡。
    """
    threshold = persist_days * 86400
    current = {}
    for category in (MISMATCH, SKEW, MISSING):
        kept = []
        for rel, detail in report.findings[category]:
            local, expected = report.signatures.get(rel, (None, None))
            old = previous.get(rel) if isinstance(previous, dict) else None
            first_seen = now
            if (isinstance(old, dict) and old.get("local") == local and old.get("expected") == expected
                    and isinstance(old.get("first_seen"), (int, float))
                    and not isinstance(old.get("first_seen"), bool) and old["first_seen"] <= now):
                first_seen = old["first_seen"]
            current[rel] = {"category": category, "local": local, "expected": expected,
                            "first_seen": first_seen}
            if now - first_seen >= threshold:
                days = int((now - first_seen) // 86400)
                report.findings[PERSISTENT].append(
                    (rel, "已連續 {} 天{}".format(days, PERSISTENT_HINT[category])))
            else:
                kept.append((rel, detail))
        report.findings[category] = kept
    return current


def load_state(path, out):
    data, error = _load_json_dict(path)
    if error is not None:
        out.write("[WARN] 狀態檔讀不出來,從頭計時:{}({})\n".format(path, error))
        return {}
    if data is None or data.get("version") != STATE_VERSION or not isinstance(data.get("roots"), dict):
        return {}
    return data["roots"]


def save_state(path, roots_state, out):
    """寫不進去只提醒:狀態檔是加值,不能因為它讓檢查本身失敗。"""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".booster_integrity.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": STATE_VERSION, "roots": roots_state}, handle,
                      ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        out.write("[WARN] 狀態檔寫不進去,下次無法判斷是否持續:{}({})\n".format(path, _one_line(exc)))


def root_status(report, dev):
    if report.findings[CORRUPT] or report.findings[PERSISTENT] or report.stamp_broken:
        return "FAIL"
    if dev:
        return "OK"
    if report.unverified or any(report.findings[k] for k in (MISMATCH, SKEW, MISSING)):
        return "WARN"
    return "OK"


def print_report(report, dev, out):
    status = root_status(report, dev)
    if report.unverified:
        out.write("[{}] {}:無法驗證 —— {}\n".format(status, report.name, report.unverified))
        return status
    counts = "、".join("{} {}".format(CATEGORY_LABEL[k], len(v))
                      for k, v in report.findings.items() if v)
    out.write("[{}] {} {}:{} 個檔,相符 {}{}\n".format(
        status, report.name, report.version, report.total, report.ok,
        ",{}".format(counts) if counts else ""))
    for key, items in report.findings.items():
        if not items:
            continue
        # --dev:工作樹本來就跟發佈版不同,只有損毀值得逐檔列出。
        if dev and key != CORRUPT:
            continue
        tag = {CORRUPT: "CORRUPT", PERSISTENT: "PERSISTENT", MISMATCH: "MISMATCH",
               SKEW: "SKEW", MISSING: "MISSING"}[key]
        for rel, detail in items[:LIST_LIMIT]:
            out.write("  [{}] {}{}\n".format(tag, rel, " —— " + detail if detail else ""))
        if len(items) > LIST_LIMIT:
            out.write("  …另有 {} 個{}\n".format(len(items) - LIST_LIMIT, CATEGORY_LABEL[key]))
    if dev and any(report.findings[k] for k in (MISMATCH, SKEW, MISSING)):
        out.write("  (--dev:開發機工作樹與發佈版不同是常態,不一致只計數不列出)\n")
    return status


def _usage_error(out, message):
    sys.stderr.write("booster_integrity: {}\n".format(message))
    out.write("{} status=ERROR reason=usage\n".format(VERDICT_PREFIX))
    return EXIT_USAGE


def main(argv=None, out=None, now=None):
    out = out or sys.stdout
    now = time.time() if now is None else now
    args = list(sys.argv[1:] if argv is None else argv)
    dev = False
    state_path = None
    persist_days = DEFAULT_PERSIST_DAYS
    roots = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--dev":
            dev = True
        elif arg in ("-h", "--help"):
            out.write(__doc__)
            return EXIT_OK
        elif arg in ("--state", "--persist-days"):
            if i + 1 >= len(args):
                return _usage_error(out, "{} 需要一個值".format(arg))
            value = args[i + 1]
            i += 1
            if arg == "--state":
                state_path = value
            else:
                try:
                    persist_days = float(value)
                except ValueError:
                    persist_days = -1
                if persist_days <= 0:
                    return _usage_error(out, "--persist-days 必須是正數:{}".format(value))
        elif arg.startswith("-"):
            return _usage_error(out, "未知參數 {}".format(arg))
        else:
            roots.append(arg)
        i += 1
    if not roots:
        return _usage_error(out, "至少要指定一個專案根目錄")
    # --dev(CLINK):工作樹本來就跟發佈版不同,「持續」沒有意義。
    use_state = state_path is not None and not dev
    previous_state = load_state(state_path, out) if use_state else {}
    new_state = {}

    bash = shutil.which("bash")
    totals = OrderedDict((key, 0) for key in CATEGORY_LABEL)
    statuses = []
    unverified = 0
    for root in roots:
        report = check_root(root, dev=dev, bash=bash)
        if use_state and not report.unverified:
            new_state[report.name] = apply_persistence(
                report, previous_state.get(report.name, {}), now, persist_days)
        statuses.append(print_report(report, dev, out))
        unverified += 1 if report.unverified else 0
        for key, items in report.findings.items():
            totals[key] += len(items)

    if use_state:
        save_state(state_path, new_state, out)
    if "FAIL" in statuses:
        overall, code = "FAIL", EXIT_FAIL
    elif "WARN" in statuses:
        overall, code = "WARN", EXIT_WARN
    else:
        overall, code = "OK", EXIT_OK
    out.write("{} status={} roots={} corrupt={} persistent={} mismatch={} skew={} missing={} "
              "unverified={}{}\n"
              .format(VERDICT_PREFIX, overall, len(roots), totals[CORRUPT], totals[PERSISTENT],
                      totals[MISMATCH], totals[SKEW], totals[MISSING], unverified,
                      " dev=1" if dev else ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
