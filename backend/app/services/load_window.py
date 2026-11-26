"""装载窗口领域服务：维护装载区间管理和偏差检测，校验水位推进，产出装载窗口数据集。

失败恢复统计范围固定为「所属事实表 + 窗口编号」；班组门槛从共享班组设置读取。
连续失败达到门槛后不自动建重跑任务，而是由值班员发起跨模块转交；转交提交时
通过同一把锁和去重键收敛并发排期，保证一个窗口只有一条有效重跑任务。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from app.services.base import BaseModuleService
from app.services.reprocess_task import ReprocessTaskService
from app.services.shift_recovery import recovery_manager
from app.store import store


class LoadWindowService(BaseModuleService):
    MODULE = "load_window"
    ENTITY = "装载窗口"
    LIST_FIELDS = ["窗口编号", "所属事实表", "窗口起点", "窗口终点", "预期行数", "实际行数", "装载水位", "装载耗时"]
    REQUIRED_FIELDS = ["窗口编号", "所属事实表", "窗口起点"]
    STATUS_ORDER = ["待装载", "装载中", "已装载", "装载中断"]
    ACTION_RULES = {"启动装载": "装载中", "确认装载": "已装载", "标记中断": "装载中断", "发起转交": "已转交"}
    NEGATIVE_ACTIONS = ["标记中断"]

    # ---------- 基础解析与判定 ----------

    @staticmethod
    def _parse_int(value: Any, default: int | None = None) -> int | None:
        if value is None or value == "":
            return default
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return default

    def _resolve_window(
        self,
        window_pk: int | str | None = None,
        window_code: str | None = None,
        fact_table: str | None = None,
    ) -> dict[str, Any] | None:
        pk = self._parse_int(window_pk)
        if pk is not None:
            row = store.find(self.MODULE, pk)
            if row is not None:
                return row

        code = str(window_code or "").strip()
        fact = str(fact_table or "").strip()
        if code:
            matches = [
                row for row in store.rows(self.MODULE)
                if str(row.get("窗口编号", "")).strip() == code
                and (not fact or str(row.get("所属事实表", "")).strip() == fact)
            ]
            if matches:
                return matches[-1]
            if not fact:
                code_matches = [
                    row for row in store.rows(self.MODULE)
                    if str(row.get("窗口编号", "")).strip() == code
                ]
                if len(code_matches) == 1:
                    return code_matches[0]
        return None

    def _evaluate_confirmation(
        self,
        row: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        expected = self._parse_int(
            context.get("expected_rows", context.get("预期行数")),
            self._parse_int(row.get("预期行数"), 0),
        ) or 0
        actual = self._parse_int(
            context.get("actual_rows", context.get("实际行数")),
            self._parse_int(row.get("实际行数"), 0),
        ) or 0
        forced = str(context.get("outcome") or context.get("result") or "").strip().lower()
        explicit_failure = forced in {"failure", "fail", "error", "failed", "失败"}
        explicit_success = forced in {"success", "ok", "succeeded", "成功"}

        if expected <= 0:
            return {
                "ok": False, "expected": expected, "actual": actual,
                "deviation": None, "reason": f"预期行数 {expected} 无效",
            }

        deviation = abs(actual - expected) / expected
        ok = explicit_success or (not explicit_failure and deviation <= 0.20)
        if explicit_failure and deviation <= 0.20:
            reason = context.get("failure_reason") or context.get("reason") or "值班员确认装载失败"
        elif not ok:
            reason = f"实际行数 {actual} 与预期 {expected} 偏差 {deviation:.1%}，窗口不得推进"
        else:
            reason = "确认装载成功"
        return {
            "ok": ok, "expected": expected, "actual": actual,
            "deviation": deviation, "reason": str(reason),
        }

    def _task_summary(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if task is None:
            return None
        return {
            "id": task.get("id"),
            "task_id": task.get("重跑编号"),
            "重跑编号": task.get("重跑编号"),
            "status": task.get("status"),
            "责任人": task.get("责任人") or "未指定责任人",
            "owner": task.get("责任人") or "未指定责任人",
            "排期人": task.get("排期人") or "",
            "scheduler": task.get("排期人") or "",
            "有效": task.get("有效", True),
            "valid": task.get("有效", True) is not False,
            "source_window": task.get("来源窗口", ""),
            "dedupe_key": task.get("去重键", ""),
        }

    def _find_task_for_scope(self, state: dict[str, Any]) -> dict[str, Any] | None:
        return ReprocessTaskService().find_effective_task(
            dedupe_key=state.get("scope_key"),
            source_window=state.get("window_code"),
            fact_table=state.get("fact_table"),
        )

    def _build_receipt(
        self,
        row: dict[str, Any] | None,
        state: dict[str, Any],
        evaluation: dict[str, Any] | None = None,
        handoff: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        *,
        accepted: bool = True,
        source: str = "action",
    ) -> dict[str, Any]:
        fact = state.get("fact_table", "")
        window_code = state.get("window_code", "")
        threshold = recovery_manager.threshold_for(state.get("team"))
        count = int(state.get("consecutive_failures", 0))
        threshold_reached = bool(state.get("threshold_reached")) or count >= threshold
        if task is None:
            task = self._find_task_for_scope(state)
        task_summary = self._task_summary(task)

        same_fact_other_windows = [
            event for event in recovery_manager.events
            if event.get("fact_table") == fact
            and event.get("window_code") != window_code
            and event.get("outcome") == "failure"
        ]
        included_events = [
            event.get("event_id") for event in recovery_manager.events
            if event.get("scope_key") == state.get("scope_key")
            and event.get("outcome") == "failure"
        ]
        window_loaded = bool(row and row.get("status") == "已装载") or bool(state.get("recovered"))
        task_valid = bool(task_summary and task_summary.get("valid"))
        task_status = str(task_summary.get("status", "")) if task_summary else ""
        todo_valid = task_valid and task_status in {"待排期", "重跑中", "重跑失败"}
        pending_todo = task_valid and task_status in {"待排期", "重跑失败"}
        rerun_active = task_valid and task_status == "重跑中"
        rerun_completed = task_valid and task_status == "已完成"

        if evaluation is None:
            confirmation_ok = window_loaded
        else:
            confirmation_ok = bool(evaluation.get("ok"))

        can_confirm = row is not None
        can_continue = window_loaded and not (row and row.get("status") == "装载中断")
        if evaluation is not None:
            can_continue = confirmation_ok

        if not threshold_reached:
            handoff_status = "未达门槛"
        elif handoff or state.get("handoff_id"):
            handoff_status = "已转交"
        else:
            handoff_status = "待值班员提交转交"

        if threshold_reached and not handoff and not state.get("handoff_id"):
            next_step = "已达班组门槛：值班员可发起跨模块转交；纠正数据后仍可做恢复性确认。"
        elif handoff_status == "已转交":
            next_step = "转交判定已留存；窗口恢复不会撤销有效重跑，已开始的重跑继续。"
        elif confirmation_ok:
            next_step = "本次确认装载成功，连续失败计数已清零，窗口可继续推进。"
        else:
            next_step = f"连续失败 {count}/{threshold}，未达门槛，仍按既有单笔确认装载处理。"

        receipt_id = recovery_manager.next_code("RCPT")
        receipt: dict[str, Any] = {
            "receipt_id": receipt_id,
            "判定编号": state.get("decision_id") or (handoff or {}).get("decision_id", ""),
            "handoff_id": (handoff or {}).get("转交编号") or state.get("handoff_id", ""),
            "scope_key": state.get("scope_key"),
            "统计范围": {"所属事实表": fact, "窗口编号": window_code},
            "fact_table": fact,
            "window_code": window_code,
            "team": recovery_manager.team,
            "班组": recovery_manager.team,
            "threshold": threshold,
            "failure_threshold": threshold,
            "班组门槛": threshold,
            "failure_count": count,
            "consecutive_failures": count,
            "连续失败次数": count,
            "threshold_reached": threshold_reached,
            "门槛已达": threshold_reached,
            "accepted": accepted,
            "第三次失败已接收": bool(state.get("threshold_accepted") or threshold_reached),
            "source": source,
            "evaluation": evaluation or {},
            "deviation": (evaluation or {}).get("deviation"),
            "confirmation_ok": confirmation_ok,
            "确认装载结果": "成功" if confirmation_ok else "失败",
            "can_confirm_load": can_confirm,
            "还能否确认装载": "能：可提交纠正后的恢复性确认" if can_confirm else "不能：窗口不存在",
            "can_continue_window": can_continue,
            "可否继续推进": "可以继续推进" if can_continue else "暂不可推进，需恢复或接手",
            "handoff_available": threshold_reached and not state.get("handoff_id"),
            "handoff_required": threshold_reached and not state.get("handoff_id"),
            "handoff_needed": threshold_reached and not state.get("handoff_id"),
            "handoff_status": handoff_status,
            "转交状态": handoff_status,
            "handoff": handoff,
            "task": task_summary,
            "reprocess_task": task_summary,
            "重跑任务": task_summary,
            "task_valid": task_valid,
            "reprocess_task_valid": task_valid,
            "todo_valid": todo_valid,
            "reprocess_todo_valid": todo_valid,
            "pending_todo_valid": pending_todo,
            "rerun_completed": rerun_completed,
            "排期员待办有效": todo_valid,
            "排期员待办仍在": pending_todo,
            "rerun_continues_after_recovery": rerun_active or todo_valid,
            "窗口恢复后重跑继续": rerun_active or todo_valid,
            "valid_task_count": 1 if task_valid else 0,
            "included_failure_events": included_events,
            "计入本窗口的失败事件": included_events,
            "other_window_failure_count_excluded": len(same_fact_other_windows),
            "其他窗口失败不计入数量": len(same_fact_other_windows),
            "next_step": next_step,
            "回执说明": next_step,
            "created_at": recovery_manager.timestamp(),
        }

        if threshold_reached and task_summary:
            receipt["冲突结论"] = (
                "第三次失败已接收；窗口恢复只改变装载窗口状态，不作废重跑任务。"
                f"值班员仍可确认装载，任务 {task_summary.get('task_id')} "
                f"（责任人：{task_summary.get('责任人')}）继续有效。"
            )
        elif threshold_reached:
            receipt["冲突结论"] = (
                "第三次失败已接收；值班员仍可纠正数据并确认装载恢复窗口，"
                "提交转交后生成的重跑待办继续有效。"
            )

        if row is not None:
            row["连续失败次数"] = count
            row["failure_count"] = count
            row["consecutive_failures"] = count
            row["班组门槛"] = threshold
            row["threshold"] = threshold
            row["threshold_reached"] = threshold_reached
            row["门槛已达"] = threshold_reached
            row["最近回执编号"] = receipt_id
            row["可否继续推进"] = receipt["可否继续推进"]
            row["handoff_required"] = receipt["handoff_required"]
            row["handoff_available"] = receipt["handoff_available"]
            row["can_confirm_load"] = can_confirm
            row["can_continue_window"] = can_continue
            row["todo_valid"] = receipt["todo_valid"]
            if handoff:
                task_owner = (task_summary or {}).get("责任人", "")
                if not task_owner or task_owner == "未指定责任人":
                    task_owner = handoff.get("task_owner") or handoff.get("successor") or handoff.get("接班人") or ""
                row["转交单编号"] = handoff.get("转交编号", "")
                row["接手人"] = task_owner
                row["重跑任务编号"] = (task_summary or {}).get("task_id", "")
                row["task_id"] = (task_summary or {}).get("task_id", "")
                row["责任人"] = task_owner
                row["owner"] = task_owner
                row["转交状态"] = "已转交"
                row["重跑有效性"] = "有效" if task_valid else "无效"
        receipt["entry_id"] = row.get("id") if row else None
        recovery_manager.remember_receipt(receipt)
        if row is not None:
            # 回执不反向放整行，避免 entry.receipt 与 receipt.entry 形成循环引用。
            row["receipt"] = receipt
        return receipt

    def _track_attempt(
        self,
        row: dict[str, Any],
        evaluation: dict[str, Any],
        *,
        operator: str = "",
        source: str = "action",
    ) -> dict[str, Any]:
        fact = str(row.get("所属事实表", ""))
        window_code = str(row.get("窗口编号", ""))
        with recovery_manager.lock:
            state = recovery_manager.scope_state(fact, window_code)
            event_id = recovery_manager.next_code("EVT")
            event = {
                "event_id": event_id,
                "scope_key": state["scope_key"],
                "fact_table": fact,
                "window_code": window_code,
                "outcome": "success" if evaluation.get("ok") else "failure",
                "expected_rows": evaluation.get("expected"),
                "actual_rows": evaluation.get("actual"),
                "deviation": evaluation.get("deviation"),
                "reason": evaluation.get("reason", ""),
                "operator": operator,
                "source": source,
                "accepted": True,
                "created_at": recovery_manager.timestamp(),
            }
            recovery_manager.events.append(event)
            state["updated_at"] = event["created_at"]
            state["last_event_id"] = event_id

            if evaluation.get("ok"):
                state["consecutive_failures"] = 0
                state["recovered"] = True
                state["last_result"] = "成功"
                row["status"] = "已装载"
                row["pending"] = False
                row["abnormal"] = False
                row["装载水位"] = row.get("窗口终点") or row.get("窗口起点") or row.get("装载水位", "")
                row["装载耗时"] = row.get("装载耗时") or "1.5s"
            else:
                state["consecutive_failures"] = int(state["consecutive_failures"]) + 1
                state["total_failures"] = int(state["total_failures"]) + 1
                state["recovered"] = False
                state["last_result"] = "失败"
                threshold = recovery_manager.threshold_for(state.get("team"))
                if state["consecutive_failures"] >= threshold and not state.get("decision_id"):
                    state["threshold_reached"] = True
                    state["threshold_accepted"] = True
                    state["accepted_event_id"] = event_id
                    state["decision_id"] = recovery_manager.next_code("JUDGE")
                elif state["consecutive_failures"] >= threshold:
                    state["threshold_reached"] = True
                    state["threshold_accepted"] = True
                    state["accepted_event_id"] = event_id
                row["status"] = "装载中断"
                row["pending"] = True
                row["abnormal"] = True
                if evaluation.get("deviation") is not None:
                    row["偏差率"] = f"{float(evaluation['deviation']):.1%}"

            return self._build_receipt(row, state, evaluation, source=source)

    # ---------- 确认装载 ----------

    def confirm_entry(
        self,
        entry_id: int | None = None,
        context: dict[str, Any] | None = None,
        *,
        window_pk: int | str | None = None,
        window_code: str | None = None,
        fact_table: str | None = None,
    ) -> dict[str, Any]:
        context = dict(context or {})
        pk = entry_id if entry_id is not None else window_pk
        row = self._resolve_window(pk, window_code or context.get("window_code"), fact_table or context.get("fact_table"))
        if row is None:
            return {
                "ok": False, "message": "装载窗口不存在或已归档",
                "entry": None, "receipt": None,
            }

        evaluation = self._evaluate_confirmation(row, context)
        if evaluation["deviation"] is None:
            return {"ok": False, "message": evaluation["reason"], "entry": None, "receipt": None}

        if context.get("actual_rows", context.get("实际行数")) not in (None, ""):
            row["实际行数"] = evaluation["actual"]
        if context.get("expected_rows", context.get("预期行数")) not in (None, ""):
            row["预期行数"] = evaluation["expected"]

        receipt = self._track_attempt(
            row, evaluation,
            operator=str(context.get("operator") or context.get("值班员") or "当前值班员"),
            source=str(context.get("source") or "confirm_action"),
        )
        if evaluation["ok"]:
            message = f"装载窗口 {row.get('窗口编号')} 已确认并推进，连续失败计数已清零"
            ok = True
        else:
            message = evaluation["reason"]
            if receipt["threshold_reached"]:
                message += (
                    f"；已连续失败 {receipt['failure_count']} 次，"
                    f"达到班组门槛 {receipt['threshold']}，可发起跨模块转交"
                )
            else:
                message += (
                    f"；连续失败 {receipt['failure_count']}/{receipt['threshold']}，"
                    "仍可按单笔确认装载纠正后重试"
                )
            ok = False
        receipt["message"] = message
        return {"ok": ok, "message": message, "entry": row, "receipt": receipt}

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool | None]:
        context = context or {}
        if action == "确认装载":
            row = store.find(self.MODULE, entry_id)
            expected = self._parse_int(
                context.get("expected_rows", context.get("预期行数")),
                self._parse_int((row or {}).get("预期行数"), 0),
            ) or 0
            forced = str(context.get("outcome") or context.get("result") or "").strip().lower()
            has_explicit_result = bool(
                forced
                or context.get("actual_rows") not in (None, "")
                or context.get("实际行数") not in (None, "")
            )
            # 没有行数依据且调用方未给失败/成功上下文时，保留旧版单笔确认行为，
            # 不把“未提供偏差数据”误记成一次连续失败。
            if row is not None and expected <= 0 and not has_explicit_result:
                entry, message = super().run_action(entry_id, action, context)
                if entry is not None:
                    receipt = self._track_attempt(
                        entry,
                        {"ok": True, "expected": None, "actual": None,
                         "deviation": None, "reason": message},
                        operator=str(context.get("operator") or "当前值班员"),
                        source="legacy_single_confirm",
                    )
                    message = f"{message}（兼容既有单笔确认装载，连续失败计数已清零）"
                    receipt["message"] = message
                return entry, message, True
            result = self.confirm_entry(entry_id, context)
            return result["entry"], result["message"], result["ok"]
        if action in {"发起转交", "发起跨模块转交", "跨模块转交", "提交转交"}:
            result = self.submit_handoff(window_pk=entry_id, context=context)
            return result.get("entry"), result["message"], result["ok"]
        entry, message = super().run_action(entry_id, action, context)
        return entry, message, None

    # ---------- 跨模块转交与并发收敛 ----------

    def submit_handoff(
        self,
        *,
        window_pk: int | str | None = None,
        window_code: str | None = None,
        fact_table: str | None = None,
        successor: str | None = None,
        operator: str | None = None,
        scheduler: str | None = None,
        task_id: str | None = None,
        strategy: str | None = None,
        reprocess_range: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = context or {}
        window_pk = window_pk if window_pk is not None else context.get("window_pk")
        code = window_code or context.get("window_code")
        fact = fact_table or context.get("fact_table")
        successor = successor or context.get("successor") or context.get("接班人") or "接班人（待登录认领）"
        operator = operator or context.get("operator") or context.get("值班员") or "当前值班员"
        scheduler = scheduler or context.get("scheduler") or context.get("排期员") or ""
        task_id = task_id or context.get("task_id")
        strategy = strategy or context.get("strategy") or "整段替换"

        with recovery_manager.lock:
            row = self._resolve_window(window_pk, code, fact)
            if row is None:
                return {"ok": False, "message": "装载窗口不存在，无法发起转交", "entry": None, "receipt": None}
            fact = str(row.get("所属事实表", fact or ""))
            code = str(row.get("窗口编号", code or ""))
            state = recovery_manager.scope_state(fact, code)
            threshold = recovery_manager.threshold_for(state.get("team"))

            if state.get("handoff_id"):
                existing_handoff = next(
                    (item for item in recovery_manager.handoffs if item.get("转交编号") == state["handoff_id"]),
                    None,
                )
                task = self._find_task_for_scope(state)
                receipt = self._build_receipt(row, state, handoff=existing_handoff, task=task)
                receipt["idempotent_replay"] = True
                receipt["message"] = "该窗口已提交过转交，返回同一份判定和回执，未重复生成任务"
                return {
                    "ok": True, "message": receipt["message"], "entry": row,
                    "receipt": receipt, "handoff": existing_handoff, "task": task,
                }

            if not state.get("threshold_reached") and int(state.get("consecutive_failures", 0)) < threshold:
                receipt = self._build_receipt(row, state)
                message = (
                    f"连续失败 {state.get('consecutive_failures', 0)}/{threshold}，"
                    "尚未达到班组转交门槛"
                )
                receipt["message"] = message
                return {"ok": False, "message": message, "entry": row, "receipt": receipt}

            state["threshold_reached"] = True
            if not state.get("decision_id"):
                state["decision_id"] = recovery_manager.next_code("JUDGE")
            dedupe_key = state["scope_key"]
            range_label = reprocess_range or context.get("reprocess_range") or (
                f"{row.get('窗口起点', '')}至{row.get('窗口终点', '')}"
            )
            desired_task_id = task_id or recovery_manager.next_code("RP-HO")
            rp = ReprocessTaskService()
            created_ok, task_message, task = rp.schedule_reprocess(
                desired_task_id,
                f"装载窗口连续失败达到门槛 {threshold} 次，需跨模块重跑",
                fact,
                str(range_label),
                strategy,
                code,
                dedupe_key=dedupe_key,
                source_window=code,
                owner=successor,
                scheduler=scheduler,
                start=bool(context.get("start", False)),
            )
            if not created_ok:
                return {"ok": False, "message": task_message, "entry": row, "receipt": None}

            task = rp.find_effective_task(dedupe_key=dedupe_key) or task
            handoff_code = recovery_manager.next_code("HO")
            created = bool(task.get("_created", task.get("id") is not None and task.get("重跑编号") == desired_task_id and not task.get("_deduplicated")))
            # 以去重服务显式标记为准；旧对象可能没有标记。
            created = bool(task.get("_created", created))
            handoff: dict[str, Any] = {
                "转交编号": handoff_code,
                "decision_id": state["decision_id"],
                "判定编号": state["decision_id"],
                "scope_key": dedupe_key,
                "fact_table": fact,
                "window_code": code,
                "threshold": threshold,
                "failure_count": state.get("consecutive_failures", threshold),
                "from_operator": operator,
                "值班员": operator,
                "successor": successor,
                "接班人": successor,
                "scheduler": task.get("排期人") or scheduler,
                "task_id": task.get("重跑编号"),
                "task_pk": task.get("id"),
                "task_status": task.get("status"),
                "task_owner": task.get("责任人") or successor,
                "责任人": task.get("责任人") or successor,
                "task_created": created,
                "deduplicated": not created,
                "existing_task_retained": not created,
                "status": "已转交",
                "created_at": recovery_manager.timestamp(),
                "task_message": task_message,
            }
            recovery_manager.handoffs.append(handoff)
            state["handoff_id"] = handoff_code
            state["task_id"] = task.get("重跑编号", "")

            receipt = self._build_receipt(row, state, handoff=handoff, task=task)
            if created:
                message = (
                    f"已发起跨模块转交 {handoff_code}，并生成重跑待办 "
                    f"{task.get('重跑编号')}，接班人 {successor} 可排期"
                )
            else:
                message = (
                    f"转交判定 {handoff_code} 已保留并发排期结果：现有任务 "
                    f"{task.get('重跑编号')}（责任人：{task.get('责任人') or '未指定'}）"
                    "仍为唯一有效任务，未重复生成"
                )
            receipt["message"] = message
            row["receipt"] = receipt
            return {
                "ok": True, "message": message, "entry": row,
                "receipt": receipt, "handoff": handoff, "task": task,
            }

    def schedule_other(
        self,
        *,
        window_pk: int | str | None = None,
        window_code: str | None = None,
        fact_table: str | None = None,
        scheduler: str | None = None,
        owner: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """模拟/接收另一名排期员的并发排期；同一范围只保留一条有效任务。"""
        scheduler = scheduler or "排期员·周敏"
        owner = owner or scheduler
        with recovery_manager.lock:
            row = self._resolve_window(window_pk, window_code, fact_table)
            if row is None and not (window_code and fact_table):
                return {"ok": False, "message": "装载窗口不存在，无法并发排期", "task": None}
            fact = str((row or {}).get("所属事实表", fact_table or ""))
            code = str((row or {}).get("窗口编号", window_code or ""))
            key = recovery_manager.scope_key(fact, code)
            task_id = task_id or recovery_manager.next_code("RP-OTHER")
            ok, message, task = ReprocessTaskService().schedule_reprocess(
                task_id,
                "他人在转交同时发起排期",
                fact,
                f"{(row or {}).get('窗口起点', '')}至{(row or {}).get('窗口终点', '')}",
                "整段替换",
                code,
                dedupe_key=key,
                source_window=code,
                owner=owner,
                scheduler=scheduler,
                start=True,
            )
            if row is not None:
                row["重跑任务编号"] = task.get("重跑编号")
                row["责任人"] = task.get("责任人") or owner
            return {"ok": ok, "message": message, "task": task}

    def race_handoff_and_schedule(
        self,
        *,
        window_pk: int | str | None = None,
        window_code: str | None = None,
        fact_table: str | None = None,
        successor: str | None = None,
        scheduler: str | None = None,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """真实使用两个线程竞争提交，验证第三次失败时只确认一条有效任务。"""
        scheduler = scheduler or "排期员·周敏"
        barrier = Barrier(2)
        handoff_result: dict[str, Any] = {}
        other_result: dict[str, Any] = {}

        def submit_handoff() -> None:
            barrier.wait(timeout=5)
            handoff_result.update(self.submit_handoff(
                window_pk=window_pk, window_code=window_code, fact_table=fact_table,
                successor=successor, operator=operator, scheduler=scheduler,
            ))

        def schedule_other() -> None:
            barrier.wait(timeout=5)
            other_result.update(self.schedule_other(
                window_pk=window_pk, window_code=window_code, fact_table=fact_table,
                scheduler=scheduler,
            ))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit_handoff)
            second = pool.submit(schedule_other)
            first.result(timeout=10)
            second.result(timeout=10)

        row = self._resolve_window(window_pk, window_code, fact_table)
        fact = str((row or {}).get("所属事实表", fact_table or ""))
        code = str((row or {}).get("窗口编号", window_code or ""))
        state = recovery_manager.scope_state(fact, code)
        with recovery_manager.lock:
            task = self._find_task_for_scope(state)
            handoff = next(
                (item for item in recovery_manager.handoffs if item.get("转交编号") == state.get("handoff_id")),
                None,
            )
            receipt = self._build_receipt(row, state, handoff=handoff, task=task)
            effective_count = sum(
                1 for item in store.rows(ReprocessTaskService.MODULE)
                if item.get("去重键") == state.get("scope_key") and item.get("有效", True) is not False
            )
            receipt["concurrency_test"] = True
            receipt["other_schedule"] = self._task_summary(other_result.get("task"))
            receipt["handoff_task"] = self._task_summary(handoff_result.get("task"))
            receipt["valid_task_count"] = effective_count
            receipt["only_one_valid_task"] = effective_count == 1
            if effective_count == 1:
                receipt["message"] = (
                    f"并发排期已收敛：只有任务 { (task or {}).get('重跑编号') } 有效，"
                    f"责任人 {(task or {}).get('责任人') or '未指定'}；转交判定和回执保留该任务。"
                )
            else:
                receipt["message"] = f"检测到 {effective_count} 条有效任务，需要人工合并"
            receipt["冲突结论"] = receipt["message"] + " 窗口即使恢复，该重跑任务仍继续有效。"
            recovery_manager.remember_receipt(receipt)
            if row is not None:
                row["receipt"] = receipt
        return {
            "ok": effective_count == 1 and bool(handoff_result.get("ok")),
            "message": receipt["message"],
            "receipt": receipt,
            "handoff_result": handoff_result,
            "other_result": other_result,
            "valid_task_count": effective_count,
        }

    # ---------- 演示数据与状态 ----------

    def update_policy_threshold(self, threshold: int, team: str | None = None) -> int:
        """保存班组实际上限，并立即按新上限重新判定当前计数。"""
        value = recovery_manager.set_threshold(threshold, team)
        with recovery_manager.lock:
            for state in recovery_manager.scopes.values():
                if int(state.get("consecutive_failures", 0)) >= value:
                    state["threshold_reached"] = True
                    state["threshold_accepted"] = True
                    if not state.get("decision_id"):
                        state["decision_id"] = recovery_manager.next_code("JUDGE")
        return value

    def prepare_demo(self) -> dict[str, Any]:
        """构造可重复演示的独立窗口，避免种子窗口历史干扰门槛统计。"""
        demo_codes = {"LW-DEMO-RECOVER", "LW-DEMO-HANDOFF", "LW-DEMO-RACE", "LW-DEMO-DECOY"}
        with recovery_manager.lock:
            recovery_manager.reset()
            recovery_manager.set_threshold(3)
            load_rows = store.rows(self.MODULE)
            load_rows[:] = [row for row in load_rows if row.get("窗口编号") not in demo_codes]
            rp_rows = store.rows(ReprocessTaskService.MODULE)
            rp_rows[:] = [
                row for row in rp_rows
                if row.get("来源窗口") not in demo_codes
                and not str(row.get("去重键", "")).endswith(tuple(demo_codes))
            ]

            demo_specs = [
                ("LW-DEMO-RECOVER", "恢复演示窗口", "待装载"),
                ("LW-DEMO-HANDOFF", "转交演示窗口", "待装载"),
                ("LW-DEMO-RACE", "并发演示窗口", "待装载"),
                ("LW-DEMO-DECOY", "其他窗口干扰项", "装载中断"),
            ]
            created: list[dict[str, Any]] = []
            for code, _label, status in demo_specs:
                row = {
                    "id": max((int(r.get("id", 0)) for r in load_rows), default=0) + 1,
                    "窗口编号": code,
                    "所属事实表": "销售事实表",
                    "窗口起点": "2026-11-20",
                    "窗口终点": "2026-11-20",
                    "预期行数": 1000,
                    "实际行数": 1000 if status == "待装载" else 400,
                    "装载水位": "",
                    "装载耗时": "",
                    "status": status,
                    "pending": status != "待装载",
                    "abnormal": status == "装载中断",
                }
                load_rows.append(row)
                created.append(row)

            # 给“其他窗口”预置三次失败，证明同事实表的其他窗口不会计入目标窗口。
            decoy = next(row for row in created if row["窗口编号"] == "LW-DEMO-DECOY")
            for _ in range(3):
                self._track_attempt(
                    decoy,
                    {"ok": False, "expected": 1000, "actual": 400, "deviation": 0.6,
                     "reason": "其他窗口的历史失败"},
                    operator="其他值班员",
                    source="demo_seed",
                )
            return {"ok": True, "message": "已准备门槛 3 的失败恢复演示", "items": created}

    def run_demo_sequence(self, kind: str) -> dict[str, Any]:
        """内置门槛三次演示，保证验收路径可重复执行。"""
        self.prepare_demo()
        code = {
            "recover": "LW-DEMO-RECOVER",
            "handoff": "LW-DEMO-HANDOFF",
            "race": "LW-DEMO-RACE",
        }.get(kind, "LW-DEMO-HANDOFF")
        row = self._resolve_window(window_code=code, fact_table="销售事实表")
        if row is None:
            return {"ok": False, "message": "演示窗口初始化失败", "receipt": None}

        logs: list[str] = []
        if kind == "recover":
            for index in range(2):
                result = self.confirm_entry(
                    window_pk=row.get("id"),
                    context={"outcome": "failure", "actual_rows": 500, "source": "demo"},
                )
                logs.append(f"第{index + 1}次失败：{result['message']}")
            result = self.confirm_entry(
                window_pk=row.get("id"),
                context={"outcome": "success", "actual_rows": 1000, "source": "demo"},
            )
            logs.append(f"纠正后正常确认：{result['message']}")
            result["demo_logs"] = logs
            result["message"] = "门槛为 3：演示二次失败仍可正常确认，纠正成功后连续失败计数清零。"
            if result.get("receipt"):
                result["receipt"]["demo_logs"] = logs
            return result

        for index in range(3):
            result = self.confirm_entry(
                window_pk=row.get("id"),
                context={"outcome": "failure", "actual_rows": 500, "source": "demo"},
            )
            logs.append(f"第{index + 1}次失败：{result['message']}")

        if kind == "race":
            race_result = self.race_handoff_and_schedule(
                window_pk=row.get("id"),
                successor="陈澈（接班人）",
                scheduler="周敏（排期员）",
                operator="林岚（值班员）",
            )
            race_result["demo_logs"] = logs
            return race_result

        result["demo_logs"] = logs
        result["message"] = "门槛为 3：前两次失败仍可确认，第三次失败已接收并出现跨模块转交入口。"
        if result.get("receipt"):
            result["receipt"]["demo_logs"] = logs
            result["receipt"]["handoff_available"] = True
        return result

    def recovery_state(self) -> dict[str, Any]:
        with recovery_manager.lock:
            rp = ReprocessTaskService()
            # 页面重进时，把已变化的重跑状态合并回同一份转交判定/回执，
            # 避免接班人已经排期后回执仍停留在“待排期”的旧快照。
            for handoff in recovery_manager.handoffs:
                task = rp.find_effective_task(dedupe_key=handoff.get("scope_key"))
                if task is not None:
                    handoff["task_status"] = task.get("status")
                    handoff["task_owner"] = task.get("责任人") or handoff.get("task_owner")
                    handoff["scheduler"] = task.get("排期人") or handoff.get("scheduler")

            for state in recovery_manager.scopes.values():
                receipt = state.get("latest_receipt")
                if not isinstance(receipt, dict):
                    continue
                task = rp.find_effective_task(dedupe_key=state.get("scope_key"))
                summary = self._task_summary(task)
                receipt["task"] = summary
                receipt["reprocess_task"] = summary
                receipt["重跑任务"] = summary
                task_valid = bool(summary and summary.get("valid"))
                task_status = str((summary or {}).get("status", ""))
                todo_valid = task_valid and task_status in {"待排期", "重跑中", "重跑失败"}
                pending_todo = task_valid and task_status in {"待排期", "重跑失败"}
                rerun_active = task_valid and task_status == "重跑中"
                receipt["task_valid"] = task_valid
                receipt["reprocess_task_valid"] = task_valid
                receipt["todo_valid"] = todo_valid
                receipt["pending_todo_valid"] = pending_todo
                receipt["rerun_completed"] = task_valid and task_status == "已完成"
                receipt["排期员待办有效"] = todo_valid
                receipt["排期员待办仍在"] = pending_todo
                receipt["rerun_continues_after_recovery"] = todo_valid
                receipt["窗口恢复后重跑继续"] = todo_valid
                receipt["valid_task_count"] = 1 if task_valid else 0

        state = recovery_manager.public_state()
        state["windows"] = [
            {
                "id": row.get("id"),
                "窗口编号": row.get("窗口编号"),
                "所属事实表": row.get("所属事实表"),
                "status": row.get("status"),
                "连续失败次数": row.get("连续失败次数", 0),
                "班组门槛": row.get("班组门槛", recovery_manager.threshold_for()),
                "转交单编号": row.get("转交单编号", ""),
                "接手人": row.get("接手人", ""),
                "重跑任务编号": row.get("重跑任务编号", ""),
                "重跑任务状态": (
                    ReprocessTaskService().find_effective_task(
                        source_window=str(row.get("窗口编号", "")),
                        fact_table=str(row.get("所属事实表", "")),
                    ) or {}
                ).get("status", "") if row.get("重跑任务编号") else "",
                "责任人": row.get("责任人", ""),
                "可否继续推进": row.get("可否继续推进", ""),
            }
            for row in store.rows(self.MODULE)
        ]
        return state

    def detect_dirty_window(self, fact_table: str) -> list[dict[str, Any]]:
        """检测脏数据窗口：偏差超阈值或中断的窗口。"""
        rows = store.rows(self.MODULE)
        return [dict(r) for r in rows if r.get("所属事实表") == fact_table
                and (r.get("status") == "装载中断" or r.get("abnormal"))]

    def advance_window(
        self, window_id: str, fact_table: str, start: str, end: str,
        expected_rows: int, actual_rows: int,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """推进装载窗口：实际行数与预期行数偏差超过 20% 时窗口不得推进。"""
        if expected_rows <= 0:
            return False, f"预期行数 {expected_rows} 无效", None

        deviation = abs(actual_rows - expected_rows) / expected_rows
        if deviation > 0.20:
            rows = store.rows(self.MODULE)
            entry = {
                "窗口编号": window_id, "所属事实表": fact_table,
                "窗口起点": start, "窗口终点": end,
                "预期行数": expected_rows, "实际行数": actual_rows,
                "装载水位": start, "装载耗时": "",
                "status": "装载中断", "pending": False, "abnormal": True,
                "偏差率": f"{deviation:.1%}",
            }
            entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append(entry)
            evaluation = {
                "ok": False, "expected": expected_rows, "actual": actual_rows,
                "deviation": deviation,
                "reason": f"实际行数 {actual_rows} 与预期 {expected_rows} 偏差 {deviation:.1%}，窗口不得推进",
            }
            receipt = self._track_attempt(entry, evaluation, source="advance_window")
            message = evaluation["reason"]
            if receipt["threshold_reached"]:
                message += f"；连续失败已达班组门槛 {receipt['threshold']}，可发起跨模块转交"
            return False, message, entry

        rows = store.rows(self.MODULE)
        entry = {
            "窗口编号": window_id, "所属事实表": fact_table,
            "窗口起点": start, "窗口终点": end,
            "预期行数": expected_rows, "实际行数": actual_rows,
            "装载水位": end, "装载耗时": "1.5s",
            "status": "已装载", "pending": False, "abnormal": False,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        evaluation = {
            "ok": True, "expected": expected_rows, "actual": actual_rows,
            "deviation": deviation, "reason": "装载窗口推进成功",
        }
        self._track_attempt(entry, evaluation, source="advance_window")
        return True, f"装载窗口 [{start}, {end}] 已推进，水位 {end}", entry
