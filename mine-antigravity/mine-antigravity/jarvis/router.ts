/**
 * jarvis/router.ts — Smart Intent-Based Tool Router
 *
 * Classifies user requests into intent classes, then:
 *  1. Returns the optimal tool subset for that intent (reduces token bloat)
 *  2. Injects a concrete usage example into the system prompt addendum
 *  3. Provides confidence score and routing rationale
 *
 * Intent classes: build_document | code_task | data_task | research | file_op | system_cmd | query
 */

// ─── Types ─────────────────────────────────────────────────────────────────────

export type IntentClass =
  | 'build_document'
  | 'code_task'
  | 'data_task'
  | 'research'
  | 'file_op'
  | 'system_cmd'
  | 'query'

export interface RoutingResult {
  intent: IntentClass
  confidence: number          // 0–1
  rationale: string
  primaryTools: string[]      // Ordered tool priority list
  systemAddendum: string      // Injected before the API call
  shouldPlanFirst: boolean
  shouldVerify: boolean
  parallelizable: boolean     // Hint: can tool calls be parallelized?
}

// ─── Intent Patterns ──────────────────────────────────────────────────────────

interface IntentPattern {
  intent: IntentClass
  weight: number
  patterns: RegExp[]
}

const INTENT_PATTERNS: IntentPattern[] = [
  {
    intent: 'build_document',
    weight: 10,
    patterns: [
      /\b(pptx|ppt|powerpoint|slides?|deck|presentation)\b/i,
      /\b(docx?|word\s+doc|report|document)\b/i,
      /\b(pdf|export\s+to)\b/i,
      /\b(make|create|build|generate|render|produce)\b.{0,30}\b(deck|slides?|presentation|report|doc)\b/i,
      /\b(pitch\s+deck|investor\s+deck|slide\s+deck)\b/i,
      /\b(gamma|beautiful\.ai|canva)\b/i,
    ],
  },
  {
    intent: 'data_task',
    weight: 9,
    patterns: [
      /\b(xlsx?|excel|spreadsheet|csv)\b/i,
      /\b(sheet|table|workbook|pivot)\b/i,
      /\b(dashboard|chart|graph|plot|visualization)\b/i,
      /\b(analyze|analyse)\b.{0,30}\b(data|csv|numbers?|metrics?)\b/i,
      /\b(make|create|build)\b.{0,30}\b(dashboard|chart|graph|sheet|spreadsheet)\b/i,
    ],
  },
  {
    intent: 'system_cmd',
    weight: 8,
    patterns: [
      /\b(run|execute|start|stop|restart|kill)\b.{0,30}\b(test|server|script|command|process)\b/i,
      /\b(install|uninstall|npm|bun|pip|yarn)\b/i,
      /\b(build|compile|transpile|bundle)\b.{0,20}\b(project|code|app)\b/i,
      /\b(git\s+(commit|push|pull|clone|status|log|diff|add))\b/i,
      /\b(docker|kubectl|terraform)\b/i,
    ],
  },
  {
    intent: 'code_task',
    weight: 7,
    patterns: [
      /\b(fix|debug|resolve)\b.{0,30}\b(bug|error|issue|problem|crash|exception)\b/i,
      /\b(implement|add|create|write)\b.{0,30}\b(function|class|method|component|feature|module|api|endpoint)\b/i,
      /\b(refactor|optimize|improve|clean\s+up)\b.{0,30}\b(code|function|class|file)\b/i,
      /\b(test|unit\s+test|spec|testing)\b/i,
      /\b(typescript|javascript|python|rust|go|java|react|vue|svelte)\b/i,
      /\b(pr|pull\s+request|review|lint|format)\b/i,
    ],
  },
  {
    intent: 'research',
    weight: 6,
    patterns: [
      /\b(search|look\s+up|find\s+online|google|web)\b/i,
      /\b(documentation|docs?|readme|guide|tutorial|example)\b/i,
      /\b(how\s+does|what\s+is|explain|tell\s+me\s+about|describe)\b/i,
      /\b(latest|recent|new\s+in|whats\s+new)\b/i,
      /\b(compare|vs|versus|difference\s+between)\b/i,
    ],
  },
  {
    intent: 'file_op',
    weight: 5,
    patterns: [
      /\b(read|open|view|show|display)\b.{0,30}\b(file|files)\b/i,
      /\b(write|save|create|update)\b.{0,30}\b(file|config|json|yaml|toml|env)\b/i,
      /\b(delete|remove|rename|move|copy)\b.{0,30}\b(file|folder|directory)\b/i,
      /\b(list|ls|dir)\b.{0,20}\b(files?|folder|directory|contents?)\b/i,
    ],
  },
  {
    intent: 'system_cmd',
    weight: 4,
    patterns: [
      /\brun\b/i,
      /\bexecute\b/i,
      /\bbash\b/i,
      /\bpowershell\b/i,
      /\bterminal\b/i,
    ],
  },
]

// ─── Tool Priority Maps ────────────────────────────────────────────────────────

const TOOL_PRIORITIES: Record<IntentClass, string[]> = {
  build_document: [
    'Skill', 'BuildDeck', 'BuildReport', 'BuildDashboard', 'BuildSheet',
    'Read', 'Write', 'Bash', 'WebFetch', 'WebSearch'
  ],
  data_task: [
    'BuildSheet', 'BuildDashboard', 'Read', 'Write', 'Bash', 'Grep', 'Glob'
  ],
  code_task: [
    'Grep', 'Read', 'Glob', 'LS', 'Edit', 'Write', 'Bash',
    'TodoWrite', 'NvidiaRagRetrieve', 'CodeAnalyze', 'RunTests'
  ],
  research: [
    'WebSearch', 'WebFetch', 'NvidiaRagRetrieve', 'Read', 'Grep'
  ],
  file_op: [
    'Read', 'Write', 'Edit', 'LS', 'Glob', 'Grep', 'Bash'
  ],
  system_cmd: [
    'Bash', 'Read', 'Write', 'Grep'
  ],
  query: [
    'NvidiaRagRetrieve', 'WebSearch', 'Read'
  ],
}

// ─── System Prompt Addenda ────────────────────────────────────────────────────

const ADDENDA: Record<IntentClass, string> = {
  build_document: `
INTENT: build_document — You must produce a real file artifact.
Workflow:
  1. Read SKILL.md for the relevant builder (deck/report/dashboard/sheet)
  2. Construct a complete JSON spec matching the schema
  3. Call BuildDeck/BuildReport/BuildSheet/BuildDashboard with the spec
  4. Confirm the output path to the user

Example (PPTX):
  BuildDeck({ spec: { theme: "midnight", slides: [
    { layout: "title", title: "Nova AI", subtitle: "The future of intelligence", image: null },
    { layout: "content_image", title: "What We Do", bullets: ["Automate repetitive tasks","Reduce costs by 40%","Ship 3× faster"], image: null }
  ]} })

Output quality mandate: Match or exceed Gamma.app and Beautiful.ai quality.
`.trim(),

  data_task: `
INTENT: data_task — Produce a data artifact (dashboard, spreadsheet, chart).
Workflow:
  1. Understand the data structure needed
  2. Construct a spec (for BuildDashboard or BuildSheet)
  3. Call the appropriate builder
  4. Confirm the output file path

Example (Dashboard):
  BuildDashboard({ spec: { title: "Sales Q3", sections: [
    { type: "kpi", items: [{ label: "Revenue", value: "$4.2M", trend: "+18%" }] },
    { type: "chart", chart_type: "bar", title: "Monthly Revenue", labels: ["Jan","Feb","Mar"], datasets: [{ label: "Revenue", data: [1.2, 1.8, 2.1] }] }
  ]} })
`.trim(),

  code_task: `
INTENT: code_task — Focus on targeted, minimal code changes.
Workflow:
  1. Search first (Grep/Glob) — find relevant code before reading
  2. Read only files needed (start_line/end_line for large files)
  3. Make the minimal correct change (Edit preferred over full Write)
  4. Verify with Bash (run tests, check syntax)
  5. Summarize what changed and why

For multi-step tasks: Create a TodoWrite plan first.
`.trim(),

  research: `
INTENT: research — Gather and synthesize external information.
Workflow:
  1. WebSearch for overview / recent results
  2. WebFetch specific documentation pages
  3. NvidiaRagRetrieve for internal codebase context
  4. Synthesize into a concise answer

Prefer authoritative sources (official docs > blog posts > forums).
`.trim(),

  file_op: `
INTENT: file_op — Direct file system operation.
Workflow:
  1. Verify paths with LS/Glob before Read/Write
  2. For edits: Read the file first to confirm the old_string
  3. Prefer Edit over full Write when changing small sections
  4. Confirm the operation completed successfully
`.trim(),

  system_cmd: `
INTENT: system_cmd — Execute shell commands.
Platform: ${process.platform === 'win32' ? 'Windows — use PowerShell syntax' : 'Linux/macOS — use bash syntax'}
Workflow:
  1. Validate the command is safe and targeted
  2. Use Bash with appropriate timeout
  3. Parse and present the output clearly
  4. Handle errors gracefully (check exit codes)
`.trim(),

  query: `
INTENT: query — Answer a question directly or with minimal tool use.
Prefer answering from context before calling tools.
Only use NvidiaRagRetrieve if the answer requires internal codebase knowledge.
Only use WebSearch if the answer requires up-to-date external information.
`.trim(),
}

// ─── Main Classifier ──────────────────────────────────────────────────────────

export function classifyIntent(input: string): RoutingResult {
  const scores = new Map<IntentClass, number>()

  for (const { intent, weight, patterns } of INTENT_PATTERNS) {
    for (const pattern of patterns) {
      if (pattern.test(input)) {
        scores.set(intent, (scores.get(intent) ?? 0) + weight)
      }
    }
  }

  // Default to 'query' if no match
  if (scores.size === 0) {
    scores.set('query', 1)
  }

  // Find best intent
  let bestIntent: IntentClass = 'query'
  let bestScore = 0
  let totalScore = 0

  for (const [intent, score] of scores) {
    totalScore += score
    if (score > bestScore) {
      bestScore = score
      bestIntent = intent
    }
  }

  const confidence = Math.min(1, bestScore / (totalScore || 1))

  const rationale = `Matched "${bestIntent}" with score ${bestScore}/${totalScore} (${(confidence * 100).toFixed(0)}% confidence)`

  return {
    intent: bestIntent,
    confidence,
    rationale,
    primaryTools: TOOL_PRIORITIES[bestIntent],
    systemAddendum: ADDENDA[bestIntent],
    shouldPlanFirst: ['code_task', 'build_document'].includes(bestIntent) && input.length > 80,
    shouldVerify: ['code_task', 'system_cmd', 'build_document'].includes(bestIntent),
    parallelizable: ['code_task', 'research'].includes(bestIntent),
  }
}

// ─── System Prompt Builder ────────────────────────────────────────────────────

export function buildRoutingPrompt(input: string): string {
  const result = classifyIntent(input)
  return `\n\n--- ROUTING ---\n${result.systemAddendum}\n\nPreferred tool order: ${result.primaryTools.slice(0, 6).join(' → ')}\nPlan first: ${result.shouldPlanFirst ? 'yes' : 'no'} | Verify result: ${result.shouldVerify ? 'yes' : 'no'}\n---`
}
