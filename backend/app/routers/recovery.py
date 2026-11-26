"""装载窗口失败恢复、班组门槛和跨模块转交的专用接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.load_window import LoadWindowService
from app.services.shift_recovery import recovery_manager

router = APIRouter(prefix="/api/load_window/recovery", tags=["装载失败恢复"])
service = LoadWindowService()


def _pick(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return default


@router.get("/state")
def recovery_state() -> dict[str, Any]:
    return service.recovery_state()


@router.post("/reset")
def reset_state() -> dict[str, Any]:
    recovery_manager.reset()
    return {"ok": True, "message": "失败恢复状态已重置", "state": service.recovery_state()}


@router.post("/policy")
def update_policy(payload: dict[str, Any]) -> dict[str, Any]:
    threshold = _pick(payload, "failure_threshold", "threshold", "班组门槛", "上限")
    team = _pick(payload, "team", "班组")
    try:
        value = service.update_policy_threshold(int(threshold), team)
    except (TypeError, ValueError):
        return {"ok": False, "message": "班组门槛必须是 1 到 20 的整数"}
    return {
        "ok": True,
        "message": f"{team or recovery_manager.team}连续失败转交门槛已设为 {value}",
        "threshold": value,
        "team": team or recovery_manager.team,
    }


@router.post("/prepare-demo")
def prepare_demo() -> dict[str, Any]:
    return service.prepare_demo()


@router.post("/demo-sequence")
def demo_sequence(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(_pick(payload, "kind", "sequence") or "handoff")
    return service.run_demo_sequence(kind)


@router.post("/attempt")
def record_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    context = dict(payload.get("context") or {})
    context.update({key: value for key, value in payload.items() if key not in {"context"}})
    result = service.confirm_entry(
        entry_id=_pick(payload, "entry_id", "window_pk", "id"),
        window_pk=_pick(payload, "window_pk", "entry_id", "id"),
        window_code=_pick(payload, "window_code", "窗口编号"),
        fact_table=_pick(payload, "fact_table", "所属事实表"),
        context=context,
    )
    return result


@router.post("/handoff")
def submit_handoff(payload: dict[str, Any]) -> dict[str, Any]:
    result = service.submit_handoff(
        window_pk=_pick(payload, "window_pk", "entry_id", "id"),
        window_code=_pick(payload, "window_code", "窗口编号"),
        fact_table=_pick(payload, "fact_table", "所属事实表"),
        successor=_pick(payload, "successor", "接班人", "owner"),
        operator=_pick(payload, "operator", "值班员"),
        scheduler=_pick(payload, "scheduler", "排期员"),
        task_id=_pick(payload, "task_id", "重跑编号"),
        strategy=_pick(payload, "strategy", "覆盖策略"),
        reprocess_range=_pick(payload, "reprocess_range", "重跑区间"),
        context=payload,
    )
    return result


@router.post("/schedule-other")
def schedule_other(payload: dict[str, Any]) -> dict[str, Any]:
    return service.schedule_other(
        window_pk=_pick(payload, "window_pk", "entry_id", "id"),
        window_code=_pick(payload, "window_code", "窗口编号"),
        fact_table=_pick(payload, "fact_table", "所属事实表"),
        scheduler=_pick(payload, "scheduler", "排期员"),
        owner=_pick(payload, "owner", "责任人"),
        task_id=_pick(payload, "task_id", "重跑编号"),
    )


@router.post("/race")
def race(payload: dict[str, Any]) -> dict[str, Any]:
    return service.race_handoff_and_schedule(
        window_pk=_pick(payload, "window_pk", "entry_id", "id"),
        window_code=_pick(payload, "window_code", "窗口编号"),
        fact_table=_pick(payload, "fact_table", "所属事实表"),
        successor=_pick(payload, "successor", "接班人"),
        scheduler=_pick(payload, "scheduler", "排期员"),
        operator=_pick(payload, "operator", "值班员"),
    )
