"""
History Store
=============
Thread-safe JSONL-based history storage with daily file sharding.
"""

import json
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from .models import HistoryRecord, RecordType


def _generate_id() -> str:
    """Generate a short unique ID."""
    return str(uuid.uuid4())[:8]


class HistoryStore:
    """
    Unified history storage for all Aria interactions.

    Storage format: JSON Lines (.jsonl), one file per day.
    Thread-safe via threading.Lock.

    Usage:
        store = HistoryStore(data_dir=Path("data/history"))
        store.add(
            record_type=RecordType.ASR,
            input_text="原始语音",
            output_text="润色后文本",
        )
        records = store.query(date="2026-03-17")
    """

    def __init__(
        self,
        data_dir: Path,
        enabled: bool = True,
        retention_days: int = 90,
    ):
        """
        Initialize history store.

        Args:
            data_dir: Directory for daily .jsonl files
            enabled: Global on/off switch
            retention_days: Auto-delete records older than this (0 = never)
        """
        self.data_dir = Path(data_dir)
        self.enabled = enabled
        self.retention_days = retention_days
        self._lock = threading.Lock()

        if self.enabled:
            self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_day_file(self, date_str: str) -> Path:
        """Get file path for a specific date (YYYY-MM-DD)."""
        return self.data_dir / f"{date_str}.jsonl"

    def add(
        self,
        record_type: RecordType,
        input_text: str,
        output_text: str = "",
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Add a history record.

        Args:
            record_type: Type of record
            input_text: Original input text
            output_text: Processed output text
            timestamp: ISO format timestamp (auto-generated if None)
            metadata: Additional metadata dict

        Returns:
            Record ID if added, None if disabled or failed
        """
        if not self.enabled:
            return None

        if not input_text or not input_text.strip():
            return None

        record_id = _generate_id()
        if timestamp is None:
            timestamp = datetime.now().isoformat()

        record = HistoryRecord(
            id=record_id,
            record_type=record_type,
            timestamp=timestamp,
            input_text=input_text.strip(),
            output_text=output_text.strip() if output_text else "",
            metadata=metadata or {},
        )

        # Extract date from timestamp for file sharding
        try:
            dt = datetime.fromisoformat(timestamp)
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_str = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                file_path = self._get_day_file(date_str)
                line = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(line)
                return record_id
            except (OSError, PermissionError):
                return None

    def query(
        self,
        date: Optional[str] = None,
        record_type: Optional[RecordType] = None,
        search_text: Optional[str] = None,
        limit: int = 200,
    ) -> List[HistoryRecord]:
        """
        Query history records with optional filters.

        Args:
            date: Date string (YYYY-MM-DD). If None, uses today.
            record_type: Filter by record type
            search_text: Search in input_text and output_text
            limit: Maximum records to return

        Returns:
            List of matching HistoryRecord, newest first
        """
        if not self.enabled:
            return []

        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        file_path = self._get_day_file(date)
        if not file_path.exists():
            return []

        records: List[HistoryRecord] = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        record = HistoryRecord.from_dict(data)
                        if record is None:
                            continue

                        # Apply filters
                        if (
                            record_type is not None
                            and record.record_type != record_type
                        ):
                            continue
                        if search_text:
                            search_lower = search_text.lower()
                            if (
                                search_lower not in record.input_text.lower()
                                and search_lower not in record.output_text.lower()
                            ):
                                continue

                        records.append(record)
                    except json.JSONDecodeError:
                        continue
        except (OSError, PermissionError):
            return []

        # Sort newest first, apply limit
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def get_dates(self, max_days: int = 30) -> List[str]:
        """
        Get list of dates that have history records.

        Returns:
            List of date strings (YYYY-MM-DD), newest first
        """
        if not self.enabled or not self.data_dir.exists():
            return []

        dates = []
        for f in sorted(self.data_dir.glob("*.jsonl"), reverse=True):
            date_str = f.stem  # e.g., "2026-03-17"
            # Validate date format
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
                dates.append(date_str)
            except ValueError:
                continue
            if len(dates) >= max_days:
                break
        return dates

    def delete(self, date: str, record_id: str) -> bool:
        """
        Delete a single record by ID.

        Rewrites the file excluding the deleted record.

        Args:
            date: Date string (YYYY-MM-DD)
            record_id: Record ID to delete

        Returns:
            True if record was found and deleted
        """
        if not self.enabled:
            return False

        file_path = self._get_day_file(date)
        if not file_path.exists():
            return False

        with self._lock:
            try:
                lines = []
                found = False
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            data = json.loads(stripped)
                            if data.get("id") == record_id:
                                found = True
                                continue  # Skip this record
                        except json.JSONDecodeError:
                            pass
                        lines.append(line)

                if found:
                    if lines:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.writelines(lines)
                    else:
                        # File is empty after deletion, remove it
                        file_path.unlink()
                return found
            except (OSError, PermissionError):
                return False

    def clear_before(self, cutoff_date: str) -> int:
        """
        Delete all records before a cutoff date.

        Args:
            cutoff_date: Date string (YYYY-MM-DD)

        Returns:
            Number of files deleted
        """
        if not self.enabled or not self.data_dir.exists():
            return 0

        count = 0
        with self._lock:
            for f in self.data_dir.glob("*.jsonl"):
                if f.stem < cutoff_date:
                    try:
                        f.unlink()
                        count += 1
                    except (OSError, PermissionError):
                        continue
        return count

    def auto_cleanup(self) -> int:
        """
        Delete records older than retention_days.

        Returns:
            Number of files deleted
        """
        if self.retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        return self.clear_before(cutoff.strftime("%Y-%m-%d"))

    def export_markdown(self, date: str) -> str:
        """
        Export a day's records as Markdown.

        Args:
            date: Date string (YYYY-MM-DD)

        Returns:
            Formatted Markdown string
        """
        from .models import RECORD_TYPE_LABELS

        records = self.query(date=date, limit=9999)
        if not records:
            return f"# {date}\n\n暂无记录\n"

        # Sort chronologically for export
        records.sort(key=lambda r: r.timestamp)

        lines = [f"# Aria 历史记录 — {date}", ""]
        for r in records:
            try:
                dt = datetime.fromisoformat(r.timestamp)
                ts = dt.strftime("%H:%M:%S")
            except ValueError:
                ts = r.timestamp[:8]

            label = RECORD_TYPE_LABELS.get(r.record_type, "其他")
            lines.append(f"## [{ts}] {label}")
            lines.append("")
            if r.input_text:
                lines.append(f"**输入：** {r.input_text}")
            if r.output_text:
                lines.append(f"**输出：** {r.output_text}")
            if r.metadata:
                meta_parts = []
                for k, v in r.metadata.items():
                    if v is not None and v != "":
                        meta_parts.append(f"{k}={v}")
                if meta_parts:
                    lines.append(f"*{', '.join(meta_parts)}*")
            lines.append("")

        return "\n".join(lines)

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about stored history."""
        if not self.enabled or not self.data_dir.exists():
            return {"total_records": 0, "total_days": 0, "types": {}}

        total = 0
        type_counts: Dict[str, int] = {}
        dates = self.get_dates(max_days=9999)

        for date in dates:
            records = self.query(date=date, limit=9999)
            total += len(records)
            for r in records:
                name = r.record_type.name
                type_counts[name] = type_counts.get(name, 0) + 1

        return {
            "total_records": total,
            "total_days": len(dates),
            "types": type_counts,
        }
