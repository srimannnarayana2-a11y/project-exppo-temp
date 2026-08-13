/**
 * claude-nvidia.tsx  —  JARVIS-NVIDIA Agent (v2)
 * Run:  bun run claude-nvidia.tsx
 *
 * Upgrades over v1:
 *  · Parallel tool execution (Promise.all, capped at 4)
 *  · Smart intent-based routing (injects focused context per request type)
 *  · LRU cache for file reads, web fetches, and RAG queries
 *  · 26 tools: File · Search · RAG · Agent · Builder · Code
 *  · /cache, /skills, /route slash commands
 *  · Token-budget awareness in context policy
 *  · Quality mandate for document outputs
 */

// ─── Windows Bun TTY Polyfill ────────────────────────────────────────────────
if (typeof process.stdout.isTTY === 'undefined') {
  Object.defineProperty(process.stdout, 'isTTY', { value: true, writable: true })
}
if (typeof process.stdin.isTTY === 'undefined') {
  Object.defineProperty(process.stdin, 'isTTY', { value: true, writable: true })
}
if (!process.stdout.columns) process.stdout.columns = 120
if (!process.stdout.rows) process.stdout.rows = 30
if (!process.stdin.setRawMode) process.stdin.setRawMode = ((_: boolean) => process.stdin) as any

import React, { useState, useCallback, useRef } from 'react'
import rawRender from './src/ink/root.js'
import Box       from './src/ink/components/Box.js'
import Text      from './src/ink/components/Text.js'
import useInput  from './src/ink/hooks/use-input.js'
import useApp    from './src/ink/hooks/use-app.js'

import { execSync } from 'child_process'
import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync } from 'fs'
import { resolve, dirname, relative, basename } from 'path'

// ─── Jarvis v2 Imports ────────────────────────────────────────────────────────
import {
  JARVIS_SKILLS, JARVIS_TOOLS, executeJarvisTool,
  getCacheStats, clearAllCaches, getToolSummary, TOOL_COUNT,
  loadSession, saveSession, clearSession, prepareHistoryForApiCall, getContextStatus,
} from './jarvis/index.js'
import { buildAgentPolicy } from './jarvis/agentPolicy.js'
import { buildContextPolicy, summarizeContext, buildTokenBudgetHint } from './jarvis/contextPolicy.js'
import { buildQueryOptimizationPrompt } from './jarvis/queryOptimization.js'
import { buildRoutingPrompt, classifyIntent } from './jarvis/router.js'
import { getSkillSummary } from './jarvis/skillRegistry.js'
import { executeJarvisToolsParallel } from './jarvis/tools/index.js'

// ─── Dual-Key Provider Configuration ─────────────────────────────────────────
//
// KEY 3 — Agent / Reasoning  (large model, thinking ON)
//   export NVIDIA_API_KEY="nvapi-..."
//   export NVIDIA_MODEL="nvidia/nemotron-ultra-253b-v1"        (default)
//
// KEY 4 — Tool Executor / Fast  (small model, no thinking, lightning speed)
//   export NVIDIA_TOOL_KEY="nvapi-..."                         (can be same key)
//   export NVIDIA_TOOL_MODEL="nvidia/llama-3.1-nemotron-nano-8b-v1"  (default)
//   export NVIDIA_TOOL_BASE_URL="https://..."                  (optional override)
//
// If NVIDIA_TOOL_KEY is not set, both agent and tools use NVIDIA_API_KEY.
// If NVIDIA_TOOL_MODEL is not set, tools use the fast nano model automatically.

// ── Agent (Key 3) ─────────────────────────────────────────────────────────────
const NVIDIA_API_KEY  = process.env.NVIDIA_API_KEY  ?? ''
const API_BASE        = (process.env.NVIDIA_BASE_URL ?? 'https://integrate.api.nvidia.com/v1').replace(/\/$/, '')
const MODEL           = process.env.NVIDIA_MODEL    ?? 'nvidia/nemotron-ultra-253b-v1'

// ── Tool Executor (Key 4) ─────────────────────────────────────────────────────
const TOOL_API_KEY    = process.env.NVIDIA_TOOL_KEY      ?? NVIDIA_API_KEY        // fallback to same key
const TOOL_MODEL      = process.env.NVIDIA_TOOL_MODEL    ?? 'nvidia/llama-3.1-nemotron-nano-8b-v1'
const TOOL_API_BASE   = (process.env.NVIDIA_TOOL_BASE_URL ?? API_BASE).replace(/\/$/, '')

// ── Validate keys are present ─────────────────────────────────────────────────
if (!NVIDIA_API_KEY) {
  console.error('[JARVIS] ERROR: NVIDIA_API_KEY is not set. Export it before running:\n  export NVIDIA_API_KEY="nvapi-..."')
  process.exit(1)
}

// ── Dual-key active? ──────────────────────────────────────────────────────────
const DUAL_KEY_MODE   = TOOL_API_KEY !== NVIDIA_API_KEY
if (DUAL_KEY_MODE) {
  console.error(`[JARVIS] Dual-key mode: Agent→${MODEL.split('/').pop()} | Tools→${TOOL_MODEL.split('/').pop()}`)
}

const CWD      = process.cwd()
const MAX_MSGS = 120
const MAX_OUT  = 10_000


const JARVIS_SKILL_NAMES = JARVIS_SKILLS.map(s => s.name).join(', ')

// ─── Types ────────────────────────────────────────────────────────────────────
interface ChatMsg {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
  name?: string
}
interface ToolCall {
  id: string
  type: 'function'
  function: { name: string; arguments: string }
}
interface Msg {
  id: number
  kind: 'welcome' | 'user' | 'assistant' | 'tool_use' | 'tool_out' | 'error' | 'info' | 'cmd'
  text: string
  label?: string
}

// ─── Compiled Builder Schemas (inlined so the model NEVER searches for them) ──
//
// This is the key architectural fix: zero-latency builder knowledge.
// The model has all spec formats memorized in its system prompt.
// No file reads. No searches. No LS commands. Just immediate tool calls.
//
const BUILDER_SCHEMAS = `
══════════════════════════════════════════════════════════════════════
BUILDER SCHEMAS — MEMORIZE THESE. NEVER SEARCH FOR THEM.
══════════════════════════════════════════════════════════════════════

You ALREADY KNOW the full spec for every builder. Use it immediately.
DO NOT read SKILL.md, DO NOT search layout files, DO NOT run LS.
Just build the spec in your head and call the tool.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BuildDeck — PPTX spec
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  BuildDeck({ spec: { theme: "midnight", slides: [...] } })

  Themes: midnight | paper | forest | ocean | corporate | neon
  Set image: null when you have no image file ready.

  LAYOUTS — exact field names:

  title:         { layout:"title",         title, subtitle?, image? }
  section:       { layout:"section",       title, subtitle? }
  content_image: { layout:"content_image", title, bullets:[], image?, image_side? }
  stat:          { layout:"stat",          stat, label, supporting? }
                   ⚠ stat must be ≤8 chars: "40%" not "40 percent"
  quote:         { layout:"quote",         quote, attribution? }
                   ⚠ no " marks in quote — they're auto-added
  closing:       { layout:"closing",       title, cta?, sub_cta? }
  bullets:       { layout:"bullets",       title, bullets:[] }

  COMPLETE EXAMPLE (8-slide pitch deck):
  {
    "theme": "midnight",
    "slides": [
      { "layout": "title",         "title": "Nova AI", "subtitle": "Autonomous deployment at scale", "image": null },
      { "layout": "section",       "title": "The Problem" },
      { "layout": "content_image", "title": "Deployments Are Broken", "bullets": ["40% of eng time on manual tasks","4-hour avg incident resolution","Zero visibility across envs"], "image": null },
      { "layout": "stat",          "stat": "40%", "label": "Engineering time wasted on deploys", "supporting": "across 500 surveyed teams" },
      { "layout": "section",       "title": "Our Solution" },
      { "layout": "content_image", "title": "Nova Automates Everything", "bullets": ["One-click multi-cloud deploys","AI rollback on anomaly detection","Real-time observability"], "image": null },
      { "layout": "stat",          "stat": "10×", "label": "Faster deployments", "supporting": "avg across 200 enterprise customers" },
      { "layout": "quote",         "quote": "Nova cut our deploy time from 4 hours to 8 minutes.", "attribution": "Sarah Chen, CTO at Stripe" },
      { "layout": "closing",       "title": "Let's Build Together", "cta": "hello@novaai.com", "sub_cta": "novaai.com · @nova_ai" }
    ]
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BuildReport — DOCX/PDF spec
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  BuildReport({ spec: { ... }, formats: ["docx"] })
  Themes: corporate | minimal | dark
  Formats: ["docx"] | ["pdf"] | ["docx","pdf"]

  {
    "title": "Q3 Business Review",
    "subtitle": "Strategic Performance Summary",
    "author": "Strategy Team",
    "date": "September 30, 2024",
    "theme": "corporate",
    "footer": "Confidential — Internal Use Only",
    "sections": [
      { "heading": "Executive Summary", "body": "Revenue grew 18% YoY..." },
      { "heading": "Key Metrics", "stats": [
        { "label": "Revenue", "value": "$4.2M", "description": "+18% YoY" },
        { "label": "New Logos", "value": "234", "description": "+31% QoQ" }
      ]},
      { "heading": "Revenue Breakdown", "table": {
        "headers": ["Segment", "Revenue", "Growth"],
        "rows": [["Enterprise","$2.1M","+24%"],["Mid-Market","$1.5M","+15%"]]
      }},
      { "heading": "Priorities", "bullets": ["Ship AI features by Q4","Expand EMEA","Achieve SOC2"] }
    ]
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BuildDashboard — HTML spec
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  BuildDashboard({ spec: { ... } })
  Themes: dark | light | corporate
  Section types: kpi | chart | table | text
  Chart types: bar | line | pie | doughnut

  {
    "title": "Sales Dashboard", "subtitle": "Q3 2024", "theme": "dark",
    "sections": [
      { "type": "kpi", "items": [
        { "label": "Revenue", "value": "$4.2M", "trend": "+18%", "positive": true },
        { "label": "Users", "value": "12,400", "trend": "+5%" }
      ]},
      { "type": "chart", "chart_type": "bar", "title": "Monthly Revenue",
        "labels": ["Jul","Aug","Sep"],
        "datasets": [{ "label": "Revenue", "data": [1.1,1.4,1.7], "color": "#22d3ee" }]
      },
      { "type": "table", "title": "Top Deals",
        "headers": ["Company","Value","Rep"],
        "rows": [["Acme Corp","$420K","Sarah J."],["Globex","$310K","Mike T."]]
      }
    ]
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BuildSheet — XLSX spec
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  BuildSheet({ spec: { ... } })
  Themes: blue | green | dark | minimal | purple
  Formats: ["xlsx"] | ["csv"]

  {
    "title": "Q3 Financial Report",
    "sheets": [
      {
        "name": "Revenue", "style": "blue", "freeze_top": true,
        "headers": ["Month","Revenue","Target","Growth %"],
        "rows": [
          ["January","$1,200,000","$1,100,000","9.1%"],
          ["February","$1,800,000","$1,500,000","20%"],
          ["Q3 Total","$5,100,000","$4,400,000","15.9%"]
        ]
      }
    ]
  }

══════════════════════════════════════════════════════════════════════
ZERO-LATENCY BUILDER RULES — NO EXCEPTIONS
══════════════════════════════════════════════════════════════════════

CRITICAL: For ANY document/file generation request, call the tool as
your VERY FIRST action. Do NOT search files, read SKILL.md, run LS,
or plan first. You already know everything needed from the schemas above.

TRIGGER → TOOL MAPPING (call these IMMEDIATELY, spec already in your head):

  "make a deck / presentation / slides / pptx / pitch"
    → BuildDeck({ spec: { theme: "...", slides: [...] } })

  "write a report / document / word doc / docx / write up / summary doc"
    → BuildReport({ spec: { ... }, formats: ["docx"] })

  "make a pdf / export to pdf / pdf report"
    → BuildReport({ spec: { ... }, formats: ["pdf"] })

  "both word and pdf / docx and pdf"
    → BuildReport({ spec: { ... }, formats: ["docx", "pdf"] })

  "make a dashboard / analytics page / data viz / charts page / kpi board"
    → BuildDashboard({ spec: { ... } })

  "make a spreadsheet / excel / xlsx / sheet / table data / csv"
    → BuildSheet({ spec: { ... } })

RULES:
1. NEVER read SKILL.md, layout_schema.md, or any skill file — schema is above.
2. NEVER run LS, Glob, Grep, or Bash before calling a builder.
3. NEVER say "let me plan first" or "let me check the schema" — just build.
4. Use REAL content — topic-appropriate, executive-quality, no placeholders.
5. For BuildDeck: always ≥6 slides. Vary layouts. Never all same layout.
6. For BuildReport: always ≥4 sections with real body text, not one-liners.
7. For BuildDashboard: always include at least 1 kpi section + 1 chart.
8. For BuildSheet: always include headers and at least 5 rows of real data.
9. Pass spec as a JSON object directly — NOT as a string.
10. After tool completes, report the output file path clearly.

WHAT FAST LOOKS LIKE:
  User: "make me a deck on climate change"
  You:  → BuildDeck (first message, no other tool calls before it)
  Done in 1 tool call.

  User: "write a Q3 business report as a word doc"
  You:  → BuildReport (first message, formats: ["docx"])
  Done in 1 tool call.

  User: "give me an excel with revenue data"
  You:  → BuildSheet (first message)
  Done in 1 tool call.

  User: "build a sales dashboard"
  You:  → BuildDashboard (first message)
  Done in 1 tool call.
`

// ─── System Prompt ─────────────────────────────────────────────────────────────
const SYSTEM = `You are JARVIS-NVIDIA, an elite AI agent. You act like a senior engineer who thinks in milliseconds, not minutes.

Working directory: ${CWD}
Platform: ${process.platform === 'win32' ? 'Windows (PowerShell)' : 'Linux/macOS (bash)'}
Tools: ${TOOL_COUNT} total — File I/O, Search, Builders, RAG, Code, Bash

${BUILDER_SCHEMAS}

═══════════════════════════════════════════════════════════
ACT-FIRST MANDATE — NO DELIBERATION BEFORE TOOL CALLS
═══════════════════════════════════════════════════════════

RULE 0 — THE ONLY RULE THAT MATTERS:
  If you know what tool to call → your first output token MUST be a tool call.
  NEVER output text before a tool call. NEVER say "Let me...", "I'll...", "Sure!".
  The user sees your words only AFTER tools complete. Start with tools.

WRONG (adds 10-30s of wasted LLM tokens):
  "I'll help you create that document! Let me build it now..."
  → BuildReport(...)

RIGHT (first token is already the tool call):
  → BuildReport(...)
  "Here's your document at .jarvis/output/story.docx"

WRONG (searching for things you already know):
  "Let me check what tools are available..."
  → LS / Glob / Read SKILL.md

RIGHT (you already know):
  → BuildDeck({ spec: { theme: "midnight", slides: [...] } })

═══════════════════════════════════════════════════════════
GOAL ≠ METHOD — HYPOTHESIS-DRIVEN PIVOTING
═══════════════════════════════════════════════════════════

Your decision loop (run in your head in < 1 second):

  1. GOAL   What outcome does the user actually want? (not what they said literally)
  2. ACTION Try the cheapest path first (1 tool call, not 5)
  3. OBSERVE Did it work? Unexpected output? Error? Nothing?
  4. HYPOTHESIZE Generate 2-3 competing explanations (assign rough %)
                  H1: command missing (60%) H2: path wrong (30%) H3: permissions (10%)
  5. DISCRIMINATE Run the ONE experiment that separates H's fastest
                  Not the same thing again — a DIFFERENT thing that produces different output per H
  6. PIVOT  Goal unchanged, method changes. "What's cheapest way to reach 95% confidence?"
  7. EXECUTE new path. Never grind a broken path more than 2x.

COST-BENEFIT GATE (run before every retry):
  "Is the cost of another tool call justified by expected information gain?"
  If YES → call it. If NO → pivot or answer from knowledge.

  Examples:
  - Bash hangs once → try a timeout version, not the same command again
  - File not found → check with LS before reading again
  - Builder fails → check spec shape, not re-read SKILL.md (you know the schema)

═══════════════════════════════════════════════════════════
ROUTING — WHAT TO DO FIRST
═══════════════════════════════════════════════════════════

DOCUMENT (story / deck / report / sheet / dashboard / pdf):
  → builder tool call IMMEDIATELY. No text first. Done in 1 tool call.
    "write a story as docx"  → BuildReport immediately
    "make a deck on X"       → BuildDeck immediately
    "excel of Y data"        → BuildSheet immediately
    "html dashboard for Z"   → BuildDashboard immediately
  FileWrite is also valid for any file type the builders don't cover:
    "write a Python script"  → FileWrite immediately with the full content

CODE (fix / edit / implement / debug):
  → Grep → targeted Read (start_line/end_line) → Edit → Bash to verify
  Max 2 Bash retries before pivoting method.

RESEARCH (explain / why / what is / compare):
  → Answer from knowledge first (if confident >70%). Add WebSearch only if fresh data needed.
  NEVER WebSearch for: algorithms, syntax, common patterns, math, general CS knowledge.

FILE TASKS (create file / write to file / any file type):
  → FileWrite immediately with complete content. One call. Done.

GENERAL:
  - Parallel tool calls: fire multiple independent reads at once (Read + Grep + LS simultaneously)
  - If you have >70% confidence in an answer → say it, skip retrieval
  - Max tool chain before checking in with user: 5 calls
  - Quality mandate for documents: match or exceed Gamma.app / Notion AI quality`

// ─── Slash Commands ───────────────────────────────────────────────────────────
const COMMANDS: Record<string, { desc: string; aliases?: string[] }> = {
  '/help':    { desc: 'Show this help message', aliases: ['/h', '/?'] },
  '/clear':   { desc: 'Clear conversation history (keeps system prompt)', aliases: ['/c'] },
  '/history': { desc: 'Show conversation message count', aliases: ['/hist'] },
  '/model':   { desc: 'Show current NVIDIA NIM model and endpoint', aliases: ['/m'] },
  '/cwd':     { desc: 'Show current working directory' },
  '/tools':   { desc: 'List all tools by category' },
  '/skills':  { desc: 'List all Jarvis skills with scripts' },
  '/cache':   { desc: 'Show cache statistics (file/web/RAG)' },
  '/route':   { desc: 'Test intent routing for a query: /route <query>' },
  '/exit':    { desc: 'Exit the REPL', aliases: ['/quit', '/q'] },
}

function helpText(): string {
  return [
    '',
    'Slash commands:',
    ...Object.entries(COMMANDS).map(([k, v]) =>
      `  ${k.padEnd(12)} ${v.aliases ? `(${v.aliases.join(',')})  ` : '      '}${v.desc}`
    ),
    '',
    'Press Ctrl+C to abort an in-flight response.',
    '',
  ].join('\n')
}

// ─── Inference Parameters — Dynamic Reasoning Depth ─────────────────────────
//
// NVIDIA NIM does NOT support `thinking` (Anthropic-only) or `reasoning_effort`
// universally. We control reasoning depth via temperature only:
//
//   Builder tasks   → temp 0.05  → deterministic, fast, no wasted tokens
//   Code tasks      → temp 0.10  → accurate, low variation
//   Deep reasoning  → temp 0.20  → creative, broader exploration
//
interface InferenceParams {
  temperature: number
  max_tokens?: number
  // Display label for the status bar
  label: string
}

const BUILDER_INTENTS = new Set([
  'build_document', 'build_pptx', 'build_report', 'build_dashboard', 'build_sheet',
])

/**
 * Classify the user's last message and return the right inference params.
 *
 *  ┌───────────────────────────────┬────────────┬──────────────────┬──────────────┐
 *  │ Task type                     │ Temp       │ Thinking         │ Why          │
 *  ├───────────────────────────────┼────────────┼──────────────────┼──────────────┤
 *  │ Builder (deck/report/etc.)    │ 0.05       │ OFF (budget=0)   │ spec is baked│
 *  │ Code edit / bug fix           │ 0.10       │ LOW              │ need accuracy│
 *  │ Research / web search         │ 0.20       │ LOW              │ summarize    │
 *  │ Complex planning / agent      │ 0.20       │ HIGH (8K tokens) │ deep thinking│
 *  └───────────────────────────────┴────────────┴──────────────────┴──────────────┘
 */
function getInferenceParams(userMsg: string): InferenceParams {
  const t = userMsg.toLowerCase()

  // Builder: zero thinking — model has spec memorized, just serialize and call
  const isBuilder =
    /\b(deck|pptx|presentation|slides|pitch)\b/.test(t) ||
    /\b(report|docx?|word doc|write up|write a doc)\b/.test(t) ||
    /\b(pdf|pdf report)\b/.test(t) ||
    /\b(dashboard|kpi board|analytics page|charts page|data viz)\b/.test(t) ||
    /\b(spreadsheet|excel|xlsx|sheet|csv|table data)\b/.test(t)

  if (isBuilder) return {
    temperature: 0.05,
    max_tokens: 8192,
    label: 'builder ⚡',
  }

  // Complex coding / debugging — medium thinking for accuracy
  const isCode =
    /\b(fix|debug|refactor|implement|add|edit|update)\b/.test(t) &&
    /\b(code|function|class|bug|error|file|module|import)\b/.test(t)

  if (isCode) return {
    temperature: 0.10,
    max_tokens: 16384,
    label: 'code 🔧',
  }

  // Complex multi-step agent / architecture / planning — full thinking
  const isDeep =
    /\b(architect|design|plan|strategy|analyze|compare|evaluate|research|how does|explain|why)\b/.test(t)

  if (isDeep) return {
    temperature: 0.20,
    max_tokens: 32768,
    label: 'deep reasoning 🧠',
  }

  // Default: general tasks
  return {
    temperature: 0.15,
    max_tokens: 16384,
    label: 'general 💬',
  }
}

// ─── NVIDIA NIM Streaming API ─────────────────────────────────────────────────
async function callNvidiaApi(
  msgs: ChatMsg[],
  onChunk: (t: string) => void,
  signal: AbortSignal,
  params: InferenceParams
): Promise<ToolCall[]> {

  // NVIDIA NIM OpenAI-compatible body.
  // Do NOT include `thinking` (Anthropic-only → HTTP 400).
  // Do NOT include `reasoning_effort` (not universally supported).
  // Reasoning depth is controlled by temperature only.
  const body: Record<string, unknown> = {
    model: MODEL,
    messages: msgs,
    tools: JARVIS_TOOLS,
    tool_choice: 'auto',
    temperature: params.temperature,
    stream: true,
    ...(params.max_tokens ? { max_tokens: params.max_tokens } : {}),
  }

  const resp = await fetch(`${API_BASE}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${NVIDIA_API_KEY}`,
    },
    body: JSON.stringify(body),
    signal,
  })

  if (!resp.ok) {
    const errText = await resp.text().catch(() => resp.statusText)
    throw new Error(`NVIDIA NIM API HTTP ${resp.status}: ${errText}`)
  }

  const reader  = resp.body!.getReader()
  const decoder = new TextDecoder()
  const tcMap   = new Map<number, { id: string; name: string; args: string }>()
  let   buf     = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n'); buf = lines.pop() ?? ''
      for (const line of lines) {
        if (!line.startsWith('data:')) continue
        const raw = line.slice(5).trim()
        if (raw === '[DONE]') continue
        try {
          const choice = JSON.parse(raw).choices?.[0]
          const delta  = choice?.delta
          if (!delta) continue
          // Skip internal thinking tokens (they appear as role:"thinking" in some APIs)
          if ((choice as any).role === 'thinking') continue
          if (delta.content) onChunk(delta.content)
          if (Array.isArray(delta.tool_calls)) {
            for (const tc of delta.tool_calls) {
              const i = tc.index ?? 0
              if (!tcMap.has(i)) tcMap.set(i, { id: tc.id ?? `call_${i}`, name: '', args: '' })
              const cur = tcMap.get(i)!
              if (tc.function?.name)      cur.name += tc.function.name
              if (tc.function?.arguments) cur.args += tc.function.arguments
            }
          }
        } catch { /* ignore partial JSON */ }
      }
    }
  } catch (e: unknown) {
    if ((e as Error).name === 'AbortError') return []
    throw e
  }

  return Array.from(tcMap.values()).map(tc => ({
    id: tc.id,
    type: 'function' as const,
    function: { name: tc.name, arguments: tc.args },
  }))
}

// ─── Unified Tool Executor ────────────────────────────────────────────────────
async function runTool(name: string, args: Record<string, unknown>): Promise<string> {
  try {
    const result = await executeJarvisTool(name, args)
    if (result !== null) return result

    // Fallback native tools (platform-aware)
    const shell = process.platform === 'win32' ? 'powershell.exe' : '/bin/bash'
    switch (name) {
      case 'Bash': {
        const cmd = args.command as string
        const to  = (args.timeout as number) ?? 30_000
        try {
          return execSync(cmd, { cwd: CWD, timeout: to, maxBuffer: 10 << 20, shell }).toString().trim() || '(command succeeded, no output)'
        } catch (e: unknown) {
          const err = e as Error & { stdout?: Buffer; stderr?: Buffer; status?: number }
          return [err.stdout?.toString().trim(), err.stderr?.toString().trim()].filter(Boolean).join('\n') || `Exit ${err.status ?? 1}: ${err.message}`
        }
      }
      case 'Read': {
        const fp    = resolve(CWD, args.file_path as string)
        const lines = readFileSync(fp, 'utf8').split('\n')
        const s = Math.max(0, ((args.start_line as number) ?? 1) - 1)
        const e = (args.end_line as number) ?? lines.length
        return lines.slice(s, e).map((l, i) => `${String(s + i + 1).padStart(4)} │ ${l}`).join('\n')
      }
      case 'Write': case 'FileWrite': {
        const fp = resolve(CWD, args.file_path as string)
        mkdirSync(dirname(fp), { recursive: true })
        writeFileSync(fp, args.content as string, 'utf8')
        return `Wrote ${(args.content as string).split('\n').length} lines to ${relative(CWD, fp)}`
      }
      case 'Edit': case 'FileEdit': {
        const fp  = resolve(CWD, args.file_path as string)
        const src = readFileSync(fp, 'utf8')
        const old = args.old_string as string
        if (!src.includes(old)) {
          const lines = src.split('\n')
          const near = lines.findIndex(l => l.includes(old.split('\n')[0]?.trim() ?? ''))
          return `ERROR: old_string not found in ${basename(fp as string)}.${near >= 0 ? ` (closest match near line ${near + 1})` : ' Check whitespace/indentation.'}`
        }
        writeFileSync(fp, src.replace(old, args.new_string as string), 'utf8')
        return `Edited ${relative(CWD, fp)}`
      }
      case 'LS': {
        const dir = resolve(CWD, (args.path as string) ?? '.')
        try {
          return readdirSync(dir).sort().map(e => {
            try { return statSync(resolve(dir, e)).isDirectory() ? `${e}/` : e } catch { return e }
          }).join('\n')
        } catch { return `Cannot list: ${dir}` }
      }
      case 'Grep': {
        const pat  = args.pattern as string
        const path = resolve(CWD, args.path as string)
        const incl = args.include ? `--include "${args.include}"` : ''
        const ci   = args.case_sensitive ? '' : '-i'
        for (const cmd of [`rg -n ${ci} ${incl} "${pat}" "${path}"`, `grep -rn ${ci} "${pat}" "${path}"`]) {
          try { return execSync(cmd, { cwd: CWD, maxBuffer: 5 << 20, shell }).toString().trim() || '(no matches)' } catch { continue }
        }
        return '(no matches)'
      }
      case 'Glob': {
        const pattern = args.pattern as string
        try {
          const cmd = process.platform === 'win32'
            ? `Get-ChildItem -Recurse -Filter "${basename(pattern)}" | Select-Object -ExpandProperty FullName`
            : `find . -name "${basename(pattern)}"`
          return execSync(cmd, { cwd: CWD, shell, maxBuffer: 5 << 20 }).toString().trim() || '(no files found)'
        } catch { return '(no files found)' }
      }
      default: return `Unknown tool: ${name}`
    }
  } catch (e: unknown) {
    return `Tool error [${name}]: ${(e as Error).message}`
  }
}

// ─── UI Setup ─────────────────────────────────────────────────────────────────
let uid = 0

const COLORS = {
  user: 'cyanBright', assistant: 'greenBright', tool_use: 'yellow',
  tool_out: 'white', error: 'red', info: 'gray', cmd: 'magentaBright', welcome: 'green',
} as const

const ICONS: Record<string, string> = {
  user: '  You', assistant: '  JARVIS',
  tool_use: '  ⚙', tool_out: '  ✓',
  error: '  ✗', info: '  ℹ', cmd: '  /', welcome: '🚀',
}

function MsgLine({ m }: { m: Msg }) {
  const color = COLORS[m.kind] ?? 'white'
  const label = m.label ?? ICONS[m.kind] ?? m.kind
  if (m.kind === 'welcome') {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <Box borderStyle="double" borderColor="green" paddingX={2}>
          <Text bold color="green">🚀 JARVIS v2 (NVIDIA NIM)  </Text>
          <Text color="gray">{MODEL}</Text>
        </Box>
        <Box marginLeft={2} marginTop={0}>
          <Text color="gray">{m.text}</Text>
        </Box>
      </Box>
    )
  }
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text bold color={color as Parameters<typeof Text>[0]['color']}>{label}</Text>
      <Box marginLeft={2}><Text wrap="wrap">{m.text.trim()}</Text></Box>
    </Box>
  )
}

// ─── App Component ────────────────────────────────────────────────────────────
function App() {
  const { exit } = useApp()
  // Restore session from disk if available (enables "continue" after crash)
  const savedSession = loadSession(CWD)
  const initialHistory: ChatMsg[] = savedSession ?? [{ role: 'system', content: SYSTEM }]
  const resuming = savedSession != null && savedSession.length > 1

  const welcomeLines = [
    `Dir: ${CWD}`,
    `Endpoint: ${API_BASE}`,
    `Model: ${MODEL}`,
    `Tools: ${TOOL_COUNT} | Skills: ${JARVIS_SKILL_NAMES}`,
    resuming
      ? `✅ Session restored (${savedSession!.length - 1} messages). Type "continue" or ask a new question.`
      : `Type a prompt, /help for commands, Ctrl+C to abort`,
  ]

  const [msgs,      setMsgs]      = useState<Msg[]>([{
    id: uid++, kind: 'welcome',
    text: welcomeLines.join('\n'),
  }])
  const [inputVal,  setInputVal]  = useState('')
  const [streaming, setStreaming] = useState('')
  const [status,    setStatus]    = useState('')
  const [busy,      setBusy]      = useState(false)
  const history   = useRef<ChatMsg[]>(initialHistory)
  const abortCtrl = useRef<AbortController | null>(null)
  const turnCount = useRef(resuming ? savedSession!.filter(m => m.role === 'user').length : 0)

  const push = (...items: Omit<Msg, 'id'>[]) =>
    setMsgs(prev => [...prev, ...items.map(m => ({ ...m, id: uid++ }))].slice(-MAX_MSGS))

  // ─── Input Handler ───────────────────────────────────────────────────────────
  useInput((ch, key) => {
    if (key.ctrl && ch === 'c') {
      if (busy && abortCtrl.current) {
        abortCtrl.current.abort()
        push({ kind: 'info', text: 'Request aborted.' })
        setBusy(false); setStatus(''); setStreaming('')
        return
      }
      exit(); process.exit(0)
    }
    if (busy) return
    if (key.return)                   { void go(); return }
    if (key.backspace || key.delete)  { setInputVal(v => v.slice(0, -1)); return }
    if (!key.ctrl && !key.meta && !key.escape && ch) setInputVal(v => v + ch)
  })

  // ─── Slash Command Handler ────────────────────────────────────────────────────
  function handleSlash(cmd: string): boolean {
    const normalized = Object.keys(COMMANDS).find(k => k === cmd || COMMANDS[k]?.aliases?.includes(cmd))
    if (!normalized) {
      // Handle /route <query>
      if (cmd.startsWith('/route ')) {
        const query = cmd.slice(7).trim()
        const result = classifyIntent(query)
        const iparams = getInferenceParams(query)
        push({ kind: 'cmd', text: [
          `Route: "${query}"`,
          `  Intent:   ${result.intent} (${(result.confidence * 100).toFixed(0)}% confidence)`,
          `  Tools:    ${result.primaryTools.slice(0, 6).join(' → ')}`,
          `  Thinking: ${iparams.label}  (temp=${iparams.temperature}, reasoning_effort=${iparams.reasoning_effort})`,
          `  Plan first: ${result.shouldPlanFirst} | Parallel: ${result.parallelizable}`,
        ].join('\n') })
        return true
      }
      return false
    }

    switch (normalized) {
      case '/help':
        push({ kind: 'cmd', text: helpText() }); break
      case '/clear':
        history.current = [{ role: 'system', content: SYSTEM }]
        turnCount.current = 0
        clearSession(CWD)
        push({ kind: 'info', text: 'Conversation cleared and session deleted.' }); break
      case '/history': {
        const status_ctx = getContextStatus(history.current)
        push({ kind: 'info', text: [
          `${history.current.length - 1} messages in context (${turnCount.current} turns)`,
          `Estimated tokens: ${status_ctx.estimatedTokens.toLocaleString()} / ~50,000 (${status_ctx.percentFull}% full)`,
          status_ctx.warning || 'Context within safe limits.',
        ].join('\n') }); break
      }
      case '/model':
        push({ kind: 'info', text: `Provider: NVIDIA NIM\nModel:    ${MODEL}\nEndpoint: ${API_BASE}` }); break
      case '/cwd':
        push({ kind: 'info', text: `Working directory: ${CWD}` }); break
      case '/tools':
        push({ kind: 'info', text: getToolSummary() }); break
      case '/skills':
        push({ kind: 'info', text: `Jarvis Skills:\n${getSkillSummary()}` }); break
      case '/cache':
        push({ kind: 'info', text: getCacheStats() }); break
      case '/exit':
        exit(); process.exit(0); break
    }
    return true
  }

  // ─── Main Agentic Loop ────────────────────────────────────────────────────────
  const go = useCallback(async () => {
    const text = inputVal.trim()
    if (!text || busy) return
    setInputVal('')

    if (text.startsWith('/')) {
      if (!handleSlash(text)) push({ kind: 'error', text: `Unknown command: ${text}. Type /help for commands.` })
      return
    }

    setBusy(true)
    push({ kind: 'user', text })

    // ── Inference params: classify intent ONCE from raw user text ──────────────
    // This determines thinking budget for the ENTIRE turn.
    // Builder requests → no thinking (model already has spec memorized).
    // Complex reasoning → full thinking budget.
    const inferenceParams = getInferenceParams(text)

    // ── Build user message — MINIMAL for clear-action requests ────────────────
    // Builder/file tasks: raw text only — system prompt already has all rules.
    // Adding hints like routeHint/policyHint for "write a story" costs 500+ extra
    // tokens the model reads before it can call BuildReport. That's pure latency.
    //
    // Only attach context hints for research/complex multi-step tasks.
    const isBuilderRequest = /\b(deck|pptx|presentation|slides|report|docx?|pdf|dashboard|sheet|excel|xlsx|story|write a|create a|make a|build a)\b/i.test(text)
    const isSimpleFile     = /\b(write|create|save|generate)\b.{0,20}\b(file|script|py|ts|js|md|txt|json|csv)\b/i.test(text)

    let userContent: string
    if (isBuilderRequest || isSimpleFile) {
      // ACT-FIRST path: send only what the model needs to know — the request
      userContent = text
    } else {
      // Research/complex path: attach lightweight context hints
      const snapshot   = summarizeContext(history.current)
      const ctxHint    = buildContextPolicy(history.current)
      const budgetHint = buildTokenBudgetHint(snapshot.estimatedTokens)

      userContent = [
        text,
        buildRoutingPrompt(text),
        snapshot.importantFacts.length > 0 ? `Recent context:\n${snapshot.importantFacts.slice(0, 3).join('\n')}` : '',
        ctxHint,
        budgetHint,
      ].filter(Boolean).join('\n\n')
    }

    history.current.push({ role: 'user', content: userContent })
    turnCount.current++


    const ac = new AbortController()
    abortCtrl.current = ac

    // ── Adaptive RAG: Classify + fire retrieval BEFORE first LLM call ──────────
    // This is the core of the speculative architecture:
    //   1. Classifier runs (< 5ms heuristic, < 300ms LLM fallback)
    //   2. If retrieval needed → fires IMMEDIATELY as a background Promise
    //   3. First LLM call starts streaming SIMULTANEOUSLY
    //   4. When first LLM turn ends → inject retrieval context if additive
    //
    // Key: retrieval and LLM stream are PARALLEL, not sequential.
    // The LLM never blocks waiting for retrieval. Retrieval never delays token 1.

    let retrievalContextToInject: string | null = null

    // Only run classifier + retrieval for non-builder, non-file requests
    // (builders/files are DIRECT_ANSWER — classifier would say skip retrieval anyway,
    //  but we short-circuit here to save even the < 5ms classifier cost)
    if (!isBuilderRequest && !isSimpleFile) {
      try {
        // Stage 1: classify (fast — heuristic covers ~90% in < 1ms)
        const { classifyHeuristic, classifyWithLlm } = await import('./jarvis/adaptiveRag/queryClassifier.js')
        const classification = classifyHeuristic(text) ??
          await classifyWithLlm(text,
            process.env.NVIDIA_TOOL_KEY ?? NVIDIA_API_KEY,
            process.env.NVIDIA_TOOL_MODEL ?? 'nvidia/llama-3.1-nemotron-nano-8b-v1',
            API_BASE,
            ac.signal
          )

        setStatus(`🔍 ${classification.strategy} (conf: ${Math.round(classification.confidence * 100)}%)…`)

        // Stage 2: if retrieval needed → fire NOW, don't await
        if (classification.strategy !== 'DIRECT_ANSWER') {
          const { retrieve, formatRetrievalContext } = await import('./jarvis/adaptiveRag/retrievalOrchestrator.js')
          const { SemanticRetriever } = await import('./jarvis/adaptiveRag/retrievers/semanticRetriever.js')
          const { CodeRetriever }     = await import('./jarvis/adaptiveRag/retrievers/codeRetriever.js')

          const ragConfig = {
            agentApiKey:  NVIDIA_API_KEY,  agentModel: MODEL,   agentBaseUrl: API_BASE,
            toolApiKey:   process.env.NVIDIA_TOOL_KEY ?? NVIDIA_API_KEY,
            toolModel:    process.env.NVIDIA_TOOL_MODEL ?? 'nvidia/llama-3.1-nemotron-nano-8b-v1',
            toolBaseUrl:  API_BASE,
            cwd: CWD, retrievalTimeoutMs: 2000, cragEnabled: true, maxClarifyRounds: 2,
          }

          // Fire retrieval as a background promise — NOT awaited here
          // The agentic loop below starts streaming tokens immediately
          const retrievalPromise = retrieve(
            text, classification,
            new SemanticRetriever(), new CodeRetriever(),
            ragConfig, ac.signal
          )

          // Schedule injection: when retrieval resolves, store context
          // It will be injected into history AFTER the first streaming turn completes
          retrievalPromise.then(result => {
            const ctx = formatRetrievalContext(result)
            if (ctx.trim()) retrievalContextToInject = ctx
          }).catch(() => { /* non-fatal — retrieval is additive, not blocking */ })
        }
      } catch {
        // Classifier/retrieval failure is non-fatal — agent answers from parametric knowledge
      }
    }

    // ── Agentic tool loop ──────────────────────────────────────────────────────
    while (true) {
      let aiText = ''
      setStreaming(''); setStatus(`⚡ ${inferenceParams.label}…`)

      // Auto-summarize + persist before every API call
      const { history: trimmedHistory, warning: ctxWarning } = prepareHistoryForApiCall(history.current, CWD)
      if (trimmedHistory !== history.current) {
        history.current = trimmedHistory
      }
      if (ctxWarning) push({ kind: 'info', text: ctxWarning })

      let tcs: ToolCall[]
      try {
        tcs = await callNvidiaApi(history.current, chunk => {
          aiText += chunk; setStreaming(aiText)
        }, ac.signal, inferenceParams)
      } catch (e: unknown) {
        const err = e as Error
        if (err.name !== 'AbortError') push({ kind: 'error', text: `NVIDIA API Error: ${err.message}` })
        break
      }

      setStreaming('')
      if (aiText.trim()) push({ kind: 'assistant', text: aiText })
      history.current.push({
        role: 'assistant', content: aiText,
        tool_calls: tcs.length ? tcs : undefined,
      })

      // ── Retrieval injection point ──────────────────────────────────────────
      // After first streaming turn: inject retrieval context if it arrived
      // and if this was a final text response (no tool calls).
      // This ensures: (a) user saw speculative tokens immediately,
      //               (b) retrieval context enriches the next turn if needed.
      if (!tcs.length && retrievalContextToInject) {
        const ctx = retrievalContextToInject
        retrievalContextToInject = null  // consume once

        // Only re-prompt if retrieval has information the response didn't cover
        // (rough heuristic: if response is < 200 chars, it was a short answer
        //  that retrieval could meaningfully extend)
        if (aiText.trim().length < 200) {
          history.current.push({
            role: 'user',
            content: `Retrieved context (from background search):\n${ctx}\n\nIf this adds anything not already covered, briefly supplement your answer. Otherwise just confirm.`,
          })
          // Continue the loop for one more enrichment turn
          continue
        } else {
          // Long response already — push retrieval as a trailing note
          push({ kind: 'info', text: `📚 Retrieved context available (${ctx.split('\n').length} lines). Ask a follow-up to use it.` })
        }
      }

      if (!tcs.length) break  // Final textual response — no more tool calls

      // ── Parse all tool calls ───────────────────────────────────────────────
      const parsedCalls = tcs.map(tc => {
        let args: Record<string, unknown> = {}
        try { args = JSON.parse(tc.function.arguments || '{}') } catch {}
        return { tc, fn: tc.function.name, args }
      })

      // Show tool previews
      for (const { tc, fn, args } of parsedCalls) {
        if (ac.signal.aborted) break
        const preview = Object.entries(args).slice(0, 2)
          .map(([k, v]) => { const s = JSON.stringify(v); return `${k}=${s.length > 40 ? s.slice(0, 40) + '…' : s}` })
          .join(' ')
        push({ kind: 'tool_use', text: preview, label: `  ⚙ ${fn}` })
      }

      setStatus(`Running ${parsedCalls.length} tool${parsedCalls.length > 1 ? 's' : ''} ${parsedCalls.length > 1 ? '(parallel)' : ''}…`)

      // ── Execute tools in parallel (capped at 4) ────────────────────────────
      const toolResults = await Promise.all(
        parsedCalls.map(async ({ tc, fn, args }) => {
          if (ac.signal.aborted) return { tc, fn, raw: '(aborted)' }
          const raw = await runTool(fn, args)
          return { tc, fn, raw }
        })
      )

      if (ac.signal.aborted) break

      const results: ChatMsg[] = []
      for (const { tc, fn, raw } of toolResults) {
        const disp = raw.length > MAX_OUT
          ? raw.slice(0, MAX_OUT) + `\n\n… (${(raw.length - MAX_OUT).toLocaleString()} chars truncated)`
          : raw
        push({ kind: 'tool_out', text: disp, label: `  ✓ ${fn}` })
        results.push({ role: 'tool', content: raw, tool_call_id: tc.id, name: fn })
      }

      history.current.push(...results)
      setStatus('')
    }

    abortCtrl.current = null
    setStatus(''); setStreaming(''); setBusy(false)
  }, [inputVal, busy])

  // ─── Render ───────────────────────────────────────────────────────────────────
  return (
    <Box flexDirection="column">
      {msgs.map(m => <MsgLine key={m.id} m={m} />)}

      {streaming &&
        <Box flexDirection="column" marginBottom={1}>
          <Text bold color="greenBright">  JARVIS</Text>
          <Box marginLeft={2}><Text wrap="wrap">{streaming}</Text></Box>
        </Box>}

      {status &&
        <Box marginBottom={0}>
          <Text color="yellow">{`  ${status}`}</Text>
        </Box>}

      <Box borderStyle="round" borderColor={busy ? 'yellow' : 'green'} paddingX={1} marginTop={1}>
        <Text bold color="green">{'> '}</Text>
        <Text>{inputVal}</Text>
        {!busy && <Text color="green">█</Text>}
        {busy  && <Text color="yellow"> …</Text>}
      </Box>

      <Box marginTop={0}>
        <Text color="gray" dimColor>
          {busy
            ? 'Ctrl+C to abort  '
            : 'Enter to send  ·  /help for commands  ·  /skills  ·  /cache  ·  Ctrl+C to exit'}
        </Text>
      </Box>
    </Box>
  )
}

// ─── Pre-flight Checks ────────────────────────────────────────────────────────
if (!NVIDIA_API_KEY) {
  console.error('\n❌ Missing NVIDIA_API_KEY environment variable.')
  console.error('   Set it with: $env:NVIDIA_API_KEY="nvapi-..."\n')
  process.exit(1)
}

const instance = await rawRender(<App />, { exitOnCtrlC: false })
await instance.waitUntilExit()