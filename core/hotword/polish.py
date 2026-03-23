"""
AI Polish Module (Layer 3)
==========================
Uses LLM to polish and correct ASR transcription output.
"""

import httpx
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse

from ..logging import get_system_logger

logger = get_system_logger()

# Shared default prompt template - single source of truth
# Based on Codex + Gemini tri-party analysis (v4.0)
# v4.0: Added phonetic alias table for cross-lingual matching
# Key insight: LLM needs explicit Chinese-sound → English-term mappings
DEFAULT_POLISH_PROMPT = """任务：修正语音识别文本的错别字和标点。

【判断流程】按顺序检查：
1. 原文是否包含"无意义乱码"或"中文音译英文"？→ 查音译表替换
2. 是否有同音字错误？→ 结合语境替换
3. 都不是？→ 仅修正标点，保留原文

【音译对照表】常见中文音译 → 英文术语
- 咖啡鱼/卡飞UI/康飞 → ComfyUI（AI绘图工具）
- 克劳德/克老德/cloud code → claude code（AI编程助手）
- 迪普seek/deep seek → deepseek（AI模型）
- 吉米奈/杰米尼 → gemini（AI模型）
- 可德克斯/code x → codex（AI编程助手）
- 阿尔特拉think/ultra think → ultrathink（思考模式）

【中文参考词汇】{hotwords_chinese}

【英文参考词汇】{hotwords_english}

【跨语言替换示例 - 应该替换】
原文：我觉得迪普seek写代码还行，就是有时候慢。
修正：我觉得deepseek写代码还行，就是有时候慢。
理由："迪普seek"是deepseek的中文音译+英文混合误识别

原文：你试试看用杰米尼来分析一下这张图。
修正：你试试看用gemini来分析一下这张图。
理由："杰米尼"是gemini的中文音译，AI工具语境

原文：那个卡飞UI的工作流我还没搭完。
修正：那个ComfyUI的工作流我还没搭完。
理由："卡飞UI"是ComfyUI的中文音译误识别，AI绘图语境

【不要替换的情况】
原文：I will try to think about it
修正：I will try to think about it。
理由：正常英语句子，不是ultrathink的误识别

原文：check the cloud service status
修正：check the cloud service status。
理由：cloud在这里就是正确的词，讨论云服务

【禁止】
- 回答或补充内容
- 改变句子原意
- 把通顺的表达强行改成专业术语
- 不要因为谨慎词汇表中存在某个词，就把原本通顺的表达强行改成该词

原文：{text}
修正："""


@dataclass
class PolishConfig:
    """AI polish configuration."""

    enabled: bool = False
    api_url: str = "https://openrouter.ai/api"
    api_key: str = ""
    model: str = "deepseek/deepseek-chat-v3.1:free"
    timeout: float = 10.0

    # 备用 API 配置（用于智能轮询）
    api_url_backup: str = ""  # 备用 API 地址，为空则不启用轮询
    api_key_backup: str = ""  # 备用 API 密钥（如果不同）
    model_backup: str = ""  # 备用模型（如果不同，为空则用主模型）

    # 智能轮询参数
    slow_threshold_ms: float = 3000.0  # 响应超过此值视为"慢"（毫秒）
    switch_after_slow_count: int = 2  # 连续慢 N 次后切换 API

    # Prompt template - supports {text}, {domain_context}, {hotwords}
    # Also supports {hotwords_critical} and {hotwords_context} for tiered system
    # Uses the shared DEFAULT_POLISH_PROMPT constant
    prompt_template: str = DEFAULT_POLISH_PROMPT

    # Domain context and hotwords for intelligent correction
    domain_context: str = ""
    hotwords: list = None  # List of hotword strings (all weight >= 0.3, v3.3)

    # v1.2: 个性化偏好 + 一键开关
    personalization_rules: str = ""  # 用户自然语言规则（每行一条）
    auto_structure: bool = False  # 自动结构化开关
    filter_filler_words: bool = True  # 口语过滤开关（默认开启，保持现有行为）

    # Tiered hotwords (set by HotWordManager, v3.1 with English support)
    hotwords_critical: list = None  # weight = 1.0: mandatory vocabulary (中英文)
    hotwords_strong: list = None  # weight = 0.5: Chinese reference words
    hotwords_english: list = None  # weight = 0.5: English reference (stricter rules)
    hotwords_cautious: list = None  # weight = 0.1: strict LLM constraint only
    hotwords_context: list = None  # unused in v3.1 (kept for backwards compat)

    def __post_init__(self):
        if self.hotwords is None:
            self.hotwords = []
        if self.hotwords_critical is None:
            self.hotwords_critical = []
        if self.hotwords_strong is None:
            self.hotwords_strong = []
        if self.hotwords_english is None:
            self.hotwords_english = []
        if self.hotwords_cautious is None:
            self.hotwords_cautious = []
        if self.hotwords_context is None:
            self.hotwords_context = []
        # Validate api_url format
        self._validate_api_url()

    def _validate_api_url(self) -> bool:
        """Validate api_url is a proper HTTP(S) URL."""
        if not self.api_url:
            return False
        try:
            parsed = urlparse(self.api_url)
            if parsed.scheme not in ("http", "https"):
                from ..logging import get_system_logger

                get_system_logger().warning(
                    f"Invalid api_url scheme: {parsed.scheme!r}. Expected http or https."
                )
                return False
            if not parsed.netloc:
                from ..logging import get_system_logger

                get_system_logger().warning(
                    f"Invalid api_url: missing host in {self.api_url!r}"
                )
                return False
            return True
        except Exception:
            return False


class AIPolisher:
    """
    AI-powered text polisher using LLM.

    Uses OpenAI-compatible API to polish ASR output.
    Supports intelligent API failover when response is slow.
    """

    def __init__(self, config: PolishConfig):
        self.config = config
        self._client: Optional[httpx.Client] = None

        # 智能轮询状态
        self._using_backup: bool = False  # 当前是否使用备用 API
        self._slow_count: int = 0  # 连续慢响应计数
        self._last_switch_reason: str = ""  # 上次切换原因（调试用）

    def _get_current_api_config(self) -> Tuple[str, str, str]:
        """
        获取当前使用的 API 配置。

        Returns:
            (api_url, api_key, model)
        """
        if self._using_backup and self.config.api_url_backup:
            return (
                self.config.api_url_backup,
                self.config.api_key_backup or self.config.api_key,
                self.config.model_backup or self.config.model,
            )
        return (self.config.api_url, self.config.api_key, self.config.model)

    def _check_and_switch_api(self, response_time_ms: float, had_error: bool) -> None:
        """
        检查响应时间，必要时切换 API。

        Args:
            response_time_ms: 本次响应时间（毫秒）
            had_error: 是否发生错误（超时、网络错误等）
        """
        # 如果没有配置备用 API，不做切换
        if not self.config.api_url_backup:
            return

        is_slow = response_time_ms > self.config.slow_threshold_ms or had_error

        if is_slow:
            self._slow_count += 1
            current_api = "备用" if self._using_backup else "主"
            logger.debug(
                f"[POLISH] {current_api}API 响应慢/错误 "
                f"({response_time_ms:.0f}ms), 连续慢次数: {self._slow_count}"
            )

            # 达到切换阈值
            if self._slow_count >= self.config.switch_after_slow_count:
                self._using_backup = not self._using_backup
                self._slow_count = 0
                new_api = "备用" if self._using_backup else "主"
                self._last_switch_reason = (
                    f"连续{self.config.switch_after_slow_count}次慢响应"
                    if not had_error
                    else "API错误"
                )
                logger.info(
                    f"[POLISH] 切换到{new_api}API (原因: {self._last_switch_reason})"
                )
        else:
            # 响应正常，重置计数
            if self._slow_count > 0:
                logger.debug(
                    f"[POLISH] 响应正常 ({response_time_ms:.0f}ms), 重置慢计数"
                )
            self._slow_count = 0

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.Client(timeout=self.config.timeout)
        return self._client

    def _build_prompt(self, text: str, screen_context: str = "") -> str:
        """Build the full prompt with hotwords, domain context, and v1.2 feature rules.

        Args:
            text: Raw ASR output to polish
            screen_context: Runtime window context string (e.g., "用户当前在WeChat中（聊天场景）")
        """
        from .utils import is_cjk_word

        template = self.config.prompt_template

        # v3.1: Separate Chinese and English hotwords
        # Chinese hotwords = critical (Chinese only) + strong (Chinese reference)
        # English hotwords = critical (English only) + hotwords_english

        # Split critical tier into Chinese and English
        critical_chinese = []
        critical_english = []
        if self.config.hotwords_critical:
            for word in self.config.hotwords_critical[:15]:
                if is_cjk_word(word):
                    critical_chinese.append(word)
                else:
                    critical_english.append(word)

        # Build Chinese hotwords list
        chinese_hotwords = critical_chinese[:]
        if self.config.hotwords_strong:
            chinese_hotwords.extend(self.config.hotwords_strong[:15])  # Unified limit

        # Build English hotwords list
        english_hotwords = critical_english[:]
        if self.config.hotwords_english:
            english_hotwords.extend(
                self.config.hotwords_english[:25]
            )  # Increased limit

        # Combined list for backwards compatibility
        all_hotwords = chinese_hotwords + english_hotwords
        hotwords_str = ", ".join(all_hotwords) if all_hotwords else "无"

        # Format for new v3.1 template
        hotwords_chinese_str = ", ".join(chinese_hotwords) if chinese_hotwords else "无"
        hotwords_english_str = ", ".join(english_hotwords) if english_hotwords else "无"

        # Format domain context
        domain_context = self.config.domain_context or "通用"

        # Replace placeholders with graceful fallback chain
        try:
            rendered = template.format(
                text=text,
                hotwords=hotwords_str,  # backwards compat
                hotwords_chinese=hotwords_chinese_str,  # v3.1
                hotwords_english=hotwords_english_str,  # v3.1
                domain_context=domain_context,
                hotwords_critical=(
                    ", ".join(self.config.hotwords_critical[:15])
                    if self.config.hotwords_critical
                    else "无"
                ),
                hotwords_strong=(
                    ", ".join(self.config.hotwords_strong[:15])
                    if self.config.hotwords_strong
                    else "无"
                ),
                hotwords_context=(
                    ", ".join(self.config.hotwords_context[:15])
                    if self.config.hotwords_context
                    else "无"
                ),
            )
        except KeyError as e:
            # Fallback 1: Try backwards compatible format (v2.x templates with {hotwords} only)
            logger.warning(
                f"Template missing placeholder {e}, trying backwards compatible format"
            )
            try:
                rendered = template.format(
                    text=text,
                    hotwords=hotwords_str,
                    domain_context=domain_context,
                )
            except KeyError as e2:
                # Fallback 2: Minimal format that preserves hotwords
                logger.error(
                    f"Template also missing {e2}, using minimal format with hotwords"
                )
                rendered = f"润色以下文字（参考词汇：{hotwords_str}）：\n\n{text}"

        # v3.5: Post-inject cautious block before the TEMPLATE's final "原文：" anchor.
        # FIX (tri-party R1): Must find anchor on raw template BEFORE format(), not after.
        # If we searched rendered text, user input containing "原文：" would corrupt injection.
        if self.config.hotwords_cautious:
            cautious_str = ", ".join(self.config.hotwords_cautious[:10])
            cautious_block = (
                f"\n【谨慎词汇 — 仅乱码时替换】{cautious_str}\n"
                "注意：以上词汇极易误触发。仅当原文出现无意义音译乱码且发音接近时才可替换。"
                "若原句表意完整通顺，即使读音相似也绝对不动。\n"
                '正例：原文"啪因好听"→"琶音好听"（"啪因"无意义，是"琶音"的乱码）\n'
                '反例：原文"爬音阶练习"→不替换（"爬音阶"通顺有意义，不是乱码）\n'
            )
            # Find anchor on the RAW TEMPLATE (before user text was injected).
            # This guarantees we never match "原文：" inside user's speech content.
            template_anchor = template.rfind("原文：")
            if template_anchor > 0:
                # Map template position to rendered position:
                # Everything before the anchor in the template maps to rendered[:offset].
                # We use the template prefix (before anchor) formatted with same args
                # to find the exact rendered offset.
                template_prefix = template[:template_anchor]
                try:
                    rendered_prefix = template_prefix.format(
                        text=text,
                        hotwords=hotwords_str,
                        hotwords_chinese=hotwords_chinese_str,
                        hotwords_english=hotwords_english_str,
                        domain_context=domain_context,
                        hotwords_critical=(
                            ", ".join(self.config.hotwords_critical[:15])
                            if self.config.hotwords_critical
                            else "无"
                        ),
                        hotwords_strong=(
                            ", ".join(self.config.hotwords_strong[:15])
                            if self.config.hotwords_strong
                            else "无"
                        ),
                        hotwords_context=(
                            ", ".join(self.config.hotwords_context[:15])
                            if self.config.hotwords_context
                            else "无"
                        ),
                    )
                    inject_pos = len(rendered_prefix)
                except (KeyError, IndexError):
                    # Fallback: if prefix format fails, use rfind on rendered
                    # (acceptable since format already succeeded for full template)
                    inject_pos = rendered.rfind("原文：")
                    if inject_pos <= 0:
                        inject_pos = 0

                if inject_pos > 0:
                    rendered = (
                        rendered[:inject_pos]
                        + cautious_block
                        + "\n"
                        + rendered[inject_pos:]
                    )
                else:
                    rendered = cautious_block + "\n" + rendered
            else:
                # No anchor in template — prepend cautious block (defensive fallback)
                rendered = cautious_block + "\n" + rendered

        # v1.2: Inject feature rules block (personalization + one-click toggles)
        feature_parts = []

        # 一键开关：结构化
        if self.config.auto_structure:
            feature_parts.append(
                "- 当内容较长且包含多个要点时，适当用换行分段、用编号列举。"
                "短句或单一话题不要加结构。禁止使用Markdown格式符，只输出纯文本"
            )

        # 一键开关：口语过滤
        if self.config.filter_filler_words:
            feature_parts.append(
                '- 去除无意义的口语填充词（如"就是"、"然后的话"、"嗯"、"呃"等），'
                "保留有实际含义的用法和句尾语气词"
            )

        # 用户自定义规则 — separate from built-in feature toggles
        user_rules = []
        if (
            self.config.personalization_rules
            and self.config.personalization_rules.strip()
        ):
            for line in self.config.personalization_rules.strip().splitlines():
                if line.strip():
                    user_rules.append(f"- {line.strip()}")

        # Built-in feature toggles: inject before 原文 (low priority)
        if feature_parts:
            feature_rules_block = "\n【偏好设置】\n" + "\n".join(feature_parts) + "\n"
            # Inject before the 原文 anchor in template
            template_anchor = template.rfind("原文：")
            if template_anchor > 0:
                template_prefix = template[:template_anchor]
                try:
                    rendered_prefix_len = len(
                        template_prefix.format(
                            text=text,
                            hotwords=hotwords_str,
                            hotwords_chinese=hotwords_chinese_str,
                            hotwords_english=hotwords_english_str,
                            domain_context=domain_context,
                            hotwords_critical=(
                                ", ".join(self.config.hotwords_critical[:15])
                                if self.config.hotwords_critical
                                else "无"
                            ),
                            hotwords_strong=(
                                ", ".join(self.config.hotwords_strong[:15])
                                if self.config.hotwords_strong
                                else "无"
                            ),
                            hotwords_context=(
                                ", ".join(self.config.hotwords_context[:15])
                                if self.config.hotwords_context
                                else "无"
                            ),
                        )
                    )
                    final_anchor = rendered.find("原文：", rendered_prefix_len - 10)
                    if final_anchor > 0:
                        rendered = (
                            rendered[:final_anchor]
                            + feature_rules_block
                            + "\n"
                            + rendered[final_anchor:]
                        )
                    else:
                        rendered = rendered + feature_rules_block
                except (KeyError, IndexError):
                    final_anchor = rendered.rfind("原文：")
                    if final_anchor > 0:
                        rendered = (
                            rendered[:final_anchor]
                            + feature_rules_block
                            + "\n"
                            + rendered[final_anchor:]
                        )
                    else:
                        rendered = rendered + feature_rules_block
            else:
                rendered = rendered + feature_rules_block

        # User personalization rules: inject at TOP
        # These can override default behavior (e.g., "translate to English")
        if user_rules:
            user_rules_block = (
                "【用户个性化规则】以下规则由用户设置，"
                "在完成错别字修正的基础上，额外执行以下要求：\n"
                + "\n".join(user_rules)
                + "\n\n"
            )
            rendered = user_rules_block + rendered

        # Inject domain context for homophone disambiguation
        # e.g., domain "3D建模" helps LLM know "景点" should be "顶点" (vertex)
        if domain_context and domain_context != "通用":
            domain_block = (
                f"\n【用户领域】{domain_context}\n" "根据用户领域纠正同音错字。\n"
            )
            final_anchor = rendered.rfind("原文：")
            if final_anchor > 0:
                rendered = (
                    rendered[:final_anchor]
                    + domain_block
                    + "\n"
                    + rendered[final_anchor:]
                )
            else:
                rendered = rendered + domain_block

        # v1.2: Inject screen context (runtime parameter, not persisted)
        if screen_context:
            screen_context_block = (
                f"\n【当前场景】{screen_context}\n"
                "根据场景调整文风：聊天场景保留口语感和语气词；"
                "文档/邮件场景偏书面化；编程场景严格保留英文标识符大小写。\n"
            )
            # Inject before 原文 anchor
            final_anchor = rendered.rfind("原文：")
            if final_anchor > 0:
                rendered = (
                    rendered[:final_anchor]
                    + screen_context_block
                    + "\n"
                    + rendered[final_anchor:]
                )
            else:
                rendered = rendered + screen_context_block

        return rendered

    def polish(self, text: str, screen_context: str = "") -> str:
        """
        Polish the transcribed text using LLM.

        Args:
            text: Raw ASR output

        Returns:
            Polished text, or original text if polish fails
        """
        if not self.config.enabled:
            return text

        if not text or len(text.strip()) < 2:
            return text

        try:
            client = self._get_client()

            # Build request with hotwords context + v1.2 screen context
            prompt = self._build_prompt(text, screen_context=screen_context)

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            }

            # System message with core constraints
            system_msg = (
                "你是语音识别文本修正工具。严格规则：\n"
                "1. 禁止回答、补充、解释任何内容\n"
                "2. 严禁改变句子原意或增删实质信息\n"
                "3. 禁止添加Markdown格式\n"
                '4. 必须返回JSON格式：{"text": "修正后的文本"}'
            )

            # Request JSON output to prevent explanations
            json_prompt = f'{prompt}\n\n输出JSON：{{"text": "修正后的文本"}}'

            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": json_prompt},
                ],
                "max_tokens": 1000,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }

            # Build full API URL - handle both /api and /api/v1 base URLs
            base_url = self.config.api_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"

            # Call API
            response = client.post(full_url, headers=headers, json=payload)

            if response.status_code != 200:
                logger.warning(f"Polish API error: {response.status_code}")
                return text

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            # Parse JSON response to extract text field
            try:
                import json

                parsed = json.loads(content)
                polished = parsed.get("text", content)
            except json.JSONDecodeError:
                # Fallback: use raw content if not valid JSON
                polished = content

            # Basic validation - polished text shouldn't be empty or too different
            if not polished or len(polished) < 1:
                logger.warning("Polish returned empty result")
                return text

            # LENGTH PROTECTION: Reject if too much content removed or added
            # Relax thresholds when user has personalization rules
            # (e.g., translation changes length significantly)
            has_custom_rules = bool(
                self.config.personalization_rules
                and self.config.personalization_rules.strip()
            )
            if has_custom_rules:
                length_ratio = 0.15  # Translation can drastically change length
                max_ratio = 8
            else:
                length_ratio = 0.6 if self.config.filter_filler_words else 0.8
                max_ratio = 3
            original_len = len(text)
            polished_len = len(polished)
            if polished_len < original_len * length_ratio:
                logger.warning(
                    f"Polish rejected: removed {100 - polished_len * 100 // original_len}% content "
                    f"({original_len} -> {polished_len} chars, threshold={length_ratio})"
                )
                return text

            # Reject if polished text is absurdly longer than original
            # (LLM echoed screen context or hallucinated content)
            if (
                polished_len > original_len * max_ratio
                and polished_len > original_len + 50
            ):
                logger.warning(
                    f"Polish rejected: output too long "
                    f"({original_len} -> {polished_len} chars, {polished_len / original_len:.1f}x)"
                )
                return text

            logger.debug(f"Polished: '{text}' -> '{polished}'")
            return polished

        except httpx.TimeoutException:
            logger.warning("Polish API timeout")
            return text
        except Exception as e:
            logger.error(f"Polish error: {e}")
            return text

    def polish_with_debug(self, text: str, screen_context: str = "") -> Dict[str, Any]:
        """
        Polish text and return full debug information.

        Args:
            text: Raw ASR output
            screen_context: Runtime window context string (v1.2)

        Returns:
            Dict with keys: output_text, changed, api_time_ms, error, http_status, full_prompt
        """
        # 获取当前 API 配置（支持智能轮询）
        current_url, current_key, current_model = self._get_current_api_config()

        debug_info = {
            "enabled": self.config.enabled,
            "api_url": current_url,
            "full_api_url": "",  # Will be set after URL construction
            "model": current_model,
            "timeout": self.config.timeout,
            "input_text": text,
            "prompt_template": self.config.prompt_template,
            "full_prompt": "",
            "output_text": text,
            "changed": False,
            "api_time_ms": 0.0,
            "error": "",
            "http_status": 0,
            "using_backup": self._using_backup,  # 是否使用备用 API
        }

        if not self.config.enabled:
            debug_info["error"] = "Polish disabled"
            return debug_info

        if not text or len(text.strip()) < 2:
            debug_info["error"] = "Text too short"
            return debug_info

        had_error = False
        try:
            client = self._get_client()

            # Build request with hotwords context + v1.2 screen context
            prompt = self._build_prompt(text, screen_context=screen_context)
            debug_info["full_prompt"] = prompt

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {current_key}",
            }

            # System message with core constraints (must match polish() method)
            system_msg = (
                "你是语音识别文本修正工具。严格规则：\n"
                "1. 禁止回答、补充、解释任何内容\n"
                "2. 严禁改变句子原意或增删实质信息\n"
                "3. 禁止添加Markdown格式\n"
                '4. 必须返回JSON格式：{"text": "修正后的文本"}'
            )

            # Request JSON output to prevent explanations
            json_prompt = f'{prompt}\n\n输出JSON：{{"text": "修正后的文本"}}'

            payload = {
                "model": current_model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": json_prompt},
                ],
                "max_tokens": 1000,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }

            # Build full API URL - handle both /api and /api/v1 base URLs
            base_url = current_url.rstrip("/")
            if base_url.endswith("/v1"):
                full_url = f"{base_url}/chat/completions"
            else:
                full_url = f"{base_url}/v1/chat/completions"
            debug_info["full_api_url"] = full_url

            # Call API with timing
            start_time = time.time()
            response = client.post(full_url, headers=headers, json=payload)
            api_time = (time.time() - start_time) * 1000
            debug_info["api_time_ms"] = api_time
            debug_info["http_status"] = response.status_code

            if response.status_code != 200:
                debug_info["error"] = (
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
                logger.warning(f"Polish API error: {response.status_code}")
                had_error = True
                # 检查是否需要切换 API
                self._check_and_switch_api(api_time, had_error)
                return debug_info

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            # Parse JSON response to extract text field
            try:
                import json

                parsed = json.loads(content)
                polished = parsed.get("text", content)
            except json.JSONDecodeError:
                # Fallback: use raw content if not valid JSON
                polished = content

            if not polished or len(polished) < 1:
                debug_info["error"] = "Empty response from API"
                logger.warning("Polish returned empty result")
                return debug_info

            # LENGTH PROTECTION: Reject if too much content removed or added
            has_custom_rules = bool(
                self.config.personalization_rules
                and self.config.personalization_rules.strip()
            )
            if has_custom_rules:
                length_ratio = 0.15
                max_ratio = 8
            else:
                length_ratio = 0.6 if self.config.filter_filler_words else 0.8
                max_ratio = 3
            original_len = len(text)
            polished_len = len(polished)
            if polished_len < original_len * length_ratio:
                debug_info["error"] = (
                    f"Removed {100 - polished_len * 100 // original_len}% content "
                    f"(threshold={length_ratio})"
                )
                logger.warning(
                    f"Polish rejected: removed too much content "
                    f"({original_len} -> {polished_len} chars, threshold={length_ratio})"
                )
                return debug_info

            # Reject if polished text is absurdly longer than original
            if (
                polished_len > original_len * max_ratio
                and polished_len > original_len + 50
            ):
                debug_info["error"] = (
                    f"Output too long ({polished_len / original_len:.1f}x)"
                )
                logger.warning(
                    f"Polish rejected: output too long "
                    f"({original_len} -> {polished_len} chars)"
                )
                return debug_info

            debug_info["output_text"] = polished
            debug_info["changed"] = polished != text

            # 检查是否需要切换 API（正常响应也要检查响应时间）
            self._check_and_switch_api(api_time, had_error=False)

            logger.debug(f"Polished: '{text}' -> '{polished}'")
            return debug_info

        except httpx.TimeoutException:
            debug_info["error"] = "Timeout"
            debug_info["api_time_ms"] = self.config.timeout * 1000  # 超时时间
            logger.warning("Polish API timeout")
            # 超时视为严重错误，检查切换
            self._check_and_switch_api(self.config.timeout * 1000, had_error=True)
            return debug_info
        except Exception as e:
            debug_info["error"] = str(e)
            logger.error(f"Polish error: {e}")
            # 其他错误也检查切换
            self._check_and_switch_api(0, had_error=True)
            return debug_info

    def close(self):
        """Close HTTP client."""
        if self._client:
            self._client.close()
            self._client = None
