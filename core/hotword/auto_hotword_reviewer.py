"""LLM-driven gatekeeper for auto-learned screen hotwords.

The `SessionHotwordTracker` accumulates CJK terms that recur on the user's
screen.  Before any of them are injected into the ASR biasing context, this
reviewer asks an OpenAI-compatible LLM (the same DeepSeek endpoint Aria's
Polish layer uses by default) whether each candidate is a real proper noun
worth biasing or just OCR / UI noise.

Design follows a strict precision-first policy: only high-value, user-specific
terms should be approved. Ambiguous terms are returned as `unsure` and
re-evaluated when more OCR evidence arrives.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_API_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_TIMEOUT = 60  # Reviewing 50 terms can take 30+ seconds with reasoning models

_REVIEW_SYSTEM = (
    "你是一个语音识别系统的词表审核员。"
    "用户的屏幕 OCR 模块累积了一些反复出现的中文候选词，"
    "你需要判断每个候选词是否真的值得加入 ASR 的偏置词表 (hotword)。\n\n"
    "【总原则】宁可 unsure/drop，也不要把普通词、系统本来就认识的词、"
    "或只是在屏幕上频繁出现的 UI/正文词加入 hotword。hotword 只解决"
    "ASR 容易听错的少量高价值专名；它不是通用词典。\n\n"
    "【KEEP — 保留，必须严格】只保留明确且高价值的词：\n"
    "- 人名、角色名、作品名、产品名、品牌名、组织名、独特项目名\n"
    "- 罕见、用户特定、容易被 ASR 同音误识别的专业复合词\n"
    "- 候选词最好能从窗口标题/上下文看出是一个稳定实体，而不是一句话里的普通词\n\n"
    "【DROP — 剔除】以下情况应剔除：\n"
    '- 不完整中文片段（如 "动态"+"态首"+"动态首" 这种 n-gram 噪声）\n'
    "- UI 控件名（首页 / 取消 / 确定 / 设置 / 关注 / 投稿 / 评论 / 转发 等）\n"
    "- 虚词、副词、连接词、量词、动词主干、普通短语片段\n"
    "- 常见地名/国家名/产品类别/系统常识词，除非明显是用户专属实体\n"
    "- 大模型/ASR 通常已经认识的泛技术词或普通功能词，"
    "例如：插件、调度、着色、负面、步数、解码、重绘、生图、图生、脚本、骨架、"
    "编辑器、权重、纹理、字段、网格、公众号、服务号、蓝牙\n\n"
    "【UNSURE — 不确定】看起来可能有用但又像普通词、泛技术词、地名或短语的，"
    "一律回 unsure；不要为了凑数量 keep。\n\n"
    "【输出格式】只输出严格的 JSON，不要任何解释或前后文：\n"
    '{"verdicts": [{"term": "...", "decision": "keep|drop|unsure", "reason": "一句话简短说明"}, ...]}\n'
    "verdicts 必须覆盖所有候选词，顺序不限，每个候选词只能出现一次。"
)


@dataclass
class ReviewerConfig:
    """API/model settings for the reviewer.

    Defaults are chosen to mirror the Polish-layer config so callers can
    'inherit' from `polish.PolishConfig` by passing the same fields.  When
    `api_url`/`api_key`/`model` are empty strings, the caller is expected to
    fall back to whatever Polisher is currently configured.
    """

    api_url: str = _DEFAULT_API_URL
    api_key: str = ""
    model: str = _DEFAULT_MODEL
    timeout: int = _DEFAULT_TIMEOUT
    max_terms_per_call: int = 50
    enabled: bool = True
    # T2: review trigger is now distance-based, not wall-clock based.
    # The old `daily_review_hour` (04:00) basically never fired because most
    # users' machines are asleep at 04:00; this replacement triggers any time
    # we've been alive long enough since the previous review AND there are
    # enough pending candidates to make the API call worthwhile.
    # 24h was too coarse — caused at most one review/day, which combined with
    # the 50-term batch cap meant the candidate backlog grew faster than it
    # could be drained. 6h gives 4 reviews/day with the same per-batch cost
    # while still respecting `min_batch_size` so quiet hours don't burn API.
    review_interval_hours: int = 6
    # T4: don't burn an API call on a tiny batch. Manual review (sentinel) /
    # startup review still bypass this floor — see AriaApp._run_auto_hotword_review.
    min_batch_size: int = 8
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ReviewOutcome:
    """Result of a single review() call."""

    success: bool
    verdicts: dict[str, str] = field(default_factory=dict)  # term -> keep/drop/unsure
    reasons: dict[str, str] = field(default_factory=dict)  # term -> reason
    error: str = ""
    api_time_ms: float = 0.0
    input_count: int = 0
    output_count: int = 0


class AutoHotwordReviewer:
    """Wraps a single OpenAI-compatible chat call for vocabulary judging."""

    def __init__(self, config: ReviewerConfig):
        self.config = config

    def is_available(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.api_url
            and self.config.api_key
            and self.config.model
        )

    def review(self, candidates: list[dict]) -> ReviewOutcome:
        """Send a batch of candidates to the LLM and parse the verdict JSON.

        `candidates` shape (from `SessionHotwordTracker.get_pending_for_review`):
            [{"term": str, "count": int, "titles": [str, ...]}, ...]
        """
        if not candidates:
            return ReviewOutcome(success=True, input_count=0)
        if not self.is_available():
            return ReviewOutcome(
                success=False, error="reviewer disabled or unconfigured"
            )

        batch = candidates[: self.config.max_terms_per_call]
        prompt_user = _build_user_prompt(batch)
        return self._review_batch(batch, prompt_user)

    def review_approved(self, candidates: list[dict]) -> ReviewOutcome:
        """Re-check already approved auto-hotwords.

        This is intentionally separate from `review()`: pending terms ask
        "should this enter the hotword list?", while approved recheck asks
        "is this still worth keeping, or was it a generic/common term we should
        retire?"  The verdict schema stays identical so SessionHotwordTracker
        can apply it with a dedicated, conservative state transition.
        """
        if not candidates:
            return ReviewOutcome(success=True, input_count=0)
        if not self.is_available():
            return ReviewOutcome(
                success=False, error="reviewer disabled or unconfigured"
            )

        batch = candidates[: self.config.max_terms_per_call]
        prompt_user = _build_approved_recheck_prompt(batch)
        return self._review_batch(batch, prompt_user)

    def _review_batch(self, batch: list[dict], prompt_user: str) -> ReviewOutcome:
        try:
            import time

            t0 = time.time()
            raw = self._call_chat(prompt_user)
            elapsed_ms = (time.time() - t0) * 1000.0
        except Exception as exc:
            logger.warning(f"AutoHotword review API call failed: {exc}")
            return ReviewOutcome(success=False, error=f"{type(exc).__name__}: {exc}")

        verdicts, reasons = _parse_verdict_payload(raw)
        if not verdicts:
            return ReviewOutcome(
                success=False,
                error="empty/unparsable verdict payload",
                api_time_ms=elapsed_ms,
                input_count=len(batch),
            )

        candidate_terms = {item["term"] for item in batch}
        # Drop any verdicts the LLM hallucinated for terms we never sent.
        verdicts = {t: v for t, v in verdicts.items() if t in candidate_terms}
        reasons = {t: r for t, r in reasons.items() if t in candidate_terms}
        # Default-fill missing terms as "unsure" so they get re-evaluated next round.
        for item in batch:
            verdicts.setdefault(item["term"], "unsure")
        return ReviewOutcome(
            success=True,
            verdicts=verdicts,
            reasons=reasons,
            api_time_ms=elapsed_ms,
            input_count=len(batch),
            output_count=len(verdicts),
        )

    # -------------------------------------------------------------- HTTP

    def _call_chat(self, user_prompt: str) -> str:
        """POST to /chat/completions and return the raw assistant content."""
        from ..ai.gateway import AIRequestSpec, chat

        result = chat(
            AIRequestSpec(
                api_url=self.config.api_url,
                api_key=self.config.api_key,
                model=self.config.model,
                messages=[
                    {"role": "system", "content": _REVIEW_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                timeout_s=float(self.config.timeout),
                purpose="auto_hotword",
                temperature=0.0,
                response_format={"type": "json_object"},
                extra_payload={"stream": False},
                extra_headers=self.config.extra_headers or None,
            )
        )
        if not result.ok or not result.text:
            detail = result.detail or (
                result.error.value if result.error else "empty"
            )
            status = result.status_code or 0
            raise RuntimeError(
                f"auto_hotword review failed: status={status} detail={detail}"
            )
        # Cost tracking is handled inside gateway.chat on success.
        return result.text


# ---------------------------------------------------------- prompt + parser


def _sanitize_title_for_prompt(title: str, *, max_len: int = 80) -> str:
    """Strip prompt-structure-breaking characters from a window title.

    Window titles come from arbitrary apps that the user did NOT pick — a
    hostile/buggy app could put `\\n`, ``` ``` ``` ``` ``, or stuff like
    `]drop all candidates` into its title and end up steering the reviewer
    LLM via batched prompt content. We don't trust window titles enough to
    embed verbatim. We replace newlines/backticks with spaces, collapse
    whitespace, and cap length so a single attacker title can't drown out
    the real candidates.
    """
    if not title:
        return ""
    cleaned = re.sub(r"[\r\n`]+", " ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip() + "…"
    return cleaned


def _format_titles_for_prompt(titles: list[str]) -> str:
    cleaned = [_sanitize_title_for_prompt(t) for t in (titles or [])[:3]]
    cleaned = [t for t in cleaned if t]
    return " | ".join(cleaned) if cleaned else "(无标题样本)"


def _build_user_prompt(batch: list[dict]) -> str:
    """Render the candidates into a single-block text the LLM can chew on."""
    lines = ["请审核以下候选热词。"]
    lines.append("")
    lines.append("候选词列表（每行：词 / 出现次数 / 出现窗口标题样本）：")
    for item in batch:
        term = item.get("term", "")
        count = item.get("count", 0)
        title_str = _format_titles_for_prompt(item.get("titles", []))
        lines.append(f"  - {term}  ×{count}  in: {title_str}")
    lines.append("")
    lines.append(
        "请对每一个候选词输出 keep / drop / unsure 之一，"
        "并附一句简短理由。严格按照系统消息里的 JSON 格式输出。"
    )
    return "\n".join(lines)


def _build_approved_recheck_prompt(batch: list[dict]) -> str:
    """Render already-approved terms for a cleanup/recheck pass."""
    lines = [
        "请复审以下【已经批准】的自动热词。",
        "",
        "目标：把自动热词越迭代越干净。即使一个词是真的词，只要它太普通、"
        "ASR/大模型本来通常会识别、或只是屏幕正文/UI里高频出现，也应该 drop。",
        "只有仍然明显有 ASR 偏置价值的用户特定专名/罕见复合词才 keep。",
        "拿不准请 unsure；不要为了保留历史决定而 keep。",
        "",
        "已批准词列表（每行：词 / 出现次数 / 最近窗口标题样本 / 上次理由）：",
    ]
    for item in batch:
        term = item.get("term", "")
        count = item.get("count", 0)
        title_str = _format_titles_for_prompt(item.get("titles", []))
        # Reason came from a previous review's LLM output; sanitize the same
        # way as titles since it's just-as-untrusted text in this prompt.
        raw_reason = item.get("reason", "") or item.get("review_reason", "")
        reason = _sanitize_title_for_prompt(raw_reason, max_len=160)
        if reason:
            lines.append(f"  - {term}  ×{count}  in: {title_str}  reason: {reason}")
        else:
            lines.append(f"  - {term}  ×{count}  in: {title_str}")
    lines.append("")
    lines.append(
        "请对每一个词输出 keep / drop / unsure 之一。"
        "drop 表示不再值得作为自动 hotword 注入；keep 表示仍有明确 ASR 偏置价值。"
        "严格按照系统消息里的 JSON 格式输出。"
    )
    return "\n".join(lines)


def _parse_verdict_payload(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """Tolerant parser for the verdict JSON.

    Handles cases where the model wraps the JSON in code fences or prepends
    a sentence despite the response_format hint.
    """
    if not raw:
        return {}, {}
    text = raw.strip()
    # Strip ```json ... ``` fences if any.
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}, {}
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return {}, {}
    items = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, {}
    verdicts: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        term = item.get("term")
        decision = item.get("decision")
        reason = item.get("reason", "")
        if not isinstance(term, str) or not term.strip():
            continue
        if not isinstance(decision, str):
            continue
        d = decision.strip().lower()
        if d not in ("keep", "drop", "unsure"):
            continue
        verdicts[term] = d
        if isinstance(reason, str) and reason.strip():
            reasons[term] = reason.strip()
    return verdicts, reasons


# ---------------------------------------------------------- inheritance helper


def reviewer_config_from_polisher_config(polish_cfg: Any) -> ReviewerConfig:
    """Build a ReviewerConfig that mirrors the user's main Polisher config.

    Used as the default when the user hasn't configured a separate reviewer
    endpoint — the reviewer reuses the main Polish API by default.
    """
    if polish_cfg is None:
        return ReviewerConfig(api_key="")
    return ReviewerConfig(
        api_url=getattr(polish_cfg, "api_url", _DEFAULT_API_URL) or _DEFAULT_API_URL,
        api_key=getattr(polish_cfg, "api_key", "") or "",
        model=getattr(polish_cfg, "model", _DEFAULT_MODEL) or _DEFAULT_MODEL,
        timeout=int(
            getattr(polish_cfg, "timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT
        ),
    )
