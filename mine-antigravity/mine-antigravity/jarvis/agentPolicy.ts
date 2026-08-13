/**
 * jarvis/agentPolicy.ts — Dynamic Agent Policy (v2)
 *
 * Upgrades over v1:
 *  - Policy is now intent-aware: different instructions for code vs document vs research tasks
 *  - Token-budget awareness built into the policy
 *  - Includes quality mandate for document outputs
 *  - Cleaner formatting for LLM consumption
 */

export type PolicyMode = 'code' | 'document' | 'research' | 'data' | 'default'

export function detectPolicyMode(task: string): PolicyMode {
  const t = task.toLowerCase()
  if (/\b(pptx|deck|slides?|report|docx?|document|pdf|presentation)\b/.test(t)) return 'document'
  if (/\b(dashboard|chart|graph|sheet|xlsx?|csv|spreadsheet)\b/.test(t)) return 'data'
  if (/\b(search|look\s+up|what\s+is|how\s+does|explain|find\s+online)\b/.test(t)) return 'research'
  if (/\b(fix|implement|debug|refactor|add|create|edit|write)\b.{0,30}\b(code|function|class|bug|error)\b/.test(t)) return 'code'
  return 'default'
}

const POLICY_BY_MODE: Record<PolicyMode, string> = {
  code: `
Execution policy (CODE MODE):
1. Search before reading — use Grep/Glob to find relevant files first.
2. Read only the files you need, using start_line/end_line for large files.
3. Make the smallest correct change using Edit (not full Write) where possible.
4. Verify with Bash: run tests, check syntax, or spot-check the output.
5. Summarize what changed, why, and any follow-up needed.

Tool preference: Grep → Read → Edit → Bash → TodoWrite
`.trim(),

  document: `
Execution policy (DOCUMENT MODE):
1. Decide the document type: deck (BuildDeck), report (BuildReport), dashboard (BuildDashboard), sheet (BuildSheet).
2. Read the SKILL.md for the chosen builder if you need schema guidance.
3. Construct a complete, rich JSON spec — do NOT produce a skeleton or placeholder.
4. Call the builder tool with the spec.
5. Report the output path and a summary of what was generated.

Quality mandate: The output MUST be premium quality — comparable to Gamma.app, Beautiful.ai, or Notion AI.
Use real content, not placeholder text. Pick the most appropriate theme.
`.trim(),

  research: `
Execution policy (RESEARCH MODE):
1. WebSearch to get an overview and identify authoritative sources.
2. WebFetch to read the specific documentation or article in full.
3. NvidiaRagRetrieve if you need internal codebase context.
4. Synthesize findings into a clear, well-structured answer.

Prefer official docs > blog posts > forums. Cite sources.
`.trim(),

  data: `
Execution policy (DATA MODE):
1. Understand the data structure needed (schema, columns, rows).
2. Construct a complete JSON spec for BuildSheet or BuildDashboard.
3. Call the appropriate builder.
4. Confirm the output path and preview the first few rows/sections.

For dashboards: use charts that best represent the data (bar for comparison, line for trends, pie for composition).
For sheets: include meaningful headers, use appropriate number formatting.
`.trim(),

  default: `
Execution policy:
1. Plan if the task has multiple steps — use TodoWrite.
2. Search first (Grep, Glob, WebSearch) before reading or editing.
3. Read only the targeted files needed.
4. Execute the minimal correct action.
5. Verify the result, then summarize clearly.

Tool preference: Search → Read → Act → Verify
`.trim(),
}

export function buildAgentPolicy(task: string): string {
  const mode = detectPolicyMode(task)
  return POLICY_BY_MODE[mode]
}

export function buildFullAgentPrompt(task: string): string {
  const policy = buildAgentPolicy(task)
  return `Goal: ${task}\n\n${policy}`
}
