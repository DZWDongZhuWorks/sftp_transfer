"""deploy/booster_integrity.py 單元測試。

重點是「什麼算損毀、什麼只是不一致」那條線(判定規則見 booster_integrity.py 檔頭):
鏡像更新不是原子的,stamp 不符本身不足以喊 FAIL —— 否則每次發佈新版,warm 跑到一半的
那幾天全船隊每天都會紅一次,紅燈很快就沒人看。反過來,0 byte、截斷、同大小變質這些
真正的損毀一個都不能放過:那正是這支檢查器存在的理由(main.py 0 byte 時整條 OTA 安靜停擺)。
"""

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "deploy"))

import booster_integrity as B  # noqa: E402
import downloader  # noqa: E402
import version_stamp  # noqa: E402

CHECKER = PROJECT_DIR / "deploy" / "booster_integrity.py"

GOOD_FILES = {
    "main.py": b"import sys\n\ndef main():\n    return 0\n\nif __name__ == '__main__':\n    sys.exit(main())\n",
    "pkg/__init__.py": b"",
    "run.sh": b"#!/usr/bin/env bash\nif true; then\n  echo hi\nfi\n",
    "config/settings.json": b'{"remote_path": "/fleet", "port": 22}\n',
    "notes.txt": b"plain text, no structure to check\n",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_root(base, name="sftp_transfer", files=None, manifest=None, stamp=True):
    """建一棵「跟發佈版完全一致」的樹;manifest=True 代表每個檔都有整檔雜湊(完整下載過)。"""
    files = dict(GOOD_FILES if files is None else files)
    root = Path(str(base)) / name
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if stamp:
        (root / B.STAMP_NAME).write_text(json.dumps({
            "version": "1.2.3", "commit": "abc1234",
            "files": {rel: sha(data) for rel, data in files.items()},
        }), encoding="utf-8")
    if manifest:
        (root / B.DOWNLOAD_MANIFEST_NAME).write_text(json.dumps({
            rel: {"size": len(data), "mtime": 1, "local_bytes": len(data),
                  "local_sha256": sha(data)}
            for rel, data in files.items()
        }), encoding="utf-8")
    return root


def run(*args):
    out = io.StringIO()
    code = B.main([str(a) for a in args], out=out)
    return code, out.getvalue()


def verdict(text):
    lines = [ln for ln in text.splitlines() if ln.startswith(B.VERDICT_PREFIX + " ")]
    assert lines, "沒有結論行:\n" + text
    return lines[-1]


# --- 基本情形 ---------------------------------------------------------------

def test_matching_tree_is_ok(tmp_path):
    root = make_root(tmp_path)
    code, text = run(root)
    assert code == B.EXIT_OK
    assert "status=OK" in verdict(text)
    assert "相符 5" in text


def test_legitimately_empty_file_is_not_corruption(tmp_path):
    """空的 __init__.py 在 stamp 裡就是空檔的雜湊 —— 0 byte 本身不是罪。"""
    code, _ = run(make_root(tmp_path))
    assert code == B.EXIT_OK


def test_verdict_line_is_always_last(tmp_path):
    root = make_root(tmp_path)
    (root / "main.py").write_bytes(b"")
    _, text = run(root)
    assert text.splitlines()[-1].startswith(B.VERDICT_PREFIX + " ")


# --- 損毀:不需要任何 manifest 證據就能確定的 -------------------------------

def test_zero_byte_main_py_is_corrupt(tmp_path):
    """促成這支檢查器的情境:main.py 0 byte,執行起來 rc=0、什麼都不做。"""
    root = make_root(tmp_path)
    (root / "main.py").write_bytes(b"")
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "status=FAIL" in verdict(text)
    assert "[CORRUPT] main.py" in text
    assert "0 byte" in text


def test_truncated_python_is_corrupt_even_without_baseline(tmp_path):
    root = make_root(tmp_path)
    (root / "main.py").write_bytes(GOOD_FILES["main.py"][:21])   # 截在 def main 之後
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "Python 編譯失敗" in text


def test_nul_bytes_in_python_is_corrupt(tmp_path):
    root = make_root(tmp_path)
    data = GOOD_FILES["main.py"]
    (root / "main.py").write_bytes(data[:10] + b"\x00" * 4 + data[14:])
    code, _ = run(root)
    assert code == B.EXIT_FAIL


def test_truncated_shell_script_is_corrupt(tmp_path):
    root = make_root(tmp_path)
    (root / "run.sh").write_bytes(b"#!/usr/bin/env bash\nif true; then\n  echo hi\n")
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "bash -n 失敗" in text


def test_truncated_json_is_corrupt(tmp_path):
    root = make_root(tmp_path)
    (root / "config/settings.json").write_bytes(b'{"remote_path": "/fl')
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "JSON 解析失敗" in text


# --- 損毀:靠下載 manifest 的整檔雜湊證明「下載之後變質」 ------------------

def test_same_size_bit_rot_is_corrupt_when_download_baseline_exists(tmp_path):
    """略過分支只比 size/mtime,這種檔每一輪都被當成已完整下載 —— 只有這裡抓得到。"""
    root = make_root(tmp_path, manifest=True)
    data = bytearray(GOOD_FILES["notes.txt"])
    data[3] ^= 0x01
    (root / "notes.txt").write_bytes(bytes(data))
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "下載後變質" in text


def test_size_changed_after_download_is_corrupt(tmp_path):
    root = make_root(tmp_path, manifest=True)
    (root / "notes.txt").write_bytes(GOOD_FILES["notes.txt"][:5])
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "大小與下載完成時不同" in text


def test_partial_checkpoint_is_not_a_baseline(tmp_path):
    """local_bytes < size 是傳到一半的檢查點,雜湊只涵蓋前段,不能拿來判損毀。"""
    root = make_root(tmp_path)
    changed = b"plain text, other version!!!!!!!!\n"
    (root / "notes.txt").write_bytes(changed)
    (root / B.DOWNLOAD_MANIFEST_NAME).write_text(json.dumps({
        "notes.txt": {"size": len(changed), "mtime": 1, "local_bytes": 4,
                      "local_sha256": sha(changed[:4])},
    }), encoding="utf-8")
    code, text = run(root)
    assert code == B.EXIT_WARN
    assert "[MISMATCH] notes.txt" in text


# --- 不一致:只 WARN,不能喊 FAIL --------------------------------------------

def test_unexplained_mismatch_only_warns(tmp_path):
    """船上多數檔還沒有整檔雜湊:內容跟 stamp 不同但結構完好,分不出是新版還是壞檔。"""
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"a different but well-formed release\n")
    code, text = run(root)
    assert code == B.EXIT_WARN
    assert "status=WARN" in verdict(text)
    assert "[MISMATCH] notes.txt" in text


def test_complete_download_of_other_version_is_skew_not_corruption(tmp_path):
    """warm 跑到一半:這個檔已是新版的完整下載(manifest 整檔雜湊相符),stamp 還是舊版。"""
    root = make_root(tmp_path)
    newer = b"import sys\n\ndef main():\n    return 1\n"
    (root / "main.py").write_bytes(newer)
    (root / B.DOWNLOAD_MANIFEST_NAME).write_text(json.dumps({
        "main.py": {"size": len(newer), "mtime": 2, "local_bytes": len(newer),
                    "local_sha256": sha(newer)},
    }), encoding="utf-8")
    code, text = run(root)
    assert code == B.EXIT_WARN
    assert "[SKEW] main.py" in text


def test_missing_file_only_warns(tmp_path):
    root = make_root(tmp_path)
    (root / "notes.txt").unlink()
    code, text = run(root)
    assert code == B.EXIT_WARN
    assert "[MISSING] notes.txt" in text


def test_files_not_in_stamp_are_ignored(tmp_path):
    """船上才有的檔(manifest、logs、failover/config.json)不在 stamp 裡,不關我們的事。"""
    root = make_root(tmp_path)
    (root / "logs").mkdir()
    (root / "logs" / "x.csv").write_bytes(b"")
    code, _ = run(root)
    assert code == B.EXIT_OK


# --- stamp 本身 -------------------------------------------------------------

def test_no_stamp_is_unverified_warning(tmp_path):
    root = make_root(tmp_path, stamp=False)
    code, text = run(root)
    assert code == B.EXIT_WARN
    assert "unverified=1" in verdict(text)


def test_corrupt_stamp_is_failure(tmp_path):
    """stamp 也是隨鏡像上船的檔;它壞了就什麼都驗不了,不能安靜地當成「沒有 stamp」。"""
    root = make_root(tmp_path)
    (root / B.STAMP_NAME).write_bytes(b"")
    code, text = run(root)
    assert code == B.EXIT_FAIL
    assert "讀取失敗" in text


def test_stamp_cannot_reach_outside_the_root(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"x")
    stamp = json.loads((root / B.STAMP_NAME).read_text(encoding="utf-8"))
    stamp["files"]["../secret.txt"] = sha(b"y")
    stamp["files"][str(outside)] = sha(b"y")
    (root / B.STAMP_NAME).write_text(json.dumps(stamp), encoding="utf-8")
    code, text = run(root)
    assert code == B.EXIT_OK
    assert "secret" not in text


def test_missing_root_is_unverified(tmp_path):
    code, text = run(tmp_path / "nope")
    assert code == B.EXIT_WARN
    assert "目錄不存在" in text


# --- 多個根目錄 / --dev / 參數 ---------------------------------------------

def test_worst_root_decides_overall_status(tmp_path):
    good = make_root(tmp_path, name="scheduler")
    bad = make_root(tmp_path, name="sftp_transfer")
    (bad / "main.py").write_bytes(b"")
    code, text = run(good, bad)
    assert code == B.EXIT_FAIL
    line = verdict(text)
    assert "roots=2" in line and "corrupt=1" in line
    assert "[OK] scheduler" in text and "[FAIL] sftp_transfer" in text


def test_dev_mode_tolerates_drift_but_not_breakage(tmp_path):
    """CLINK:工作樹與發佈版不同是常態,但 0 byte / 語法壞掉照樣是損毀。"""
    root = make_root(tmp_path, manifest=True)
    (root / "notes.txt").write_bytes(b"edited on the dev machine, same-ish\n")
    code, text = run(root, "--dev")
    assert code == B.EXIT_OK
    assert "[MISMATCH]" not in text and "dev=1" in verdict(text)
    (root / "main.py").write_bytes(b"")
    code, _ = run(root, "--dev")
    assert code == B.EXIT_FAIL


def test_dev_mode_ignores_stale_download_manifest(tmp_path):
    """開發機的 manifest 是陳年的:大小不同在那裡是手改,不是損毀。"""
    root = make_root(tmp_path, manifest=True)
    (root / "notes.txt").write_bytes(b"much longer edited content than the release had\n")
    assert run(root)[0] == B.EXIT_FAIL
    assert run(root, "--dev")[0] == B.EXIT_OK


@pytest.mark.parametrize("args", [[], ["--bogus", "x"]])
def test_usage_errors_still_print_a_verdict(args):
    code, text = run(*args)
    assert code == B.EXIT_USAGE
    assert "status=ERROR" in verdict(text)


# --- 與發佈端 / 下載器的規則保持一致 -----------------------------------------

def test_hash_matches_version_stamp(tmp_path):
    """基準是 version_stamp.sha256_file 算的;兩邊算法一旦走鐘,全部檔都會變成不一致。"""
    path = tmp_path / "f.bin"
    path.write_bytes(b"\x00\x01" * 70000)
    assert B.sha256_file(str(path)) == version_stamp.sha256_file(path)


@pytest.mark.parametrize("entry", [
    None, {}, {"size": 5}, {"size": 5, "local_bytes": 5},
    {"size": 5, "local_bytes": 5, "local_sha256": "a" * 64},
    {"size": 5, "local_bytes": 4, "local_sha256": "a" * 64},
    {"size": True, "local_bytes": True, "local_sha256": "a" * 64},
    {"size": 5, "local_bytes": 5, "local_sha256": "short"},
    {"size": "5", "local_bytes": 5, "local_sha256": "a" * 64},
])
def test_whole_file_digest_agrees_with_downloader(entry):
    """刻意複製而不 import downloader(被檢查的對象不能是檢查器的相依),所以要釘住一致性。"""
    ours = B.whole_file_digest(entry)
    theirs = downloader.whole_file_digest(entry)
    assert (ours is not None) == bool(theirs)
    if ours is not None:
        assert ours == (theirs["local_sha256"], theirs["local_bytes"])


def test_checker_imports_nothing_from_the_project():
    imports = [ln.strip() for ln in CHECKER.read_text(encoding="utf-8").splitlines()
               if ln.startswith(("import ", "from "))]
    assert imports, "掃描失效:找不到任何 import 行"
    for module in ("downloader", "settings", "version_stamp", "main", "uploader", "gitignore"):
        for line in imports:
            assert line.split()[1].split(".")[0] != module, line


# --- 以子程序執行:呼叫端看到的就是這個 ---------------------------------------

def test_cli_reports_zero_byte_main_py(tmp_path):
    root = make_root(tmp_path)
    (root / "main.py").write_bytes(b"")
    proc = subprocess.run([sys.executable, str(CHECKER), str(root)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == B.EXIT_FAIL
    assert proc.stdout.decode("utf-8").splitlines()[-1].startswith("BOOSTER_INTEGRITY status=FAIL")


def test_empty_checker_exits_zero_without_verdict(tmp_path):
    """記錄呼叫端契約的理由:檢查器自己被截成 0 byte 時 rc=0 —— 只看離開碼會以為一切正常。"""
    empty = tmp_path / "booster_integrity.py"
    empty.write_bytes(b"")
    proc = subprocess.run([sys.executable, str(empty), str(tmp_path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0
    assert B.VERDICT_PREFIX not in proc.stdout.decode("utf-8")


# --- 持續不一致(--state):好幾天都一模一樣,就不是「更新進行中」 ---------------------

DAY = 86400.0
T0 = 1800000000.0


def run_at(now, *args):
    out = io.StringIO()
    code = B.main([str(a) for a in args], out=out, now=now)
    return code, out.getvalue()


def test_mismatch_persisting_three_days_becomes_failure(tmp_path):
    """大小沒變的 bit rot / 語法仍完整的截斷:結構驗證抓不到,只有時間抓得到。"""
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"silently rotted content, same shape\n")
    state = tmp_path / "state.json"
    code, _ = run_at(T0, root, "--state", state)
    assert code == B.EXIT_WARN
    code, _ = run_at(T0 + 2.9 * DAY, root, "--state", state)
    assert code == B.EXIT_WARN, "還沒滿 3 天,仍可能是更新進行中"
    code, text = run_at(T0 + 3 * DAY, root, "--state", state)
    assert code == B.EXIT_FAIL
    assert "persistent=1" in verdict(text) and "mismatch=0" in verdict(text)
    assert "[PERSISTENT] notes.txt" in text and "已連續 3 天" in text and "很可能已損毀" in text


def test_persisting_skew_points_at_the_shore_copy(tmp_path):
    """完整下載、卻一直不是發佈時的樣子 → 岸端那份本身壞了;重抓只會抓回同一份。"""
    root = make_root(tmp_path)
    shore = b"what the shore actually serves\n"
    (root / "notes.txt").write_bytes(shore)
    (root / B.DOWNLOAD_MANIFEST_NAME).write_text(json.dumps({
        "notes.txt": {"size": len(shore), "mtime": 1, "local_bytes": len(shore),
                      "local_sha256": sha(shore)}}), encoding="utf-8")
    state = tmp_path / "state.json"
    assert run_at(T0, root, "--state", state)[0] == B.EXIT_WARN
    code, text = run_at(T0 + 4 * DAY, root, "--state", state)
    assert code == B.EXIT_FAIL
    assert "岸端 STANDARD 上那份很可能本身就壞了" in text


def test_persisting_missing_file_is_a_failure(tmp_path):
    root = make_root(tmp_path)
    (root / "notes.txt").unlink()
    state = tmp_path / "state.json"
    run_at(T0, root, "--state", state)
    code, text = run_at(T0 + 3 * DAY, root, "--state", state)
    assert code == B.EXIT_FAIL and "漏送或被刪" in text


def test_new_release_restarts_the_clock(tmp_path):
    """岸上發佈新版 → stamp 的 sha 變了 → 換版期間的不一致不能被誤判為持續。"""
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"old release content\n")
    state = tmp_path / "state.json"
    run_at(T0, root, "--state", state)
    stamp = json.loads((root / B.STAMP_NAME).read_text(encoding="utf-8"))
    stamp["files"]["notes.txt"] = sha(b"brand new release\n")
    (root / B.STAMP_NAME).write_text(json.dumps(stamp), encoding="utf-8")
    run_at(T0 + 2 * DAY, root, "--state", state)
    code, _ = run_at(T0 + 4 * DAY, root, "--state", state)
    assert code == B.EXIT_WARN, "新版從 T0+2 天才開始計時,到 T0+4 天只有 2 天"


def test_local_change_restarts_the_clock_and_fix_clears_state(tmp_path):
    root = make_root(tmp_path)
    state = tmp_path / "state.json"
    (root / "notes.txt").write_bytes(b"first wrong content\n")
    run_at(T0, root, "--state", state)
    (root / "notes.txt").write_bytes(b"second wrong content\n")   # 下載換過一次內容
    assert run_at(T0 + 3 * DAY, root, "--state", state)[0] == B.EXIT_WARN
    (root / "notes.txt").write_bytes(GOOD_FILES["notes.txt"])      # 修好了
    assert run_at(T0 + 4 * DAY, root, "--state", state)[0] == B.EXIT_OK
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["roots"]["sftp_transfer"] == {}, "修好的不一致要從狀態裡消失"


def test_without_state_nothing_persists(tmp_path):
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"different\n")
    assert run_at(T0, root)[0] == B.EXIT_WARN
    assert run_at(T0 + 30 * DAY, root)[0] == B.EXIT_WARN


def test_dev_mode_never_uses_state(tmp_path):
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"edited on CLINK\n")
    state = tmp_path / "state.json"
    run_at(T0, root, "--dev", "--state", state)
    assert run_at(T0 + 30 * DAY, root, "--dev", "--state", state)[0] == B.EXIT_OK
    assert not state.exists()


def test_broken_or_unwritable_state_never_breaks_the_check(tmp_path):
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"different\n")
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    code, text = run_at(T0, root, "--state", bad)
    assert code == B.EXIT_WARN and "狀態檔讀不出來" in text
    code, text = run_at(T0, root, "--state", tmp_path / "no-such-dir" / "state.json")
    assert code == B.EXIT_WARN and "狀態檔寫不進去" in text
    assert text.splitlines()[-1].startswith(B.VERDICT_PREFIX + " ")


@pytest.mark.parametrize("args", [["--state"], ["--persist-days", "0"], ["--persist-days", "x"]])
def test_state_option_usage_errors(tmp_path, args):
    code, text = run(make_root(tmp_path), *args)
    assert code == B.EXIT_USAGE and "status=ERROR" in verdict(text)


def test_persist_days_is_configurable(tmp_path):
    root = make_root(tmp_path)
    (root / "notes.txt").write_bytes(b"different\n")
    state = tmp_path / "state.json"
    run_at(T0, root, "--state", state, "--persist-days", "1")
    assert run_at(T0 + DAY, root, "--state", state, "--persist-days", "1")[0] == B.EXIT_FAIL
