# -*- coding: utf-8 -*-
"""deploy/automation_health_check.py 的 timer 判定測試。

不碰 systemd:把 systemctl_show() 換成回傳合成屬性的假函式,所以結果不隨「執行測試那台
機器現在裝了哪些 unit」而變(那個狀態會在部署後改變,測試跟著飄就等於沒有測試)。

【這一組守的是什麼】wave 兩支是 README 明載的空樁,而 scheduler 的 install_timers.sh 對
OPTIONAL_UNITS 刻意是「佈署 unit 檔但不 enable」—— 早期它們被 enable 起來、每 10 分鐘觸發
一次 status=127,把 failed 清單佔住並持續污染各 unit 的檔案 log。

所以 `UnitFileState=disabled` 是那兩支的**預期穩定狀態**。timer 這一層原本無條件用 FAIL
(optional 只套用在 evaluate_service 與 ExecStart 檢查上),於是每一條船都固定吐兩個紅字 →
整體 UNHEALTHY,而 deploy_offline.sh 把這支程式當作首次部署唯一的驗證關卡:真正的故障會被
那兩個常駐紅字遮蔽。
"""
import subprocess
import sys
from pathlib import Path
# typing.Dict 而不是 PEP 585 的 dict[...]：這支測試會在船上跑（health_check 會執行整個
# 測試套件），而 Bionic 的 venv 是 3.6 —— dict[str, str] 在 def 求值時就 TypeError，
# 那會讓**整份**測試套件收集失敗（1 error），健康檢查因此永遠是紅的。
from typing import Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

import automation_health_check as hc  # noqa: E402


# 一支健康的 timer 該有的屬性。
HEALTHY_TIMER = {
    "LoadState": "loaded",
    "UnitFileState": "enabled",
    "ActiveState": "active",
    "SubState": "waiting",
    "Result": "success",
    "NextElapseUSecRealtime": "Fri 2026-08-14 02:30:00 GMT",
    "LastTriggerUSec": "Thu 2026-08-13 02:30:00 GMT",
}
# install_timers.sh 對「腳本未提供的 optional unit」刻意留下的狀態:佈署了但沒 enable。
DEPLOYED_NOT_ENABLED = {
    "LoadState": "loaded",
    "UnitFileState": "disabled",
    "ActiveState": "inactive",
    "SubState": "dead",
    "Result": "success",
    "NextElapseUSecRealtime": "",
    "LastTriggerUSec": "",
}
# 一支真的壞掉的 timer(enable 了卻沒在跑)。
BROKEN_TIMER = {
    "LoadState": "loaded",
    "UnitFileState": "enabled",
    "ActiveState": "failed",
    "SubState": "failed",
    "Result": "exit-code",
    "NextElapseUSecRealtime": "",
    "LastTriggerUSec": "Thu 2026-08-13 02:30:00 GMT",
}
HEALTHY_ONESHOT = {
    "LoadState": "loaded",
    "UnitFileState": "static",
    "ActiveState": "inactive",
    "SubState": "dead",
    "Result": "success",
    "ExecMainStatus": "0",
}


@pytest.fixture
def fake_systemd(monkeypatch):
    """把 systemctl_show 換掉,並清空 RESULTS。回傳一個「設定每支 unit 屬性」的 dict。

    預設所有 timer 健康、所有 service 正常結束;測試只覆寫它關心的那幾支。
    """
    states = {}  # type: Dict[str, Dict[str, str]]

    def fake_show(unit, *properties):
        if unit in states:
            return dict(states[unit], _returncode="0")
        base = HEALTHY_TIMER if unit.endswith(".timer") else HEALTHY_ONESHOT
        return dict(base, _returncode="0")

    monkeypatch.setattr(hc, "systemctl_show", fake_show)
    # record() 會 print,測試不需要那些輸出;RESULTS 是斷言的來源。
    monkeypatch.setattr(hc, "_COMPACT", True)
    hc.RESULTS.clear()
    return states


def timer_checks():
    """只取 section == "timers" 的結果 → {unit: status}。"""
    return {c.name: c.status for c in hc.RESULTS if c.section == "timers"}


def test_healthy_timer_passes(fake_systemd):
    hc.check_timers(strict_wave=False)
    assert timer_checks()["nssms-warm-env.timer"] == "PASS"


def test_wave_deployed_but_not_enabled_is_skip_not_fail(fake_systemd):
    """**這是回歸測試的核心。**

    install_timers.sh 刻意不 enable 腳本未提供的 wave 空樁,所以 disabled 不是故障。
    這一項若變回 FAIL,全船隊的巡檢會固定 UNHEALTHY,把真正的故障蓋掉。
    """
    fake_systemd["nssms-wave-send.timer"] = DEPLOYED_NOT_ENABLED
    fake_systemd["nssms-wave-update.timer"] = DEPLOYED_NOT_ENABLED

    hc.check_timers(strict_wave=False)
    checks = timer_checks()

    assert checks["nssms-wave-send.timer"] == "SKIP"
    assert checks["nssms-wave-update.timer"] == "SKIP"
    # 降級不得外溢到別的 timer。
    assert checks["nssms-cleanup-old-files.timer"] == "PASS"
    assert checks["nssms-warm-env.timer"] == "PASS"
    # 整體不得因為 wave 而變成有 FAIL。
    assert not [c for c in hc.RESULTS if c.status == "FAIL"]


def test_skip_detail_explains_why(fake_systemd):
    """報告上的 SKIP 必須說明原因,否則看起來像「這項沒被檢查」。"""
    fake_systemd["nssms-wave-send.timer"] = DEPLOYED_NOT_ENABLED
    hc.check_timers(strict_wave=False)
    detail = next(
        c.detail for c in hc.RESULTS
        if c.section == "timers" and c.name == "nssms-wave-send.timer"
    )
    assert "install_timers" in detail
    assert "--strict-wave" in detail


def test_strict_wave_restores_fail(fake_systemd):
    """--strict-wave 要能還原成嚴格檢查 —— 降級不是「永遠放過 wave」。"""
    fake_systemd["nssms-wave-send.timer"] = DEPLOYED_NOT_ENABLED
    hc.check_timers(strict_wave=True)
    assert timer_checks()["nssms-wave-send.timer"] == "FAIL"


def test_non_optional_timer_still_fails_when_disabled(fake_systemd):
    """降級只給 OPTIONAL_WAVE。非 optional 的 timer 沒 enable 就是真的有問題 ——
    那代表 install_timers 沒跑成功或有人手動 disable 掉了排程。"""
    fake_systemd["nssms-cleanup-old-files.timer"] = DEPLOYED_NOT_ENABLED
    hc.check_timers(strict_wave=False)
    assert timer_checks()["nssms-cleanup-old-files.timer"] == "FAIL"


def test_wave_enabled_but_failed_is_still_skip_in_lenient_mode(fake_systemd):
    """wave 真的壞掉時,非嚴格模式仍記 SKIP(與 evaluate_service 的既有語意一致),
    但 --strict-wave 要抓得到 —— 這一條把「可選」的邊界寫下來,免得日後誤以為
    非嚴格模式會替你發現 wave 的故障。"""
    fake_systemd["nssms-wave-send.timer"] = BROKEN_TIMER
    hc.check_timers(strict_wave=False)
    assert timer_checks()["nssms-wave-send.timer"] == "SKIP"

    hc.RESULTS.clear()
    hc.check_timers(strict_wave=True)
    assert timer_checks()["nssms-wave-send.timer"] == "FAIL"


def test_timers_list_covers_every_scheduler_timer(fake_systemd):
    """TIMERS 必須涵蓋 scheduler/timers/ 內每一支。

    同樣的斷言也住在 device_monitor/tests/test_integration.sh —— 那一份是從 scheduler
    那側檢查的,只有在三個專案都在場時才跑得到。這裡多釘一次,讓 sftp_transfer 自己的
    測試就能抓到「scheduler 新增了 timer 但這份清單忘了跟上」。

    scheduler 不在場時 skip:三個專案由 SFTP 各自獨立下載,不該讓「那個專案沒下載好」
    變成這支測試失敗。
    """
    timers_dir = hc.TIMERS_DIR
    if not timers_dir.is_dir():
        pytest.skip(f"scheduler 不在場:{timers_dir}")
    on_disk = {p.stem for p in timers_dir.glob("*.timer")}
    assert on_disk, f"{timers_dir} 內沒有任何 .timer"
    missing = on_disk - set(hc.TIMERS)
    assert not missing, f"TIMERS 沒涵蓋:{sorted(missing)}"


def test_ipc3_n_a_timer_is_skip_when_absent(fake_systemd, monkeypatch, tmp_path):
    """IPC3 沒有 failover/web 工作負載；未安裝限制型 timer 是正確狀態。"""
    monkeypatch.setattr(hc, "USER_UNIT_DIR", tmp_path / "units")
    absent = {
        "LoadState": "not-found",
        "UnitFileState": "",
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
    }
    fake_systemd["nssms-download-photos.timer"] = absent
    fake_systemd["nssms-download-photos.service"] = absent

    hc.check_timers(strict_wave=False, role="ipc3")

    assert timer_checks()["nssms-download-photos.timer"] == "SKIP"
    assert timer_checks()["nssms-warm-env.timer"] == "PASS"


def test_ipc3_n_a_timer_fails_if_still_active(fake_systemd, monkeypatch, tmp_path):
    """Bionic 曾忽略 ExecCondition；IPC3 上殘留且 active 的 timer 必須浮成 FAIL。"""
    monkeypatch.setattr(hc, "USER_UNIT_DIR", tmp_path / "units")
    fake_systemd["nssms-download-photos.timer"] = HEALTHY_TIMER
    fake_systemd["nssms-download-photos.service"] = HEALTHY_ONESHOT

    hc.check_timers(strict_wave=False, role="ipc3")

    assert timer_checks()["nssms-download-photos.timer"] == "FAIL"


def test_ipc3_identity_ignores_failover_flag(monkeypatch, tmp_path):
    vessel_info = tmp_path / "vessel.json"
    vessel_info.write_text(
        '{"vsl_name":"WH102","ipc":"IPC3","failover":true}', encoding="utf-8"
    )
    monkeypatch.setattr(hc, "VESSEL_INFO", vessel_info)
    monkeypatch.setattr(hc, "LEGACY_FAILOVER_STATE", tmp_path / "absent.json")
    monkeypatch.setattr(hc, "_COMPACT", True)
    hc.RESULTS.clear()

    vessel, role = hc.check_identity()

    assert (vessel, role) == ("WH102", "ipc3")
    takeover = next(c for c in hc.RESULTS if c.name == "接管狀態")
    assert takeover.status == "WARN"
    assert "忽略" in takeover.detail


def test_ipc3_heartbeat_probe_is_skip_without_config(monkeypatch, tmp_path):
    """IPC3 不需要 failover/config.json，更不應實際開 socket 探測。"""
    monkeypatch.setattr(hc, "FAILOVER_DIR", tmp_path / "missing-failover")
    monkeypatch.setattr(hc, "_COMPACT", True)
    hc.RESULTS.clear()

    hc.heartbeat_probe("ipc3")

    check = next(c for c in hc.RESULTS if c.name == "角色探針")
    assert check.status == "SKIP"
    assert "不參與" in check.detail


def test_unknown_role_still_checks_gated_timers(fake_systemd, monkeypatch, tmp_path):
    """身分檔缺席/壞掉時,限制型 unit 不可一律當成 N/A。

    role="unknown" 是 check_identity 讀不到身分檔時的回傳值。若那時把有 NSSMS-BaseIPC
    宣告的 unit 都判成 N/A,一台裝好的 ipc1 會被報成「N/A 卻殘留 unit,請重跑安裝器」——
    把操作者指向重裝正確的東西,而真正的問題(身分檔)已經另有一筆 FAIL 在記錄。
    """
    monkeypatch.setattr(hc, "USER_UNIT_DIR", tmp_path / "units")
    fake_systemd["nssms-download-photos.timer"] = HEALTHY_TIMER
    fake_systemd["nssms-download-photos.service"] = HEALTHY_ONESHOT

    hc.check_timers(strict_wave=False, role="unknown")

    assert timer_checks()["nssms-download-photos.timer"] == "PASS"


def test_takeover_role_keeps_base_ipc_applicability(fake_systemd, monkeypatch, tmp_path):
    """接管中的 ipc2emer 仍是實體 ipc2：照片同步照樣要跑（適用性是 failover-invariant）。"""
    monkeypatch.setattr(hc, "USER_UNIT_DIR", tmp_path / "units")
    fake_systemd["nssms-download-photos.timer"] = HEALTHY_TIMER
    fake_systemd["nssms-download-photos.service"] = HEALTHY_ONESHOT

    hc.check_timers(strict_wave=False, role="ipc2emer")

    assert timer_checks()["nssms-download-photos.timer"] == "PASS"


def _fake_run(returncode, stdout="", stderr=""):
    def runner(cmd, timeout=15):
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return runner


def test_sudoers_check_accepts_the_shipped_whitelist(monkeypatch, tmp_path):
    """白名單的指令路徑是跨 repo 契約：scheduler 那份改了,這支巡檢必須跟著。

    sudo 是逐字比對指令路徑的,所以「巡檢期待的字串」與「scheduler 出貨的白名單」一旦
    分岔,不是巡檢誤報 FAIL,就是巡檢對真正的錯誤路徑蓋章 PASS（WH102-3 就是後者：
    白名單寫 /usr/bin/systemctl,而 Bionic 只有 /bin/systemctl）。
    """
    sudoers_src = hc.SCHEDULER_DIR / "etc" / "nssms-scheduler.sudoers"
    if not sudoers_src.is_file():
        pytest.skip(f"scheduler 不在場:{sudoers_src}")
    placeholder = tmp_path / "nssms-scheduler"
    placeholder.write_text("", encoding="utf-8")
    monkeypatch.setattr(hc, "SUDOERS_FILE", placeholder)
    monkeypatch.setattr(hc, "_COMPACT", True)
    monkeypatch.setattr(
        hc, "run", _fake_run(0, sudoers_src.read_text(encoding="utf-8"))
    )
    hc.RESULTS.clear()

    hc.check_sudoers()

    check = next(c for c in hc.RESULTS if c.name == "NOPASSWD 白名單")
    assert check.status == "PASS", check.detail


STALE_WHITELIST = (
    "  (root) NOPASSWD: /usr/bin/systemctl reboot, "
    "/usr/bin/teamviewer daemon restart\n"
)


def _stale_whitelist_check(monkeypatch, tmp_path, legacy_path):
    placeholder = tmp_path / "nssms-scheduler"
    placeholder.write_text("", encoding="utf-8")
    monkeypatch.setattr(hc, "SUDOERS_FILE", placeholder)
    monkeypatch.setattr(hc, "LEGACY_SYSTEMCTL", legacy_path)
    monkeypatch.setattr(hc, "_COMPACT", True)
    monkeypatch.setattr(hc, "run", _fake_run(0, STALE_WHITELIST))
    hc.RESULTS.clear()

    hc.check_sudoers()

    return next(c for c in hc.RESULTS if c.name == "NOPASSWD 白名單")


def test_stale_whitelist_is_only_drift_where_the_old_path_exists(monkeypatch, tmp_path):
    """Jammy:sudo 比對前會解符號連結,舊拼法仍放行得了 unit 的 /bin/systemctl。

    實測(sudo 1.9.9、/bin -> usr/bin、裝著舊白名單):`sudo -l /bin/systemctl reboot`
    回報的是白名單那條 `/usr/bin/systemctl reboot`,而參數改成沒放行的 `restart foo`
    就退回原樣回印 —— 證明命中的是那條規則,不是 fallthrough。所以只吃 OTA 的機器
    不會壞;報 FAIL 只會讓它們固定紅一條,把真正的故障蓋掉。
    """
    legacy = tmp_path / "systemctl"
    legacy.write_text("", encoding="utf-8")

    check = _stale_whitelist_check(monkeypatch, tmp_path, legacy)

    assert check.status == "WARN"
    assert "deploy_offline.sh" in check.detail


def test_stale_whitelist_is_a_real_failure_where_the_old_path_is_absent(
    monkeypatch, tmp_path
):
    """Bionic 沒有 usr-merge:/usr/bin/systemctl 不存在,sudo 無從比對 → 真的壞掉。"""
    check = _stale_whitelist_check(monkeypatch, tmp_path, tmp_path / "absent-systemctl")

    assert check.status == "FAIL"
    assert "/usr/bin/systemctl" in check.detail
    assert "deploy_offline.sh" in check.detail


def test_timezone_falls_back_to_etc_timezone(monkeypatch, tmp_path):
    """Bionic 的 systemd 237 沒有 `timedatectl show`,不該讓報告固定印 unknown。"""
    monkeypatch.setattr(hc, "run", _fake_run(1, "", "Unknown command verb show."))
    etc_timezone = tmp_path / "timezone"
    etc_timezone.write_text("Asia/Taipei\n", encoding="utf-8")
    monkeypatch.setattr(hc, "ETC_TIMEZONE", etc_timezone)

    assert hc.detect_timezone() == "Asia/Taipei"


def test_timezone_falls_back_to_localtime_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "run", _fake_run(1, "", "Unknown command verb show."))
    monkeypatch.setattr(hc, "ETC_TIMEZONE", tmp_path / "absent")
    localtime = tmp_path / "localtime"
    localtime.symlink_to("/usr/share/zoneinfo/Asia/Taipei")
    monkeypatch.setattr(hc, "ETC_LOCALTIME", localtime)

    assert hc.detect_timezone() == "Asia/Taipei"


def test_timezone_reports_unknown_when_nothing_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "run", _fake_run(1, "", "Unknown command verb show."))
    monkeypatch.setattr(hc, "ETC_TIMEZONE", tmp_path / "absent")
    monkeypatch.setattr(hc, "ETC_LOCALTIME", tmp_path / "absent-localtime")

    assert hc.detect_timezone() == "unknown"
