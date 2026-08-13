/**
 * jarvis/adaptiveRag/queryClassifier.ts
 *
 * Fast two-stage query classifier:
 *   Stage 1: Regex/heuristic (< 1ms) — catches obvious cases
 *   Stage 2: Cheap LLM call on Key 4 (no thinking) — for ambiguous cases
 *
 * Returns a RetrievalStrategy + confidence in < 5ms for 90% of queries.
 */

import type { ClassificationResult } from './types.js'

// ─── Stage 1: Fast Heuristic Patterns ────────────────────────────────────────

// Builder requests — model has full spec memorized, never needs retrieval
const BUILDER_RE = /\b(deck|pptx|presentation|slides|pitch deck|report|docx?|word doc|write up|pdf|dashboard|kpi board|analytics|spreadsheet|excel|xlsx|sheet|csv)\b/i

// LLM clearly knows this — common knowledge, basic CS, math
const DIRECT_RE = /\b(fibonacci|factorial|fizzbuzz|hello world|sorting|binary search|big.?o|time complexity|merge sort|quicksort|linked list|stack|queue|hash map|how to print|syntax for)\b/i

// Strong code signals
const CODE_RE = /\b(function|method|class|module|import|export|api|endpoint|bug|error|exception|crash|debug|refactor|implement|compile|runtime|namespace|interface|generic|async|await|promise)\b/i

// Strong semantic/conceptual signals
const SEMANTIC_RE = /\b(what is|explain|describe|how does|why does|when did|who is|concept|theory|overview|research|paper|article|history|background|difference between|compare|pros and cons)\b/i

// Signals that indicate fresh/real-time data needed
const FRESH_DATA_RE = /\b(latest|current|today|2024|2025|2026|recent|news|update|release|version|new features|changelog)\b/i

/**
 * Stage 1: Pure heuristic, no LLM, < 1ms.
 * Returns null if uncertain (triggers Stage 2).
 */
export function classifyHeuristic(query: string): ClassificationResult | null {
  const q = query.trim()

  // Builder — always DIRECT_ANSWER (schema in system prompt, no retrieval)
  if (BUILDER_RE.test(q)) {
    return {
      strategy: 'DIRECT_ANSWER',
      confidence: 0.97,
      semanticWeight: 0,
      codeWeight: 0,
      needsClarification: false,
      candidateIntents: ['build_document'],
      reason: 'Builder request — schema is baked into system prompt',
    }
  }

  // Very short query — high ambiguity, clarify + retrieve in parallel
  const wordCount = q.split(/\s+/).filter(Boolean).length
  if (wordCount <= 3) {
    return {
      strategy: 'CLARIFY_AND_RETRIEVE',
      confidence: 0.5,
      semanticWeight: 0.5,
      codeWeight: 0.5,
      needsClarification: true,
      candidateIntents: ['code_question', 'concept_question', 'factual_lookup'],
      reason: 'Query too short to determine intent',
    }
  }

  // LLM clearly knows this
  if (DIRECT_RE.test(q) && !FRESH_DATA_RE.test(q)) {
    return {
      strategy: 'DIRECT_ANSWER',
      confidence: 0.88,
      semanticWeight: 0,
      codeWeight: 0,
      needsClarification: false,
      candidateIntents: ['general_knowledge'],
      reason: 'Common knowledge — LLM parametric knowledge sufficient',
    }
  }

  const hasCode     = CODE_RE.test(q)
  const hasSemantic = SEMANTIC_RE.test(q)
  const hasFresh    = FRESH_DATA_RE.test(q)

  // Pure code question
  if (hasCode && !hasSemantic && !hasFresh) {
    return {
      strategy: 'CODE_ONLY',
      confidence: 0.82,
      semanticWeight: 0.1,
      codeWeight: 0.9,
      needsClarification: false,
      candidateIntents: ['code_question'],
      reason: 'Strong code signals, no conceptual/fresh-data signals',
    }
  }

  // Pure semantic/conceptual question
  if (hasSemantic && !hasCode && !hasFresh) {
    return {
      strategy: 'SEMANTIC_ONLY',
      confidence: 0.82,
      semanticWeight: 1.0,
      codeWeight: 0,
      needsClarification: false,
      candidateIntents: ['concept_question'],
      reason: 'Conceptual/factual question — semantic retriever only',
    }
  }

  // Mixed signals or fresh data needed
  if (hasCode && (hasSemantic || hasFresh)) {
    return {
      strategy: 'HYBRID',
      confidence: 0.72,
      semanticWeight: hasFresh ? 0.7 : 0.5,
      codeWeight: hasFresh ? 0.3 : 0.5,
      needsClarification: false,
      candidateIntents: ['code_question', 'concept_question'],
      reason: 'Mixed code+semantic signals — hybrid retrieval',
    }
  }

  // Fresh data needed, no clear code/semantic split
  if (hasFresh) {
    return {
      strategy: 'SEMANTIC_ONLY',
      confidence: 0.75,
      semanticWeight: 1.0,
      codeWeight: 0,
      needsClarification: false,
      candidateIntents: ['factual_lookup'],
      reason: 'Fresh/real-time data required — semantic search',
    }
  }

  // Uncertain — fall through to Stage 2
  return null
}

/**
 * Stage 2: Cheap LLM classification on Key 4 (thinking OFF, 50 tokens max).
 * Only called when Stage 1 returns null.
 */
export async function classifyWithLlm(
  query: string,
  toolApiKey: string,
  toolModel: string,
  toolBaseUrl: string,
  signal?: AbortSignal
): Promise<ClassificationResult> {
  const PROMPT = `Classify this user query into EXACTLY ONE category. Reply with only the category name and a confidence 0-100.

Categories:
DIRECT_ANSWER   - LLM general knowledge is sufficient (math, common facts, basic syntax)
SEMANTIC_ONLY   - Needs conceptual/web search (papers, news, explanations)
CODE_ONLY       - Needs code search (functions, APIs, implementations)
HYBRID          - Needs both code and semantic search
CLARIFY_FIRST   - Intent is too ambiguous to answer without a question

Query: "${query.slice(0, 200)}"

Reply format (JSON only, no other text):
{"category":"CATEGORY_NAME","confidence":85}`

  try {
    const resp = await fetch(`${toolBaseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${toolApiKey}`,
      },
      body: JSON.stringify({
        model: toolModel,
        messages: [{ role: 'user', content: PROMPT }],
        temperature: 0,
        max_tokens: 60,
        stream: false,
      }),
      signal,
    })

    if (resp.ok) {
      const data = await resp.json() as { choices?: Array<{ message?: { content?: string } }> }
      const text = data.choices?.[0]?.message?.content?.trim() ?? ''
      const match = text.match(/\{[^}]+\}/)
      if (match) {
        const parsed = JSON.parse(match[0]) as { category?: string; confidence?: number }
        const category = parsed.category as string
        const confidence = (parsed.confidence ?? 50) / 100
        return buildResult(category, confidence, query)
      }
    }
  } catch {
    // Fall through to default
  }

  // Default if LLM call fails
  return {
    strategy: 'HYBRID',
    confidence: 0.5,
    semanticWeight: 0.6,
    codeWeight: 0.4,
    needsClarification: false,
    candidateIntents: [],
    reason: 'LLM classification failed — using hybrid default',
  }
}

function buildResult(category: string, confidence: number, query: string): ClassificationResult {
  switch (category) {
    case 'DIRECT_ANSWER':
      return { strategy: 'DIRECT_ANSWER', confidence, semanticWeight: 0, codeWeight: 0, needsClarification: false, candidateIntents: [], reason: 'LLM classified as direct answer' }
    case 'SEMANTIC_ONLY':
      return { strategy: 'SEMANTIC_ONLY', confidence, semanticWeight: 1, codeWeight: 0, needsClarification: false, candidateIntents: ['concept_question'], reason: 'LLM classified as semantic' }
    case 'CODE_ONLY':
      return { strategy: 'CODE_ONLY', confidence, semanticWeight: 0.1, codeWeight: 0.9, needsClarification: false, candidateIntents: ['code_question'], reason: 'LLM classified as code' }
    case 'CLARIFY_FIRST':
      return { strategy: 'CLARIFY_FIRST', confidence, semanticWeight: 0.5, codeWeight: 0.5, needsClarification: true, candidateIntents: [], reason: 'LLM flagged as ambiguous' }
    default:  // HYBRID
      return { strategy: 'HYBRID', confidence, semanticWeight: 0.5, codeWeight: 0.5, needsClarification: false, candidateIntents: [], reason: 'LLM classified as hybrid' }
  }
}

/**
 * Main entry: classify a query as fast as possible.
 * Falls back to LLM only when heuristic is uncertain.
 */
export async function classifyQuery(
  query: string,
  toolApiKey: string,
  toolModel: string,
  toolBaseUrl: string,
  signal?: AbortSignal
): Promise<ClassificationResult> {
  const heuristic = classifyHeuristic(query)
  if (heuristic !== null) return heuristic

  // Only hit the LLM if confidence is genuinely uncertain
  return classifyWithLlm(query, toolApiKey, toolModel, toolBaseUrl, signal)
}
