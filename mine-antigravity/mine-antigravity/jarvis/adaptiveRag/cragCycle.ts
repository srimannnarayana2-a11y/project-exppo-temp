/**
 * jarvis/adaptiveRag/cragCycle.ts
 *
 * Corrective RAG (CRAG) — Relevance grading + correction cycles.
 *
 * After retrieval, grades each document 0-5 using Key 4 (no thinking).
 * If average grade < threshold → reformulate query → re-retrieve (max 1 cycle).
 * If still low → fall back to web search (WebSearch tool).
 *
 * This runs on Key 4 (thinking OFF) to minimize latency impact.
 * Target: < 200ms per grade batch (cheap LLM, no thinking, 10 tokens per doc).
 */

import type { RetrievedDoc, AdaptiveRagConfig } from './types.js'

const GRADE_THRESHOLD = 2.5   // below this → reformulate
const MIN_DOCS_TO_GRADE = 1   // don't grade if retrieval returned nothing

/**
 * Grade retrieved documents for relevance to the query.
 * Uses Key 4 (thinking OFF) for speed.
 * Returns 0-5 score per document.
 */
export async function gradeDocuments(
  query: string,
  docs: RetrievedDoc[],
  config: AdaptiveRagConfig,
  signal?: AbortSignal
): Promise<RetrievedDoc[]> {
  if (docs.length < MIN_DOCS_TO_GRADE) return docs

  // Grade all docs in a single batch call (1 LLM call, not N calls)
  const docList = docs.slice(0, 5).map((d, i) =>
    `[${i}] ${d.content.slice(0, 200)}`
  ).join('\n\n')

  const prompt = `Grade each document's relevance to the query. Reply with ONLY a JSON array of scores 0-5.
0=completely irrelevant, 3=somewhat relevant, 5=perfectly answers the query.

Query: "${query.slice(0, 150)}"

Documents:
${docList}

Reply (JSON array only, length ${Math.min(docs.length, 5)}): [score, score, ...]`

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
        temperature: 0,
        max_tokens: 30,
        stream: false,
      }),
      signal,
    })

    if (resp.ok) {
      const data = await resp.json() as { choices?: Array<{ message?: { content?: string } }> }
      const text = data.choices?.[0]?.message?.content?.trim() ?? ''
      const match = text.match(/\[[\d,\s.]+\]/)
      if (match) {
        const scores = JSON.parse(match[0]) as number[]
        return docs.map((doc, i) => ({
          ...doc,
          gradedScore: scores[i] ?? doc.score * 5,
        }))
      }
    }
  } catch { /* non-fatal — use raw scores */ }

  // Fallback: use raw retriever scores * 5
  return docs.map(doc => ({ ...doc, gradedScore: doc.score * 5 }))
}

/**
 * Reformulate a query that returned low-relevance documents.
 * Uses Key 4 to generate a better search query.
 */
export async function reformulateQuery(
  originalQuery: string,
  failedDocs: RetrievedDoc[],
  config: AdaptiveRagConfig,
  signal?: AbortSignal
): Promise<string> {
  const failedSample = failedDocs.slice(0, 2).map(d => d.content.slice(0, 100)).join(' | ')

  const prompt = `The following search query returned irrelevant results. Reformulate it to be more specific and targeted.

Original query: "${originalQuery}"
Sample of irrelevant results: "${failedSample}"

Reply with ONLY the reformulated query (one line, no quotes):`

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
        max_tokens: 80,
        stream: false,
      }),
      signal,
    })

    if (resp.ok) {
      const data = await resp.json() as { choices?: Array<{ message?: { content?: string } }> }
      const reformulated = data.choices?.[0]?.message?.content?.trim()
      if (reformulated && reformulated.length > 5) return reformulated
    }
  } catch { /* fallback */ }

  // Fallback: append "specifically" to be more targeted
  return `${originalQuery} specifically technical details`
}

/**
 * Main CRAG orchestrator:
 * Grade → if low → reformulate → signal caller to re-retrieve.
 *
 * Returns { docs, reformulatedQuery } where reformulatedQuery is non-null
 * if a correction cycle should happen.
 */
export async function runCragCycle(
  query: string,
  docs: RetrievedDoc[],
  config: AdaptiveRagConfig,
  signal?: AbortSignal
): Promise<{ docs: RetrievedDoc[]; reformulatedQuery: string | null }> {
  // gradeDocuments already returns early if docs.length < MIN_DOCS_TO_GRADE
  if (!config.cragEnabled) return { docs, reformulatedQuery: null }

  // Grade all docs
  const graded = await gradeDocuments(query, docs, config, signal)

  // Average grade across graded docs only
  const gradedWithScores = graded.filter(d => d.gradedScore != null)
  if (gradedWithScores.length === 0) return { docs: graded, reformulatedQuery: null }

  const avg = gradedWithScores.reduce((s, d) => s + d.gradedScore!, 0) / gradedWithScores.length

  if (avg >= GRADE_THRESHOLD) {
    // Good enough — return sorted by grade
    return {
      docs: graded.sort((a, b) => (b.gradedScore ?? 0) - (a.gradedScore ?? 0)),
      reformulatedQuery: null,
    }
  }

  // Low relevance — reformulate query for a correction cycle
  const reformulatedQuery = await reformulateQuery(query, graded, config, signal)
  return { docs: graded, reformulatedQuery }
}
