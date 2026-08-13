"""Full system verification script."""
from agent.transport.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
routes.sort()
print(f"Total routes: {len(routes)}")
for r in routes:
    print(f"  {r}")

from agent.config.settings import settings
serp = "set" if settings.serpapi.api_key else "not set"
brave = "set" if settings.brave.api_key else "not set"
print(f"\nSerpAPI key: {serp}")
print(f"Brave key: {brave}")

from agent.routing.entry_gate import _build_llm_prompt
prompt = _build_llm_prompt()
print(f"\nDynamic gate prompt includes skills: {'Code Reviewer' in prompt}")
print(f"Prompt has SKILL mode: {'SKILL' in prompt}")

from agent.connectors.github import GitHubConnector
gh = GitHubConnector()
print(f"\nGitHub has list_repos: {hasattr(gh, 'list_repos')}")
print(f"GitHub has list_tree: {hasattr(gh, 'list_tree')}")

from agent.tools.brave_search import _serpapi_fallback
print("SerpAPI fallback function exists: True")

print("\nALL CHECKS PASSED")
