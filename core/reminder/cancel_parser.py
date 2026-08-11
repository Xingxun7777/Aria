"""Parse natural-language reminder cancellation commands."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReminderCancelRequest:
    """Structured reminder-cancellation intent."""

    scope: str  # current | latest | all | matching
    query: str = ""
    recurring_only: bool = False


def parse_reminder_cancel(text: str) -> ReminderCancelRequest:
    """Parse the command text after the wakeword.

    Deliberately deterministic: no model call is needed to cancel an alarm.
    """
    clean = re.sub(r"[，,。.！!？?；;：:、\s]", "", str(text or ""))
    recurring_only = "重复" in clean or "循环" in clean

    if "所有" in clean or "全部" in clean:
        return ReminderCancelRequest("all", recurring_only=recurring_only)
    if any(marker in clean for marker in ("这个", "当前", "正在响", "刚响")):
        return ReminderCancelRequest("current", recurring_only=recurring_only)
    if any(marker in clean for marker in ("刚才", "上一个", "最新")):
        return ReminderCancelRequest("latest", recurring_only=recurring_only)
    if recurring_only:
        return ReminderCancelRequest("all", recurring_only=True)

    query = clean
    for phrase in (
        "不要再提醒我",
        "别再提醒我",
        "取消提醒",
        "关闭提醒",
        "停止提醒",
        "取消闹钟",
        "关闭闹钟",
        "停止闹钟",
        "关掉提醒",
        "关掉闹钟",
    ):
        query = query.replace(phrase, "", 1)
    query = re.sub(r"^(?:取消|关闭|停止|关掉|结束|不要|别)+", "", query)
    query = re.sub(r"(?:提醒|闹钟|定时)$", "", query)
    query = query.strip("的了一下")
    if query:
        return ReminderCancelRequest("matching", query=query)
    return ReminderCancelRequest("latest")
