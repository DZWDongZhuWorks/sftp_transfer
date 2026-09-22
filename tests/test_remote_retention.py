# -*- coding: utf-8 -*-
"""remote_retention.py：岸端 SFTP log 的保留政策。

這支工具會刪掉別人機器上的檔案，所以測試的重點全放在**什麼情況下不該刪**。
"""
import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

import remote_retention as rr

DAY = 86400
ROOT = "/fleet/logs"


def make_task(tmp_path, sftp, *, retention_days=30, apply=False, logger=None, **kwargs):
    """直接建 RemoteRetention 並塞好假連線；run() 裡的連線與等網路都繞過。"""
    task = rr.RemoteRetention(
        host="shore.example.com", port=22, username="aiuser", password="x",
        remote_path=ROOT, local_path=str(tmp_path / "mirror"),
        wait_for_network=False, resume=False,
        delete_source_pattern=list(rr.DEFAULT_PATTERNS),
        logger=logger, retention_days=retention_days, apply=apply, **kwargs,
    )
    task.sftp = sftp
    task._connect_with_retry = lambda: None
    task._close = lambda: None
    return task


def remote_tree(ages_days, size=100):
    """{rel_path: 幾天前} → (files, mtimes) 給 FakeSFTPClient。"""
    files, mtimes = {}, {}
    now = time.time()
    for rel, age in ages_days.items():
        full = f"{ROOT}/{rel}"
        files[full] = b"x" * size
        mtimes[full] = now - age * DAY
    return files, mtimes


def mirror(tmp_path, rels, size=100):
    """在本地鏡像裡放出對應的檔案（大小預設與遠端相同）。"""
    for rel in rels:
        p = tmp_path / "mirror" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)


class TestRetentionDecision:
    def test_preview_reports_but_deletes_nothing(self, tmp_path, fake_sftp_factory, logger):
        """預設只預覽 —— 預覽與實刪走同一條判定，差別只在最後那一步。"""
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a_1.csv": 40})
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_a_1.csv"])
        task = make_task(tmp_path, sftp, logger=logger)

        assert task.run() == 0
        assert task.stats["deleted"] == 1      # 有算進「將刪除」
        assert sftp.remove_calls == []         # 但一個檔都沒動
        assert len(sftp.files) == 1

    def test_apply_deletes_only_beyond_the_window(self, tmp_path, fake_sftp_factory, logger):
        files, mtimes = remote_tree({
            "download/WH1/IPC-1/a/D_old.csv": 31,
            "download/WH1/IPC-1/a/D_edge.csv": 29,
            "download/WH1/IPC-1/a/D_new.csv": 1,
        })
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_old.csv",
                          "download/WH1/IPC-1/a/D_edge.csv",
                          "download/WH1/IPC-1/a/D_new.csv"])
        task = make_task(tmp_path, sftp, apply=True, logger=logger)

        assert task.run() == 0
        assert sftp.remove_calls == [f"{ROOT}/download/WH1/IPC-1/a/D_old.csv"]
        assert task.stats["deleted"] == 1

    def test_pattern_protects_everything_else(self, tmp_path, fake_sftp_factory, logger):
        """預設樣式只吃 D_*.csv／U_*.csv。哪天那棵樹多了別的東西,不該被一起刪掉。"""
        files, mtimes = remote_tree({
            "download/WH1/IPC-1/a/D_log.csv": 40,
            "download/WH1/IPC-1/a/U_log.csv": 40,
            "download/WH1/IPC-1/a/readme.txt": 40,
            "download/WH1/IPC-1/a/settings.json": 40,
            "download/WH1/IPC-1/a/report.csv": 40,      # 是 .csv 但沒有 D_/U_ 前綴
        })
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, list(k[len(ROOT) + 1:] for k in files))
        task = make_task(tmp_path, sftp, apply=True, logger=logger)

        assert task.run() == 0
        assert sorted(p.rsplit("/", 1)[-1] for p in sftp.remove_calls) == ["D_log.csv", "U_log.csv"]

    def test_unknown_mtime_is_kept(self, tmp_path, fake_sftp_factory, logger):
        """判斷不了年紀時不刪是唯一安全的選擇（沿用 _delete_source_kept_reason）。"""
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a.csv": 40})
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_a.csv"])
        task = make_task(tmp_path, sftp, apply=True, logger=logger)
        original = sftp.listdir_attr

        def strip_mtime(path):
            for attr in original(path):
                attr.st_mtime = None
                yield attr

        sftp.listdir_attr = lambda p: list(strip_mtime(p))
        assert task.run() == 0
        assert sftp.remove_calls == []

    def test_symlinks_are_skipped(self, tmp_path, fake_sftp_factory, logger):
        """`sftp.remove` 刪的是連結本身,而年紀是連結的 lstat —— 兩者對不上,一律不碰。"""
        import stat as stat_mod
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a.csv": 40})
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_a.csv"])
        task = make_task(tmp_path, sftp, apply=True, logger=logger)
        original = sftp.listdir_attr

        def as_link(path):
            out = []
            for attr in original(path):
                if attr.filename.endswith(".csv"):
                    attr.st_mode = (attr.st_mode & ~stat_mod.S_IFMT(attr.st_mode)) | stat_mod.S_IFLNK
                out.append(attr)
            return out

        sftp.listdir_attr = as_link
        assert task.run() == 0
        assert sftp.remove_calls == []
        assert task.stats["symlinks"] == 1


class TestLocalMirrorCheck:
    def test_size_mismatch_keeps_the_remote_copy(self, tmp_path, fake_sftp_factory, logger):
        """本地那份大小不符 ＝ 我們手上不是遠端現在這一版,不能刪。"""
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a.csv": 40}, size=100)
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_a.csv"], size=57)   # 不同大小
        task = make_task(tmp_path, sftp, apply=True, logger=logger)

        assert task.run() == 0
        assert sftp.remove_calls == []

    def test_missing_local_copy_still_deletes(self, tmp_path, fake_sftp_factory, logger):
        """本地那份不在是**預期**的：本地鏡像也是 30 天窗、用同一個時間戳。

        把「本地必須還在」當硬門檻的話,本地清掃先跑的日子遠端就永遠刪不掉（漏水）。
        這個降級是 30 天 / 30 天的代價,見 remote_retention 檔頭安全設計第 3 點。
        """
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a.csv": 40})
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/keep_me.csv"])   # 鏡像存在但沒有那一份
        task = make_task(tmp_path, sftp, apply=True, logger=logger)

        assert task.run() == 0
        assert sftp.remove_calls == [f"{ROOT}/download/WH1/IPC-1/a/D_a.csv"]


class TestEmptyDirs:
    def test_emptied_leaf_dir_is_removed_but_never_the_top_two_levels(
        self, tmp_path, fake_sftp_factory, logger
    ):
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a.csv": 40})
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_a.csv"])
        task = make_task(tmp_path, sftp, apply=True, remove_empty_dirs=True, logger=logger)

        assert task.run() == 0
        removed = [p[len(ROOT) + 1:] for p in sftp.rmdir_calls]
        # 由深到淺清掉,但 download 那一層永遠不動（它會被別船的上傳一直用到）
        assert "download/WH1/IPC-1/a" in removed
        assert "download/WH1" in removed
        assert "download" not in removed

    def test_dirs_are_left_alone_without_the_flag(self, tmp_path, fake_sftp_factory, logger):
        files, mtimes = remote_tree({"download/WH1/IPC-1/a/D_a.csv": 40})
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_a.csv"])
        task = make_task(tmp_path, sftp, apply=True, logger=logger)

        assert task.run() == 0
        assert sftp.rmdir_calls == []

    def test_dir_with_survivors_is_not_removed(self, tmp_path, fake_sftp_factory, logger):
        files, mtimes = remote_tree({
            "download/WH1/IPC-1/a/D_old.csv": 40,
            "download/WH1/IPC-1/a/D_new.csv": 1,
        })
        sftp = fake_sftp_factory(files=files, mtimes=mtimes)
        mirror(tmp_path, ["download/WH1/IPC-1/a/D_old.csv", "download/WH1/IPC-1/a/D_new.csv"])
        task = make_task(tmp_path, sftp, apply=True, remove_empty_dirs=True, logger=logger)

        assert task.run() == 0
        assert sftp.rmdir_calls == []      # rmdir 由伺服器判定「還有東西」而失敗
        assert task.stats["dirs_removed"] == 0


class TestSyncFreshnessFailClosed:
    """最重要的一道：同步壞了就一個檔都不刪。

    真實案例：09-15 人工清除的地板線 01:38:53 正好是我們那趟同步收工的時刻，
    只差五分鐘就會永久少掉 20 天的 download log。
    """

    def _config(self, tmp_path, mirror_dir):
        cfg = tmp_path / "sync.json"
        cfg.write_text(json.dumps({
            "mode": "download", "host": "h", "port": 22,
            "username": "u", "password": "p",
            "remote_path": ROOT, "local_path": str(mirror_dir),
            "log_dir": str(tmp_path / "logs"),
        }), encoding="utf-8")
        return cfg

    def test_aborts_when_mirror_has_no_records(self, tmp_path):
        m = tmp_path / "mirror"
        m.mkdir()
        with mock.patch.object(rr, "RemoteRetention") as cls:
            assert rr.main(["--config", str(self._config(tmp_path, m))]) == 2
        cls.assert_not_called()          # 連線都不該發生

    def test_aborts_when_newest_record_is_too_old(self, tmp_path):
        m = tmp_path / "mirror"
        (m / "download").mkdir(parents=True)
        stale = m / "download" / "D_a.csv"
        stale.write_bytes(b"x")
        old = time.time() - 5 * DAY
        os.utime(stale, (old, old))
        with mock.patch.object(rr, "RemoteRetention") as cls:
            assert rr.main(["--config", str(self._config(tmp_path, m)),
                            "--require-local-sync-hours", "48"]) == 2
        cls.assert_not_called()

    def test_proceeds_when_mirror_is_fresh(self, tmp_path):
        m = tmp_path / "mirror"
        (m / "download").mkdir(parents=True)
        (m / "download" / "D_a.csv").write_bytes(b"x")
        with mock.patch.object(rr, "RemoteRetention") as cls:
            cls.return_value.run.return_value = 0
            assert rr.main(["--config", str(self._config(tmp_path, m))]) == 0
        cls.assert_called_once()

    def test_html_and_manifest_do_not_count_as_freshness(self, tmp_path):
        """log_monitor.html 與 manifest 每輪都會被改寫,即使一個 log 都沒下載到 ——
        拿它們當「同步很新鮮」的證據是假的,所以只看符合樣式的紀錄。"""
        m = tmp_path / "mirror"
        m.mkdir()
        (m / "log_monitor.html").write_text("x", encoding="utf-8")
        (m / ".sftp_download_manifest.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(rr, "RemoteRetention") as cls:
            assert rr.main(["--config", str(self._config(tmp_path, m))]) == 2
        cls.assert_not_called()

    def test_check_can_be_switched_off(self, tmp_path):
        m = tmp_path / "mirror"
        m.mkdir()
        with mock.patch.object(rr, "RemoteRetention") as cls:
            cls.return_value.run.return_value = 0
            assert rr.main(["--config", str(self._config(tmp_path, m)),
                            "--require-local-sync-hours", "0"]) == 0
        cls.assert_called_once()


class TestConfigFailClosed:
    def test_missing_config_is_exit_2(self, tmp_path):
        assert rr.main(["--config", str(tmp_path / "nope.json")]) == 2

    def test_config_without_paths_is_exit_2(self, tmp_path):
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"host": "h", "username": "u"}), encoding="utf-8")
        assert rr.main(["--config", str(cfg)]) == 2


class TestNewestLocalMtime:
    def test_only_matching_names_count(self, tmp_path):
        root = tmp_path / "m"
        (root / "x").mkdir(parents=True)
        newer = root / "x" / "other.csv"
        newer.write_bytes(b"x")
        wanted = root / "x" / "D_a.csv"
        wanted.write_bytes(b"x")
        old = time.time() - 10 * DAY
        os.utime(wanted, (old, old))
        got = rr.newest_local_mtime(root, ["D_*.csv"])
        assert abs(got - old) < 2          # 只看 D_*.csv,不被 other.csv 拉新

    def test_returns_none_when_nothing_matches(self, tmp_path):
        root = tmp_path / "m"
        root.mkdir()
        assert rr.newest_local_mtime(root, ["D_*.csv"]) is None
