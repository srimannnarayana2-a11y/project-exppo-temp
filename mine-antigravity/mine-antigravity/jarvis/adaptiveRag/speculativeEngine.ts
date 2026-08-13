/**
 * jarvis/adaptiveRag/speculativeEngine.ts
 *
 * Speculative/Optimistic Streaming Engine.
 *
 * Pattern: Agent starts generating IMMEDIATELY using its parametric knowledge.
 * Retrieval runs in the background. When retrieval arrives:
 *   Case A: Retrieval confirms speculation → seamless continuation
 *   Case B: Retrieval adds new info → append enriched section inline
 *   Case C: Retrieval contradicts → append correction (no restart, lower latency)
 *
 * This is the dual-stream pattern used in production RAG systems:
 *   - Speculative draft from LLM knowledge (t=0ms)
 *   - Context injection when retrieval resolves (t=retrieval_latency)
 *   - Never blocks — user always sees tokens flowing
 *
 * Key 3 (thinking ON): Handles all LLM calls in this file
 * Key 4 (thinking OFF): Only for tool calls — handled by AsyncToolScheduler
 */

import type { ChatMessage, ToolCall, TokenChunk, RetrievalResult, AdaptiveRagConfig } from './types.js'

// ─── NVIDIA NIM Streaming (Key 3) ─────────────────────────────────────────────

interface StreamResult {
  textStream: AsyncGenerator<string>
  toolCallsPromise: Promise<ToolCall[]>
}

/**
 * Open a streaming connection to Key 3 (agent, thinking ON).
 * Returns both a text stream and a promise for tool calls.
 */
export function streamAgent(
  messages: ChatMessage[],
  tools: unknown[],
  config: AdaptiveRagConfig,
  signal: AbortSignal,
  opts: { temperature?: number; reasoning_effort?: string; max_tokens?: number } = {}
): StreamResult {
  let resolveToolCalls!: (tcs: ToolCall[]) => void
  const toolCallsPromise = new Promise<ToolCall[]>(res => { resolveToolCalls = res })

  const textStream = (async function * (): AsyncGenerator<string> {
    const body = {
      model: config.agentModel,
      messages,
      // Only pass tools if provided (empty array causes 400 on some endpoints)
      ...(tools.length > 0 ? { tools, tool_choice: 'auto' } : {}),
      temperature: opts.temperature ?? 0.15,
      stream: true,
      ...(opts.max_tokens ? { max_tokens: opts.max_tokens } : {}),
      // reasoning_effort intentionally omitted — not universally supported by NVIDIA NIM
    }

    let resp: Response
    try {
      resp = await fetch(`${config.agentBaseUrl}/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${config.agentApiKey}`,
        },
        body: JSON.stringify(body),
        signal,
      })
    } catch (e: unknown) {
      if ((e as Error).name !== 'AbortError') throw e
      resolveToolCalls([])
      return
    }

    if (!resp.ok) {
      const err = await resp.text().catch(() => resp.statusText)
      resolveToolCalls([])
      throw new Error(`NVIDIA API Key 3 HTTP ${resp.status}: ${err}`)
    }

    const reader  = resp.body!.getReader()
    const decoder = new TextDecoder()
    const tcMap   = new Map<number, { id: string; name: string; args: string }>()
    let buf       = ''

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
            if (delta.content) yield delta.content
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
    } finally {
      reader.releaseLock()
      resolveToolCalls(
        Array.from(tcMap.values()).map(tc => ({
          id: tc.id,
          type: 'function' as const,
          function: { name: tc.name, arguments: tc.args },
        }))
      )
    }
  })()

  return { textStream, toolCallsPromise }
}

// ─── Speculative Context Injection ────────────────────────────────────────────

/**
 * Merge retrieval results into an ongoing stream.
 *
 * Strategy:
 *   1. Yield all tokens from the agent stream (speculative)
 *   2. When retrieval resolves → yield a context-injection chunk
 *   3. Fire a second, context-enriched agent call for additional info
 *
 * The caller receives a unified TokenChunk stream:
 *   - 'token' chunks → forward to UI immediately
 *   - 'retrieval_context' → show as an annotation in UI
 *   - 'tool_start/done' → show in UI as tool activity
 *   - 'done' → streaming complete
 */
export async function * speculativeStream(
  messages: ChatMessage[],
  tools: unknown[],
  retrievalPromise: Promise<RetrievalResult>,
  config: AdaptiveRagConfig,
  signal: AbortSignal,
  inferenceOpts: { temperature?: number; reasoning_effort?: string; max_tokens?: number }
): AsyncGenerator<TokenChunk> {

  // ── Phase 1: Speculative generation (t=0ms) ────────────────────────────────
  const { textStream, toolCallsPromise } = streamAgent(
    messages, tools, config, signal, inferenceOpts
  )

  let speculativeText = ''
  let retrievalDone   = false
  let retrievalResult: RetrievalResult | null = null

  // Race: yield speculative tokens, but also watch for retrieval completion
  const retrievalRace = retrievalPromise.then(r => {
    retrievalDone   = true
    retrievalResult = r
  }).catch(() => { retrievalDone = true })

  for await (const token of textStream) {
    if (signal.aborted) break
    speculativeText += token
    yield { type: 'token', content: token }
  }

  // ── Phase 2: Tool calls from speculative generation ────────────────────────
  const toolCalls = await toolCallsPromise
  for (const tc of toolCalls) {
    yield { type: 'tool_start', toolName: tc.function.name, toolId: tc.id }
  }

  // ── Phase 3: Wait for retrieval (non-blocking, 100ms max extra wait) ────────
  // Retrieval may already be done by the time streaming finishes.
  // Only wait the additional 100ms if it's not yet resolved.
  if (!retrievalDone) {
    await Promise.race([
      retrievalRace,
      new Promise<void>(r => setTimeout(r, 100)),
    ])
  }

  if (retrievalResult && retrievalResult.docs.length > 0) {
    const ctx = formatContextForInjection(retrievalResult)

    // Check if retrieval adds anything beyond what was speculatively said
    const isAdditive = !specContainsContext(speculativeText, retrievalResult)

    if (isAdditive) {
      yield {
        type: 'retrieval_context',
        content: ctx,
        source: retrievalResult.strategy,
        corrected: retrievalResult.corrected,
      }

      // ── Phase 4: Enriched continuation using retrieval ─────────────────────
      // Only fire if retrieval actually added new information
      const enrichedMessages: ChatMessage[] = [
        ...messages,
        { role: 'assistant', content: speculativeText },
        { role: 'user', content: `Additional retrieved context:\n${ctx}\n\nPlease add any corrections or additional details based on this context. Be brief — only add what's not already covered.` },
      ]

      const { textStream: enrichedStream } = streamAgent(
        enrichedMessages, [], config, signal,
        { ...inferenceOpts, max_tokens: 512 }
      )

      for await (const token of enrichedStream) {
        if (signal.aborted) break
        yield { type: 'token', content: token }
      }
    }
  }

  yield { type: 'done' }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatContextForInjection(result: RetrievalResult): string {
  if (result.docs.length === 0) return ''
  return result.docs
    .slice(0, 4)
    .map(d => `[${d.source}] ${d.content.slice(0, 400)}`)
    .join('\n\n')
}

/**
 * Check if the speculative text already covers the key content from retrieval.
 * Heuristic: if >40% of unique terms in retrieval docs appear in the spec text,
 * the retrieval is likely confirmatory (not additive).
 */
function specContainsContext(speculativeText: string, result: RetrievalResult): boolean {
  if (result.docs.length === 0) return true

  const specWords   = new Set(speculativeText.toLowerCase().split(/\W+/).filter(w => w.length > 4))
  const topDoc      = result.docs[0]?.content ?? ''
  const docWords    = topDoc.toLowerCase().split(/\W+/).filter(w => w.length > 4)
  const uniqueDocWords = [...new Set(docWords)]

  const overlap = uniqueDocWords.filter(w => specWords.has(w)).length
  const coverageRatio = overlap / Math.max(uniqueDocWords.length, 1)

  return coverageRatio > 0.4  // >40% overlap → speculative answer likely correct
}
