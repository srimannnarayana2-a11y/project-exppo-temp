/**
 * jarvis/adaptiveRag/retrievalOrchestrator.ts
 *
 * Parallel multi-source retrieval with:
 *   - Both retrievers fire simultaneously (Promise.allSettled)
 *   - Hard timeout per retriever (no single slow source blocks everything)
 *   - Weighted merge + deduplication
 *   - CRAG correction cycle (max 1 round)
 *   - AbortSignal propagation
 *
 * Total target latency: < 800ms including CRAG
 */

import type { RetrievedDoc, RetrievalResult, ClassificationResult, AdaptiveRagConfig, IRetriever } from './types.js'
import { runCragCycle } from './cragCycle.js'

const DEFAULT_TIMEOUT_MS = 2000
const MAX_DOCS_PER_SOURCE = 5
const MAX_TOTAL_DOCS = 8

// ─── Timeout Utility ─────────────────────────────────────────────────────────

// Timer that auto-clears on success to prevent leaks
function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timerId: ReturnType<typeof setTimeout>
  const timeout = new Promise<never>((_, reject) => {
    timerId = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms)
  })
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timerId!))
}

// ─── Merge + Dedup ────────────────────────────────────────────────────────────

/**
 * Merge results from multiple sources, apply weights, deduplicate by content.
 * Higher weight = source's score matters more in final ranking.
 */
function mergeResults(
  results: Array<{ docs: RetrievedDoc[]; weight: number }>
): RetrievedDoc[] {
  const seen = new Set<string>()
  const merged: RetrievedDoc[] = []

  for (const { docs, weight } of results) {
    for (const doc of docs) {
      // Dedup by content fingerprint (first 100 chars)
      const fp = doc.content.slice(0, 100).toLowerCase().replace(/\s+/g, ' ')
      if (seen.has(fp)) continue
      seen.add(fp)
      merged.push({ ...doc, score: doc.score * weight })
    }
  }

  // Sort by weighted score, descending
  return merged
    .sort((a, b) => b.score - a.score)
    .slice(0, MAX_TOTAL_DOCS)
}

// ─── Core Retrieval ───────────────────────────────────────────────────────────

/**
 * Fire all applicable retrievers in parallel.
 * Returns merged results as soon as ALL complete or timeout.
 */
async function retrieveParallel(
  query: string,
  classification: ClassificationResult,
  semanticRetriever: IRetriever,
  codeRetriever: IRetriever,
  timeoutMs: number,
  signal?: AbortSignal
): Promise<Array<{ docs: RetrievedDoc[]; weight: number }>> {
  const tasks: Promise<{ docs: RetrievedDoc[]; weight: number }>[] = []

  if (classification.semanticWeight > 0) {
    const p = withTimeout(
      semanticRetriever.query(query, {
        weight: classification.semanticWeight,
        maxDocs: MAX_DOCS_PER_SOURCE,
        signal,
      }),
      timeoutMs,
      'SemanticRetriever'
    ).then(docs => ({ docs, weight: classification.semanticWeight }))
    tasks.push(p)
  }

  if (classification.codeWeight > 0) {
    const p = withTimeout(
      codeRetriever.query(query, {
        weight: classification.codeWeight,
        maxDocs: MAX_DOCS_PER_SOURCE,
        signal,
      }),
      timeoutMs,
      'CodeRetriever'
    ).then(docs => ({ docs, weight: classification.codeWeight }))
    tasks.push(p)
  }

  // allSettled: we get whatever completes — no hard failure if one source dies
  const settled = await Promise.allSettled(tasks)

  return settled
    .filter((r): r is PromiseFulfilledResult<{ docs: RetrievedDoc[]; weight: number }> =>
      r.status === 'fulfilled'
    )
    .map(r => r.value)
}

// ─── Main Entry ───────────────────────────────────────────────────────────────

/**
 * Orchestrate full retrieval:
 *   1. Fire semantic + code retrievers in parallel
 *   2. Merge + dedup results
 *   3. Run CRAG grading
 *   4. If low quality → reformulate → re-retrieve once
 *   5. Return final docs
 */
export async function retrieve(
  query: string,
  classification: ClassificationResult,
  semanticRetriever: IRetriever,
  codeRetriever: IRetriever,
  config: AdaptiveRagConfig,
  signal?: AbortSignal
): Promise<RetrievalResult> {
  const startMs = Date.now()
  const timeoutMs = config.retrievalTimeoutMs ?? DEFAULT_TIMEOUT_MS

  // Skip retrieval entirely for direct answers
  if (classification.strategy === 'DIRECT_ANSWER') {
    return { docs: [], strategy: 'DIRECT_ANSWER', latencyMs: 0, corrected: false }
  }

  // ── Round 1: Parallel retrieval ────────────────────────────────────────────
  const round1Sources = await retrieveParallel(
    query, classification, semanticRetriever, codeRetriever, timeoutMs, signal
  )
  const round1Docs = mergeResults(round1Sources)

  // ── CRAG: Grade + maybe reformulate ───────────────────────────────────────
  const { docs: gradedDocs, reformulatedQuery } = await runCragCycle(
    query, round1Docs, config, signal
  )

  // ── Round 2 (if CRAG says reformulate): single correction cycle ───────────
  if (reformulatedQuery && !signal?.aborted) {
    const reformulatedClass: ClassificationResult = {
      ...classification,
      // Widen search slightly on reformulation
      semanticWeight: Math.max(classification.semanticWeight, 0.4),
      codeWeight: Math.max(classification.codeWeight, 0.2),
    }

    const round2Sources = await retrieveParallel(
      reformulatedQuery, reformulatedClass,
      semanticRetriever, codeRetriever,
      Math.min(timeoutMs, 1000),  // tighter timeout for correction round
      signal
    )
    // Merge round1 + round2 docs into a flat weighted list
    // Both source arrays have same structure: { docs[], weight }
    const allSources = [...round1Sources, ...round2Sources]
    const round2Docs = mergeResults(allSources)

    return {
      docs: round2Docs,
      strategy: classification.strategy,
      latencyMs: Date.now() - startMs,
      corrected: true,
      correctionQuery: reformulatedQuery,
    }
  }

  return {
    docs: gradedDocs,
    strategy: classification.strategy,
    latencyMs: Date.now() - startMs,
    corrected: false,
  }
}

/**
 * Format retrieval results into a context string for injection into the agent.
 */
export function formatRetrievalContext(result: RetrievalResult): string {
  if (result.docs.length === 0) return ''

  const lines = [
    `\n\n── Retrieved Context (${result.docs.length} docs, ${result.latencyMs}ms${result.corrected ? ', CRAG-corrected' : ''}) ──`,
  ]

  for (const doc of result.docs) {
    const grade = doc.gradedScore != null ? ` [grade: ${doc.gradedScore.toFixed(1)}/5]` : ''
    lines.push(`\n[${doc.source}${grade}]\n${doc.content.slice(0, 600)}`)
  }

  lines.push('\n── End Retrieved Context ──\n')
  return lines.join('\n')
}
