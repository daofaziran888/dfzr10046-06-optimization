"""装载窗口连续失败门槛与跨模块转交的共享内存状态。

状态放在服务层单例而不是组件里：关闭页面后重新进入仍能读到同一份判定、
回执和责任人；后端进程重启后回到种子/演示初始状态。
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Any


class ShiftRecoveryManager:
    """班组门槛、失败事件、转交判定和回执的一致状态。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.team = "数据工程组"
        self.thresholds: dict[str, int] = {self.team: 3}
        self.events: list[dict[str, Any]] = []
        self.scopes: dict[str, dict[str, Any]] = {}
        self.handoffs: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self._seq = itertools.count(1)

    def reset(self) -> None:
        with self.lock:
            self.team = "数据工程组"
            self.thresholds = {self.team: 3}
            self.events.clear()
            self.scopes.clear()
            self.handoffs.clear()
            self.receipts.clear()
            self._seq = itertools.count(1)

    def next_serial(self) -> int:
        return next(self._seq)

    def next_code(self, prefix: str) -> str:
        return f"{prefix}-{self.next_serial():04d}"

    @staticmethod
    def scope_key(fact_table: str, window_code: str) -> str:
        # 统计范围只由事实表和窗口确定；班组只决定门槛数值。
        return f"{(fact_table or '').strip()}::{(window_code or '').strip()}"

    def threshold_for(self, team: str | None = None) -> int:
        return int(self.thresholds.get((team or self.team).strip(), 3))

    def set_threshold(self, threshold: int, team: str | None = None) -> int:
        team_name = (team or self.team).strip() or self.team
        value = max(1, min(20, int(threshold)))
        with self.lock:
            self.thresholds[team_name] = value
        return value

    def scope_state(self, fact_table: str, window_code: str) -> dict[str, Any]:
        key = self.scope_key(fact_table, window_code)
        with self.lock:
            state = self.scopes.get(key)
            if state is None:
                state = {
                    "scope_key": key,
                    "team": self.team,
                    "fact_table": fact_table,
                    "window_code": window_code,
                    "consecutive_failures": 0,
                    "total_failures": 0,
                    "threshold_reached": False,
                    "threshold_accepted": False,
                    "accepted_event_id": "",
                    "recovered": False,
                    "last_result": "",
                    "last_event_id": "",
                    "decision_id": "",
                    "handoff_id": "",
                    "task_id": "",
                    "updated_at": "",
                }
                self.scopes[key] = state
            # 门槛始终读取班组当前实际设置，避免把旧阈值快照误当成上限。
            state["threshold"] = self.threshold_for(state.get("team") or self.team)
            return state

    def timestamp(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def remember_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.receipts.append(receipt)
            scope_key = receipt.get("scope_key")
            if scope_key and scope_key in self.scopes:
                self.scopes[scope_key]["latest_receipt"] = receipt
            # 内存演示环境保留最近 100 份即可，页面仍可按窗口查到完整当前判定。
            if len(self.receipts) > 100:
                del self.receipts[:-100]
        return receipt

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "team": self.team,
                "threshold": self.threshold_for(),
                "thresholds": dict(self.thresholds),
                "scopes": [dict(item) for item in self.scopes.values()],
                "handoffs": [dict(item) for item in self.handoffs],
                "receipts": [dict(item) for item in self.receipts[-30:]],
                "events": [dict(item) for item in self.events[-100:]],
            }


recovery_manager = ShiftRecoveryManager()
