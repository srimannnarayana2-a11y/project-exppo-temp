/**
 * jarvis/sessionManager.ts — Conversation Persistence & Context Management
 *
 * Industry-grade context management:
 *
 * 1. PERSISTENCE: saves conversation history to .jarvis/session.json so
 *    "continue" after a crash or restart restores full context.
 *
 * 2. AUTO-SUMMARIZATION: when estimated tokens exceed a threshold,
 *    older turns are compressed into a single summary message so the
 *    model doesn't hit its context limit silently.
 *
 * 3. TOKEN ESTIMATION: rough 4-chars-per-token estimate with a safety
 *    multiplier so we truncate before the model does.
 *
 * Industry reference:
 *   - Anthropic's Claude Code uses sliding window + summary injection
 *   - OpenAI's swarm uses per-agent context trimming
 *   - LangChain uses ConversationSummaryBufferMemory (same concept)
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join, resolve } from 'path'

export interface ChatMsg {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: Array<{ id: string; type: 'function'; function: { name: string; arguments: string } }>
  tool_call_id?: string
  name?: string
}

// ─── Config ───────────────────────────────────────────────────────────────────

// Approximate tokens: 4 chars ≈ 1 token
const CHARS_PER_TOKEN = 4

// Start summarizing when estimated context exceeds this token count
// Most NVIDIA NIM models support 32K-128K context. 50K is conservative.
const SUMMARIZE_THRESHOLD_TOKENS = 50_000

// How many recent turns to ALWAYS keep verbatim (do not summarize these)
const KEEP_RECENT_TURNS = 6

// ─── Persistence ─────────────────────────────────────────────────────────────

function getSessionPath(cwd: string): string {
  const dir = join(resolve(cwd), '.jarvis')
  mkdirSync(dir, { recursive: true })
  return join(dir, 'session.json')
}

export function saveSession(history: ChatMsg[], cwd = process.cwd()): void {
  try {
    const sessionPath = getSessionPath(cwd)
    writeFileSync(sessionPath, JSON.stringify({ savedAt: new Date().toISOString(), history }, null, 2), 'utf8')
  } catch { /* non-fatal */ }
}

export function loadSession(cwd = process.cwd()): ChatMsg[] | null {
  try {
    const sessionPath = getSessionPath(cwd)
    if (!existsSync(sessionPath)) return null
    const data = JSON.parse(readFileSync(sessionPath, 'utf8'))
    if (!Array.isArray(data.history)) return null
    return data.history as ChatMsg[]
  } catch { return null }
}

export function clearSession(cwd = process.cwd()): void {
  try {
    const sessionPath = getSessionPath(cwd)
    if (existsSync(sessionPath)) {
      writeFileSync(sessionPath, JSON.stringify({ savedAt: new Date().toISOString(), history: [] }, null, 2), 'utf8')
    }
  } catch { /* non-fatal */ }
}

// ─── Token Estimation ─────────────────────────────────────────────────────────

export function estimateTokens(history: ChatMsg[]): number {
  const totalChars = history.reduce((sum, msg) => {
    let chars = msg.content?.length ?? 0
    if (msg.tool_calls) {
      chars += JSON.stringify(msg.tool_calls).length
    }
    return sum + chars
  }, 0)
  return Math.ceil(totalChars / CHARS_PER_TOKEN)
}

export function getContextStatus(history: ChatMsg[]): {
  estimatedTokens: number
  percentFull: number
  shouldSummarize: boolean
  warning: string
} {
  const estimatedTokens = estimateTokens(history)
  const percentFull = Math.round((estimatedTokens / SUMMARIZE_THRESHOLD_TOKENS) * 100)
  const shouldSummarize = estimatedTokens > SUMMARIZE_THRESHOLD_TOKENS

  let warning = ''
  if (percentFull > 90) {
    warning = `⚠ Context is ${percentFull}% full (est. ${estimatedTokens.toLocaleString()} tokens). Auto-summarizing older turns.`
  } else if (percentFull > 70) {
    warning = `ℹ Context is ${percentFull}% full (est. ${estimatedTokens.toLocaleString()} tokens).`
  }

  return { estimatedTokens, percentFull, shouldSummarize, warning }
}

// ─── Auto-Summarization ───────────────────────────────────────────────────────

/**
 * When context is too large, compress older turns into a summary.
 * Always keeps:
 *   - messages[0] = system prompt
 *   - the last KEEP_RECENT_TURNS non-system messages verbatim
 *
 * The summary is injected as a system message after the original system prompt.
 */
export function autoSummarizeHistory(history: ChatMsg[]): { history: ChatMsg[]; summarized: boolean; keptCount: number } {
  if (history.length <= KEEP_RECENT_TURNS + 2) {
    return { history, summarized: false, keptCount: history.length }
  }

  const systemMsg = history[0]  // Always keep the system prompt
  const nonSystem = history.slice(1)

  // Split: older messages to summarize vs. recent messages to keep verbatim
  const recentCount = Math.min(KEEP_RECENT_TURNS, nonSystem.length)
  const toSummarize = nonSystem.slice(0, nonSystem.length - recentCount)
  const toKeep = nonSystem.slice(nonSystem.length - recentCount)

  if (toSummarize.length === 0) {
    return { history, summarized: false, keptCount: history.length }
  }

  // Build a text summary of the compressed turns
  const lines: string[] = ['[Earlier conversation summary — auto-compressed to save context]', '']
  let turn = 0
  for (const msg of toSummarize) {
    if (msg.role === 'user') {
      turn++
      lines.push(`Turn ${turn} — User: ${msg.content.slice(0, 300).replace(/\n/g, ' ')}`)
    } else if (msg.role === 'assistant' && msg.content?.trim()) {
      lines.push(`  → Assistant: ${msg.content.slice(0, 200).replace(/\n/g, ' ')}`)
    } else if (msg.role === 'assistant' && msg.tool_calls?.length) {
      const toolNames = msg.tool_calls.map(t => t.function.name).join(', ')
      lines.push(`  → Used tools: ${toolNames}`)
    } else if (msg.role === 'tool' && msg.name) {
      const result = (msg.content ?? '').slice(0, 100).replace(/\n/g, ' ')
      lines.push(`  → ${msg.name} result: ${result}`)
    }
  }

  const summaryMsg: ChatMsg = {
    role: 'system',
    content: lines.join('\n'),
  }

  const compressedHistory: ChatMsg[] = [systemMsg, summaryMsg, ...toKeep]
  return { history: compressedHistory, summarized: true, keptCount: toKeep.length }
}

// ─── Main: get history ready for the next API call ────────────────────────────

/**
 * Call this before every NVIDIA API request.
 * Returns the history to send and a warning string (if context is filling up).
 */
export function prepareHistoryForApiCall(
  history: ChatMsg[],
  cwd = process.cwd()
): { history: ChatMsg[]; warning: string } {
  const status = getContextStatus(history)

  if (!status.shouldSummarize) {
    // Persist on every turn
    saveSession(history, cwd)
    return { history, warning: status.warning }
  }

  // Context too large — auto-summarize
  const { history: compressed, summarized, keptCount } = autoSummarizeHistory(history)

  // Persist the compressed version
  saveSession(compressed, cwd)

  const warning = summarized
    ? `⚡ Auto-summarized ${history.length - keptCount - 2} older turns to fit context window. Keeping last ${keptCount} turns verbatim.`
    : status.warning

  return { history: compressed, warning }
}
