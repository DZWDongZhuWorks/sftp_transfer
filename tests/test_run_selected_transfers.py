# -*- coding: utf-8 -*-
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import run_selected_transfers as selected


def _touch(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")


def test_scan_uses_same_filename_rules_as_run_all_scripts(tmp_path):
    _touch(tmp_path / "ecdis_download_settings.json")
    _touch(tmp_path / "ecdis_upload_settings.json")
    _touch(tmp_path / "radar_download_settings.json")
    _touch(tmp_path / "upload_test_settings.json")
    _touch(tmp_path / "notes.json")

    items = selected.scan_setting_files(tmp_path)

    assert [(item.project, item.mode) for item in items] == [
        ("ecdis", "download"),
        ("ecdis", "upload"),
        ("radar", "download"),
    ]


def test_scan_can_limit_direction(tmp_path):
    _touch(tmp_path / "a_download_settings.json")
    _touch(tmp_path / "a_upload_settings.json")

    items = selected.scan_setting_files(tmp_path, "upload")

    assert [item.mode for item in items] == ["upload"]


def test_toggle_all_visible_skips_dev_machine_downloads(tmp_path):
    download = selected.TransferItem(tmp_path / "a_download_settings.json", "download", "a")
    upload = selected.TransferItem(tmp_path / "a_upload_settings.json", "upload", "a")
    state = selected.SelectionState()

    selected.toggle_all_visible(state, [download, upload], downloads_disabled=True)

    assert state.selected == {upload.path}
    selected.toggle_item(state, download, downloads_disabled=True)
    assert download.path not in state.selected
    assert "禁止下載" in state.message


def test_filter_and_clamp_selection(tmp_path):
    items = [
        selected.TransferItem(tmp_path / "a_download_settings.json", "download", "a"),
        selected.TransferItem(tmp_path / "a_upload_settings.json", "upload", "a"),
    ]
    assert selected.visible_items(items, "upload") == [items[1]]

    state = selected.SelectionState(index=99, scroll=50)
    selected.clamp_state(state, items)
    assert state.index == 1
    assert state.scroll == 1


def test_execute_selected_continues_and_summarizes_failures(tmp_path):
    items = [
        selected.TransferItem(tmp_path / "a_download_settings.json", "download", "a"),
        selected.TransferItem(tmp_path / "b_upload_settings.json", "upload", "b"),
    ]
    with mock.patch.object(
        selected.subprocess,
        "run",
        side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=7)],
    ) as run:
        assert selected.execute_selected(items) == 1

    assert run.call_count == 2
    first_command = run.call_args_list[0].args[0]
    second_command = run.call_args_list[1].args[0]
    assert first_command[-4:] == ["--mode", "download", "--config", str(items[0].path)]
    assert second_command[-4:] == ["--mode", "upload", "--config", str(items[1].path)]
