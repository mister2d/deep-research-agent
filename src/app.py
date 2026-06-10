import warnings
warnings.filterwarnings("ignore", message=".*is experimental and may change.*")

from engine.sdk import AgentBuilder, SubAgentConfig
from tools import (
    read_workspace_file,
    write_workspace_file,
    list_workspace_files,
    grep_workspace_file,
    fetch_url_to_workspace,
    web_search,
    write_todos,
    read_todos,
    think_tool,
)
from prompts import (
    ORCHESTRATOR_INSTRUCTIONS,
    SEARCH_SUBAGENT_INSTRUCTIONS,
    ANALYZER_SUBAGENT_INSTRUCTIONS,
)
import config

# ============================================================
# DELEGATION CHAIN (strictly hierarchical, one direction only):
#   Orchestrator → delegates to → Searcher → delegates to → Analyzer
# ============================================================

# 1. Leaf agent (Analyzer) — file reading only, NO web, NO delegation
#    No sub_agents = no delegate_tasks tool injected (leaf node)
analyzer = SubAgentConfig(
    name="Analyzer",
    instructions=ANALYZER_SUBAGENT_INSTRUCTIONS,
    tools=[read_workspace_file, grep_workspace_file, think_tool]
)

# 2. Middle agent (Searcher) — web only, NO file reading (forces delegation to Analyzer)
#    sub_agents=[analyzer] = can ONLY delegate to Analyzer, nothing else
searcher = SubAgentConfig(
    name="Searcher",
    instructions=SEARCH_SUBAGENT_INSTRUCTIONS,
    tools=[web_search, fetch_url_to_workspace, think_tool],
    sub_agents=[analyzer]
)

# 3. Orchestrator — task management only, NO web, NO file reading
#    sub_agents=[searcher] = can ONLY delegate to Searcher, cannot bypass to Analyzer
app = AgentBuilder(
    name=config.APP_TITLE,
    description=config.APP_DESCRIPTION,
    instructions=ORCHESTRATOR_INSTRUCTIONS,
    tools=[write_workspace_file, list_workspace_files, write_todos, read_todos, think_tool],
    sub_agents=[searcher]
)

def cli_main():
    app.start()

if __name__ == "__main__":
    cli_main()
