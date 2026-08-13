/**
 * jarvis/adaptiveRag/informationGainClarifier.ts
 *
 * EIG-based "20 Questions" clarifier.
 *
 * Instead of asking the "next logical question", asks the question that
 * eliminates the MOST possible intents — Bayesian Expected Information Gain.
 *
 * Research basis:
 *   - "Entropy-reduction question selection outperforms baseline questioning"
 *   - Gains flatten after 2-3 questions — hard cap at 2 rounds
 *   - 1 well-chosen question beats 4 naive questions (Bayesian EIG paper)
 *
 * Runs on Key 4 (no thinking) — it's a structured generation task, not reasoning.
 * Target: < 300ms to generate and score candidate questions.
 */

import type { ClarifyingQuestion, ClassificationResult, AdaptiveRagConfig } from './types.js'

const MAX_CLARIFY_ROUNDS = 2

// ─── Question Generation ──────────────────────────────────────────────────────

/**
 * Generate candidate clarifying questions and score them by EIG.
 * Returns the top-scoring question to ask.
 * Uses Key 4 (no thinking, fast structured output).
 */
export async function generateClarifyingQuestion(
  query: string,
  classification: ClassificationResult,
  conversationSoFar: string[],   // previous questions already asked
  config: AdaptiveRagConfig,
  signal?: AbortSignal
): Promise<ClarifyingQuestion | null> {
  // Hard cap — don't over-clarify
  if (conversationSoFar.length >= MAX_CLARIFY_ROUNDS) return null

  const candidateIntents = classification.candidateIntents.length > 0
    ? classification.candidateIntents
    : ['code implementation', 'conceptual explanation', 'file/doc generation', 'debugging']

  const prevQs = conversationSoFar.length > 0
    ? `\nAlready asked: ${conversationSoFar.join(' | ')}`
    : ''

  const prompt = `You are a clarification agent. Generate 3 candidate questions to resolve the ambiguity in this user query.
IMPORTANT: Each question must eliminate the maximum number of possible intents. Like the "20 questions" game — one good question collapses many possibilities.

User query: "${query.slice(0, 200)}"
Possible intents: ${candidateIntents.join(', ')}${prevQs}

For each candidate question, estimate how many intents it eliminates if answered "yes" (EIG score, 0-10).

Reply with ONLY this JSON (no other text):
{
  "candidates": [
    {"question": "...", "eig": 8, "eliminates": ["intent1", "intent2"]},
    {"question": "...", "eig": 6, "eliminates": ["intent3"]},
    {"question": "...", "eig": 5, "eliminates": ["intent4", "intent5"]}
  ]
}`

  try {
    const resp = await fetch(`${config.toolBaseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${config.toolApiKey}`,
      },
      body: JSON.stringify({
        model: config.toolModel,
        messages: [{ role: 'user', content: prompt }],
        temperature: 0.3,
        max_tokens: 300,
        stream: false,
      }),
      signal,
    })

    if (!resp.ok) return fallbackQuestion(query, candidateIntents)

    const data = await resp.json() as { choices?: Array<{ message?: { content?: string } }> }
    const text = data.choices?.[0]?.message?.content?.trim() ?? ''
    const match = text.match(/\{[\s\S]+\}/)
    if (!match) return fallbackQuestion(query, candidateIntents)

    const parsed = JSON.parse(match[0]) as {
      candidates?: Array<{ question: string; eig: number; eliminates: string[] }>
    }

    const candidates = parsed.candidates ?? []
    if (candidates.length === 0) return fallbackQuestion(query, candidateIntents)

    // Pick highest EIG candidate
    const best = candidates.reduce((a, b) => a.eig >= b.eig ? a : b)
    return {
      question: best.question,
      eigScore: best.eig / 10,   // normalize to 0-1
      eliminatedIntents: best.eliminates ?? [],
    }
  } catch {
    return fallbackQuestion(query, candidateIntents)
  }
}

/** Fallback if LLM call fails — ask a generic but useful question */
function fallbackQuestion(query: string, intents: string[]): ClarifyingQuestion {
  const q = intents.includes('code implementation')
    ? 'Are you looking for code implementation, or a conceptual explanation?'
    : 'Should I search existing documents/files, or answer from general knowledge?'

  return {
    question: q,
    eigScore: 0.5,
    eliminatedIntents: intents.slice(0, Math.ceil(intents.length / 2)),
  }
}

// ─── Answer Processing ────────────────────────────────────────────────────────

/**
 * Update classification based on the user's answer to a clarifying question.
 * Returns an updated ClassificationResult with narrowed strategy.
 */
export function processClarification(
  answer: string,
  previousClassification: ClassificationResult,
  question: ClarifyingQuestion
): ClassificationResult {
  const a = answer.toLowerCase().trim()

  // Simple heuristics on the answer content
  const codeSignals    = /\bcode|implement|function|class|file|script\b/.test(a)
  const semanticSignals = /\bexplain|concept|theory|what|why|overview\b/.test(a)
  const docSignals     = /\bpptx|report|deck|doc|dashboard|sheet|excel\b/.test(a)
  const noRetrievalSignals = /\bgeneral|basic|simple|just tell me|don't search\b/.test(a)

  if (docSignals) {
    return {
      ...previousClassification,
      strategy: 'DIRECT_ANSWER',
      confidence: 0.92,
      needsClarification: false,
      reason: 'Clarification confirmed: document generation task',
    }
  }
  if (noRetrievalSignals) {
    return {
      ...previousClassification,
      strategy: 'DIRECT_ANSWER',
      confidence: 0.85,
      needsClarification: false,
      reason: 'User indicated no retrieval needed',
    }
  }
  if (codeSignals && !semanticSignals) {
    return {
      ...previousClassification,
      strategy: 'CODE_ONLY',
      confidence: 0.88,
      codeWeight: 0.9,
      semanticWeight: 0.1,
      needsClarification: false,
      reason: 'Clarification confirmed: code task',
    }
  }
  if (semanticSignals && !codeSignals) {
    return {
      ...previousClassification,
      strategy: 'SEMANTIC_ONLY',
      confidence: 0.88,
      semanticWeight: 1.0,
      codeWeight: 0,
      needsClarification: false,
      reason: 'Clarification confirmed: conceptual/factual task',
    }
  }

  // Partial information — reduce ambiguity slightly, proceed with hybrid
  return {
    ...previousClassification,
    confidence: Math.min(previousClassification.confidence + 0.2, 0.85),
    needsClarification: false,
    reason: 'Post-clarification: proceeding with hybrid retrieval',
  }
}
