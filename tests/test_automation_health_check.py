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
import sys
from pathlib import Path

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
    states: dict[str, dict[str, str]] = {}

    def fake_show(unit: str, *properties: str) -> dict[str, str]:
        if unit in states:
            return dict(states[unit], _returncode="0")
        base = HEALTHY_TIMER if unit.endswith(".timer") else HEALTHY_ONESHOT
        return dict(base, _returncode="0")

    monkeypatch.setattr(hc, "systemctl_show", fake_show)
    # record() 會 print,測試不需要那些輸出;RESULTS 是斷言的來源。
    monkeypatch.setattr(hc, "_COMPACT", True)
    hc.RESULTS.clear()
    return states


def timer_checks() -> dict[str, str]:
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
