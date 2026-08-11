"""Unified user-facing feedback copy for AI errors and delivery statuses.

Maps machine enums to short, actionable Chinese notices. Messages must never
embed user text, wakeword literals, or API keys.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from aria.system.selection_transaction import SelectionTransactionStatus

from .gateway import AIErrorCategory

AI_ERROR_MESSAGES: dict[AIErrorCategory, str] = {
    AIErrorCategory.NOT_CONFIGURED: "AI 未配置，请先在设置中填写接口与密钥",
    AIErrorCategory.AUTH: "API Key 无效或已过期，请检查设置后重试",
    AIErrorCategory.RATE_LIMITED: "请求过于频繁，请稍后再试",
    AIErrorCategory.SERVER_ERROR: "AI 服务异常，请稍后再试",
    AIErrorCategory.TIMEOUT: "连接超时，请检查网络后重试",
    AIErrorCategory.CONNECT: "无法连接网络，请检查网络后重试",
    AIErrorCategory.PROTOCOL: "响应格式异常，请稍后重试",
    AIErrorCategory.EMPTY: "AI 返回空结果，请换个说法再试",
    AIErrorCategory.CANCELLED: "已取消",
}

_DEFAULT_AI_ERROR = "AI 没有完成处理，本次未改动文字"

DELIVERY_MESSAGES: dict[SelectionTransactionStatus, str] = {
    SelectionTransactionStatus.READY: "目标已就绪，但本次未完成替换",
    SelectionTransactionStatus.CONFIRMED: "替换已确认",
    SelectionTransactionStatus.SENT: "替换指令已发送，请确认目标内容",
    SelectionTransactionStatus.NO_CHANGE: "原文无需改动，本次未替换",
    SelectionTransactionStatus.INVALID_ARGUMENT: "修改参数无效，本次未改动文字",
    SelectionTransactionStatus.TARGET_UNAVAILABLE: "目标窗口不可用，本次未自动替换",
    SelectionTransactionStatus.TARGET_CHANGED: "目标窗口已变化，本次未自动替换",
    SelectionTransactionStatus.UNSUPPORTED_SURFACE: "当前载体不支持安全替换，本次未改动文字",
    SelectionTransactionStatus.SELECTION_UNAVAILABLE: "无法读取原选区，本次未自动替换",
    SelectionTransactionStatus.SELECTION_CHANGED: "选区已变化，本次未自动替换",
    SelectionTransactionStatus.CONTENT_CHANGED: "原文内容已变化，本次未自动替换",
    SelectionTransactionStatus.PROTECTED_CONTROL: "受保护的输入框禁止自动替换",
    SelectionTransactionStatus.READ_ONLY: "当前输入框为只读，本次未改动文字",
    SelectionTransactionStatus.UNDO_UNAVAILABLE: "无法保证完整撤销，本次未自动替换",
    SelectionTransactionStatus.ELEVATION_REQUIRED: "目标窗口权限高于 Aria，本次未自动替换",
    SelectionTransactionStatus.WRITE_REJECTED: "写入被拒绝，本次未改动文字",
    SelectionTransactionStatus.WRITE_PARTIAL: (
        "写入状态未确认，请先检查原文；为避免重复输入，未再次写入"
    ),
    SelectionTransactionStatus.NATIVE_FAILED: "系统写入失败，本次未自动替换",
}

_DEFAULT_DELIVERY = "原文范围或目标已变化，本次未自动替换"

# StandardTextEditStatus shares many string values with SelectionTransactionStatus.
# Remaining voice-edit-only codes get dedicated copy here.
VOICE_EDIT_STATUS_MESSAGES: dict[str, str] = {
    "confirmed": "已完成精确替换",
    "target_unavailable": "目标窗口不可用，本次未修改",
    "unsupported_surface": "当前载体不支持精确语音编辑，本次未修改",
    "unsupported_control": "当前输入框不支持精确语音编辑，本次未修改",
    "protected_control": "密码输入框禁止读取和语音编辑",
    "read_only": "当前输入框为只读，本次未修改",
    "undo_unavailable": "当前旧式多行输入框无法保证完整单步撤销，本次未修改",
    "elevation_required": "目标窗口权限高于 Aria，本次未修改",
    "invalid_argument": "编辑参数无效，本次未修改",
    "text_unavailable": "无法读取当前输入框文字，本次未修改",
    "text_too_long": "当前输入框文字过长，本次未修改",
    "source_not_found": "没有找到完全一致的原文，本次未修改",
    "ambiguous_match": "匹配位置存在歧义，本次未修改；请把原词说得更具体",
    "too_many_matches": "找到超过 20 处相同原文，未修改；请把原词说得更具体",
    "overlapping_matches": "原文存在互相重叠的匹配，本次未修改；请把原词说得更具体",
    "batch_unsupported": "当前富文本含多处匹配，为避免破坏格式，本次未批量修改",
    "selection_unavailable": "无法定位编辑选区，本次未修改",
    "selection_changed": "编辑选区已变化，本次未修改",
    "selection_failed": "无法安全建立编辑选区，本次未修改",
    "target_changed": "编辑期间目标已变化，本次未修改",
    "content_changed": "编辑期间输入框内容已变化，本次未修改",
    "write_rejected": "写入被拒绝，本次未修改",
    "write_partial": (
        "编辑结果未能确认，可能已经修改；请先检查目标，若确有变化可撤销"
    ),
}

_DEFAULT_VOICE_EDIT = "无法安全完成精确编辑，本次未修改"


def _coerce_ai_category(
    category: Union[AIErrorCategory, str, None],
) -> Optional[AIErrorCategory]:
    if category is None:
        return None
    if isinstance(category, AIErrorCategory):
        return category
    try:
        return AIErrorCategory(str(category))
    except ValueError:
        return None


def describe_ai_error(
    category: Union[AIErrorCategory, str, None],
    default: str = _DEFAULT_AI_ERROR,
) -> str:
    """Return short Chinese copy for an AI error category."""

    resolved = _coerce_ai_category(category)
    if resolved is None:
        return default
    return AI_ERROR_MESSAGES.get(resolved, default)


def _coerce_delivery_status(
    status: Any,
) -> Optional[SelectionTransactionStatus]:
    if status is None:
        return None
    if isinstance(status, SelectionTransactionStatus):
        return status
    value = getattr(status, "value", status)
    try:
        return SelectionTransactionStatus(str(value))
    except ValueError:
        return None


def describe_delivery_status(
    status: Any,
    default: str = _DEFAULT_DELIVERY,
) -> str:
    """Return short Chinese copy for a selection/delivery transaction status."""

    resolved = _coerce_delivery_status(status)
    if resolved is None:
        return default
    return DELIVERY_MESSAGES.get(resolved, default)


def describe_voice_edit_status(
    status: Any,
    default: str = _DEFAULT_VOICE_EDIT,
) -> str:
    """Return short Chinese copy for a StandardTextEditStatus (or value)."""

    if status is None:
        return default
    value = str(getattr(status, "value", status) or "").strip()
    if not value:
        return default
    return VOICE_EDIT_STATUS_MESSAGES.get(value, default)
