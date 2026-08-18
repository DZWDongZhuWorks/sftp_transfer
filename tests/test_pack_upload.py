"""pack_upload.py：沿用 upload 規則建立本地 tar，不連線到 SFTP。"""

import json
import logging
import tarfile
from pathlib import Path

import pytest

import pack_upload


def _write(path, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _settings(local_path, remote_path="/fleet/releases/radar", **overrides):
    result = {
        "mode": "upload",
        "local_path": local_path if isinstance(local_path, list) else str(local_path),
        "remote_path": remote_path,
        "recursive": True,
        "ignore_file": "",
    }
    result.update(overrides)
    return result


def test_archive_uses_upload_ignore_and_builtin_exclusions(tmp_path):
    source = tmp_path / "radar"
    _write(source / "keep.txt", b"keep")
    _write(source / "logs" / "skip.log", b"skip")
    _write(source / ".sftp_upload_manifest.json", b"{}")
    _write(source / ".sftp_download_manifest.json", b"{}")
    _write(source / "unfinished.bin.part", b"half")
    (source / "empty").mkdir(parents=True)
    ignore = tmp_path / "radar_ignore.txt"
    ignore.write_text("logs/\n", encoding="utf-8")

    plan = pack_upload.build_archive_plan(
        _settings(source, ignore_file=str(ignore)),
        logger=logging.getLogger("test_pack_upload"),
    )
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    with tarfile.open(output, "r") as archive:
        names = set(archive.getnames())
        assert "radar" in names
        assert "radar/empty" in names
        assert "radar/keep.txt" in names
        assert archive.extractfile("radar/keep.txt").read() == b"keep"
        assert not any(name.startswith("radar/logs") for name in names)
        assert "radar/.sftp_upload_manifest.json" not in names
        assert "radar/.sftp_download_manifest.json" not in names
        assert "radar/unfinished.bin.part" not in names


def test_non_recursive_matches_single_level_upload(tmp_path):
    source = tmp_path / "source"
    _write(source / "top.txt")
    _write(source / "nested" / "deep.txt")

    plan = pack_upload.build_archive_plan(_settings(source, recursive=False))

    assert [entry.archive_path for entry in plan.files] == ["radar/top.txt"]
    assert not any(entry.archive_path.startswith("radar/nested") for entry in plan.directories)


def test_merged_sources_use_later_file_like_uploader(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first / "same.txt", b"first")
    _write(second / "same.txt", b"second")

    settings = _settings([str(first), str(second)])
    plan = pack_upload.build_archive_plan(settings)
    output = pack_upload.write_archive(plan, tmp_path / "merged.tar")

    assert [entry.archive_path for entry in plan.files] == ["radar/same.txt"]
    with tarfile.open(output, "r") as archive:
        assert archive.extractfile("radar/same.txt").read() == b"second"


def test_paired_destinations_keep_distinct_archive_roots(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first / "x.txt", b"x")
    _write(second / "y.txt", b"y")

    settings = _settings(
        [str(first), str(second)],
        remote_path=["/fleet/releases/radar", "/fleet/releases/shm"],
    )
    plan = pack_upload.build_archive_plan(settings)

    assert [entry.archive_path for entry in plan.files] == ["radar/x.txt", "shm/y.txt"]


def test_existing_output_requires_force_and_force_replaces_it(tmp_path):
    source = tmp_path / "source"
    _write(source / "x.txt", b"new")
    plan = pack_upload.build_archive_plan(_settings(source))
    output = tmp_path / "radar.tar"
    output.write_bytes(b"old")

    with pytest.raises(FileExistsError, match="--force"):
        pack_upload.write_archive(plan, output)

    pack_upload.write_archive(plan, output, force=True)
    with tarfile.open(output, "r") as archive:
        assert archive.extractfile("radar/x.txt").read() == b"new"


def test_output_inside_source_is_explicitly_excluded(tmp_path):
    source = tmp_path / "source"
    _write(source / "x.txt")
    output = source / "radar.tar"
    output.write_bytes(b"old archive")

    plan = pack_upload.build_archive_plan(_settings(source), excluded_paths=(output,))

    assert [entry.archive_path for entry in plan.files] == ["radar/x.txt"]


def test_load_pack_settings_ignores_unrelated_placeholders_and_credentials(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "radar_upload_settings.json"
    config.write_text(
        json.dumps(
            {
                "mode": "upload",
                "local_path": str(source),
                "remote_path": "/fleet/radar",
                "recursive": True,
                "ignore_file": "",
                "device_name": "{vsl_name}_{ipc}",
                "password": "must-not-be-needed",
            }
        ),
        encoding="utf-8",
    )

    loaded = pack_upload.load_pack_settings(config)

    assert loaded == {
        "mode": "upload",
        "local_path": str(source),
        "remote_path": "/fleet/radar",
        "recursive": True,
        "ignore_file": "",
    }


def test_rejects_download_config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(ValueError, match="mode 必須是 upload"):
        pack_upload.build_archive_plan(_settings(source, mode="download"))


def _members(output):
    with tarfile.open(output, "r") as archive:
        return {m.name: m for m in archive.getmembers()}


def test_symlinks_inside_source_are_preserved_as_links(tmp_path):
    source = tmp_path / "radar"
    _write(source / "realdir" / "a.txt", b"A")
    (source / "filelink").symlink_to("realdir/a.txt")
    (source / "dirlink").symlink_to("realdir")

    plan = pack_upload.build_archive_plan(_settings(source))
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    members = _members(output)
    assert members["radar/filelink"].issym()
    assert members["radar/filelink"].linkname == "realdir/a.txt"
    assert members["radar/dirlink"].issym()
    assert members["radar/dirlink"].linkname == "realdir"
    # 連結沒有被展開成第二份內容。
    assert "radar/dirlink/a.txt" not in members
    assert members["radar/realdir/a.txt"].isfile()


def test_symlink_pointing_outside_source_stores_real_content(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "out.txt", b"OUT")
    source = tmp_path / "radar"
    _write(source / "keep.txt", b"keep")
    (source / "abslink").symlink_to(outside / "out.txt")
    (source / "outlink").symlink_to(outside)

    plan = pack_upload.build_archive_plan(_settings(source))
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    members = _members(output)
    # 保留成連結的話，解到目的端會指向不存在的路徑，所以改存實際內容。
    assert members["radar/abslink"].isfile()
    assert members["radar/outlink"].isdir()
    assert members["radar/outlink/out.txt"].isfile()
    with tarfile.open(output, "r") as archive:
        assert archive.extractfile("radar/abslink").read() == b"OUT"


def test_broken_symlink_inside_source_is_kept_instead_of_failing(tmp_path):
    source = tmp_path / "radar"
    _write(source / "keep.txt", b"keep")
    (source / "broken").symlink_to("nowhere.txt")

    plan = pack_upload.build_archive_plan(_settings(source))
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    members = _members(output)
    assert members["radar/broken"].issym()
    assert members["radar/broken"].linkname == "nowhere.txt"
    assert members["radar/keep.txt"].isfile()


def test_broken_symlink_outside_source_is_skipped(tmp_path):
    source = tmp_path / "radar"
    _write(source / "keep.txt", b"keep")
    (source / "absbroken").symlink_to(tmp_path / "gone" / "x.txt")

    plan = pack_upload.build_archive_plan(_settings(source))
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    members = _members(output)
    assert "radar/absbroken" not in members
    assert members["radar/keep.txt"].isfile()


def test_symlink_cycle_does_not_duplicate_the_subtree(tmp_path):
    source = tmp_path / "radar"
    _write(source / "realdir" / "a.txt", b"A")
    (source / "realdir" / "cycle").symlink_to("..")

    plan = pack_upload.build_archive_plan(_settings(source))
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    members = _members(output)
    assert members["radar/realdir/cycle"].issym()
    assert sorted(members) == ["radar", "radar/realdir", "radar/realdir/a.txt", "radar/realdir/cycle"]


def test_ignored_symlink_is_not_packed(tmp_path):
    source = tmp_path / "radar"
    _write(source / "realdir" / "a.txt", b"A")
    (source / "filelink").symlink_to("realdir/a.txt")
    ignore = tmp_path / "radar_ignore.txt"
    ignore.write_text("filelink\n", encoding="utf-8")

    plan = pack_upload.build_archive_plan(_settings(source, ignore_file=str(ignore)))
    output = pack_upload.write_archive(plan, tmp_path / "radar.tar")

    assert "radar/filelink" not in _members(output)
