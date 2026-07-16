"""manifest.py — emit manifest.json for a completed OKF run_ directory.

Called at the end of headless mode in src/engine/tui.py.
No new dependencies: stdlib only + yaml (already in requirements).
"""
import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


def _split_frontmatter(content: str):
    """Mirror of fs._split_frontmatter — parse YAML frontmatter without importing fs."""
    if not content.startswith("---"):
        return None, content
    m = re.match(r"^---\n(.*?)\n---\n?(.*)\Z", content, re.DOTALL)
    if not m:
        return None, content
    try:
        parsed = yaml.safe_load(m.group(1)) or {}
        if not isinstance(parsed, dict):
            return None, content
    except yaml.YAMLError:
        return None, content
    return parsed, m.group(2)


def _parse_md_file(path: str) -> Dict[str, Any]:
    """Read an .md file and return its frontmatter fields as a dict."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return {}
    fm, _ = _split_frontmatter(raw)
    return fm or {}


def write_manifest(run_dir: str, topic: str = "") -> str:
    """Build and write <run_dir>/manifest.json, then append to ~/.deep-research-agent/runs.json.

    Args:
        run_dir: Absolute path to the run_ directory (e.g. /…/workspace/run_1234567890).
        topic:   The original research prompt / query.

    Returns:
        Absolute path to the written manifest file, or an error string.
    """
    run_dir = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir):
        return f"Error: run_dir does not exist: {run_dir}"

    run_id = os.path.basename(run_dir)

    EXCLUDED = {"final_report.md", "_todos.md"}

    documents: List[Dict[str, Any]] = []
    final_report: Optional[str] = None
    all_source_urls: set = set()

    # Walk the run directory for .md files
    for fname in sorted(os.listdir(run_dir)):
        if not fname.endswith(".md"):
            continue

        fpath = os.path.join(run_dir, fname)
        fm = _parse_md_file(fpath)

        # Track final_report separately
        if fname == "final_report.md":
            final_report = "final_report.md"
            continue

        # Excluded files are skipped from documents list
        if fname in EXCLUDED:
            continue

        # Collect source URLs for stats
        src_url = fm.get("source_url", "") or ""
        if src_url:
            all_source_urls.add(src_url)
        # Also collect from sources list (final_report etc.)
        for s in (fm.get("sources") or []):
            if isinstance(s, str) and s.startswith("http"):
                all_source_urls.add(s)

        tags = fm.get("tags", [])
        if not isinstance(tags, list):
            tags = [tags] if tags else []

        documents.append({
            "filename": fname,
            "title": fm.get("title", ""),
            "tags": tags,
            "entities": [],           # reserved for future NER enrichment
            "source_url": src_url,
        })

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest: Dict[str, Any] = {
        "version": "1.0",
        "run_id": run_id,
        "run_dir": run_dir,
        "topic": topic,
        "created_at": now_iso,
        "final_report": final_report,
        "documents": documents,
        "stats": {
            "document_count": len(documents),
            "source_count": len(all_source_urls),
        },
    }

    manifest_path = os.path.join(run_dir, "manifest.json")
    try:
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except OSError as exc:
        return f"Error writing manifest.json: {exc}"

    # ── Append to ~/.deep-research-agent/runs.json (file-locked) ──────────────
    _append_runs_json(run_id, run_dir, manifest_path, topic, now_iso)

    return manifest_path


def _append_runs_json(
    run_id: str,
    run_dir: str,
    manifest_path: str,
    topic: str,
    created_at: str,
) -> None:
    """Append a summary entry to ~/.deep-research-agent/runs.json, using fcntl for safety."""
    # Resolve app dir — matches the default used elsewhere in the project
    app_dir = Path.home() / ".deep-research-agent"
    app_dir.mkdir(parents=True, exist_ok=True)
    runs_path = app_dir / "runs.json"

    entry = {
        "run_id": run_id,
        "run_dir": run_dir,
        "manifest": manifest_path,
        "topic": topic,
        "created_at": created_at,
    }

    # Open with 'a+' so we can read the existing content and append atomically
    try:
        with open(runs_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()
                if raw:
                    try:
                        runs: List[Dict] = json.loads(raw)
                        if not isinstance(runs, list):
                            runs = [runs]
                    except json.JSONDecodeError:
                        runs = []
                else:
                    runs = []

                runs.append(entry)

                # Rewrite entire file
                fh.seek(0)
                fh.truncate()
                json.dump(runs, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        # Non-fatal — manifest was written; runs.json update is best-effort
        pass
