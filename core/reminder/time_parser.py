"""
Chinese Time Expression Parser
==============================
Parses natural Chinese time expressions into absolute datetime.

Handles:
- Relative: 三小时后, 半小时后, 两个半小时后, 十分钟后, 五天后
- Absolute: 晚上八点, 明天下午两点, 后天上午十点半
- Weekday: 下周五, 下下周一下午三点
- Edge cases: past time auto-advance, Chinese numerals, 半=30min

No external dependencies beyond stdlib + dateutil.relativedelta.
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from dateutil.relativedelta import relativedelta


# ============================================================================
# Chinese numeral conversion
# ============================================================================

_CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "贰": 2,
    "两": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
    "十": 10,
    "拾": 10,
}

_CN_WEEKDAY = {
    "一": 0,
    "1": 0,
    "二": 1,
    "2": 1,
    "三": 2,
    "3": 2,
    "四": 3,
    "4": 3,
    "五": 4,
    "5": 4,
    "六": 5,
    "6": 5,
    "日": 6,
    "天": 6,
    "7": 6,
}


def _cn_to_int(text: str) -> Optional[int]:
    """Convert Chinese numeral string to integer.

    Handles: 一→1, 两→2, 十→10, 十五→15, 二十→20, 二十三→23, 三十→30
    Also handles plain Arabic digits: "3" → 3, "15" → 15
    """
    if not text:
        return None

    # Already a number
    if text.isdigit():
        return int(text)

    # Single character
    if len(text) == 1 and text in _CN_DIGIT:
        return _CN_DIGIT[text]

    # Multi-character Chinese numeral (up to 99)
    # Pattern: [tens]十[ones] e.g. 二十三, 十五, 三十
    result = 0
    has_ten = "十" in text or "拾" in text

    if not has_ten:
        # No 十, try single digit
        if text in _CN_DIGIT:
            return _CN_DIGIT[text]
        return None

    parts = re.split(r"[十拾]", text)
    tens_part = parts[0] if parts[0] else ""
    ones_part = parts[1] if len(parts) > 1 and parts[1] else ""

    if tens_part:
        if tens_part in _CN_DIGIT:
            result += _CN_DIGIT[tens_part] * 10
        else:
            return None
    else:
        # Just 十X → 1X
        result += 10

    if ones_part:
        if ones_part in _CN_DIGIT:
            result += _CN_DIGIT[ones_part]
        else:
            return None

    return result


def _normalize_chinese_nums(text: str) -> str:
    """Replace Chinese numerals in text with Arabic digits.

    Handles compound forms like 二十三→23, 十五→15.
    Processes longest matches first to avoid partial replacement.
    """

    # Match compound Chinese numerals (up to 3 chars with 十)
    def _replace_match(m):
        val = _cn_to_int(m.group(0))
        return str(val) if val is not None else m.group(0)

    # Pattern: optional digit + 十 + optional digit (e.g. 二十三, 十五, 三十, 十)
    text = re.sub(
        r"[零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾]{1,4}",
        _replace_match,
        text,
    )
    return text


# ============================================================================
# Time-of-day to hour offset
# ============================================================================

_TIME_OF_DAY = {
    "凌晨": (0, 5),  # 0:00 - 5:59
    "早上": (6, 11),  # 6:00 - 11:59
    "上午": (6, 11),
    "早晨": (6, 11),
    "中午": (11, 13),  # 11:00 - 13:59
    "下午": (12, 17),  # 12:00 - 17:59
    "傍晚": (16, 19),  # 16:00 - 19:59
    "晚上": (18, 23),  # 18:00 - 23:59
    "夜里": (20, 3),  # 20:00 - 3:59 (wraps)
    "夜间": (20, 3),
    "半夜": (23, 4),
}


def _adjust_hour_for_period(hour: int, period: str) -> int:
    """Adjust hour based on time-of-day period.

    e.g. 下午3点 → 15, 晚上8点 → 20, 上午11点 → 11
    """
    if period not in _TIME_OF_DAY:
        return hour

    low, high = _TIME_OF_DAY[period]

    # For periods that clearly mean PM, add 12 if hour < 12
    if period in ("下午", "傍晚", "晚上", "夜里", "夜间", "半夜"):
        if 1 <= hour <= 11:
            return hour + 12
    elif period == "中午":
        if hour == 12:
            return 12
        if 1 <= hour <= 2:
            return hour + 12  # 中午1点 = 13:00
    # 凌晨/早上/上午: no adjustment needed (hour stays as-is)

    return hour


# ============================================================================
# Day offset patterns
# ============================================================================

_DAY_OFFSETS = {
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "後天": 2,
    "大后天": 3,
    "大後天": 3,
}

# Compound day+period shortcuts (e.g. 今晚=今天晚上, 明晚=明天晚上)
_DAY_PERIOD_SHORTCUTS = {
    "今晚": (0, "晚上"),
    "今早": (0, "早上"),
    "明早": (1, "早上"),
    "明晚": (1, "晚上"),
}


# ============================================================================
# Core parsing functions
# ============================================================================


def _parse_relative_time(text: str, now: datetime) -> Optional[datetime]:
    """Parse relative time expressions like '三小时后', '半小时后', '十分钟后'."""

    # N个半小时后 (e.g. 两个半小时后 = 2.5h)
    m = re.search(r"(\d+)个半小时后", text)
    if m:
        hours = int(m.group(1)) + 0.5
        return now + timedelta(hours=hours)

    # 半小时后
    if "半小时后" in text:
        return now + timedelta(minutes=30)

    # N小时M分(钟)后 (e.g. 一小时三十分后 = 1h30m)
    m = re.search(r"(\d+)个?小时(\d+)分(?:钟)?后", text)
    if m:
        return now + timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))

    # N小时后
    m = re.search(r"(\d+)个?小时后", text)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    # N分钟后
    m = re.search(r"(\d+)分钟后", text)
    if m:
        return now + timedelta(minutes=int(m.group(1)))

    # N天后
    m = re.search(r"(\d+)天后", text)
    if m:
        return now + timedelta(days=int(m.group(1)))

    # N周后 / N个星期后
    m = re.search(r"(\d+)个?(?:周|星期)后", text)
    if m:
        return now + timedelta(weeks=int(m.group(1)))

    # N个月后
    m = re.search(r"(\d+)个?月后", text)
    if m:
        return now + relativedelta(months=int(m.group(1)))

    return None


def _parse_absolute_time(text: str, now: datetime) -> Optional[datetime]:
    """Parse absolute time expressions like '明天下午两点', '晚上八点半'."""

    # Check compound day+period shortcuts first (今晚/明晚/今早/明早)
    day_offset = None
    period = None
    for shortcut, (offset, per) in _DAY_PERIOD_SHORTCUTS.items():
        if shortcut in text:
            day_offset = offset
            period = per
            break

    # Extract day offset (今天/明天/后天/大后天)
    if day_offset is None:
        for day_word, offset in _DAY_OFFSETS.items():
            if day_word in text:
                day_offset = offset
                break

    # Extract time-of-day period (if not already set by shortcut)
    if period is None:
        for p in _TIME_OF_DAY:
            if p in text:
                period = p
                break

    # Extract hour and minute
    hour = None
    minute = 0

    # Match: N点M分 or N点半 or N点 or N时M分 or N时半 or N时
    m = re.search(r"(\d{1,2})[点时](?:(\d{1,2})分?|半)?", text)
    if m:
        hour = int(m.group(1))
        if m.group(2):
            minute = int(m.group(2))
        elif "半" in text[m.start() : m.end() + 2]:
            minute = 30

    # Also check for standalone 半 right after 点/时
    if hour is not None and minute == 0:
        after_match = text[m.end() : m.end() + 1] if m else ""
        if after_match == "半":
            minute = 30

    if hour is None:
        # No hour found — can't parse as absolute time
        return None

    # Apply time-of-day adjustment
    if period:
        hour = _adjust_hour_for_period(hour, period)

    # Validate hour/minute range (prevents ValueError in datetime constructor)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None

    # Determine the target date
    target_date = now.date()
    if day_offset is not None:
        target_date = (now + timedelta(days=day_offset)).date()
    else:
        # No day specified — infer today or tomorrow
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.date() == now.date() and candidate <= now - timedelta(minutes=5):
            # Already past today, auto-advance to tomorrow
            target_date = (now + timedelta(days=1)).date()

    result = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        0,
    )
    return result


def _parse_weekday_time(text: str, now: datetime) -> Optional[datetime]:
    """Parse weekday expressions like '下周五', '下下周一下午三点', '下礼拜天'."""

    # Normalize 礼拜 → 周 for unified matching
    text = text.replace("礼拜", "周")
    # Normalize 星期 → 周
    text = text.replace("星期", "周")

    # Match: 下下周X or 下周X (with optional time)
    m = re.search(r"下(下)?周([一二三四五六日天1-7])", text)
    if not m:
        # Also try: 这周X / 本周X (this week)
        m = re.search(r"[这本]周([一二三四五六日天1-7])", text)
        if m:
            target_weekday = _CN_WEEKDAY.get(m.group(1))
            if target_weekday is None:
                return None
            days_ahead = (target_weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 0  # This week same day = today
            target_date = now.date() + timedelta(days=days_ahead)
        else:
            return None
    else:
        is_double = m.group(1) is not None  # 下下周
        target_weekday = _CN_WEEKDAY.get(m.group(2))
        if target_weekday is None:
            return None

        # Calculate days until target weekday
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # Same weekday = next week

        if is_double:
            days_ahead += 7  # Extra week for 下下周

        target_date = now.date() + timedelta(days=days_ahead)

    # Extract optional time from the rest of the text
    hour = 9  # Default: morning 9 AM
    minute = 0

    # Look for time-of-day + hour pattern in remaining text
    time_text = text[m.end() :]

    period = None
    for p in _TIME_OF_DAY:
        if p in time_text:
            period = p
            break

    tm = re.search(r"(\d{1,2})[点时](?:(\d{1,2})分?|半)?", time_text)
    if tm:
        hour = int(tm.group(1))
        if tm.group(2):
            minute = int(tm.group(2))
        elif "半" in time_text[tm.start() : tm.end() + 2]:
            minute = 30
        if period:
            hour = _adjust_hour_for_period(hour, period)

    # Validate hour/minute range
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None

    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        0,
    )


def parse_chinese_time(text: str, now: datetime = None) -> Optional[datetime]:
    """Parse a Chinese time expression into an absolute datetime.

    Args:
        text: Chinese time expression (e.g. "三小时后", "明天下午两点", "下周五")
        now: Reference time (default: datetime.now())

    Returns:
        Absolute datetime, or None if unparsable.
    """
    if not text or not text.strip():
        return None

    if now is None:
        now = datetime.now()

    # Normalize Chinese numerals to Arabic
    normalized = _normalize_chinese_nums(text.strip())

    # Try parsers in priority order
    result = _parse_relative_time(normalized, now)
    if result:
        return _validate_result(result, now)

    result = _parse_weekday_time(normalized, now)
    if result:
        return _validate_result(result, now)

    result = _parse_absolute_time(normalized, now)
    if result:
        return _validate_result(result, now)

    return None


def _validate_result(result: datetime, now: datetime) -> Optional[datetime]:
    """Validate parsed time: not too far past, not too far future."""
    # Allow up to 5 minutes in the past (speech delay grace)
    if result < now - timedelta(minutes=5):
        return None

    # Clamp to at least 1 minute from now if slightly past
    if result < now:
        result = now + timedelta(minutes=1)

    # Max 365 days in the future
    if result > now + timedelta(days=365):
        return None

    return result


# ============================================================================
# Content + time extraction from full command text
# ============================================================================

# Pivot words that separate time from content
_PIVOT_WORDS = [
    "提醒我",
    "帮我提醒",
    "提醒一下",
    "帮我定时",
    "定时提醒",
]

# Time signal words — if present, that segment likely contains the time
_TIME_SIGNALS = re.compile(
    r"小时后|分钟后|天后|周后|星期后|月后|半小时后"
    r"|今天|明天|后天|大后天|今晚|明晚|今早|明早"
    r"|凌晨|早上|上午|中午|下午|傍晚|晚上|夜里"
    r"|下周|下下周|这周|下礼拜|下星期"
    r"|[点时]"
)


def parse_reminder_text(
    text: str, now: datetime = None
) -> Tuple[Optional[str], Optional[datetime]]:
    """Parse a full reminder command into (content, trigger_time).

    Handles both orderings:
    - "三小时后提醒我开会" → ("开会", now+3h)
    - "提醒我明天下午两点开会" → ("开会", tomorrow 14:00)
    - "提醒我三小时后开会" → ("开会", now+3h)

    Args:
        text: Full command text (after wakeword removal)
        now: Reference time (default: datetime.now())

    Returns:
        (content, trigger_time) or (None, None) if unparsable.
    """
    if not text or not text.strip():
        return None, None

    if now is None:
        now = datetime.now()

    text = text.strip()
    # Remove common punctuation that ASR adds
    text = re.sub(r"[，,。.！!？?；;：:、]", "", text)

    # Find pivot word position
    pivot_word = None
    pivot_pos = -1
    for pw in _PIVOT_WORDS:
        pos = text.find(pw)
        if pos != -1:
            pivot_word = pw
            pivot_pos = pos
            break

    if pivot_pos == -1:
        # No pivot word found — try parsing entire text as time
        # Content defaults to "提醒"
        trigger_time = parse_chinese_time(text, now)
        if trigger_time:
            return "提醒", trigger_time
        return None, None

    before_pivot = text[:pivot_pos].strip()
    after_pivot = text[pivot_pos + len(pivot_word) :].strip()

    # Determine which part is time and which is content
    before_has_time = (
        bool(_TIME_SIGNALS.search(before_pivot)) if before_pivot else False
    )
    after_has_time = bool(_TIME_SIGNALS.search(after_pivot)) if after_pivot else False

    if before_has_time and not after_has_time:
        # "三小时后 提醒我 开会"
        trigger_time = parse_chinese_time(before_pivot, now)
        content = after_pivot or "提醒"
    elif after_has_time and not before_has_time:
        # "提醒我 明天下午两点 开会"
        # Need to separate time from content in after_pivot
        trigger_time, content = _split_time_and_content(after_pivot, now)
    elif before_has_time and after_has_time:
        # Both have time signals — try combining them first
        # e.g. "明天 提醒我 下午三点开会" → "明天下午三点" + "开会"
        combined_time, combined_content = _split_time_and_content(
            before_pivot + after_pivot, now
        )
        if combined_time:
            trigger_time = combined_time
            content = combined_content
        else:
            # Fallback: try before alone, then after alone
            trigger_time = parse_chinese_time(before_pivot, now)
            content = after_pivot or "提醒"
            if trigger_time is None:
                trigger_time, content = _split_time_and_content(after_pivot, now)
    else:
        # Neither has obvious time signals — try both
        trigger_time = parse_chinese_time(before_pivot, now)
        if trigger_time:
            content = after_pivot or "提醒"
        else:
            trigger_time = parse_chinese_time(after_pivot, now)
            content = before_pivot or "提醒"

    if trigger_time is None:
        # Last resort: try entire text
        trigger_time = parse_chinese_time(text, now)
        if trigger_time:
            content = "提醒"
        else:
            return None, None

    return content.strip() or "提醒", trigger_time


def _split_time_and_content(text: str, now: datetime) -> Tuple[Optional[datetime], str]:
    """Split a string that contains both time and content.

    e.g. "明天下午两点开会" → (tomorrow 14:00, "开会")
    e.g. "明天下午三点喝下午茶" → (tomorrow 15:00, "喝下午茶")

    Strategy: find the rightmost time token boundary, but require period words
    (下午/晚上/etc.) to be followed by a digit or 点/时 to avoid consuming
    content words like "下午茶" or "晚上总结".
    """
    cn_num = r"[零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾\d]"
    time_end_patterns = [
        # Relative patterns (unambiguous — always time tokens)
        cn_num + r"+个?小时" + cn_num + r"+分(?:钟)?后",  # N小时M分后
        cn_num + r"+个?小时后",
        r"半小时后",
        cn_num + r"+分钟后",
        cn_num + r"+天后",
        cn_num + r"+个?(?:周|礼拜|星期)后",
        cn_num + r"+个?月后",
        # Time with hour marker (unambiguous)
        cn_num + r"+[点时](?:" + cn_num + r"+分?|半)?",
        # Day offsets (unambiguous)
        r"(?:今天|明天|后天|大后天|今日|明日|今晚|明晚|今早|明早)",
        # Weekday (unambiguous)
        r"下下?(?:周|礼拜|星期)[一二三四五六日天1-7]",
        r"[这本](?:周|礼拜|星期)[一二三四五六日天1-7]",
        # Period words ONLY when followed by digit/点/时 (avoids 下午茶/晚上总结)
        r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间|半夜)(?="
        + cn_num
        + r"|[点时])",
    ]

    rightmost_end = 0
    for pattern in time_end_patterns:
        for m in re.finditer(pattern, text):
            if m.end() > rightmost_end:
                rightmost_end = m.end()

    if rightmost_end == 0:
        return None, text

    time_part = text[:rightmost_end].strip()
    content_part = text[rightmost_end:].strip()

    result = parse_chinese_time(time_part, now)
    if result:
        return result, content_part or "提醒"

    return None, text
