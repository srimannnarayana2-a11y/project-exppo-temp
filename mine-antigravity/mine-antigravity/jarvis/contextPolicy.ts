/**
 * jarvis/contextPolicy.ts — Rolling Context Manager (v2)
 *
 * Upgrades over v1:
 *  - buildContextPolicy is much more compact to save tokens
 *  - summarizeContext extracts proper facts vs. noise
 *  - Adds token budget estimation
 *  - Rolling summary truncates old turns intelligently
 */

export interface ContextSnapshot {
  summary: string
  importantFacts: string[]
  openQuestions: string[]
  nextActions: string[]
  estimatedTokens: number
}

// ─── Rolling Context Builder ──────────────────────────────────────────────────

export function buildContextPolicy(
  history: Array<{ role: string; content: string }>
): string {
  if (history.length <= 2) return '' // Just system + 1st user, no context needed

  // Keep only the last 6 turns to minimize token waste
  const recent = history.slice(-6).filter(t => t.role !== 'system')
  if (recent.length === 0) return ''

  const lines = recent.map(t => {
    const snippet = t.content.slice(0, 160).replace(/\n+/g, ' ').trim()
    return `${t.role === 'user' ? '▷' : t.role === 'tool' ? '⚙' : '◈'} ${snippet}`
  })

  return `Recent context:\n${lines.join('\n')}`
}

// ─── Context Summarizer ───────────────────────────────────────────────────────

export function summarizeContext(
  history: Array<{ role: string; content: string }>
): ContextSnapshot {
  const userTurns = history.filter(t => t.role === 'user').slice(-5)
  const toolOutputs = history.filter(t => t.role === 'tool').slice(-6)
  const assistantTurns = history.filter(t => t.role === 'assistant').slice(-3)

  // Extract facts from tool outputs
  const importantFacts: string[] = []
  for (const t of toolOutputs) {
    const content = t.content.slice(0, 300)
    if (content.includes('ERROR') || content.includes('failed')) {
      importantFacts.push(`⚠ Error: ${content.slice(0, 120)}`)
    } else if (content.length > 30) {
      importantFacts.push(`✓ ${content.slice(0, 120).replace(/\n/g, ' ')}`)
    }
  }

  // Last user goal
  const lastUserGoal = userTurns.at(-1)?.content.slice(0, 200) ?? 'No active goal.'

  // Last assistant action
  const lastAction = assistantTurns.at(-1)?.content.slice(0, 160).replace(/\n/g, ' ') ?? ''

  // Rough token estimate (1 token ≈ 4 chars)
  const totalChars = history.reduce((sum, t) => sum + t.content.length, 0)
  const estimatedTokens = Math.round(totalChars / 4)

  return {
    summary: lastAction ? `Last action: ${lastAction}` : `Working on: ${lastUserGoal}`,
    importantFacts: importantFacts.slice(0, 5),
    openQuestions: [],
    nextActions: ['Continue with the next highest-value step.', 'Verify and summarize the result.'],
    estimatedTokens,
  }
}

// ─── Token Budget String ──────────────────────────────────────────────────────

export function buildTokenBudgetHint(estimatedTokens: number): string {
  if (estimatedTokens < 20_000) return ''
  if (estimatedTokens < 60_000) return 'Context: moderate. Prefer concise tool outputs.'
  return 'Context: large. Minimize tool call count. Use targeted reads (start_line/end_line). Summarize aggressively.'
}
