/**
 * jarvis/adaptiveRag/index.ts
 *
 * AdaptiveRagAgent — Main Orchestrator
 *
 * Wires together:
 *   QueryClassifier → RetrievalOrchestrator → SpeculativeEngine
 *   InformationGainClarifier → AsyncToolScheduler → CRAG cycles
 *
 * State machine:
 *   IDLE → CLASSIFYING → {DIRECT | RETRIEVING | CLARIFYING | CLARIFY_AND_RETRIEVE}
 *          → STREAMING (speculative) → INJECTING_CONTEXT → DONE
 *
 * Key 3 (NVIDIA_AGENT_KEY): orchestration, thinking, streaming
 * Key 4 (NVIDIA_TOOL_KEY): classification, CRAG grading, tool execution
 *
 * Deadlock prevention:
 *   - All async paths have timeouts via Promise.race
 *   - AbortSignal propagated through every layer
 *   - No circular await dependencies
 *   - allSettled() used for parallel paths
 */

import type {
  TokenChunk, ChatMessage, AdaptiveRagConfig, ClassificationResult,
  ClarifyingQuestion, RetrievalResult,
} from './types.js'

import { classifyQuery }           from './queryClassifier.js'
import { retrieve, formatRetrievalContext } from './retrievalOrchestrator.js'
import { speculativeStream }        from './speculativeEngine.js'
import { AsyncToolScheduler }       from './asyncToolScheduler.js'
import {
  generateClarifyingQuestion,
  processClarification,
} from './informationGainClarifier.js'
import { SemanticRetriever }        from './retrievers/semanticRetriever.js'
import { CodeRetriever }            from './retrievers/codeRetriever.js'

// ─── Agent State ──────────────────────────────────────────────────────────────
type AgentState =
  | 'IDLE'
  | 'CLASSIFYING'
  | 'CLARIFYING'
  | 'RETRIEVING'
  | 'STREAMING'
  | 'DONE'

export interface TurnContext {
  query: string
  classification: ClassificationResult
  retrieval: RetrievalResult | null
  clarifyingQuestions: string[]
  pendingClarification: ClarifyingQuestion | null
  state: AgentState
}

// ─── AdaptiveRagAgent ─────────────────────────────────────────────────────────
export class AdaptiveRagAgent {
  private readonly semanticRetriever: SemanticRetriever
  private readonly codeRetriever: CodeRetriever
  private state: AgentState = 'IDLE'
  private currentTurn: TurnContext | null = null
  private clarifyBuffer: string[] = []

  constructor(
    private readonly config: AdaptiveRagConfig,
    private readonly tools: unknown[],   // JARVIS_TOOLS schema array
  ) {
    this.semanticRetriever = new SemanticRetriever({
      endpoint: process.env.SEMANTIC_RETRIEVER_ENDPOINT,
      apiKey: config.agentApiKey,
    })
    this.codeRetriever = new CodeRetriever({
      endpoint: process.env.CODE_RETRIEVER_ENDPOINT,
      apiKey: config.agentApiKey,
    })
  }

  /**
   * Process a user message.
   * Returns an AsyncGenerator of TokenChunks for the UI to consume.
   *
   * The generator yields:
   *   { type: 'status' }          → show in status bar
   *   { type: 'token' }           → append to response
   *   { type: 'clarify' }         → pause and ask user a question
   *   { type: 'retrieval_context'}→ show retrieval annotation
   *   { type: 'tool_start/done' } → show tool activity
   *   { type: 'done' }            → response complete
   */
  async * respond(
    userMessage: string,
    history: ChatMessage[],
    signal: AbortSignal,
    inferenceOpts: { temperature?: number; max_tokens?: number } = {}
  ): AsyncGenerator<TokenChunk> {

    this.state = 'CLASSIFYING'

    // ── Step 1: Classify (< 5ms for heuristic, < 300ms for LLM fallback) ─────
    yield { type: 'status', message: 'Classifying…' }

    const classification = await classifyQuery(
      userMessage,
      this.config.toolApiKey,
      this.config.toolModel,
      this.config.toolBaseUrl,
      signal
    )

    // ── Step 2: Handle clarification need ─────────────────────────────────────
    if (classification.needsClarification &&
        classification.strategy !== 'CLARIFY_AND_RETRIEVE') {

      this.state = 'CLARIFYING'
      const question = await generateClarifyingQuestion(
        userMessage, classification, this.clarifyBuffer, this.config, signal
      )

      if (question) {
        yield { type: 'clarify', question }
        // Control returns to UI — user answers — then respond() is called again
        // with the answer appended. processClarification() handles the update.
        this.state = 'IDLE'
        return
      }
    }

    // ── Step 3: If CLARIFY_AND_RETRIEVE, fire both in parallel ────────────────
    this.state = 'RETRIEVING'
    yield { type: 'status', message: `Retrieving (${classification.strategy})…` }

    // Fire retrieval immediately — don't await it yet
    const retrievalPromise: Promise<RetrievalResult> =
      classification.strategy === 'DIRECT_ANSWER'
        ? Promise.resolve({ docs: [], strategy: 'DIRECT_ANSWER', latencyMs: 0, corrected: false })
        : retrieve(
            userMessage,
            classification,
            this.semanticRetriever,
            this.codeRetriever,
            this.config,
            signal
          )

    // If CLARIFY_AND_RETRIEVE: fire clarification question in parallel with retrieval
    let clarifyPromise: Promise<ClarifyingQuestion | null> = Promise.resolve(null)
    if (classification.strategy === 'CLARIFY_AND_RETRIEVE') {
      clarifyPromise = generateClarifyingQuestion(
        userMessage, classification, this.clarifyBuffer, this.config, signal
      )
    }

    // ── Step 4: Speculative streaming starts IMMEDIATELY ──────────────────────
    this.state = 'STREAMING'
    yield { type: 'status', message: `⚡ Streaming (temp=${inferenceOpts.temperature ?? 0.15})…` }

    // Build the messages array — no retrieval hint injected here.
    // Any hint that says "start generating while retrieval runs" makes the agent
    // output text preamble instead of calling tools immediately.
    const streamMessages: ChatMessage[] = [
      ...history,
      { role: 'user', content: userMessage },
    ]

    // Create tool scheduler for this turn (Key 4)
    const scheduler = new AsyncToolScheduler(this.config, signal)

    // If this looks like a document request, pre-create skeleton immediately
    if (this.isDocumentRequest(userMessage)) {
      const docType = this.detectDocType(userMessage)
      const title = this.extractTitle(userMessage)
      // Fire skeleton creation — don't await (background task)
      scheduler.preCreateSkeleton(docType, title).catch(() => {})
      yield { type: 'status', message: `Creating ${docType} skeleton…` }
    }

    // Stream tokens speculatively while retrieval runs in background
    for await (const chunk of speculativeStream(
      streamMessages,
      this.tools,
      retrievalPromise,
      this.config,
      signal,
      inferenceOpts
    )) {
      if (signal.aborted) break
      yield chunk
    }

    // ── Step 5: Handle parallel clarification result ───────────────────────────
    const clarifyQuestion = await clarifyPromise
    if (clarifyQuestion && !signal.aborted) {
      yield { type: 'clarify', question: clarifyQuestion }
    }

    this.state = 'DONE'
  }

  /**
   * Process a user's answer to a clarifying question.
   * Updates classification and re-routes if needed.
   */
  processClarifyAnswer(
    answer: string,
    question: ClarifyingQuestion,
    classification: ClassificationResult
  ): ClassificationResult {
    this.clarifyBuffer.push(question.question)
    return processClarification(answer, classification, question)
  }

  /** Reset clarification buffer between unrelated conversations */
  clearClarifyBuffer(): void {
    this.clarifyBuffer = []
    this.state = 'IDLE'
  }

  // ─── Document Type Detection ─────────────────────────────────────────────────
  private isDocumentRequest(query: string): boolean {
    return /\b(deck|pptx|report|docx?|dashboard|sheet|excel|pdf)\b/i.test(query)
  }

  private detectDocType(query: string): 'deck' | 'report' | 'dashboard' | 'sheet' {
    if (/\b(deck|pptx|presentation|slides)\b/i.test(query)) return 'deck'
    if (/\b(dashboard|kpi|analytics)\b/i.test(query)) return 'dashboard'
    if (/\b(sheet|excel|xlsx|csv|spreadsheet)\b/i.test(query)) return 'sheet'
    return 'report'
  }

  private extractTitle(query: string): string {
    // Try to extract a meaningful title from the query
    const cleaned = query
      .replace(/\b(make|create|build|write|generate|give me|produce)\b/gi, '')
      .replace(/\b(a|an|the)\b/gi, '')
      .replace(/\b(deck|report|dashboard|sheet|pptx|docx?)\b/gi, '')
      .trim()
    return cleaned.slice(0, 40).replace(/\s+/g, '-') || 'document'
  }

  get currentState(): AgentState { return this.state }
}

// ─── Factory ──────────────────────────────────────────────────────────────────

export function createAdaptiveRagAgent(tools: unknown[]): AdaptiveRagAgent {
  const config: AdaptiveRagConfig = {
    agentApiKey:  process.env.NVIDIA_AGENT_KEY  ?? process.env.NVIDIA_API_KEY ?? '',
    agentModel:   process.env.NVIDIA_AGENT_MODEL ?? process.env.NVIDIA_MODEL  ?? 'nvidia/nemotron-ultra-253b-v1',
    agentBaseUrl: (process.env.NVIDIA_BASE_URL ?? 'https://integrate.api.nvidia.com/v1').replace(/\/$/, ''),

    toolApiKey:   process.env.NVIDIA_TOOL_KEY   ?? process.env.NVIDIA_API_KEY ?? '',
    toolModel:    process.env.NVIDIA_TOOL_MODEL  ?? 'nvidia/llama-3.1-nemotron-nano-8b-v1',
    toolBaseUrl:  (process.env.NVIDIA_TOOL_BASE_URL ?? process.env.NVIDIA_BASE_URL ?? 'https://integrate.api.nvidia.com/v1').replace(/\/$/, ''),

    cwd:                  process.cwd(),
    retrievalTimeoutMs:   2000,
    cragEnabled:          true,
    maxClarifyRounds:     2,
  }

  return new AdaptiveRagAgent(config, tools)
}

// Re-export types for consumers
export type { TokenChunk, AdaptiveRagConfig, TurnContext } from './types.js'
