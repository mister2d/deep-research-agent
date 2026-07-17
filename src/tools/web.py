import httpx
import os
import re
import time
import asyncio
import threading
import json
import subprocess
from bs4 import BeautifulSoup
from agent_framework import tool
from tools.core import with_quota
from tools.fs import (_get_safe_path, _get_workspace_type, _get_workspace_dir,
    _IN_MEMORY_FS, _build_okf_frontmatter)

TIKA_BASE_URL = os.environ.get("TIKA_BASE_URL", "https://tika.service.internal.novuscotia.com/")

# Self-hosted endpoints (injected via nix flake env vars)
SEARXNG_URL   = os.environ.get("SEARXNG_URL", "")
CRAWL4AI_URL  = os.environ.get("CRAWL4AI_URL", "")
CRAWL4AI_TOKEN = os.environ.get("CRAWL4AI_AUTH_TOKEN", "")
_ddgs_lock = threading.Lock()
_ddgs_client = None

# Retry/backoff for transient failures (HTTP 429 / 5xx / reported rate limiting).
# TWO retries after the initial attempt, exponential backoff in seconds.
_RETRY_BACKOFFS = (2, 6)


def _is_rate_limit_text(text: str) -> bool:
    """Heuristic: does this error/output text indicate rate limiting or a transient server error?"""
    t = (text or "").lower()
    return ("rate limit" in t or "rate-limit" in t or "ratelimit" in t
            or "too many requests" in t or "429" in t
            or "503" in t or "502" in t or "500" in t
            or "temporarily unavailable" in t or "try again" in t)

def get_ddgs_client():
    global _ddgs_client
    with _ddgs_lock:
        if _ddgs_client is None:
            from ddgs import DDGS
            _ddgs_client = DDGS()
            _ddgs_client._get_engines("text", "auto")
            _ddgs_client._get_engines("news", "auto")
    return _ddgs_client

# ============================================================
# Search: SearXNG REST API (preferred) or DuckDuckGo (fallback)
# ============================================================
def _sanitize_snippet(text: str) -> str:
    """Strip CSS, SVG, and HTML artifacts from search snippets."""
    text = re.sub(r'<svg[\s\S]*?</svg>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r"(?:[\w-]+=(?:'[^']*'|\"[^\"]*\")[\s]*){3,}", '', text)
    text = re.sub(r'%3[CEce][^%\s]{10,}', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def _searxng_search(query: str, max_results: int, topic: str) -> str:
    """Query SearXNG REST API; returns formatted results or empty string on failure."""
    if not SEARXNG_URL:
        return ""
    try:
        # SEARXNG_URL is a base url; append /search unless already present.
        base = SEARXNG_URL.rstrip("/")
        search_url = base if base.endswith("/search") else base + "/search"
        params = {"q": query, "format": "json"}
        resp = httpx.get(search_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return ""
        # SearXNG returns full-page excerpts — truncate to ~30 chars and return only top 2 results.
        # Short snippets + limited results force the LLM to fetch URLs for depth.
        MAX_SNIPPET = 15
        lines = [f"🔍 Found {len(results)} result(s) for '{query}':"]
        for r in results[:2]:  # Only return top 2 to force multi-query fetching
            title = r.get("title", "No title")
            url = r.get("url", "")
            snippet = _sanitize_snippet(r.get("content", r.get("snippet", "No snippet available")))
            if len(snippet) > MAX_SNIPPET:
                snippet = snippet[:MAX_SNIPPET].rsplit(" ", 1)[0] + "..."
            lines.append(f"## {title}")
            lines.append(f"**URL:** {url}")
            lines.append(f"**Snippet:** {snippet}")
            lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""

def _ddgs_search(query: str, max_results: int, topic: str) -> str:
    """Fallback DuckDuckGo search — same format as upstream."""
    ddgs = get_ddgs_client()
    engine = "news" if topic == "news" else None
    search_results = ddgs.text(query, max_results=max_results, engine=engine)
    if not search_results:
        return f"🔍 Found 0 result(s) for '{query}':"
    lines = [f"🔍 Found {len(search_results)} result(s) for '{query}':"]
    for r in search_results:
        title = r.get("title", "No title")
        url = r.get("href", "")
        snippet = _sanitize_snippet(r.get("body", "No snippet available"))
        lines.append(f"## {title}")
        lines.append(f"**URL:** {url}")
        lines.append(f"**Snippet:** {snippet}")
        lines.append("")
    return "\n".join(lines)

# ============================================================
# Tool A: web_search — Hybrid SearXNG + Crawl4AI pipeline
# ============================================================
# Maps config.yaml settings.web_search keys -> the HYBRID_SEARCH_* env vars the
# hybrid-web-search skill reads. This var-name contract is shared with the skill.
_SEARCH_ENV_MAP = {
    "max_crawl_urls": "HYBRID_SEARCH_MAX_CRAWL_URLS",
    "max_output_chunks": "HYBRID_SEARCH_MAX_OUTPUT_CHUNKS",
    "chunk_token_target": "HYBRID_SEARCH_CHUNK_TOKEN_TARGET",
    "crawl_score_threshold": "HYBRID_SEARCH_CRAWL_SCORE_THRESHOLD",
    "timeout_ms": "HYBRID_SEARCH_TIMEOUT_MS",
}


def _search_env(base_env: dict) -> dict:
    """Return a copy of base_env with HYBRID_SEARCH_* vars applied from config.

    Precedence: a value present in config.yaml (settings.web_search) overrides
    the ambient env (explicit operator intent). When config is silent for a key,
    whatever is already in the ambient env passes through untouched, and the
    skill's own defaults rule when neither is set.
    """
    env = {**base_env}
    try:
        from config import cfg
        ws = cfg.get("settings", {}).get("web_search", {}) or {}
    except Exception:
        ws = {}
    for key, env_var in _SEARCH_ENV_MAP.items():
        if key in ws and ws[key] is not None:
            env[env_var] = str(ws[key])
    return env


@tool
@with_quota
async def web_search(
    query: str,
    max_results: int | None = None,
    topic: str = "general",
) -> str:
    """Search the web for information on a given query.

    Uses the hybrid-web-search skill: SearXNG → Orama BM25 ranking →
    Crawl4AI crawling → chunk ranking. Returns structured ranked results
    with full crawled content when available. By default all results the
    pipeline returns are shown (bounded by settings.web_search config);
    pass max_results only to cap them further.
    """
    def _do_search():
        skill_dir = os.environ.get(
            "HYBRID_SEARCH_SKILL_DIR",
            os.path.expanduser("~/.hermes/skills/hybrid-web-search"),
        )
        if not os.path.isdir(skill_dir):
            return ("hybrid-web-search skill not found at "
                    f"'{skill_dir}'. Set HYBRID_SEARCH_SKILL_DIR to the skill's "
                    "directory (the one containing run-json.mjs).")
        env = _search_env(os.environ)
        env.setdefault("CRAWL4AI_AUTH_TOKEN", "dummy")
        # This runs inside asyncio.to_thread, so a blocking time.sleep here does
        # NOT block the event loop — other coroutines keep running. Retry on
        # rate-limit / transient failures with exponential backoff.
        try:
            result = None
            last_err = ""
            for attempt in range(len(_RETRY_BACKOFFS) + 1):
                result = subprocess.run(
                    ["npx", "tsx", "run-json.mjs", query],
                    capture_output=True, text=True, timeout=120,
                    cwd=skill_dir,
                    env=env,
                )
                if result.returncode != 0:
                    last_err = result.stderr.strip() or "unknown error"
                    if (_is_rate_limit_text(last_err)
                            and attempt < len(_RETRY_BACKOFFS)):
                        time.sleep(_RETRY_BACKOFFS[attempt])
                        continue
                    return f"Search failed: {last_err}"
                data = json.loads(result.stdout.strip())
                if "error" in data:
                    err = str(data["error"])
                    if (_is_rate_limit_text(err)
                            and attempt < len(_RETRY_BACKOFFS)):
                        time.sleep(_RETRY_BACKOFFS[attempt])
                        continue
                    return f"Search error: {err}"
                break
            else:
                return f"Search failed after retries: {last_err or 'rate limited'}"

            items = data.get("items", [])
            stats = data.get("stats", {})

            lines = [f"🔍 Web search results for '{query}':"]
            lines.append(f"   SearXNG: {stats.get('searxngResultCount', 0)} results | Crawled: {stats.get('crawledUrlCount', 0)} | Chunks ranked: {stats.get('totalChunksRanked', 0)}")
            lines.append("")

            for i, item in enumerate(items[:max_results], 1):
                c = "CRAWLED" if item.get("crawled") else "SNIPPET"
                title = item.get("title", "No title")
                url = item.get("url", "")
                score = item.get("oramaScore", 0)
                text = item.get("text", "")
                lines.append(f"## [{i}] [{c}] {title}")
                lines.append(f"**URL:** {url}")
                lines.append(f"**Score:** {score:.2f}")
                if text:
                    lines.append(f"**Content:** {text[:500]}{'...' if len(text) > 500 else ''}")
                lines.append("")

            if not items:
                lines.append("No results found.")
            return "\n".join(lines)
        except subprocess.TimeoutExpired:
            return "Search timed out (120s)."
        except json.JSONDecodeError:
            return f"Search returned invalid JSON: {result.stdout[:200]}"
        except Exception as e:
            return f"Search error: {e}"

    return await asyncio.to_thread(_do_search)

# ============================================================
# Content-type routing helpers
# ============================================================
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Extensions Tika handles better than an HTML crawler / plain httpx.
_TIKA_EXTS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".epub", ".rtf", ".odt", ".odp", ".ods",
    ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp",
)


def _is_html_content(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "text/html" in ct or "application/xhtml" in ct


def _needs_tika(url: str, content_type: str) -> bool:
    """Route non-HTML documents that need parsing to Tika.

    Sniffs by Content-Type first, then falls back to the URL extension.
    HTML/plain-text stays on the Crawl4AI/httpx path; PDFs, Office docs,
    EPUB, RTF, images and other binary/document types go to Tika.
    """
    ct = (content_type or "").lower()
    if ct:
        if _is_html_content(ct) or ct.startswith("text/plain"):
            return False
        if ("application/pdf" in ct or "application/vnd" in ct
                or "application/epub" in ct or "application/rtf" in ct
                or "application/msword" in ct or ct.startswith("image/")
                or "application/vnd.openxmlformats" in ct):
            return True
    # Fall back to extension sniffing (path only, ignore query string).
    path = url.split("?", 1)[0].lower()
    return path.endswith(_TIKA_EXTS)


def _sniff_content_type(url: str) -> str:
    """HEAD the URL for its Content-Type; empty string on failure."""
    try:
        resp = httpx.head(url, headers={"User-Agent": _UA}, timeout=10,
                          follow_redirects=True)
        return resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    except Exception:
        return ""


def _save_source(filename: str, content, source_url: str, content_type: str) -> str:
    """Persist fetched content as a workspace source doc.

    Markdown/text is enriched with OKF v0.1 frontmatter (doc_type 'source')
    via the shared fs builder; binary payloads are written as-is.
    """
    path = _get_safe_path(filename)
    if not path:
        return f"Error: Invalid filename '{filename}'."
    is_text = isinstance(content, str)
    if is_text and (filename.endswith(".md") or filename.endswith(".markdown")):
        content = _build_okf_frontmatter(
            content,
            doc_type="source",
            source_url=source_url,
            content_type=content_type,
        )
    if _get_workspace_type() == "disk":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        mode = "w" if is_text else "wb"
        kwargs = {"encoding": "utf-8"} if is_text else {}
        with open(path, mode, **kwargs) as f:
            f.write(content)
        return f"Fetched URL successfully to '{filename}' on disk."
    _IN_MEMORY_FS[path] = content
    return f"Fetched URL successfully to '{filename}' in memory."


# ============================================================
# Tool B: fetch_url_to_workspace
# ============================================================
@tool
@with_quota
async def fetch_url_to_workspace(url: str, filename: str, convert_to_md: bool = True) -> str:
    """Fetch external web content and save it directly to the workspace.

    Routes by content type: HTML/text goes through self-hosted Crawl4AI
    (or the httpx+BS4 fallback), while PDFs, Office docs, EPUB, RTF, images
    and other binary/document types go through Apache Tika. Saved markdown
    carries OKF v0.1 frontmatter automatically.
    """
    if not convert_to_md:
        def _fetch_raw():
            return httpx.get(url, headers={"User-Agent": _UA}, timeout=30,
                             follow_redirects=True).content
        data = await asyncio.to_thread(_fetch_raw)
        return _save_source(filename, data, url, "")

    content_type = await asyncio.to_thread(_sniff_content_type, url)

    # Non-HTML documents needing parsing -> Tika (any content type / extension).
    if _needs_tika(url, content_type):
        return await _fetch_via_tika(url, filename, content_type)

    # --- Self-hosted Crawl4AI path ---
    if CRAWL4AI_URL:
        return await _fetch_via_crawl4ai(url, filename, content_type or "text/html")

    # --- Original httpx path ---
    return await _fetch_via_httpx(url, filename, convert_to_md, content_type or "text/html")

async def _fetch_via_crawl4ai(url: str, filename: str, content_type: str = "text/html") -> str:
    """Fetch a page via the self-hosted Crawl4AI REST service (direct httpx POST)."""
    if not filename.endswith('.md'):
        filename += '.md'
    path = _get_safe_path(filename)
    if not path:
        return f"Error: Invalid filename '{filename}'."

    endpoint = CRAWL4AI_URL.rstrip("/") + "/crawl"
    payload = {
        "urls": [url],
        "extract": "markdown",
        "crawler_params": {
            "excluded_tags": ["nav", "footer", "aside", "header", "script", "style"],
            "remove_forms": True,
            "remove_overlay_elements": True,
            "markdown_generator": {
                "type": "DefaultMarkdownGenerator",
                "params": {
                    "content_filter": {
                        "type": "PruningContentFilter",
                        "params": {"threshold": 0.2, "threshold_type": "fixed"}
                    }
                }
            },
        },
    }
    headers = {"Content-Type": "application/json"}
    if CRAWL4AI_TOKEN:
        headers["Authorization"] = f"Bearer {CRAWL4AI_TOKEN}"

    def _do_crawl():
        return httpx.post(endpoint, json=payload, headers=headers, timeout=60)

    # Retry on transient failures (HTTP 429/5xx). This function is async, so we
    # await asyncio.sleep for the backoff to avoid blocking the event loop.
    resp = None
    last_exc = None
    for attempt in range(len(_RETRY_BACKOFFS) + 1):
        try:
            resp = await asyncio.to_thread(_do_crawl)
        except Exception as e:
            last_exc = e
            if attempt < len(_RETRY_BACKOFFS):
                await asyncio.sleep(_RETRY_BACKOFFS[attempt])
                continue
            return f"Failed to reach Crawl4AI at '{endpoint}': {e}"
        if (resp.status_code == 429 or 500 <= resp.status_code < 600) \
                and attempt < len(_RETRY_BACKOFFS):
            await asyncio.sleep(_RETRY_BACKOFFS[attempt])
            continue
        break

    if resp is None:
        return f"Failed to reach Crawl4AI at '{endpoint}': {last_exc}"

    if resp.status_code < 200 or resp.status_code >= 300:
        detail = resp.text[:300].strip()
        return (f"Crawl4AI request for '{url}' failed after retries: "
                f"HTTP {resp.status_code}{' — ' + detail if detail else ''}")

    try:
        data = resp.json()
    except Exception:
        return (f"Crawl4AI returned non-JSON for '{url}' "
                f"(HTTP {resp.status_code}): {resp.text[:200]}")

    if isinstance(data, dict) and data.get("success") is False:
        err = data.get("detail") or data.get("error") or "unknown error"
        return f"Crawl4AI reported failure for '{url}': {err}"

    # Response shape: {"results": [{"markdown": {"fit_markdown", "raw_markdown"},
    #                 "cleaned_html", "metadata": {...}}]}
    results = data.get("results") if isinstance(data, dict) else None
    result = None
    if isinstance(results, list) and results:
        result = results[0]
    elif isinstance(data, dict) and isinstance(data.get("result"), dict):
        result = data["result"]
    elif isinstance(data, dict):
        result = data
    if not isinstance(result, dict):
        return f"Crawl4AI returned no usable result for '{url}'."

    md = result.get("markdown")
    content = ""
    if isinstance(md, dict):
        content = md.get("fit_markdown") or md.get("raw_markdown") or ""
    elif isinstance(md, str):
        content = md
    if not content:
        content = result.get("cleaned_html") or ""
    if not content:
        return f"Crawl4AI returned empty content for '{url}' (HTTP {resp.status_code})."

    content = content[:5000000]
    return _save_source(filename, content, url, content_type)

async def _fetch_via_httpx(url: str, filename: str, convert_to_md: bool = True,
                           content_type: str = "text/html") -> str:
    """Original httpx + BS4 fetch — unchanged from upstream."""
    if not filename.endswith('.md'):
        filename += '.md'
    path = _get_safe_path(filename)
    if not path:
        return f"Error: Invalid filename '{filename}'."

    def _fetch():
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/91.0.4472.124 Safari/537.36"}
        resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)

        if not convert_to_md:
            return resp.content

        content_type = resp.headers.get("content-type", "").lower()
        is_actual_pdf = resp.content[:4] == b"%PDF"
        is_pdf = is_actual_pdf or ("application/pdf" in content_type and is_actual_pdf)

        if is_pdf:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            try:
                import shutil
                if shutil.which("liteparse"):
                    import subprocess
                    r = subprocess.run(["liteparse", tmp_path],
                                       capture_output=True, text=True, timeout=60)
                    if r.returncode == 0 and r.stdout.strip():
                        return r.stdout
                from utils.parsers import convert_to_markdown
                md_content = convert_to_markdown(tmp_path)
                if md_content:
                    return md_content
            finally:
                os.unlink(tmp_path)
            return f"[ERROR: PDF at {url} could not be parsed. Size: {len(resp.content)} bytes.]"

        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup(["script", "style", "nav", "footer"]):
            script.extract()
        return '\n'.join(line for line in (l.strip() for l in
                 soup.get_text(separator='\n').splitlines()) if line)

    try:
        data = await asyncio.to_thread(_fetch)
        data = data[:5000000]
        return _save_source(filename, data, url, content_type)
    except Exception as e:
        import traceback
        return f"Failed to fetch URL: {e}\n\nTraceback:\n{traceback.format_exc()}"


async def _fetch_via_tika(url: str, filename: str, content_type: str = "") -> str:
    """Extract non-HTML documents (PDF/Office/EPUB/RTF/image/...) via Apache Tika.

    Follows tika-analyst conventions: PDFs are requested with Accept: text/html
    (Tika preserves more structure that way; plain-text extraction of slide-deck
    PDFs is unreliable), and any X-TIKA:EXCEPTION header is surfaced as a warning
    rather than silently ignored.
    """
    if not filename.endswith('.md'):
        filename += '.md'
    path = _get_safe_path(filename)
    if not path:
        return f"Error: Invalid filename '{filename}'."

    try:
        headers = {"User-Agent": _UA}
        resp = await asyncio.to_thread(
            lambda: httpx.get(url, headers=headers, timeout=30, follow_redirects=True))
        data = resp.content
    except Exception as e:
        return f"Failed to download '{url}': {e}"

    resolved_ct = content_type or resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    is_pdf = "application/pdf" in resolved_ct or url.split("?", 1)[0].lower().endswith(".pdf")

    def _do_tika_extract():
        """Extract text/markup from the file using the Tika REST API."""
        tika_url = TIKA_BASE_URL.rstrip("/") + "/tika"
        # PDFs: Accept text/html for better structure; everything else: plain text.
        accept = "text/html" if is_pdf else "text/plain"
        tika_headers = {
            "Accept": accept,
            "Content-Type": resolved_ct or "application/octet-stream",
            "User-Agent": _UA,
        }
        r = httpx.put(tika_url, headers=tika_headers, data=data, timeout=60)
        exception = r.headers.get("X-TIKA:EXCEPTION") or r.headers.get("x-tika:exception")
        return r.text.strip(), exception

    try:
        content, tika_exception = await asyncio.to_thread(_do_tika_extract)
        warning = ""
        if tika_exception:
            warning = f" [WARNING: Tika reported X-TIKA:EXCEPTION: {tika_exception[:200]}]"
        if not content:
            return (f"[Tika extracted no content from '{url}' — file may be "
                    f"image-only or encrypted.]{warning}")
        content = content[:5000000]
        result = _save_source(filename, content, url, resolved_ct)
        return result + " (via Tika)" + warning
    except Exception as e:
        return f"[Tika extraction failed for '{url}': {e}]"