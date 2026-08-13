/**
 * jarvis/NvidiaRagTool.ts — NVIDIA RAG Vector Retrieval Tool
 */

export const NvidiaRagToolDef = {
  type: 'function' as const,
  function: {
    name: 'NvidiaRagRetrieve',
    description: 'Retrieve relevant code context, documentation, or codebase snippets from the NVIDIA RAG / NeMo Retriever index.',
    parameters: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description: 'Search query or code pattern to search within the indexed knowledge base.'
        },
        top_k: {
          type: 'number',
          description: 'Number of relevant chunks to retrieve (default: 5).'
        }
      },
      required: ['query']
    }
  }
}

export interface NvidiaRagArgs {
  query: string
  top_k?: number
}

interface RagHit {
  text?: string
  passage?: string
  score?: number
  metadata?: Record<string, unknown>
}

export async function executeNvidiaRag(args: NvidiaRagArgs): Promise<string> {
  const { query, top_k = 5 } = args
  const apiKey = process.env.NVIDIA_API_KEY ?? ''
  const ragEndpoint = process.env.NVIDIA_RAG_ENDPOINT ?? 'https://integrate.api.nvidia.com/v1/retrieval'

  if (!apiKey) {
    return 'ERROR: NVIDIA_API_KEY environment variable is not set.'
  }

  const startTime = Date.now()

  try {
    const response = await fetch(ragEndpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
        'Accept': 'application/json'
      },
      body: JSON.stringify({
        query,
        top_k
      })
    })

    const durationMs = Date.now() - startTime

    if (!response.ok) {
      const errorText = await response.text()
      return `[NVIDIA RAG | HTTP ${response.status}] Endpoint returned an error: ${errorText || response.statusText}\n(Query: "${query}")`
    }

    const data = (await response.json()) as { hits?: RagHit[]; results?: RagHit[]; data?: RagHit[] } | RagHit[]
    
    // Normalize response arrays across different NVIDIA retriever schemas
    const hits: RagHit[] = Array.isArray(data) 
      ? data 
      : (data.hits || data.results || data.data || [])

    if (hits.length === 0) {
      return `[NVIDIA RAG Search (${durationMs}ms)] No relevant context chunks found for query: "${query}"`
    }

    const snippets = hits.map((hit, idx) => {
      const content = hit.passage || hit.text || JSON.stringify(hit)
      const scoreStr = hit.score ? ` (Score: ${hit.score.toFixed(3)})` : ''
      return `--- [Chunk ${idx + 1}${scoreStr}] ---\n${content.trim()}`
    }).join('\n\n')

    return `[NVIDIA RAG Retrieved ${hits.length} Chunks (${durationMs}ms) for "${query}"]\n\n${snippets}`

  } catch (err: unknown) {
    return `ERROR executing NVIDIA RAG retrieval: ${(err as Error).message}`
  }
}