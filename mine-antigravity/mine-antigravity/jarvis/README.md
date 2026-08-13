# Jarvis agent runtime

This folder contains a lightweight Claude Code-style agent runtime with:

- a structured tool registry
- builder-oriented tools for dashboards, decks, reports, and spreadsheets
- a skills directory that hosts reusable skill packages
- a compact execution policy
- a lightweight context policy for memory compression

## How skills and tools are wired

The runtime uses a simple contract:

1. Skills live under the Jarvis skills directory and describe when to use a capability.
2. Tools live under the Jarvis tools registry and execute the capability.
3. The agent routes requests through the tool registry first, and the skill layer only guides intent selection.

That keeps the system consistent: a user request flows from skill intent -> tool selection -> execution, all inside the Jarvis runtime.

## Design notes

The runtime is optimized for high-quality, low-token overhead behavior:

1. Search first, read targeted files, and avoid redundant actions.
2. Keep context compact by preserving summaries rather than raw transcript dumps.
3. Prefer short plans and concise tool use.
4. Verify changes before finishing.
