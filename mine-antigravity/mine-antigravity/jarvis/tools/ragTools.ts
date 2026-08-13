/**
 * jarvis/tools/ragTools.ts — NVIDIA RAG Retrieval with Cache + Retry
 *
 * Upgrades over v1:
 *  - Results cached for 10 minutes (LRU) — repeated queries are instant
 *  - Exponential backoff retry on transient HTTP errors (429, 500, 503)
 *  - Structured output with chunk scores and metadata
 *  - Configurable collection/namespace via NVIDIA_RAG_COLLECTION env var
 */

import { getCachedRag, setCachedRag } from '../cache.js'
import type { JarvisToolDefinition, JarvisToolEntry } from './index.js'

function createDefinition(name: string, description: string, required: string[]) {
  return {
    type: 'function' as const,
    function: {
      name,
      description,
      parameters: {
        type: 'object' as const,
        properties: {
          query: { type: 'string', description: 'Query to retrieve relevant context for' },
          top_k: { type: 'number', description: 'Number of results to return (default: 5, max: 20)' },
          collection: { type: 'string', description: 'Optional RAG collection/namespace to query' },
        },
        required,
      },
    },
  }
}

async function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function ragHandler(args: Record<string, unknown>): Promise<string> {
  const query = args.query as string
  const topK = Math.min((args.top_k as number) ?? 5, 20)
  const collection = (args.collection as string) ?? process.env.NVIDIA_RAG_COLLECTION ?? ''

  // Cache hit
  const cacheKey = `${collection}::${query}::${topK}`
  const cached = getCachedRag(query, topK)
  if (cached) {
    return `[RAG Cache Hit]\n${cached}`
  }

  const apiKey = process.env.NVIDIA_API_KEY ?? ''
  const endpoint = (process.env.NVIDIA_RAG_ENDPOINT ?? 'https://integrate.api.nvidia.com/v1/retrieval').replace(/\/$/, '')

  if (!apiKey) {
    return 'ERROR: NVIDIA_API_KEY environment variable is not set.'
  }

  const body: Record<string, unknown> = { query, top_k: topK }
  if (collection) body.collection = collection

  const RETRYABLE = new Set([429, 500, 502, 503, 504])
  const MAX_RETRIES = 3

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const startTime = Date.now()

    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${apiKey}`,
          'Accept': 'application/json',
        },
        body: JSON.stringify(body),
      })

      const durationMs = Date.now() - startTime

      if (!response.ok) {
        if (RETRYABLE.has(response.status) && attempt < MAX_RETRIES) {
          const backoff = Math.pow(2, attempt) * 500
          await sleep(backoff)
          continue
        }
        const errorText = await response.text()
        return `[NVIDIA RAG | HTTP ${response.status}] ${errorText || response.statusText}\n(Query: "${query}")`
      }

      type RagHit = { text?: string; passage?: string; score?: number; metadata?: Record<string, unknown> }
      const data = (await response.json()) as { hits?: RagHit[]; results?: RagHit[]; data?: RagHit[] } | RagHit[]
      const hits: RagHit[] = Array.isArray(data)
        ? data
        : (data.hits ?? data.results ?? data.data ?? [])

      if (hits.length === 0) {
        return `[NVIDIA RAG (${durationMs}ms)] No relevant context found for: "${query}"`
      }

      const snippets = hits.map((hit, idx) => {
        const content = (hit.passage ?? hit.text ?? JSON.stringify(hit)).trim()
        const scoreStr = hit.score != null ? ` | score: ${hit.score.toFixed(3)}` : ''
        return `─── Chunk ${idx + 1}${scoreStr} ───\n${content}`
      }).join('\n\n')

      const result = `[NVIDIA RAG | ${hits.length} chunks | ${durationMs}ms | "${query}"]\n\n${snippets}`

      // Cache the result
      setCachedRag(query, topK, result)

      return result

    } catch (err: unknown) {
      if (attempt < MAX_RETRIES) {
        await sleep(Math.pow(2, attempt) * 500)
        continue
      }
      return `ERROR executing NVIDIA RAG retrieval: ${(err as Error).message}`
    }
  }

  return `ERROR: NVIDIA RAG retrieval failed after ${MAX_RETRIES} retries for query: "${query}"`
}

export function createRagToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition(
        'NvidiaRagRetrieve',
        'Retrieve relevant context from the NVIDIA RAG / NeMo Retriever index. Results are cached for 10 minutes. Supports retry on transient errors.',
        ['query']
      ) as JarvisToolDefinition,
      handler: ragHandler,
    },
  ]
}
