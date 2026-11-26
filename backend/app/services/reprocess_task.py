"""重跑任务领域服务：覆盖策略与血缘影响传递，调度重跑状态机和水位推进，产出重跑任务数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.shift_recovery import recovery_manager
from app.store import store


ACTIVE_TASK_STATUSES = {"待排期", "重跑中", "重跑失败"}


class ReprocessTaskService(BaseModuleService):
    MODULE = "reprocess_task"
    ENTITY = "重跑任务"
    LIST_FIELDS = [
        "重跑编号", "触发原因", "影响事实表", "重跑区间", "覆盖策略",
        "依赖批次", "重跑水位", "重跑耗时", "责任人", "来源窗口", "去重键",
    ]
    REQUIRED_FIELDS = ["重跑编号", "触发原因", "影响事实表"]
    STATUS_ORDER = ["待排期", "重跑中", "已完成", "重跑失败"]
    ACTION_RULES = {"排期重跑": "重跑中", "确认完成": "已完成", "标记失败": "重跑失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    def find_effective_task(
        self,
        dedupe_key: str | None = None,
        source_window: str | None = None,
        fact_table: str | None = None,
        *,
        include_finished: bool = False,
    ) -> dict[str, Any] | None:
        """查找同一事实表/窗口当前唯一有效的重跑任务。"""
        with recovery_manager.lock:
            matches: list[dict[str, Any]] = []
            for row in store.rows(self.MODULE):
                if row.get("有效") is False or row.get("是否重复") is True:
                    continue
                if not include_finished and row.get("status") not in ACTIVE_TASK_STATUSES:
                    continue
                same_key = bool(dedupe_key) and row.get("去重键") == dedupe_key
                same_scope = (
                    bool(source_window)
                    and (
                        (row.get("来源窗口") == source_window and (
                            not fact_table or row.get("影响事实表") == fact_table
                        ))
                        or (
                            fact_table
                            and row.get("影响事实表") == fact_table
                            and row.get("依赖批次") == source_window
                        )
                    )
                )
                if same_key or same_scope:
                    matches.append(row)
            if not matches:
                return None
            # 正常并发只会走到一条；若历史脏数据产生多条，保留最早一条并作废其余。
            primary = min(matches, key=lambda item: int(item.get("id", 0)))
            for row in matches:
                if row is not primary:
                    row["有效"] = False
                    row["是否重复"] = True
                    row["重复合并到"] = primary.get("重跑编号")
                    row["status"] = "已作废"
                    row["pending"] = False
            primary["有效"] = True
            return primary

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        dedupe_key = str(values.get("去重键") or "").strip()
        with recovery_manager.lock:
            if dedupe_key:
                existing = self.find_effective_task(dedupe_key=dedupe_key)
                if existing is not None:
                    existing["_deduplicated"] = True
                    existing["_created"] = False
                    return existing, []
            entry, missing = super().create_entry(values)
            if entry is not None:
                entry["有效"] = True
                entry["_created"] = True
            return entry, missing

    def schedule_reprocess(
        self, task_id: str, reason: str, target_table: str,
        reprocess_range: str, strategy: str, depends_on: str,
        *,
        dedupe_key: str | None = None,
        source_window: str | None = None,
        owner: str | None = None,
        scheduler: str | None = None,
        start: bool = False,
    ) -> tuple[bool, str, dict[str, Any]]:
        """排期重跑任务：覆盖策略决定旧区间是整段替换还是并存留痕。

        同一去重键/来源窗口只保留一条有效任务；并发排期会复用既有任务，
        不产生第二条待办。
        """
        if strategy not in ("整段替换", "并存留痕"):
            return False, f"覆盖策略 {strategy} 无效", {}

        with recovery_manager.lock:
            existing = self.find_effective_task(
                dedupe_key=dedupe_key,
                source_window=source_window,
                fact_table=target_table,
            )
            if existing is not None:
                if owner and not existing.get("责任人"):
                    existing["责任人"] = owner
                if scheduler:
                    existing["排期人"] = scheduler
                if start:
                    existing["status"] = "重跑中"
                    existing["pending"] = False
                    existing["排期时间"] = recovery_manager.timestamp()
                existing["有效"] = True
                existing["_deduplicated"] = True
                existing["_created"] = False
                return True, (
                    f"重跑任务 {existing.get('重跑编号')} 已存在，"
                    f"责任人为 {existing.get('责任人') or '未指定'}，未重复创建"
                ), existing

            entry: dict[str, Any] = {
                "重跑编号": task_id, "触发原因": reason,
                "影响事实表": target_table, "重跑区间": reprocess_range,
                "覆盖策略": strategy, "依赖批次": depends_on,
                "重跑水位": "", "重跑耗时": "",
                "责任人": owner or "",
                "排期人": scheduler or "",
                "来源窗口": source_window or depends_on,
                "去重键": dedupe_key or "",
                "有效": True,
                "status": "待排期", "pending": True, "abnormal": False,
            }
            if start:
                entry["status"] = "重跑中"
                entry["pending"] = False
                entry["排期时间"] = recovery_manager.timestamp()
            rows = store.rows(self.MODULE)
            entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            entry["_created"] = True
            entry["_deduplicated"] = False
            rows.append(entry)
            return True, f"重跑任务 {task_id} 已排期，策略：{strategy}", entry

    def run_action(
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        context = context or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "排期重跑":
            with recovery_manager.lock:
                entry["status"] = "重跑中"
                entry["pending"] = False
                scheduler = context.get("scheduler") or context.get("operator") or context.get("责任人")
                if scheduler:
                    entry["排期人"] = scheduler
                    entry.setdefault("责任人", scheduler)
                entry["排期时间"] = recovery_manager.timestamp()
                return entry, f"{self.ENTITY}已排期重跑"
        return super().run_action(entry_id, action, context)  # type: ignore[arg-type]

    def propagate_lineage_impact(self, target_table: str) -> dict[str, list[str]]:
        """沿血缘边传递重跑影响范围。"""
        lineage_rows = store.rows("lineage_edge")
        direct: list[str] = []
        for r in lineage_rows:
            if r.get("上游对象") == target_table:
                direct.append(str(r.get("下游对象", "")))
        seen = set(direct)
        indirect: list[str] = []
        for d in direct:
            for r in lineage_rows:
                if r.get("上游对象") == d and r.get("下游对象") not in seen:
                    indirect.append(str(r.get("下游对象", "")))
                    seen.add(str(r.get("下游对象", "")))
        return {"direct_impact": direct, "indirect_impact": indirect}

    def complete_reprocess(self, task_id: str, watermark: str) -> tuple[bool, str]:
        """确认重跑完成：更新重跑水位。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("重跑编号") == task_id:
                r["status"] = "已完成"
                r["重跑水位"] = watermark
                r["pending"] = False
                return True, f"重跑任务 {task_id} 已完成，水位 {watermark}"
        return False, f"重跑任务 {task_id} 不存在"
