import hashlib
import os
from datetime import datetime, timezone
from typing import Dict, List
from urllib.parse import urlparse
import re
import contextvars
import yaml
from agent_framework import tool
from tools.core import with_quota, _get_tool_rule

# --- WORKSPACE FILE SYSTEM ---
_IN_MEMORY_FS: Dict[str, str] = {}
session_dir_ctx = contextvars.ContextVar('session_dir', default="")

def _get_workspace_type() -> str:
    from config import cfg
    return cfg.get("settings", {}).get("workspace", {}).get("type", "memory")

def _get_workspace_dir() -> str:
    from config import cfg
    return cfg.get("settings", {}).get("workspace", {}).get("dir", ".")

def _get_safe_path(filename: str) -> str:
    # Safely allow subdirectories while blocking traversal hacks
    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
        return ""
    session_dir = session_dir_ctx.get()
    if session_dir:
        filename = os.path.join(session_dir, filename)
    if _get_workspace_type() == "disk":
        return os.path.join(_get_workspace_dir(), filename)
    return filename

# ================================================================
# OKF v0.1 frontmatter builder
# ================================================================
def _registered_domain(url: str) -> str:
    """Best-effort registered domain (e.g. 'seia.org') from a URL, for tagging."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    host = host.lower().lstrip(".")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _slug_to_title(filename: str) -> str:
    """Turn 'anne_arundel-solar.md' into 'Anne Arundel Solar'."""
    base = os.path.basename(filename)
    for ext in (".markdown", ".md"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    words = re.split(r"[_\-\s]+", base)
    return " ".join(w[:1].upper() + w[1:] for w in words if w)


def _first_h1(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def _split_frontmatter(content: str):
    """If content leads with a YAML frontmatter block, return (parsed_dict, body).
    Otherwise return (None, content). Body is everything after the closing '---'."""
    if not content.startswith("---"):
        return None, content
    # Match: '---\n' ... '\n---\n'  (closing fence on its own line)
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


def _build_okf_frontmatter(
    body: str,
    *,
    title: str = "",
    doc_type: str = "summary",
    tags: List[str] | None = None,
    source_url: str = "",
    sources: List[str] | None = None,
    content_type: str = "",
) -> str:
    """Return `body` with a complete OKF v0.1 YAML frontmatter block prepended.

    Output is exactly '---\\n' + yaml + '---\\n' + body (no inserted blank line).
    sha256 is computed over exactly the body bytes that follow the closing
    '---\\n', so `content.split('---', 2)[2]` re-hashes stable (llm-wiki
    drift-check convention).

    If `body` already carries a frontmatter block, its values are preserved and
    only missing required keys are filled — enrichment is never skipped.
    """
    existing, real_body = _split_frontmatter(body)
    existing = existing or {}

    now = datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now.strftime("%Y-%m-%d")

    # Resolve title: explicit arg -> existing -> first H1 -> slug from... nothing here,
    # so caller-provided title/slug fallback handled before calling when a filename is known.
    resolved_title = (
        title
        or existing.get("title")
        or _first_h1(real_body)
        or "Untitled"
    )

    resolved_type = existing.get("type") or doc_type

    # Tags: explicit arg -> existing -> ["research"] (+ domain for source docs)
    if tags:
        resolved_tags = list(tags)
    elif existing.get("tags"):
        et = existing["tags"]
        resolved_tags = list(et) if isinstance(et, list) else [et]
    else:
        resolved_tags = ["research"]
        if resolved_type == "source":
            dom = _registered_domain(source_url or existing.get("source_url", ""))
            if dom and dom not in resolved_tags:
                resolved_tags.append(dom)

    fm: Dict = {
        "title": resolved_title,
        "created": existing.get("created") or today,
        "timestamp": existing.get("timestamp") or iso_ts,
        "type": resolved_type,
        "tags": resolved_tags,
        "okf_version": "0.1",
    }

    resolved_source_url = source_url or existing.get("source_url", "")
    if resolved_type == "source":
        if resolved_source_url:
            fm["source_url"] = resolved_source_url
        fm["ingested"] = existing.get("ingested") or today
        resolved_ct = content_type or existing.get("content_type", "")
        if resolved_ct:
            fm["content_type"] = resolved_ct
    else:
        if resolved_source_url:
            fm["source_url"] = resolved_source_url

    resolved_sources = sources if sources is not None else existing.get("sources")
    if resolved_sources:
        fm["sources"] = list(resolved_sources)

    # sha256 is computed over exactly the bytes that `content.split('---', 2)[2]`
    # yields for the emitted document — the body after the closing '---' fence,
    # including the single '\n' that immediately follows it. This is the
    # llm-wiki drift-check convention and lets a re-ingest detect changes.
    hashed_body = "\n" + real_body
    fm["sha256"] = hashlib.sha256(hashed_body.encode("utf-8")).hexdigest()

    yaml_block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return "---\n" + yaml_block + "---\n" + real_body


def _session_source_files() -> List[str]:
    """List *.md files in the current session dir, excluding final_report.md and _todos.md.
    Returned as bare filenames for the final report's `sources:` list."""
    excluded = {"final_report.md", "_todos.md"}
    files = [f for f in get_workspace_files()
             if f.endswith(".md") and os.path.basename(f) not in excluded]
    return sorted(files)

def get_workspace_files() -> List[str]:
    """Helper for TUI to list files agnostic of storage backend.
    Returns bare filenames (without session prefix) so agents can pass them
    directly to read_workspace_file/grep_workspace_file. The session prefix
    is transparently added by _get_safe_path inside those functions.
    """
    session_dir = session_dir_ctx.get()
    if _get_workspace_type() == "disk":
        d = _get_workspace_dir()
        if session_dir:
            d = os.path.join(d, session_dir)
        if not os.path.isdir(d): return []
        res = []
        for root, _, files in os.walk(d):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), d)
                res.append(rel.replace("\\", "/"))
        return res
    if session_dir:
        prefix = session_dir + "/"
        return [k[len(prefix):] for k in _IN_MEMORY_FS.keys() if k.startswith(prefix)]
    return list(_IN_MEMORY_FS.keys())

def get_workspace_file_content(filename: str) -> str | None:
    """Helper for TUI to read a file agnostic of storage backend."""
    path = _get_safe_path(filename)
    if not path: return None
    if _get_workspace_type() == "disk":
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return None
        return None
    return _IN_MEMORY_FS.get(path)

@tool
@with_quota
def read_workspace_file(filename: str, start_line: int = 1, end_line: int = -1) -> str:
    """Read a stored text file. Use start_line and end_line bounds to read large files safely. Both bounds are 1-indexed."""
    try:
        content = get_workspace_file_content(filename)
        if content is None: return f"Error: '{filename}' not found."
        lines = content.splitlines()
        total = len(lines)
        max_lines = _get_tool_rule("read_workspace_file", "max_lines", 300)
        if end_line == -1: end_line = total
        start = max(1, start_line)
        end = min(total, end_line)
        if (end - start + 1) > max_lines:
            return f"Error: Requested {end - start + 1} lines, but your quota restricts you to {max_lines} lines per read. Use grep_workspace_file or chunked bounds."
        chunk = "\n".join(lines[start - 1:end])
        return f"--- {filename} [Lines {start}-{end} of {total}] ---\n{chunk}"
    except Exception as e:
        import traceback
        return f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}"

@tool
@with_quota
def write_workspace_file(filename: str, content: str, source_url: str = "",
                         title: str = "", tags: str = "") -> str:
    """Save content to your workspace. Markdown files are auto-injected with OKF v0.1 YAML frontmatter.

    You MAY pass `title` and `tags` (comma-separated) to enrich the frontmatter.
    Never write YAML frontmatter yourself — it is handled automatically.
    """
    try:
        path = _get_safe_path(filename)
        if not path: return f"Error: Invalid filename '{filename}'."

        # OKF frontmatter for markdown files only
        if filename.endswith(".md") or filename.endswith(".markdown"):
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] or None
            resolved_title = title or _first_h1(content) or _slug_to_title(filename)
            if os.path.basename(filename) == "final_report.md":
                content = _build_okf_frontmatter(
                    content,
                    title=title or _first_h1(content) or "Research Report",
                    doc_type="summary",
                    tags=tag_list or ["research", "report"],
                    sources=_session_source_files(),
                )
            elif source_url:
                content = _build_okf_frontmatter(
                    content,
                    title=resolved_title,
                    doc_type="source",
                    tags=tag_list,
                    source_url=source_url,
                )
            else:
                content = _build_okf_frontmatter(
                    content,
                    title=resolved_title,
                    doc_type="summary",
                    tags=tag_list,
                )

        if _get_workspace_type() == "disk":
            parent_dir = os.path.dirname(path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Wrote '{filename}' to disk."
        else:
            _IN_MEMORY_FS[path] = content
            return f"Wrote '{filename}' to memory."
    except Exception as e:
        import traceback
        return f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}"

@tool
@with_quota
def list_workspace_files() -> str:
    """List all files in your workspace, showing line and character counts."""
    files = get_workspace_files()
    if not files: return "Workspace empty."
    res = []
    for k in sorted(files):
        content = get_workspace_file_content(k) or ""
        res.append(f"{k} (Lines: {len(content.splitlines())}, Chars: {len(content)})")
    return "\n".join(res)

@tool
@with_quota
def grep_workspace_file(filename: str, pattern: str, context_lines: int = 2) -> str:
    """Search for a regex pattern within a file, returning matching lines with surrounding context."""
    try:
        content = get_workspace_file_content(filename)
        if content is None: return f"Error: '{filename}' not found."
        lines = content.splitlines()
        max_matches = _get_tool_rule("grep_workspace_file", "max_matches", 10)
        compiled = re.compile(pattern, re.IGNORECASE)
        matches = []
        for i, line in enumerate(lines):
            if compiled.search(line):
                matches.append(i)
                if len(matches) >= max_matches:
                    break
        if not matches: return f"No matches found for '{pattern}'."
        out = []
        for match_idx in matches:
            start = max(0, match_idx - context_lines)
            end = min(len(lines), match_idx + context_lines + 1)
            out.append(f"--- Match near line {match_idx + 1} ---")
            for j in range(start, end):
                prefix = "> " if j == match_idx else "  "
                out.append(f"{j + 1:04d}{prefix}{lines[j]}")
        return "\n".join(out)
    except Exception as e:
        import traceback
        return f"Grep Error: {e}\n\nTraceback:\n{traceback.format_exc()}"

@tool
@with_quota
def remove_workspace_file(filename: str) -> str:
    """A destructive action that mandates human oversight. Deletes a file."""
    try:
        path = _get_safe_path(filename)
        if not path: return f"Error: Invalid filename '{filename}'."
        if _get_workspace_type() == "disk":
            if os.path.exists(path):
                os.remove(path)
                return f"Deleted: {filename}"
        else:
            if path in _IN_MEMORY_FS:
                del _IN_MEMORY_FS[path]
                return f"Deleted: {filename}"
        return f"Error: '{filename}' not found."
    except Exception as e:
        import traceback
        return f"Error: {e}\n\nTraceback:\n{traceback.format_exc()}"