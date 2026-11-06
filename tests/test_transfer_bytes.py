"""TRANSFER_BYTES：每趟傳輸的檔案內容位元組數（見 downloader.SFTPBase._log_transfer_bytes）。

這一行是給「每個傳輸任務每天用掉多少流量」的統計用的，所以要守住的是**數字的意思**：
- 只算這一趟經過連線的位元組 —— 續傳前已在目的地的不算、略過的不算；
- 傳到一半斷掉的那幾塊**要算**（一樣走過了鏈路）；
- 每一種結局（成功、失敗、SIGTERM 取消）都要有這一行，且在 log 上傳之前；
- 加了這一行之後，log_monitor 對同一份 log 的判定不變。
"""

import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import downloader as dl  # noqa: E402
import uploader as up  # noqa: E402
from monitor.log_monitor import parse_log_file  # noqa: E402

FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def _transfer_bytes_lines(caplog):
    return [r.message for r in caplog.records if r.message.startswith("[TRANSFER_BYTES]")]


def _fields(message):
    return {k: json.loads(v) for k, v in FIELD.findall(message)}


def _only_fields(caplog):
    lines = _transfer_bytes_lines(caplog)
    assert len(lines) == 1, lines
    return _fields(lines[0])


def _wire(d, fake):
    d._connect_with_retry = MagicMock(side_effect=lambda: setattr(d, "sftp", fake))
    d._close = MagicMock()
    return d


class TestDownloadRun:
    def test_fresh_download_counts_every_byte(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        files = {"/remote/a.bin": b"A" * 1000, "/remote/sub/b.bin": b"B" * 234}
        fake = fake_sftp_factory(files=files, mtimes={p: 1 for p in files})
        d = _wire(downloader_factory(wait_for_network=False), fake)

        with caplog.at_level(logging.INFO):
            assert d.run() is True

        f = _only_fields(caplog)
        assert f == {"direction": "download", "bytes": 1234, "files": 2,
                     "elapsed_s": f["elapsed_s"], "result": "success"}

    def test_completion_line_carries_the_size(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        fake = fake_sftp_factory(files={"/remote/a.bin": b"A" * 2048}, mtimes={"/remote/a.bin": 1})
        d = _wire(downloader_factory(wait_for_network=False), fake)
        with caplog.at_level(logging.INFO):
            d.run()
        done = next(r.message for r in caplog.records if r.message.startswith("完成下載: a.bin"))
        # 單檔的量給人看；統計只讀 TRANSFER_BYTES 那一行。
        assert done.startswith(f"完成下載: a.bin（{dl.format_size(2048)}，平均 ")

    def test_second_run_that_skips_everything_moves_zero_bytes(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        fake = fake_sftp_factory(files={"/remote/a.bin": b"A" * 500}, mtimes={"/remote/a.bin": 1})
        d = _wire(downloader_factory(wait_for_network=False), fake)
        assert d.run() is True
        caplog.clear()

        with caplog.at_level(logging.INFO):
            assert d.run() is True

        f = _only_fields(caplog)
        assert (f["bytes"], f["files"], f["result"]) == (0, 0, "success")

    def test_resume_counts_only_what_this_run_fetched(self, downloader_factory, fake_sftp_factory, tmp_path):
        full = b"AAAAABBBBBCCCCCDDDDDEEEEE"
        (tmp_path / ("f.bin" + dl.PART_SUFFIX)).write_bytes(full[:10])
        d = downloader_factory(duplicate_mode="overwrite")
        d._manifest = {"f.bin": {"size": len(full), "mtime": 1000,
                                 "local_sha256": hashlib.sha256(full[:10]).hexdigest(), "local_bytes": 10}}
        d.sftp = fake_sftp_factory(files={"/remote/f.bin": full}, mtimes={"/remote/f.bin": 1000})

        assert d._download_one_file("/remote/f.bin", "f.bin", tmp_path) == "downloaded"
        assert (d._bytes_moved, d._files_moved) == (len(full) - 10, 1)

    def test_failed_run_still_reports(self, downloader_factory, fake_sftp_factory, tmp_path, caplog):
        d = downloader_factory(wait_for_network=False)
        d._connect_with_retry = MagicMock(side_effect=RuntimeError("boom"))
        d._close = MagicMock()
        with caplog.at_level(logging.INFO):
            assert d.run() is False
        f = _only_fields(caplog)
        assert (f["direction"], f["bytes"], f["result"]) == ("download", 0, "failed")

    def test_counters_reset_between_runs_on_the_same_object(
        self, downloader_factory, fake_sftp_factory, tmp_path, caplog
    ):
        fake = fake_sftp_factory(files={"/remote/a.bin": b"A" * 300}, mtimes={"/remote/a.bin": 1})
        d = _wire(downloader_factory(wait_for_network=False), fake)
        d.run()
        fake.files["/remote/b.bin"] = b"B" * 7
        fake.mtimes["/remote/b.bin"] = 1
        caplog.clear()
        with caplog.at_level(logging.INFO):
            d.run()
        f = _only_fields(caplog)
        assert (f["bytes"], f["files"]) == (7, 1)


class TestUploadRun:
    def test_fresh_upload_counts_every_byte(self, uploader_factory, fake_sftp_factory, tmp_path, caplog):
        (tmp_path / "a.txt").write_bytes(b"a" * 100)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_bytes(b"b" * 50)
        d = _wire(uploader_factory(wait_for_network=False), fake_sftp_factory(files={}))

        with caplog.at_level(logging.INFO):
            assert d.run() is True

        f = _only_fields(caplog)
        assert (f["direction"], f["bytes"], f["files"], f["result"]) == ("upload", 150, 2, "success")
        done = next(r.message for r in caplog.records if r.message.startswith("完成上傳: a.txt"))
        assert dl.format_size(100) in done

    def test_resume_counts_only_the_remainder(self, uploader_factory, fake_sftp_factory, tmp_path):
        content = b"Z" * (up.CHUNK_SIZE * 3)
        partial = up.CHUNK_SIZE
        local = tmp_path / "big.bin"
        local.write_bytes(content)
        d = uploader_factory()
        d.sftp = fake_sftp_factory(files={"/remote/big.bin": content[:partial]})
        d._manifest = {"big.bin": {"size": len(content), "mtime": int(local.stat().st_mtime),
                                   "local_sha256": hashlib.sha256(content[:partial]).hexdigest(),
                                   "local_bytes": partial}}

        assert d._upload_one_file(local, "big.bin", "/remote", tmp_path) == "uploaded"
        assert d._bytes_moved == len(content) - partial

    def test_bytes_sent_before_an_interruption_are_counted(
        self, uploader_factory, fake_sftp_factory, tmp_path
    ):
        content = b"C" * (up.CHUNK_SIZE * 3)
        local = tmp_path / "big.bin"
        local.write_bytes(content)
        d = uploader_factory()
        sftp = fake_sftp_factory(files={})
        original_open = sftp.open
        writes = {"n": 0}

        def failing_open(path, mode="rb"):
            fake_file = original_open(path, mode)
            original_write = fake_file.write

            def failing_write(chunk):
                writes["n"] += 1
                if writes["n"] == 2:
                    raise OSError("link dropped")
                return original_write(chunk)

            fake_file.write = failing_write
            return fake_file

        sftp.open = failing_open
        d.sftp = sftp
        with pytest.raises(OSError):
            d._upload_one_file(local, "big.bin", "/remote", tmp_path)
        # 第一塊已經送出去了：檔沒傳完，鏈路的流量照樣花掉。
        assert (d._bytes_moved, d._files_moved) == (up.CHUNK_SIZE, 0)

    def test_sigterm_still_reports_before_reraising(self, uploader_factory, fake_sftp_factory, tmp_path, caplog):
        (tmp_path / "a.txt").write_bytes(b"a" * 10)
        d = _wire(uploader_factory(wait_for_network=False, upload_log=True, remote_log_dir="/fleet/logs"),
                  fake_sftp_factory(files={}))
        d._upload_log_file = MagicMock()
        d._run = MagicMock(side_effect=up.TransferCancelled(15))

        with caplog.at_level(logging.INFO), pytest.raises(up.TransferCancelled):
            d.run()

        assert _only_fields(caplog)["result"] == "cancelled"
        d._upload_log_file.assert_not_called()  # 取消時照舊不上傳 log

    def test_line_is_written_before_the_log_upload(self, uploader_factory, fake_sftp_factory, tmp_path, caplog):
        # 回傳岸端的 CSV 要包含這一行，所以它必須在 _upload_log_file 之前寫。
        (tmp_path / "a.txt").write_bytes(b"a")
        d = _wire(uploader_factory(wait_for_network=False, upload_log=True, remote_log_dir="/fleet/logs"),
                  fake_sftp_factory(files={}))
        seen = []
        d._upload_log_file = MagicMock(side_effect=lambda: seen.extend(_transfer_bytes_lines(caplog)))
        with caplog.at_level(logging.INFO):
            d.run()
        assert len(seen) == 1

    def test_a_broken_logger_does_not_break_the_run_epilogue(self, uploader_factory):
        d = uploader_factory()
        d.logger = MagicMock()
        d.logger.info.side_effect = RuntimeError("handler exploded")
        d._log_transfer_bytes(0.0, "success")  # 不拋


class TestLogMonitorStillParses:
    def test_real_csv_with_the_new_line_is_still_a_success(
        self, downloader_factory, fake_sftp_factory, tmp_path, logger
    ):
        # 走本體真正的 CSV handler 寫出 log，再交給 log_monitor：驗的是實際落地的格式。
        log_path = tmp_path / "D_TEST_IPC-1_proj_20261106_010101.csv"
        logger.setLevel(logging.INFO)
        handler = dl._CSVFileHandler(str(log_path), "TEST_IPC-1_proj")
        logger.addHandler(handler)
        try:
            local = tmp_path / "dest"
            local.mkdir()
            fake = fake_sftp_factory(files={"/remote/a.bin": b"A" * 64}, mtimes={"/remote/a.bin": 1})
            d = _wire(downloader_factory(wait_for_network=False, local_path=str(local)), fake)
            assert d.run() is True
        finally:
            logger.removeHandler(handler)
            handler.close()

        text = log_path.read_text(encoding="utf-8-sig")
        assert "[TRANSFER_BYTES]" in text and "bytes=64" in text
        rec = parse_log_file(log_path)
        assert (rec.mode, rec.status, rec.success, rec.failed) == ("download", "success", 1, 0)
