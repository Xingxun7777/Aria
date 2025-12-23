"""
VoiceType Insight Store
=======================
Simple storage for voice transcripts to enable AI-powered retrieval.

Usage:
    from voicetype.core.insight_store import InsightStore

    store = InsightStore(data_dir=Path("data/insights"))
    store.add(text="我的想法...", timestamp="2025-12-12T10:30:45", ...)

    # Later: AI reads all entries
    entries = store.get_recent(days=30)
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import threading


class InsightStore:
    """
    Simple JSON-based insight storage.

    Stores transcripts in monthly JSON files for easy browsing and AI analysis.
    """

    _lock = threading.Lock()

    def __init__(self, data_dir: Path):
        """
        Initialize the insight store.

        Args:
            data_dir: Directory to store monthly JSON files
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_month_file(self, year: int, month: int) -> Path:
        """Get the file path for a specific month."""
        return self.data_dir / f"{year}-{month:02d}.json"

    def _load_month(self, year: int, month: int) -> Dict:
        """Load data for a specific month."""
        file_path = self._get_month_file(year, month)
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {"month": f"{year}-{month:02d}", "entries": []}
        return {"month": f"{year}-{month:02d}", "entries": []}

    def _save_month(self, year: int, month: int, data: Dict) -> None:
        """Save data for a specific month."""
        file_path = self._get_month_file(year, month)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add(
        self,
        text: str,
        timestamp: str,
        duration_s: float = 0.0,
        session_id: int = 0,
        entry_type: str = "transcription",
        attributes: dict = None,
    ) -> bool:
        """
        Add a new insight entry.

        Args:
            text: The transcribed text (polish 后)
            timestamp: ISO format timestamp
            duration_s: Audio duration in seconds
            session_id: Reference to DebugLog session
            entry_type: Type of entry ("transcription", "highlight", etc.)
            attributes: Optional additional attributes (tags, importance, etc.)

        Returns:
            True if successfully added
        """
        if not text or not text.strip():
            return False

        # Parse timestamp to get year/month
        try:
            dt = datetime.fromisoformat(timestamp)
        except ValueError:
            dt = datetime.now()

        with self._lock:
            data = self._load_month(dt.year, dt.month)

            # Generate next ID
            next_id = max([e.get("id", 0) for e in data["entries"]], default=0) + 1

            entry = {
                "id": next_id,
                "timestamp": timestamp,
                "text": text.strip(),
                "duration_s": round(duration_s, 2),
                "session_id": session_id,
                "type": entry_type,
            }

            # Add optional attributes
            if attributes:
                entry["attributes"] = attributes

            data["entries"].append(entry)
            self._save_month(dt.year, dt.month, data)
            return True

    def get_month(self, year: int, month: int) -> List[Dict]:
        """
        Get all entries for a specific month.

        Args:
            year: Year (e.g., 2025)
            month: Month (1-12)

        Returns:
            List of entry dictionaries
        """
        data = self._load_month(year, month)
        return data.get("entries", [])

    def get_recent(self, days: int = 7) -> List[Dict]:
        """
        Get entries from the last N days.

        Args:
            days: Number of days to look back

        Returns:
            List of entry dictionaries, sorted by timestamp
        """
        cutoff = datetime.now() - timedelta(days=days)
        entries = []

        # Check current and previous months
        current = datetime.now()
        months_to_check = set()
        for d in range(days + 1):
            dt = current - timedelta(days=d)
            months_to_check.add((dt.year, dt.month))

        for year, month in months_to_check:
            month_entries = self.get_month(year, month)
            for entry in month_entries:
                try:
                    entry_dt = datetime.fromisoformat(entry["timestamp"])
                    if entry_dt >= cutoff:
                        entries.append(entry)
                except (ValueError, KeyError):
                    continue

        # Sort by timestamp
        entries.sort(key=lambda e: e.get("timestamp", ""))
        return entries

    def export_text(self, year: int, month: int) -> str:
        """
        Export a month's entries as plain text for AI analysis.

        Args:
            year: Year
            month: Month

        Returns:
            Formatted text with all entries
        """
        entries = self.get_month(year, month)
        if not entries:
            return f"No entries for {year}-{month:02d}"

        lines = [f"# Voice Insights - {year}-{month:02d}", ""]
        for entry in entries:
            ts = entry.get("timestamp", "")[:16].replace("T", " ")
            text = entry.get("text", "")
            lines.append(f"[{ts}] {text}")

        return "\n".join(lines)

    def get_stats(self) -> Dict:
        """Get statistics about stored insights."""
        total_entries = 0
        total_chars = 0
        months = []

        for file_path in sorted(self.data_dir.glob("*.json")):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    entries = data.get("entries", [])
                    total_entries += len(entries)
                    total_chars += sum(len(e.get("text", "")) for e in entries)
                    months.append(data.get("month", file_path.stem))
            except (json.JSONDecodeError, IOError):
                continue

        return {
            "total_entries": total_entries,
            "total_chars": total_chars,
            "months": months,
        }
