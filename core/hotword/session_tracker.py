"""Session-level auto hotword tracker.

Tracks CJK terms that repeatedly appear on screen during a session and
maintains a three-state machine (pending / approved / rejected). Only
LLM-approved terms are injected into the ASR context — see
`auto_hotword_reviewer.py` for the gatekeeper.

State machine:
    record() new term         → counter[term]=1, status=pending
    record() count reaches N  → still pending (eligible for review)
    review keep               → status=approved (injected into ASR)
    review drop               → status=rejected (permanent blacklist)
    review unsure             → still pending (re-evaluated next round)

Lives in `data/auto_hotwords.json`. Atomic writes via tmp+replace.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable

# Reuse the existing screen_corrector vocabularies so the tracker and the
# polish-side corrector stay aligned on what counts as UI noise. We do NOT
# reuse `_iter_cjk_terms` — that emits every 2-8 sliding n-gram, which is
# too noisy for "send to an LLM as a candidate" (overlapping fragments
# saturate the cap before real proper nouns can accumulate count). See
# `_extract_candidate_terms` below.
from .screen_corrector import _CJK_STOP_TERMS, _looks_like_noisy_body_cjk_term


_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"
_STATUS_REJECTED = "rejected"

# Extra exact-match stop terms for the auto-learning path.
#
# The screen corrector deliberately keeps a broad recall surface because it is
# anchored by the user's ASR text.  The auto-hotword tracker is different: OCR
# alone can see dense app chrome for hours, so common UI labels must not occupy
# review slots or the bounded pending pool.
_AUTO_HOTWORD_STOP_TERMS = set(_CJK_STOP_TERMS) | {
    # Generic app/browser/navigation labels
    "中心",
    "首页",
    "历史",
    "列表",
    "菜单",
    "设置",
    "选项",
    "搜索",
    "全部",
    "更多",
    "返回",
    "刷新",
    "关闭",
    "打开",
    "发送",
    "消息",
    "通知",
    "评论",
    "分享",
    "收藏",
    "关注",
    "取消",
    "确认",
    "完成",
    # Bilibili/video-site chrome that repeatedly floods the tracker
    "会员",
    "大会员",
    "创作",
    "投稿",
    "稿件",
    "弹幕",
    "作者",
    "动态",
    "充电",
    "发消息",
    "记笔记",
    "赛事",
    # Generic workflow words that are useful as UI text, but weak ASR hotwords
    "自动",
    "模式",
    "状态",
    "建议",
    "文档",
    "图片",
    "逻辑",
    "参数",
}

# Approved OCR terms still pass through one final injection gate.  The reviewer
# can be intentionally conservative, but older state files may already contain
# broad/common words that add prompt noise while providing little ASR value.
# Keep the data for audit/manual review; just do not inject these generic terms
# into ASR/Polish session context.
_AUTO_HOTWORD_DO_NOT_INJECT_TERMS = {
    # Generic software / UI / workflow words the ASR and LLM already know.
    "插件",
    "调度",
    "着色",
    "负面",
    "步数",
    "解码",
    "重绘",
    "生图",
    "图生",
    "噪波",
    "采样器",
    "随机种",
    "脚本",
    "英雄",
    "骨架",
    "编辑器",
    "权重",
    "中路",
    "合成",
    "快照",
    "二测",
    "纹理",
    "字段",
    "网格",
    "头材质",
    "图灵奖",
    # Common public/product/category words that are too broad as auto-hotwords.
    "公众号",
    "服务号",
    "蓝牙",
    "北京",
    "罗马",
    "意大利",
    "油管",
    "小米",
    # Fragment-like candidates observed from dense OCR prose.
    "撤销脸",
    "驴哥说",
    "杯周赛",
    "邀请赛",
}


def _is_safe_auto_hotword_injection(term: str) -> bool:
    """Return whether an approved OCR term is safe to inject automatically.

    User-curated hotwords are handled elsewhere and are not subject to this
    gate.  This function only limits screen-OCR-learned terms, where a common
    word repeated on screen is often noise rather than useful ASR bias.
    """

    return bool(term and term not in _AUTO_HOTWORD_DO_NOT_INJECT_TERMS)


class SessionHotwordTracker:
    """Frequency-based CJK term tracker, LLM-gated for ASR injection.

    Thread-safe — `record()` runs on the OCR worker thread while
    `get_active_hotwords()` runs on the ASR worker. All public methods take
    the same lock.
    """

    MIN_COUNT_FOR_REVIEW = 3  # Must appear at least N times to be reviewed
    # The extractor cannot pre-classify posseg-driven false merges — only the
    # LLM can. Cap is sized so prose noise has 14-day TTL headroom to age out
    # via housekeeping without evicting real names mid-accumulation.
    MAX_TRACKED = 2500  # Hard cap on dict size (LRU-evict pending)
    MAX_INJECT = 100  # Max approved terms injected into ASR
    APPROVED_TTL_DAYS = 30  # Approved terms drop out if unseen for N days
    MAX_TITLE_SAMPLES = 5  # Per-term window-title samples kept for review
    DEDUP_WINDOW_S = 60.0  # Same-content fingerprint suppresses re-record
    PENDING_NOISE_TTL_DAYS = 14  # Pending<3-count older than this gets swept
    APPROVED_RECHECK_INTERVAL_HOURS = 24  # Revisit approved terms at most daily

    def __init__(self, data_path: Path | str, user_hotwords: set[str] | None = None):
        self._path = Path(data_path)
        self._lock = threading.RLock()
        self._terms: dict[str, dict] = {}
        # Caller can pass in the user-configured hotwords so we never re-track
        # terms the user has already curated.
        self._user_hotwords: set[str] = set(user_hotwords or ())
        # T1 (节流式 save): dirty flag flipped by record/apply/manual_override,
        # cleared by save(). The owning AriaApp drives a periodic save in its
        # daily-loop thread (every 30s) so a crash never loses more than ~30s
        # of in-memory pending state.
        self._dirty: bool = False
        # T2 (审查触发器): persisted timestamp of the most recent successful
        # review. Replaces the old "fire at 04:00" wall-clock trigger.
        self._last_review_at: str = ""
        # T3 (内容指纹去重): when the same OCR text re-arrives within
        # DEDUP_WINDOW_S the record() is treated as a no-op. Stops "user spoke
        # 10 times on the same screen → every term +10" pollution.
        self._last_record_fp: str = ""
        self._last_record_ts: float = 0.0
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            if isinstance(data.get("terms"), dict):
                self._terms = data["terms"]
            if isinstance(data.get("last_review_at"), str):
                self._last_review_at = data["last_review_at"]

    def save(self) -> None:
        """Persist to disk atomically. Tolerant to transient I/O errors so
        antivirus / cloud-sync interference can't poison the in-memory state.
        """
        with self._lock:
            payload = {
                "version": 2,
                "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "last_review_at": self._last_review_at,
                "terms": self._terms,
            }
            try:
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self._path)
                self._dirty = False
            except Exception:
                # Leave _dirty=True so the next periodic save retries. Don't
                # raise — record/review must never break because of disk
                # pressure or AV rescanning the tmp file mid-replace.
                pass

    def save_if_dirty(self) -> bool:
        """Idempotent persistence trigger. Used by the daily-loop heartbeat to
        flush pending state every ~30s without re-writing on every call."""
        with self._lock:
            if not self._dirty:
                return False
        self.save()
        return True

    def is_dirty(self) -> bool:
        with self._lock:
            return self._dirty

    def get_last_review_at(self) -> _dt.datetime | None:
        with self._lock:
            return _parse_iso(self._last_review_at)

    def mark_review_completed(self) -> None:
        """Record that a review pass just finished (used by review-trigger
        scheduler to compute "distance since last review")."""
        with self._lock:
            self._last_review_at = _dt.datetime.now().isoformat(timespec="seconds")
            self._dirty = True

    # --------------------------------------------------------- public ops

    def update_user_hotwords(self, words: Iterable[str]) -> None:
        """Refresh the user-curated set so we stop tracking words that the
        user just added to their own hotword list."""
        with self._lock:
            new_set = set(words or ())
            if new_set == self._user_hotwords:
                return
            self._user_hotwords = new_set
            # Drop any tracked term that became a user hotword — user wins.
            removed = False
            for term in list(self._terms):
                if term in self._user_hotwords:
                    self._terms.pop(term, None)
                    removed = True
            if removed:
                self._dirty = True

    def record(self, screen_text: str, window_title: str = "") -> int:
        """Account for one OCR result. Returns count of newly observed terms.

        T3 dedup: if the same OCR text re-arrives within DEDUP_WINDOW_S the
        record is a no-op. This is the difference between "this term reappears
        on screen" (the signal we want) and "user kept talking about the same
        screen" (the noise that used to leak through `_on_speech_start` firing
        OCR on every utterance).
        """
        if not screen_text:
            return 0
        # Cheap structural fingerprint — a 16-hex-char prefix is enough to
        # disambiguate every screen the user is realistically going to face;
        # collision risk on millions of screens is still negligible because we
        # only compare against the most recent fingerprint.
        fp = hashlib.md5(screen_text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        now_ts = time.time()
        now = _dt.datetime.now().isoformat(timespec="seconds")
        title = (window_title or "").strip()
        new_observations = 0
        with self._lock:
            if (
                self._last_record_fp == fp
                and (now_ts - self._last_record_ts) < self.DEDUP_WINDOW_S
            ):
                # Same exact screen seen within the dedup window — count once.
                # We still update _last_record_ts to bias any TTL window away
                # from the very moment of arrival (gives the user N seconds of
                # quiet before the same content can re-trigger).
                self._last_record_ts = now_ts
                return 0
            self._last_record_fp = fp
            self._last_record_ts = now_ts

            for term in _extract_candidate_terms(screen_text):
                if term in self._user_hotwords:
                    continue
                entry = self._terms.get(term)
                if entry is None:
                    if len(self._terms) >= self.MAX_TRACKED:
                        # Evict the oldest pending term to keep size bounded.
                        self._evict_one_pending_locked()
                        if len(self._terms) >= self.MAX_TRACKED:
                            continue
                    entry = {
                        "count": 0,
                        "first": now,
                        "last": now,
                        "status": _STATUS_PENDING,
                        "titles": [],
                    }
                    self._terms[term] = entry
                    new_observations += 1
                # Rejected terms stay rejected — don't even bump count, that's
                # the whole point of "permanent blacklist".
                if entry["status"] == _STATUS_REJECTED:
                    continue
                entry["count"] = int(entry.get("count", 0)) + 1
                entry["last"] = now
                if title and title not in entry["titles"]:
                    entry["titles"].append(title)
                    if len(entry["titles"]) > self.MAX_TITLE_SAMPLES:
                        entry["titles"] = entry["titles"][-self.MAX_TITLE_SAMPLES :]
            self._dirty = True
        return new_observations

    def get_active_hotwords(self) -> list[str]:
        """Return approved terms that have been seen recently (≤TTL).

        These are the only terms safe to inject into the ASR context — they
        must have been LLM-approved before reaching this list.
        """
        cutoff = _dt.datetime.now() - _dt.timedelta(days=self.APPROVED_TTL_DAYS)
        active: list[tuple[int, str]] = []
        with self._lock:
            for term, entry in self._terms.items():
                if entry.get("status") != _STATUS_APPROVED:
                    continue
                if not _is_safe_auto_hotword_injection(term):
                    continue
                last_seen = _parse_iso(entry.get("last", ""))
                if last_seen and last_seen < cutoff:
                    continue
                active.append((int(entry.get("count", 0)), term))
        active.sort(reverse=True)
        return [term for _, term in active[: self.MAX_INJECT]]

    def get_pending_for_review(self, max_terms: int = 50) -> list[dict]:
        """Pending terms with enough hits to be worth asking the LLM about.

        Returns a list of {term, count, titles} dicts, ordered by count desc.
        The caller (`AutoHotwordReviewer`) builds the LLM prompt from this.
        """
        eligible: list[tuple[int, str, list[str]]] = []
        with self._lock:
            for term, entry in self._terms.items():
                if entry.get("status") != _STATUS_PENDING:
                    continue
                count = int(entry.get("count", 0))
                if count < self.MIN_COUNT_FOR_REVIEW:
                    continue
                # "unsure" remains pending, but should not be resent forever
                # with identical evidence.  Otherwise a high-count ambiguous
                # term can sit at the top of every 50-term batch and starve the
                # rest of the learning queue.  Re-review only after OCR has
                # observed it again and raised the count beyond the last review.
                if (
                    entry.get("last_review_decision") == "unsure"
                    and int(entry.get("last_review_count", 0)) >= count
                ):
                    continue
                eligible.append((count, term, list(entry.get("titles", []))))
        eligible.sort(reverse=True)
        return [
            {"term": term, "count": count, "titles": titles}
            for count, term, titles in eligible[:max_terms]
        ]

    def get_approved_for_recheck(self, max_terms: int = 50) -> list[dict]:
        """Approved terms that should be re-checked for cleanup.

        Selection is biased toward terms that are currently approved but should
        probably not be injected (generic/common policy gate), then toward
        high-frequency approvals that have never been rechecked or have not
        been rechecked within APPROVED_RECHECK_INTERVAL_HOURS.
        """
        if max_terms <= 0:
            return []

        now = _dt.datetime.now()
        recheck_cutoff = now - _dt.timedelta(hours=self.APPROVED_RECHECK_INTERVAL_HOURS)
        eligible: list[tuple[int, int, str, dict]] = []
        with self._lock:
            for term, entry in self._terms.items():
                if entry.get("status") != _STATUS_APPROVED:
                    continue

                # Keep normal TTL policy aligned with get_active_hotwords().
                last_seen = _parse_iso(entry.get("last", ""))
                approved_cutoff = now - _dt.timedelta(days=self.APPROVED_TTL_DAYS)
                if last_seen and last_seen < approved_cutoff:
                    continue

                last_recheck = _parse_iso(entry.get("last_approved_recheck_at", ""))
                if (
                    last_recheck is not None
                    and last_recheck > recheck_cutoff
                    and _is_safe_auto_hotword_injection(term)
                ):
                    continue

                # Priority 1: hard policy-gated generic approvals should be
                # cleaned up first. Priority 0: normal daily recheck.
                priority = 1 if not _is_safe_auto_hotword_injection(term) else 0
                count = int(entry.get("count", 0))
                eligible.append((priority, count, term, dict(entry)))

        eligible.sort(reverse=True)
        result = []
        for _priority, count, term, entry in eligible[:max_terms]:
            result.append(
                {
                    "term": term,
                    "count": count,
                    "titles": list(entry.get("titles", [])),
                    "reason": entry.get("review_reason", ""),
                }
            )
        return result

    def apply_review_results(
        self, results: dict[str, str], reasons: dict[str, str] | None = None
    ) -> dict:
        """Apply LLM review verdicts. Values: "keep" | "drop" | "unsure".

        Unknown/missing terms are silently skipped. Returns counts summary.
        """
        # Take a defensive copy: callers pass `outcome.reasons` directly and
        # this method synthesizes "generic/common term" entries for the
        # safety-gated keep→reject downgrade. Mutating the caller's dict
        # would leak fabricated reasons back into the ReviewOutcome object,
        # confusing any downstream code that re-reads outcome.reasons.
        reasons = dict(reasons or {})
        now = _dt.datetime.now().isoformat(timespec="seconds")
        approved = rejected = unsure = 0
        with self._lock:
            for term, decision in results.items():
                entry = self._terms.get(term)
                if entry is None:
                    continue
                d = (decision or "").strip().lower()
                if d == "keep":
                    if _is_safe_auto_hotword_injection(term):
                        entry["status"] = _STATUS_APPROVED
                        approved += 1
                    else:
                        entry["status"] = _STATUS_REJECTED
                        rejected += 1
                        reasons[term] = reasons.get(
                            term,
                            "generic/common term; not useful enough for auto-injection",
                        )
                elif d == "drop":
                    entry["status"] = _STATUS_REJECTED
                    rejected += 1
                else:
                    d = "unsure"
                    unsure += 1
                entry["reviewed_at"] = now
                entry["last_review_decision"] = d
                entry["last_review_count"] = int(entry.get("count", 0))
                if reasons.get(term):
                    entry["review_reason"] = reasons[term]
            if approved or rejected or unsure:
                self._dirty = True
        return {"approved": approved, "rejected": rejected, "unsure": unsure}

    def apply_approved_recheck_results(
        self, results: dict[str, str], reasons: dict[str, str] | None = None
    ) -> dict:
        """Apply verdicts from an approved-term cleanup pass.

        Values: keep | drop | unsure.
        - keep: remains approved
        - unsure: remains approved, but recorded for audit
        - drop: demoted to rejected so it stops being injected/re-reviewed

        This is intentionally conservative: only an explicit drop (or the
        deterministic generic-term safety gate) removes an approved term.
        """
        # Defensive copy — see apply_review_results docstring for rationale.
        reasons = dict(reasons or {})
        now = _dt.datetime.now().isoformat(timespec="seconds")
        kept = demoted = unsure = 0
        with self._lock:
            for term, decision in results.items():
                entry = self._terms.get(term)
                if entry is None or entry.get("status") != _STATUS_APPROVED:
                    continue
                d = (decision or "").strip().lower()
                if not _is_safe_auto_hotword_injection(term) or d == "drop":
                    entry["status"] = _STATUS_REJECTED
                    demoted += 1
                    if not _is_safe_auto_hotword_injection(term):
                        reasons[term] = reasons.get(
                            term,
                            "generic/common term; not useful enough for auto-injection",
                        )
                    d = "drop"
                elif d == "keep":
                    kept += 1
                else:
                    d = "unsure"
                    unsure += 1

                entry["reviewed_at"] = now
                entry["last_review_decision"] = d
                entry["last_review_count"] = int(entry.get("count", 0))
                entry["last_approved_recheck_at"] = now
                entry["last_approved_recheck_decision"] = d
                if reasons.get(term):
                    entry["review_reason"] = reasons[term]

            if kept or demoted or unsure:
                self._dirty = True
        return {"kept": kept, "demoted": demoted, "unsure": unsure}

    def housekeeping(self) -> dict:
        """T7: drop stale tracker entries to bound disk + memory.

        Sweep policy:
          - approved: drop if `last` older than APPROVED_TTL_DAYS
          - pending with count<MIN_COUNT_FOR_REVIEW: drop if `last` older
            than PENDING_NOISE_TTL_DAYS (these are 2-char OCR ghosts that will
            never accumulate enough hits to be worth reviewing)
          - rejected: NEVER drop (permanent blacklist is the whole point)
          - pending with count>=MIN_COUNT_FOR_REVIEW: keep (eligible to be
            sent on the next review)
        """
        now = _dt.datetime.now()
        approved_cutoff = now - _dt.timedelta(days=self.APPROVED_TTL_DAYS)
        pending_cutoff = now - _dt.timedelta(days=self.PENDING_NOISE_TTL_DAYS)
        dropped_approved = 0
        dropped_pending = 0
        with self._lock:
            for term in list(self._terms):
                entry = self._terms[term]
                status = entry.get("status", _STATUS_PENDING)
                last = _parse_iso(entry.get("last", ""))
                if last is None:
                    continue
                if status == _STATUS_APPROVED and last < approved_cutoff:
                    self._terms.pop(term, None)
                    dropped_approved += 1
                elif (
                    status == _STATUS_PENDING
                    and int(entry.get("count", 0)) < self.MIN_COUNT_FOR_REVIEW
                    and last < pending_cutoff
                ):
                    self._terms.pop(term, None)
                    dropped_pending += 1
            if dropped_approved or dropped_pending:
                self._dirty = True
        return {
            "dropped_approved": dropped_approved,
            "dropped_pending": dropped_pending,
        }

    def stats(self) -> dict:
        """Quick counts for UI/debug."""
        with self._lock:
            counts = {
                _STATUS_PENDING: 0,
                _STATUS_APPROVED: 0,
                _STATUS_REJECTED: 0,
            }
            for entry in self._terms.values():
                counts[entry.get("status", _STATUS_PENDING)] = (
                    counts.get(entry.get("status", _STATUS_PENDING), 0) + 1
                )
            return {
                "total": len(self._terms),
                "pending": counts[_STATUS_PENDING],
                "approved": counts[_STATUS_APPROVED],
                "rejected": counts[_STATUS_REJECTED],
            }

    def manual_override(self, term: str, decision: str, reason: str = "") -> bool:
        """User-driven keep/drop. Bypasses the LLM judgement (future-UI hook)."""
        with self._lock:
            entry = self._terms.get(term)
            if entry is None:
                return False
            d = (decision or "").strip().lower()
            if d == "keep":
                entry["status"] = _STATUS_APPROVED
            elif d == "drop":
                entry["status"] = _STATUS_REJECTED
            elif d == "reset":
                entry["status"] = _STATUS_PENDING
            else:
                return False
            entry["reviewed_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            if reason:
                entry["review_reason"] = reason
            self._dirty = True
            return True

    # ----------------------------------------------------------- internal

    def _evict_one_pending_locked(self) -> None:
        """Drop the oldest pending term when the dict is full.

        Approved/rejected terms are protected from eviction — they cost so
        little to keep and represent real signal.
        """
        oldest_term: str | None = None
        oldest_first: str = "9999"
        for term, entry in self._terms.items():
            if entry.get("status") != _STATUS_PENDING:
                continue
            first = entry.get("first", "9999")
            if first < oldest_first:
                oldest_first = first
                oldest_term = term
        if oldest_term:
            self._terms.pop(oldest_term, None)
            self._dirty = True


# --- POS-tag based extraction ----------------------------------------------
#
# These tags come from jieba.posseg's standard POS scheme (an extension of the
# ICTCLAS tagset). The split below is what drives candidate accumulation:
#
#   _BOUNDARY_TAGS    function words / particles / time / quantifier — flush
#                     the buffer and emit whatever was being built; never
#                     accumulate a function word INTO a candidate.
#   _CONTENT_TAGS     proper nouns, plain nouns, English chunks, verb-noun.
#                     A 2+ char content token is the "anchor" that triggers
#                     emission of (1-char ambiguous prefix) + (this token).
#   _LONG_AMBIG_TAGS  verbs / adjectives / adverbs / numerals — only count as
#                     boundary when 2+ chars long (because "叫做" is a common
#                     verb but a single CJK character at the start of a
#                     proper-noun run is just a name fragment).
#
# Rationale: an n-gram extractor cannot tell a 3-char proper noun apart from
# its overlapping 2-char fragments. jieba+POS at least tells us where Chinese
# function words live, which lets us TRUST the segmentation boundaries instead
# of generating every 2-8 substring.
_BOUNDARY_TAGS = {
    "r",  # pronouns: 我/你/他/这/那
    "p",  # prepositions: 在/对/从/给
    "c",  # conjunctions: 和/或/但/而/若
    "u",
    "uj",
    "ud",
    "ul",
    "uv",
    "uz",  # auxiliaries: 的/了/着/过/...
    "y",  # modal particles: 啊/呢/吧/呀
    "x",  # punctuation/whitespace
    "w",  # alt punctuation in some jieba versions
    "t",  # time: 今天/明天/现在
    "q",  # quantifier: 个/只/件
    "f",  # locative: 上/下/前/后/里
    "h",  # head morpheme
    "zg",  # status morpheme: 很/不
    "d",  # adverbs: 也/都/就/还
}
_CONTENT_TAGS = {
    "n",
    "nr",
    "ns",
    "nz",
    "nt",
    "nx",
    "eng",
    "vn",
    "an",
}
# 'b' = jieba's "distinguishing word" tag (e.g. 装载/普通/特殊). When 2+ chars
# it's a content-like modifier that must NOT pull adjacent 1-char tokens into
# itself; treat as a boundary so candidates aren't glued through it.
_LONG_AMBIG_TAGS = {"v", "a", "ad", "i", "m", "b"}

# When a content-anchor (2+ char n/nr/ns/...) lands on top of accumulated
# 1-char ambiguous tokens we prepend them to recover novel proper nouns from
# (v 1ch) + (ns 2ch) splits. But unbounded prepending causes mis-merges
# across verb boundaries — without a cap, "我跟你说<3-char-noun>" would emit
# "说<3-char-noun>". Cap the prefix at 2 chars (the realistic surname-length
# window for unknown names); anything older falls off and is implicitly
# discarded by the buffer reset.
_MAX_AMBIG_PREFIX = 2


def _all_cjk(s: str) -> bool:
    if not s:
        return False
    return all(0x4E00 <= ord(c) <= 0x9FFF or 0x3400 <= ord(c) <= 0x4DBF for c in s)


def _filter_emit(s: str, seen: set[str]) -> str | None:
    """Final gate before yielding a candidate.

    Returns the term if it should be emitted, None otherwise.
    All emission paths in `_extract_candidate_terms` go through here so the
    length / stop-term / noisy-body / dedup checks live in one place.
    """
    if not s or not (2 <= len(s) <= 6):
        return None
    if not _all_cjk(s):
        return None
    if s in _AUTO_HOTWORD_STOP_TERMS:
        return None
    if _looks_like_noisy_body_cjk_term(s):
        return None
    if s in seen:
        return None
    seen.add(s)
    return s


def _extract_candidate_terms(text: str) -> Iterable[str]:
    """Two-pass proper-noun candidate extractor.

    Pass 1: emit every short (2-6 char) all-CJK run AS-IS. Catches proper
            nouns jieba mis-tags (e.g. a time-tagged 2-char run, a doubled
            4-char site name split into halves, or a 3-char proper noun
            split into connector + 2-char fragment). Whitespace and
            punctuation are the only signal we need for these.

    Pass 2: jieba.posseg-driven merge across the WHOLE text. Recovers
            proper nouns embedded in long runs where Pass 1 doesn't fire —
            for a 10-char run, posseg merging pulls a 1-char verb-tagged
            head + 2-char ns-tagged tail into the full 3-char proper noun.
            A 2+ char content token (n/nr/ns/nz/nt/eng/vn) anchors emission
            and may absorb up to `_MAX_AMBIG_PREFIX=2` adjacent 1-char
            ambiguous tokens; older ambiguous tokens fall off cleanly so
            "我跟你说<3-char-noun>" emits <3-char-noun>, not 说<3-char-noun>.

    Both passes share the `seen` dedup set so identical candidates aren't
    duplicated. Length / stop-term / noise filtering live in `_filter_emit`.
    """
    if not text:
        return

    import jieba.posseg as _pseg  # lazy: avoid 400ms dict-build at import time

    seen: set[str] = set()
    pending: list[str] = []

    # Pass 1: short-CJK-run direct emission. ONLY 2-3 char runs — that's the
    # length window where Pass 2's posseg merge tends to fail (jieba tags
    # certain 2-char runs as 't' time, splits unfamiliar 3-char names into
    # connector + suffix, splits novel 3-char product names into noun + 'd
    # iposition' tail). For 4+ char runs we trust Pass 2 alone — those are
    # dominated by sentence fragments / glued UI labels whose direct emission
    # floods the cap. Loss case: 4-char proper nouns jieba doesn't know AND
    # can't merge via prefix-2 (e.g. doubled 2-char site names) — user can
    # add them manually.
    buf: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            buf.append(ch)
        else:
            if buf:
                run = "".join(buf)
                if 2 <= len(run) <= 3:
                    out = _filter_emit(run, seen)
                    if out is not None:
                        pending.append(out)
                buf = []
    if buf:
        run = "".join(buf)
        if 2 <= len(run) <= 3:
            out = _filter_emit(run, seen)
            if out is not None:
                pending.append(out)

    # Pass 2: posseg merge across the WHOLE text. Pass-1's emissions are
    # already in `seen`, so merged candidates that turn out identical are
    # silently deduped.
    buffer: list[tuple[str, str]] = []  # [(token, pos_flag), ...]

    def flush() -> None:
        """Emit whatever the buffer joins to, if it passes the filter."""
        if not buffer:
            return
        joined = "".join(t for t, _ in buffer)
        out = _filter_emit(joined, seen)
        if out is not None:
            pending.append(out)
        buffer.clear()

    for tok, flag in _pseg.cut(text, HMM=True):
        if not tok:
            continue
        # Hard boundary: function words flush whatever was building, then we
        # drop the function word itself (never want it inside a candidate).
        if flag in _BOUNDARY_TAGS:
            flush()
            continue
        # 2+ char common verb/adj/adverb/numeral — boundary too. A SINGLE-char
        # one is kept (它 might be a fragment of a name we don't recognise).
        if len(tok) >= 2 and flag in _LONG_AMBIG_TAGS:
            flush()
            continue
        # 2+ char content token: this is an anchor.
        if len(tok) >= 2 and flag in _CONTENT_TAGS:
            # Trim the buffer to at most _MAX_AMBIG_PREFIX 1-char tokens.
            # This is what protects against "我跟你说<proper-noun>" merging
            # the whole "说<proper-noun>" run: only the last 2 ambiguous chars
            # survive as prefix, so a 2-char head + 2-char anchor still merge
            # into the full proper noun while the verb 说 falls off cleanly.
            if buffer and all(len(t) == 1 for t, _ in buffer):
                if len(buffer) > _MAX_AMBIG_PREFIX:
                    del buffer[: len(buffer) - _MAX_AMBIG_PREFIX]
                buffer.append((tok, flag))
                flush()
            elif not buffer:
                buffer.append((tok, flag))
                flush()
            else:
                # Buffer already has multi-char content — flush first, then
                # emit this content token as its own candidate so we don't
                # glue two unrelated nouns together.
                flush()
                out = _filter_emit(tok, seen)
                if out is not None:
                    pending.append(out)
            continue
        # Otherwise: 1-char tokens (any tag) accumulate as ambiguous prefix.
        buffer.append((tok, flag))

    flush()
    yield from pending


def _parse_iso(s: str) -> _dt.datetime | None:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except Exception:
        return None
