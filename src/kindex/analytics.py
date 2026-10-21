"""Archive analytics — read from Claude Code archive database."""

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import platform_io

if TYPE_CHECKING:
    from .config import Config


def find_archive_db(config: "Config") -> Path | None:
    """Find the Claude Code archive database."""
    candidates = [
        config.claude_path / "archive" / "index.db",
        config.claude_path / "archive.db",
        platform_io.home() / ".claude" / "archive" / "index.db",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def session_stats(config: "Config") -> dict:
    """Get session statistics from the archive.

    Returns:
        total_sessions, sessions_by_month, avg_duration,
        total_messages, messages_by_role, top_projects
    """
    db_path = find_archive_db(config)
    if not db_path:
        return {"error": "Archive database not found"}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    result = {}

    try:
        # Total sessions
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        result["total_sessions"] = count[0] if count else 0
    except Exception:
        # Table might not exist or have different schema
        result["total_sessions"] = 0

    # Try to get session data
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1000"
        ).fetchall()

        by_month = Counter()
        projects = Counter()

        for row in rows:
            d = dict(row)
            created = d.get("created_at", "")[:7]  # YYYY-MM
            if created:
                by_month[created] += 1
            project = d.get("project", d.get("cwd", ""))
            if project:
                projects[Path(project).name] += 1

        result["sessions_by_month"] = dict(by_month.most_common(12))
        result["top_projects"] = dict(projects.most_common(10))
    except Exception:
        result["sessions_by_month"] = {}
        result["top_projects"] = {}

    conn.close()
    return result


def activity_heatmap(config: "Config", days: int = 90) -> dict:
    """Generate activity heatmap data (sessions per day of week per hour)."""
    db_path = find_archive_db(config)
    if not db_path:
        return {"error": "Archive database not found"}

    # Initialize 7x24 grid
    heatmap = {dow: {h: 0 for h in range(24)} for dow in range(7)}

    conn = sqlite3.connect(str(db_path))
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT created_at FROM sessions WHERE created_at > ?",
            (cutoff,)
        ).fetchall()

        for row in rows:
            try:
                dt = datetime.fromisoformat(row[0])
                heatmap[dt.weekday()][dt.hour] += 1
            except (ValueError, TypeError):
                pass
    except Exception:
        pass

    conn.close()

    # Convert to serializable format
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {
        "grid": {day_names[k]: v for k, v in heatmap.items()},
        "days_analyzed": days,
    }
