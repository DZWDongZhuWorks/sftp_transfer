# -*- coding: utf-8 -*-
import json
from pathlib import Path
from unittest import mock

import run_selected_transfers as selected


def _touch(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")


def _write(path: Path, **fields) -> Path:
    path.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
    return path


def _telemetry(path: Path, mode: str, project: str) -> "selected.TransferItem":
    return selected.TransferItem(path, mode, project, selected.TELEMETRY)


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


def test_toggle_all_visible_skips_locked_direction(tmp_path):
    download = selected.TransferItem(tmp_path / "a_download_settings.json", "download", "a")
    upload = selected.TransferItem(tmp_path / "a_upload_settings.json", "upload", "a")

    publish_state = selected.SelectionState()
    selected.toggle_all_visible(publish_state, [download, upload], locked_mode="download")
    assert publish_state.selected == {upload.path}
    selected.toggle_item(publish_state, download, locked_mode="download")
    assert download.path not in publish_state.selected
    assert "禁止下載" in publish_state.message

    deploy_state = selected.SelectionState()
    selected.toggle_all_visible(deploy_state, [download, upload], locked_mode="upload")
    assert deploy_state.selected == {download.path}
    selected.toggle_item(deploy_state, upload, locked_mode="upload")
    assert upload.path not in deploy_state.selected
    assert "回灌 OTA" in deploy_state.message


def test_locked_mode_for_machine_role_is_fail_safe():
    assert selected.locked_mode_for_role(dev_machine=True) == "download"
    assert selected.locked_mode_for_role(dev_machine=False) == "upload"


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
        selected.TransferItem(tmp_path / "b_download_settings.json", "download", "b"),
    ]
    with mock.patch.object(
        selected,
        "_run_transfer",
        side_effect=[(0, (3, 4, 0)), (7, (1, 2, 5))],
    ) as run:
        assert selected.execute_selected(items, locked_mode="upload") == 1

    assert run.call_count == 2
    # [0][0] 而不是 .args[0]：Call.args 是 3.8 才有，3.6 上會取到一個名為 args 的
    # 子 Call（斷言因此比較的是兩個不相干的東西）。船上的 venv 是 3.6。
    assert run.call_args_list[0][0][0] == items[0]
    assert run.call_args_list[1][0][0] == items[1]


def test_execute_selected_prints_file_counts_for_each_item(tmp_path, capsys):
    items = [
        selected.TransferItem(tmp_path / "a_download_settings.json", "download", "a"),
        selected.TransferItem(tmp_path / "b_download_settings.json", "download", "b"),
    ]
    with mock.patch.object(
        selected,
        "_run_transfer",
        side_effect=[(0, (3, 4, 0)), (7, (1, 2, 5))],
    ):
        selected.execute_selected(items, locked_mode="upload")

    output = capsys.readouterr().out
    assert "[下載] a (成功)｜成功 3，略過 4，失敗 0" in output
    assert "[下載] b (失敗 rc=7)｜成功 1，略過 2，失敗 5" in output


def test_execute_selected_blocks_wrong_direction_before_subprocess(tmp_path):
    forbidden = selected.TransferItem(tmp_path / "old_upload_settings.json", "upload", "old")

    # patch _run_transfer 而非 subprocess.run：實際傳輸走的是 subprocess.Popen，
    # 盯著 run 會讓這個斷言恆真，抓不到「forbidden 檢查被移除」的迴歸。
    with mock.patch.object(selected, "_run_transfer") as run:
        assert selected.execute_selected([forbidden], locked_mode="upload") == 3

    run.assert_not_called()


# --- trans_type：流類別守門 ---------------------------------------------------


def test_telemetry_is_exempt_from_direction_lock_on_both_roles(tmp_path):
    """回傳流兩個方向都碰不到方向鎖保護的東西，故兩端都可選。"""
    download = _telemetry(tmp_path / "fleet_reports_download_settings.json", "download", "r")
    upload = _telemetry(tmp_path / "report_upload_settings.json", "upload", "report")

    # CLINK 發佈端（鎖 download）仍可抓全船隊報表
    assert selected.is_selectable(download, locked_mode="download")
    # 部署端（鎖 upload）仍可回傳自己的報表
    assert selected.is_selectable(upload, locked_mode="upload")


def test_deploy_stays_locked_in_both_directions(tmp_path):
    download = selected.TransferItem(tmp_path / "radar_download_settings.json", "download", "radar")
    upload = selected.TransferItem(tmp_path / "radar_upload_settings.json", "upload", "radar")

    assert not selected.is_selectable(download, locked_mode="download")
    assert not selected.is_selectable(upload, locked_mode="upload")


def test_toggle_all_visible_includes_telemetry_in_locked_direction(tmp_path):
    deploy_download = selected.TransferItem(tmp_path / "a_download_settings.json", "download", "a")
    telemetry_download = _telemetry(tmp_path / "r_download_settings.json", "download", "r")

    state = selected.SelectionState()
    selected.toggle_all_visible(state, [deploy_download, telemetry_download], locked_mode="download")
    assert state.selected == {telemetry_download.path}

    selected.toggle_item(state, telemetry_download, locked_mode="download")
    assert telemetry_download.path not in state.selected
    assert state.message == ""


def test_execute_selected_allows_telemetry_in_locked_direction(tmp_path):
    item = _telemetry(tmp_path / "r_download_settings.json", "download", "r")

    with mock.patch.object(selected, "_run_transfer", return_value=(0, (1, 0, 0))) as run:
        assert selected.execute_selected([item], locked_mode="download") == 0

    run.assert_called_once()


def test_trans_type_defaults_to_deploy_when_unclear(tmp_path):
    """缺欄位／值拼錯／JSON 壞掉／檔案不存在，一律 fail-closed。"""
    missing_field = _write(tmp_path / "a_upload_settings.json", mode="upload")
    typo = _write(tmp_path / "b_upload_settings.json", trans_type="telemetary")
    wrong_type = _write(tmp_path / "c_upload_settings.json", trans_type=["telemetry"])
    broken = tmp_path / "d_upload_settings.json"
    broken.write_text("{not json", encoding="utf-8")
    not_a_dict = tmp_path / "e_upload_settings.json"
    not_a_dict.write_text("[1, 2, 3]", encoding="utf-8")
    absent = tmp_path / "f_upload_settings.json"

    for path in (missing_field, typo, wrong_type, broken, not_a_dict, absent):
        assert selected.read_trans_type(path, "upload") == selected.DEPLOY, path.name


def test_telemetry_label_cannot_unlock_uploads_to_the_publish_tree(tmp_path):
    """標籤只能讓守門更嚴：誤標的發佈類上傳仍受管制。"""
    standard = _write(
        tmp_path / "device_monitor_upload_settings.json",
        trans_type="telemetry",
        remote_path="/fleet/wanhai_nssms_deploy/STANDARD/share/device_monitor",
    )
    unique = _write(
        tmp_path / "devices_upload_settings.json",
        trans_type="telemetry",
        remote_path="/fleet/wanhai_nssms_deploy/UNIQUE",
    )
    # 路徑陣列只要有任何一條落在發佈樹就算
    mixed = _write(
        tmp_path / "mixed_upload_settings.json",
        trans_type="telemetry",
        remote_path=[
            "/fleet/wanhai_nssms_deploy/device_monitor_reports",
            "/fleet/wanhai_nssms_deploy/STANDARD/radar",
        ],
    )

    for path in (standard, unique, mixed):
        assert selected.read_trans_type(path, "upload") == selected.DEPLOY, path.name

    honest = _write(
        tmp_path / "report_upload_settings.json",
        trans_type="telemetry",
        remote_path="/fleet/wanhai_nssms_deploy/device_monitor_reports/{vsl_name}/{ipc}",
    )
    assert selected.read_trans_type(honest, "upload") == selected.TELEMETRY


def test_scan_reads_trans_type_without_resolving_placeholders(tmp_path, monkeypatch):
    """回歸守衛：不得改用 settings.load_settings()。

    它會做佔位符解析，在沒有 vessel_basic_info.json 的機器上會拋 PlaceholderError，
    讓整個選單開不起來。掃描只需要欄位字面值。
    """
    monkeypatch.setenv("VESSEL_INFO_PATH", str(tmp_path / "does_not_exist.json"))
    _write(
        tmp_path / "report_upload_settings.json",
        trans_type="telemetry",
        remote_path="/fleet/wanhai_nssms_deploy/device_monitor_reports/{vsl_name}/{ipc}",
        local_path="/data/{vsl_name}/reports",
    )
    _write(
        tmp_path / "radar_download_settings.json",
        remote_path="/fleet/wanhai_nssms_deploy/UNIQUE/{vsl_name}/radar",
    )

    items = selected.scan_setting_files(tmp_path)

    assert [(item.project, item.trans_type) for item in items] == [
        ("radar", selected.DEPLOY),
        ("report", selected.TELEMETRY),
    ]
