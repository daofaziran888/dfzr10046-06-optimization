"""装载窗口连续失败门槛、跨模块转交和并发排期收敛测试。"""
from __future__ import annotations

from app.services.load_window import LoadWindowService
from app.services.reprocess_task import ReprocessTaskService
from app.services.shift_recovery import recovery_manager
from app.store import store


def setup_function() -> None:
    recovery_manager.reset()


def make_window(code: str, fact: str = "门槛测试事实表") -> dict:
    rows = store.rows("load_window")
    row = {
        "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
        "窗口编号": code,
        "所属事实表": fact,
        "窗口起点": "2026-11-20",
        "窗口终点": "2026-11-20",
        "预期行数": 1000,
        "实际行数": 1000,
        "装载水位": "",
        "装载耗时": "",
        "status": "待装载",
        "pending": True,
        "abnormal": False,
    }
    rows.append(row)
    return row


def fail(lw: LoadWindowService, row: dict, actual: int = 500) -> dict:
    return lw.confirm_entry(
        window_pk=row["id"],
        context={"outcome": "failure", "actual_rows": actual},
    )


def test_two_failures_still_allow_single_confirmation_and_reset() -> None:
    lw = LoadWindowService()
    row = make_window("LW-UT-RECOVER")

    first = fail(lw, row)
    second = fail(lw, row)

    assert first["ok"] is False
    assert first["receipt"]["failure_count"] == 1
    assert second["receipt"]["failure_count"] == 2
    assert second["receipt"]["threshold"] == 3
    assert second["receipt"]["threshold_reached"] is False
    assert second["receipt"]["can_confirm_load"] is True

    recovered = lw.confirm_entry(
        window_pk=row["id"], context={"outcome": "success", "actual_rows": 1000},
    )
    assert recovered["ok"] is True
    assert recovered["entry"]["status"] == "已装载"
    assert recovered["receipt"]["failure_count"] == 0
    assert recovered["receipt"]["can_continue_window"] is True


def test_legacy_single_confirm_without_row_counts_still_succeeds() -> None:
    lw = LoadWindowService()
    rows = store.rows("load_window")
    row = {
        "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
        "窗口编号": "LW-UT-LEGACY",
        "所属事实表": "门槛测试事实表",
        "窗口起点": "2026-11-20",
        "status": "待装载",
        "pending": True,
    }
    rows.append(row)

    entry, message, ok = lw.run_action(row["id"], "确认装载")
    assert ok is True
    assert entry is not None
    assert entry["status"] == "已装载"
    assert "单笔确认" in message


def test_team_actual_threshold_is_used() -> None:
    lw = LoadWindowService()
    row = make_window("LW-UT-POLICY")
    lw.update_policy_threshold(2)

    first = fail(lw, row)
    second = fail(lw, row)

    assert first["receipt"]["threshold"] == 2
    assert first["receipt"]["threshold_reached"] is False
    assert second["receipt"]["threshold"] == 2
    assert second["receipt"]["threshold_reached"] is True


def test_other_windows_failures_do_not_count() -> None:
    lw = LoadWindowService()
    target = make_window("LW-UT-TARGET-SCOPE")
    other = make_window("LW-UT-OTHER-SCOPE")

    for _ in range(5):
        fail(lw, other)

    result = fail(lw, target)
    receipt = result["receipt"]
    assert receipt["failure_count"] == 1
    assert receipt["other_window_failure_count_excluded"] >= 5
    assert receipt["threshold_reached"] is False


def test_third_failure_creates_handoff_todo_and_started_rerun_continues() -> None:
    lw = LoadWindowService()
    rp = ReprocessTaskService()
    row = make_window("LW-UT-HANDOFF")

    fail(lw, row)
    fail(lw, row)
    third = fail(lw, row)
    assert third["receipt"]["failure_count"] == 3
    assert third["receipt"]["threshold_reached"] is True
    assert third["receipt"]["handoff_available"] is True

    handed = lw.submit_handoff(window_pk=row["id"], successor="测试接班人")
    assert handed["ok"] is True
    task = handed["task"]
    assert task["status"] == "待排期"
    assert task["责任人"] == "测试接班人"
    assert handed["receipt"]["task"]["task_id"] == task["重跑编号"]

    scheduled, message = rp.run_action(task["id"], "排期重跑", {"scheduler": "测试接班人"})
    assert scheduled is not None
    assert scheduled["status"] == "重跑中"
    assert message

    recovered = lw.confirm_entry(
        window_pk=row["id"], context={"outcome": "success", "actual_rows": 1000},
    )
    receipt = recovered["receipt"]
    assert receipt["can_confirm_load"] is True
    assert receipt["confirmation_ok"] is True
    assert receipt["task_valid"] is True
    assert receipt["rerun_continues_after_recovery"] is True
    assert receipt["task"]["status"] == "重跑中"


def test_existing_concurrent_schedule_keeps_one_task_and_owner_in_receipt() -> None:
    lw = LoadWindowService()
    row = make_window("LW-UT-CONCURRENT-EXISTING")
    for _ in range(3):
        fail(lw, row)

    other = lw.schedule_other(
        window_pk=row["id"], scheduler="并发排期员甲", owner="责任人乙",
    )
    assert other["ok"] is True
    existing_id = other["task"]["重跑编号"]

    handed = lw.submit_handoff(window_pk=row["id"], successor="接班人丙")
    receipt = handed["receipt"]

    assert handed["ok"] is True
    assert handed["task"]["重跑编号"] == existing_id
    assert handed["handoff"]["deduplicated"] is True
    assert receipt["task"]["task_id"] == existing_id
    assert receipt["task"]["责任人"] == "责任人乙"
    assert receipt["valid_task_count"] == 1

    tasks = [
        item for item in store.rows("reprocess_task")
        if item.get("去重键") == recovery_manager.scope_key(
            "门槛测试事实表", "LW-UT-CONCURRENT-EXISTING",
        )
        and item.get("有效", True) is not False
    ]
    assert len(tasks) == 1


def test_true_race_creates_exactly_one_valid_task() -> None:
    lw = LoadWindowService()
    row = make_window("LW-UT-RACE")
    for _ in range(3):
        fail(lw, row)

    result = lw.race_handoff_and_schedule(
        window_pk=row["id"], successor="接班人", scheduler="排期员",
    )
    assert result["ok"] is True
    assert result["valid_task_count"] == 1
    assert result["receipt"]["only_one_valid_task"] is True
    assert result["receipt"]["task"]["责任人"]
