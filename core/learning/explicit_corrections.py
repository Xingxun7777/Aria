"""Local, explicit ASR correction rules.

This module deliberately keeps two ideas separate:

* ``编辑 A 为 B`` mutates one addressable text target and must never learn.
* ``纠正 A 为 B`` records an opt-in local rule used by future ASR results.

Rules are stored as a small append-only JSONL event log.  The log contains
only the two operands and timestamps -- never surrounding text, window titles,
document names, or target paths.  A rule becomes active only after its event
has been flushed to disk successfully.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


STORE_VERSION = 1
MAX_SOURCE_CHARS = 128
MAX_REPLACEMENT_CHARS = 256
MAX_COMMAND_CHARS = 800
MAX_STORE_BYTES = 4 * 1024 * 1024

_ACTION_SET = "set"
_ACTION_REVOKE = "revoke"
_ACTION_CLEAR = "clear"
_VALID_ACTIONS = frozenset({_ACTION_SET, _ACTION_REVOKE, _ACTION_CLEAR})

_LEADING_WAKEWORD = "小助手"
_TRAILING_ASR_PUNCTUATION = "。.!！?？"
_PAIRED_QUOTES = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _rule_key(source: str) -> str:
    """Conflict key matching the processor's case-insensitive semantics."""

    return unicodedata.normalize("NFC", source).casefold()


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in value)


def validate_operands(source: str, replacement: str) -> str:
    """Return an empty string when a rule is safe, otherwise a reason code."""

    if not isinstance(source, str) or not isinstance(replacement, str):
        return "invalid_type"
    if source != source.strip() or replacement != replacement.strip():
        return "outer_whitespace"
    if not source or not replacement:
        return "empty_operand"
    if len(source) > MAX_SOURCE_CHARS:
        return "source_too_long"
    if len(replacement) > MAX_REPLACEMENT_CHARS:
        return "replacement_too_long"
    if _has_control_characters(source) or _has_control_characters(replacement):
        return "invalid_character"
    if source == replacement:
        return "no_change"
    # One-character sources ("的", "C") are too broad for a global future-ASR
    # rule.  Multi-character technical tokens such as C++ remain valid.
    if len(source) < 2 or not any(ch.isalnum() for ch in source):
        return "source_too_broad"
    return ""


@dataclass(frozen=True)
class ExplicitCorrectionRule:
    rule_id: str
    source: str
    replacement: str
    created_at: str


@dataclass(frozen=True)
class CorrectionMutationResult:
    success: bool
    status: str
    rule: Optional[ExplicitCorrectionRule] = None
    restored_rule: Optional[ExplicitCorrectionRule] = None


@dataclass(frozen=True)
class VoiceCorrectionCommand:
    action: str  # add | undo | view
    source: str = ""
    replacement: str = ""


@dataclass(frozen=True)
class VoiceCorrectionParseResult:
    recognized: bool
    command: Optional[VoiceCorrectionCommand] = None
    reason_code: str = "not_voice_correction"


def _strip_wakeword(text: str) -> tuple[str, bool]:
    value = text.strip()
    if not value.startswith(_LEADING_WAKEWORD):
        return value, False
    value = value[len(_LEADING_WAKEWORD) :].lstrip()
    return value.lstrip("，,：:、 "), True


def _strip_optional_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and _PAIRED_QUOTES.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return text


def _recognized_failure(reason: str) -> VoiceCorrectionParseResult:
    return VoiceCorrectionParseResult(recognized=True, reason_code=reason)


def _split_quoted_operands(body: str) -> Optional[tuple[str, str]]:
    """Parse the unambiguous ``“source” 为 “replacement”`` form."""

    if not body or body[0] not in _PAIRED_QUOTES:
        return None
    closing = _PAIRED_QUOTES[body[0]]
    close_index = body.find(closing, 1)
    if close_index < 0:
        return ("", "")
    source = body[1:close_index].strip()
    remainder = body[close_index + 1 :].strip()
    delimiter = next(
        (candidate for candidate in ("改成", "改为", "为") if remainder.startswith(candidate)),
        "",
    )
    if not delimiter:
        return ("", "")
    replacement_raw = remainder[len(delimiter) :].strip()
    if replacement_raw[:1] in _PAIRED_QUOTES:
        expected_close = _PAIRED_QUOTES[replacement_raw[0]]
        if len(replacement_raw) < 2 or replacement_raw[-1] != expected_close:
            return ("", "")
    replacement = _strip_optional_quotes(replacement_raw)
    return source, replacement


def parse_voice_correction(text: str) -> VoiceCorrectionParseResult:
    """Parse strict, wakeword-gated correction-management commands.

    Supported forms are ``小助手纠正 A 为 B``,
    ``小助手撤销上一次纠正`` and ``小助手查看纠正规则``.  Any utterance
    beginning with ``小助手纠正`` is consumed even when malformed, so the
    command itself can never fall through into normal dictation.
    """

    value, has_wakeword = _strip_wakeword(str(text or ""))
    if not has_wakeword or not value:
        return VoiceCorrectionParseResult(recognized=False)
    if value[-1:] in _TRAILING_ASR_PUNCTUATION:
        value = value[:-1].rstrip()
    if value in ("撤销上一次纠正", "撤销上条纠正"):
        return VoiceCorrectionParseResult(
            recognized=True,
            command=VoiceCorrectionCommand(action="undo"),
            reason_code="ready",
        )
    if value in ("查看纠正规则", "管理纠正规则"):
        return VoiceCorrectionParseResult(
            recognized=True,
            command=VoiceCorrectionCommand(action="view"),
            reason_code="ready",
        )
    if not value.startswith("纠正"):
        return VoiceCorrectionParseResult(recognized=False)
    if len(value) > MAX_COMMAND_CHARS:
        return _recognized_failure("command_too_long")
    if _has_control_characters(value):
        return _recognized_failure("invalid_character")

    body = value[2:].strip()
    quoted_operands = _split_quoted_operands(body)
    if quoted_operands is not None:
        source, replacement = quoted_operands
        reason = validate_operands(source, replacement)
        if reason:
            return _recognized_failure(reason)
        return VoiceCorrectionParseResult(
            recognized=True,
            command=VoiceCorrectionCommand(
                action="add", source=source, replacement=replacement
            ),
            reason_code="ready",
        )

    candidates: list[tuple[int, str]] = []
    for delimiter in ("改成", "改为", "为"):
        start = 0
        while True:
            index = body.find(delimiter, start)
            if index < 0:
                break
            candidates.append((index, delimiter))
            start = index + 1
    if not candidates:
        return _recognized_failure("syntax_invalid")

    # "改为" contains the shorter delimiter "为".  Keep only the longest
    # candidate covering a given position, but reject truly distinct split
    # points instead of guessing user intent.
    filtered: list[tuple[int, str]] = []
    for index, delimiter in candidates:
        contained = any(
            len(other) > len(delimiter)
            and other_index <= index
            and index + len(delimiter) <= other_index + len(other)
            for other_index, other in candidates
        )
        if not contained:
            filtered.append((index, delimiter))
    by_index: dict[int, str] = {}
    for index, delimiter in filtered:
        if len(delimiter) > len(by_index.get(index, "")):
            by_index[index] = delimiter
    if len(by_index) != 1:
        return _recognized_failure("syntax_ambiguous")

    index, delimiter = next(iter(by_index.items()))
    source = _strip_optional_quotes(body[:index])
    replacement = _strip_optional_quotes(body[index + len(delimiter) :])
    reason = validate_operands(source, replacement)
    if reason:
        return _recognized_failure(reason)
    return VoiceCorrectionParseResult(
        recognized=True,
        command=VoiceCorrectionCommand(
            action="add", source=source, replacement=replacement
        ),
        reason_code="ready",
    )


class ExplicitCorrectionStore:
    """Thread-safe append-only store with reversible active-rule replay."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._active_rules: list[ExplicitCorrectionRule] = []
        self._load_error = ""
        self._corrupt_lines = 0
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def corrupt_lines(self) -> int:
        return self._corrupt_lines

    def _load(self) -> None:
        records: list[dict] = []
        corrupt = 0
        error = ""
        if self._path.exists():
            try:
                raw = self._path.read_bytes()
            except OSError:
                raw = b""
                error = "load_failed"
            if not error:
                for raw_line in raw.splitlines():
                    if not raw_line.strip():
                        continue
                    try:
                        data = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        corrupt += 1
                        continue
                    record = self._validate_record(data)
                    if record is None:
                        corrupt += 1
                        continue
                    records.append(record)
        with self._lock:
            self._records = records
            self._corrupt_lines = corrupt
            self._load_error = error
            self._rebuild_active_locked()

    @staticmethod
    def _validate_record(data: object) -> Optional[dict]:
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return None
        action = data.get("action")
        record_id = data.get("id")
        if action not in _VALID_ACTIONS or not isinstance(record_id, str) or not record_id:
            return None
        created_at = data.get("created_at")
        if not isinstance(created_at, str):
            return None
        if action == _ACTION_SET:
            source = data.get("source")
            replacement = data.get("replacement")
            if validate_operands(source, replacement):
                return None
            return {
                "version": STORE_VERSION,
                "id": record_id,
                "action": action,
                "source": source,
                "replacement": replacement,
                "created_at": created_at,
            }
        if action == _ACTION_REVOKE:
            target_id = data.get("target_id")
            if not isinstance(target_id, str) or not target_id:
                return None
            return {
                "version": STORE_VERSION,
                "id": record_id,
                "action": action,
                "target_id": target_id,
                "created_at": created_at,
            }
        source = data.get("source")
        if not isinstance(source, str) or not source or _has_control_characters(source):
            return None
        return {
            "version": STORE_VERSION,
            "id": record_id,
            "action": action,
            "source": source,
            "created_at": created_at,
        }

    def _rebuild_active_locked(self) -> None:
        revoked: set[str] = set()
        latest_clear: dict[str, int] = {}
        set_records: list[tuple[int, dict]] = []
        for index, record in enumerate(self._records):
            action = record["action"]
            if action == _ACTION_REVOKE:
                revoked.add(record["target_id"])
            elif action == _ACTION_CLEAR:
                latest_clear[_rule_key(record["source"])] = index
            elif action == _ACTION_SET:
                set_records.append((index, record))

        latest_by_source: dict[str, tuple[int, dict]] = {}
        for index, record in set_records:
            key = _rule_key(record["source"])
            if index <= latest_clear.get(key, -1) or record["id"] in revoked:
                continue
            latest_by_source[key] = (index, record)

        ordered = sorted(latest_by_source.values(), key=lambda item: item[0])
        self._active_rules = [
            ExplicitCorrectionRule(
                rule_id=record["id"],
                source=record["source"],
                replacement=record["replacement"],
                created_at=record["created_at"],
            )
            for _index, record in ordered
        ]

    def _append_record_locked(self, record: dict) -> str:
        if self._load_error:
            return "load_failed"
        try:
            current = self._path.read_bytes() if self._path.exists() else b""
        except OSError:
            return "persistence_failed"
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        separator = b"\n" if current and not current.endswith(b"\n") else b""
        payload = current + separator + encoded
        if len(payload) > MAX_STORE_BYTES:
            return "store_full"
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # A previous crash may leave a never-committed temp snapshot.  It
            # is not active state and is safe to discard before the next
            # single-process transaction.
            tmp.unlink(missing_ok=True)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            fd = os.open(str(tmp), flags, 0o600)
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    count = os.write(fd, view[written:])
                    if count <= 0:
                        raise OSError("short record write")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
            # Logical history stays append-only, while the physical file uses
            # temp+replace so an AV/cloud-sync interruption leaves either the
            # complete old log or the complete new log -- never a memory-only
            # active rule or a partially appended active event.
            os.replace(tmp, self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return "persistence_failed"
        self._records.append(record)
        self._rebuild_active_locked()
        return ""

    def active_rules(self) -> list[ExplicitCorrectionRule]:
        with self._lock:
            return list(self._active_rules)

    def active_replacements(self) -> dict[str, str]:
        with self._lock:
            return {rule.source: rule.replacement for rule in self._active_rules}

    def add_rule(self, source: str, replacement: str) -> CorrectionMutationResult:
        reason = validate_operands(source, replacement)
        if reason:
            return CorrectionMutationResult(False, reason)
        with self._lock:
            existing = next(
                (
                    rule
                    for rule in self._active_rules
                    if _rule_key(rule.source) == _rule_key(source)
                ),
                None,
            )
            if existing and existing.replacement == replacement and existing.source == source:
                return CorrectionMutationResult(True, "unchanged", existing)
            rule_id = uuid.uuid4().hex
            record = {
                "version": STORE_VERSION,
                "id": rule_id,
                "action": _ACTION_SET,
                "source": source,
                "replacement": replacement,
                "created_at": _now_iso(),
            }
            error = self._append_record_locked(record)
            if error:
                return CorrectionMutationResult(False, error)
            rule = next(rule for rule in self._active_rules if rule.rule_id == rule_id)
            return CorrectionMutationResult(
                True, "updated" if existing else "added", rule
            )

    def undo_last(self) -> CorrectionMutationResult:
        with self._lock:
            if not self._active_rules:
                return CorrectionMutationResult(False, "empty")
            target = self._active_rules[-1]
            record = {
                "version": STORE_VERSION,
                "id": uuid.uuid4().hex,
                "action": _ACTION_REVOKE,
                "target_id": target.rule_id,
                "created_at": _now_iso(),
            }
            error = self._append_record_locked(record)
            if error:
                return CorrectionMutationResult(False, error)
            restored = next(
                (
                    rule
                    for rule in self._active_rules
                    if _rule_key(rule.source) == _rule_key(target.source)
                ),
                None,
            )
            return CorrectionMutationResult(
                True, "revoked", target, restored_rule=restored
            )

    def clear_rule(self, rule_id: str) -> CorrectionMutationResult:
        """Disable a source completely, including superseded older mappings."""

        with self._lock:
            target = next(
                (rule for rule in self._active_rules if rule.rule_id == rule_id), None
            )
            if target is None:
                return CorrectionMutationResult(False, "not_found")
            record = {
                "version": STORE_VERSION,
                "id": uuid.uuid4().hex,
                "action": _ACTION_CLEAR,
                "source": target.source,
                "created_at": _now_iso(),
            }
            error = self._append_record_locked(record)
            if error:
                return CorrectionMutationResult(False, error)
            return CorrectionMutationResult(True, "cleared", target)


class ExplicitCorrectionProcessor:
    """One-pass, non-chaining application of active correction rules."""

    def __init__(self, rules: Optional[Iterable[ExplicitCorrectionRule]] = None):
        self._snapshot: tuple[
            tuple[ExplicitCorrectionRule, ...],
            Optional[re.Pattern],
            dict[str, ExplicitCorrectionRule],
        ] = ((), None, {})
        self.update_rules(rules or ())

    @staticmethod
    def _rule_fragment(source: str) -> str:
        escaped = re.escape(source)
        if source.isascii():
            return rf"(?<![A-Za-z0-9_])(?i:{escaped})(?![A-Za-z0-9_])"
        # Unicode case-insensitivity is useful for mixed CJK/ASCII terms while
        # leaving Han characters themselves unchanged.
        return rf"(?i:{escaped})"

    def update_rules(self, rules: Iterable[ExplicitCorrectionRule]) -> None:
        ordered = sorted(
            list(rules), key=lambda rule: (-len(rule.source), rule.created_at, rule.rule_id)
        )
        by_group: dict[str, ExplicitCorrectionRule] = {}
        fragments: list[str] = []
        for index, rule in enumerate(ordered):
            group = f"r{index}"
            by_group[group] = rule
            fragments.append(f"(?P<{group}>{self._rule_fragment(rule.source)})")
        pattern = re.compile("|".join(fragments)) if fragments else None
        # One reference swap keeps concurrent readers on either the complete
        # old snapshot or the complete new snapshot.
        self._snapshot = (tuple(ordered), pattern, by_group)

    @property
    def rules(self) -> tuple[ExplicitCorrectionRule, ...]:
        return self._snapshot[0]

    def source_keys(self) -> set[str]:
        return {_rule_key(rule.source) for rule in self._snapshot[0]}

    def process(self, text: str) -> tuple[str, int]:
        _rules, pattern, by_group = self._snapshot
        if not text or pattern is None:
            return text, 0
        applied = 0

        def replace(match: re.Match) -> str:
            nonlocal applied
            group = match.lastgroup
            rule = by_group.get(group or "")
            if rule is None:
                return match.group(0)
            applied += 1
            return rule.replacement

        return pattern.sub(replace, text), applied


__all__ = [
    "CorrectionMutationResult",
    "ExplicitCorrectionProcessor",
    "ExplicitCorrectionRule",
    "ExplicitCorrectionStore",
    "VoiceCorrectionCommand",
    "VoiceCorrectionParseResult",
    "parse_voice_correction",
    "validate_operands",
]
