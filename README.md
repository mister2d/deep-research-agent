# Deep Research Agent

A hierarchical deep research agent built with the **Microsoft Agent Framework** and **Textual** TUI. Uses a strict 3-tier delegation chain: **Orchestrator → Searcher → Analyzer** to perform web-based research and document analysis while keeping context windows lean for local LLMs.

## Configuration

Endpoints and credentials are supplied via environment variables, loaded from a
local `.env` file (gitignored). Copy `.env.example` to `.env` and point each
value at your own infrastructure; `direnv` loads it automatically via `.envrc`
(`use flake` + `dotenv_if_exists .env`). The flake declares no endpoints, so
each operator can target their own self-hosted services.

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_BASE` / `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI-compatible LLM (thinking) endpoint |
| `SEARXNG_URL` | SearXNG base URL for web search (the app appends `/search`) |
| `CRAWL4AI_URL` | Self-hosted Crawl4AI REST service (falls back to httpx+BS4 if unset) |
| `CRAWL4AI_AUTH_TOKEN` | Crawl4AI auth token (defaults to `dummy`) |
| `TIKA_BASE_URL` | Apache Tika server for PDF/Office/EPUB/RTF/image extraction |
| `HYBRID_SEARCH_SKILL_DIR` | Directory of the hybrid-web-search skill (`run-json.mjs`) |

### Tuning web search

The hybrid-web-search pipeline (crawl breadth, chunk count/size, score cutoff,
timeout) can be tuned either from `~/.deep-research-agent/config.yaml` under
`settings.web_search`, or via ambient `HYBRID_SEARCH_*` environment variables.
A value set in `config.yaml` overrides the ambient env; when both are silent,
the skill's own scout-grade defaults apply. The shipped `config_template.yaml`
enables deep-research-grade defaults (`max_crawl_urls: 5`, `max_output_chunks: 8`,
`crawl_score_threshold: 0.0`, `timeout_ms: 20000`) — remove the `web_search`
section to fall back to the skill's leaner scout-grade behavior.
The mapping is `max_crawl_urls → HYBRID_SEARCH_MAX_CRAWL_URLS`,
`max_output_chunks → HYBRID_SEARCH_MAX_OUTPUT_CHUNKS`,
`chunk_token_target → HYBRID_SEARCH_CHUNK_TOKEN_TARGET`,
`crawl_score_threshold → HYBRID_SEARCH_CRAWL_SCORE_THRESHOLD`,
`timeout_ms → HYBRID_SEARCH_TIMEOUT_MS`.

Every document the agent writes carries OKF v0.1 YAML frontmatter (title,
timestamps, type, tags, `sha256` over the body, `okf_version`). Crawled sources
also record `source_url`, `content_type`, and `ingested`; `final_report.md`
enumerates its `sources`. Validate a run directory with
`python eval/validate_okf.py <run_dir>`.

## Architecture

```
+-----------------------------------+
|    Orchestrator (Planner)         |
|-----------------------------------|
| Tools: write_workspace_file,      |
|        list_workspace_files,      |
|        write_todos, read_todos,   |
|        think_tool, delegate_tasks |
| No web or file reading tools.     |
+-----------------+-----------------+
                  | delegates to
                  v
       +--------------------+
       |   Searcher         |
       |--------------------|
       | Tools: web_search, |
       |        fetch_url,  |
       |        think_tool, |
       |        delegate    |
       | No file reading.   |
       +--------+-----------+
                | delegates to
                v
       +--------------------+
       |   Analyzer (Leaf)  |
       |--------------------|
       | Tools: read_file,  |
       |        grep_file,  |
       |        think_tool  |
       | No web, no delegate.|
       +--------------------+
```

### Delegation Chain & Tool Separation

- **Orchestrator**: Plans research, dispatches Searchers, synthesizes `final_report.md`. Has NO web tools and NO file reading tools. Delegates ONLY to the Searcher.
- **Searcher**: Searches the web, fetches URLs to the workspace. Has NO file reading tools — forced to delegate to the Analyzer. Delegates ONLY to the Analyzer.
- **Analyzer**: Reads and extracts data from downloaded files. Has NO web tools and NO delegation capability. Leaf node.

This separation prevents any single agent from bloating its context window with raw web content.

### Proportional Search Depth

The Orchestrator assesses query complexity before planning:
- **Simple factual queries**: Dispatch a single Searcher. One authoritative source is sufficient.
- **Multi-fact queries**: A single Searcher is still sufficient.
- **Comparative/synthesis queries**: Dispatch one Searcher per independent angle, concurrently.
- **Deep research**: Full multi-phase approach with multiple delegations.

### Source Quality Awareness

The Searcher evaluates source authority:
- **Authoritative** (official docs, spec sheets): One source is sufficient.
- **Semi-authoritative** (established publications): One is usually enough, a second is welcome.
- **Informal** (forums, blogs): Corroborate with at least one additional source.

### Session Isolation

Each run gets a timestamped isolated folder (e.g., `run_1748192400/`). File tools automatically map all operations into this folder. Agents are unaware of the run folder and read/write files directly.

## Setup Instructions

### 1. Create the Environment & Install

```bash
cd /home/kyuz0/video/deep-research
python -m venv venv
source venv/bin/activate
pip install -e .
```

**System-Wide Installation (Optional):**

```bash
pipx install .
```

### 2. Configure Endpoints

By default, the application uses an OpenAI-compatible API on `localhost:8080` (e.g., `llama.cpp`). Create a `.env` file:

```env
OPENAI_API_BASE=http://localhost:8080/v1
OPENAI_API_KEY=dummy
OPENAI_MODEL=local-model
```

### 3. Configure the Agent

On first run, the config is auto-created at `~/.deep-research-agent/config.yaml` from `src/config_template.yaml`. Key settings:

```yaml
settings:
  concurrency:
    max_concurrent_tasks: 3    # Max parallel sub-agent execution
  quotas:                      # Global tool call limits
    web_search: 15
    fetch_url_to_workspace: 10
    delegate_tasks: 10
    read_workspace_file:
      limit: 60
      rules:
        max_lines: 400
  workspace:
    type: disk                 # "memory" or "disk"
    session_isolation: true    # Timestamped run folders
```

### 4. Run the TUI

```bash
python src/app.py
```

### 5. Headless Mode

```bash
python src/app.py --prompt "Compare the AI research strategies of OpenAI, Google DeepMind, and Anthropic in 2024." --auto-approve
```

**Useful Flags:**
- `--prompt "..."`: Run headlessly with a specific query.
- `--auto-approve`: Bypass Human-in-the-Loop tool approvals (required for headless).
- `--list-sessions`: List saved session histories.
- `--resume <session_id>`: Restore a previous session.
- `/toggle_thinking`: Toggle LLM reasoning traces in the TUI.
- `/files`: Browse workspace files in the TUI.

## Included Tools

| Tool | Description |
|------|-------------|
| `web_search` | DuckDuckGo search (no API key needed) |
| `fetch_url_to_workspace` | Fetch URLs → parse to Markdown → save to workspace |
| `read_workspace_file` | Read files with line-range chunking |
| `grep_workspace_file` | Regex search within workspace files |
| `write_workspace_file` | Write files to workspace |
| `list_workspace_files` | List all workspace files |
| `write_todos` / `read_todos` | Markdown checkbox task tracking |
| `think_tool` | Forced reflection pause for structured reasoning |
| `delegate_tasks` | Auto-injected for agents with children |

## Security

- **No shell execution**: The `run_shell_command` tool is removed from this agent.
- **Quota enforcement**: Every tool has a global call limit to prevent infinite loops.
- **Session isolation**: Each run is sandboxed into its own timestamped folder.
- **Anti-looping directives**: Baked into all agent system prompts to prevent infinite retry cycles.
