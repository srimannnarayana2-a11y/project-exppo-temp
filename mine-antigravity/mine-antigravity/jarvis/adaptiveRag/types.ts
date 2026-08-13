/**
 * jarvis/adaptiveRag/types.ts
 * Shared types for the Adaptive Speculative RAG system.
 */

// ─── Retrieval Strategy ───────────────────────────────────────────────────────
export type RetrievalStrategy =
  | 'DIRECT_ANSWER'        // LLM already knows — skip retrieval entirely
  | 'SEMANTIC_ONLY'        // Conceptual/factual — semantic retriever only
  | 'CODE_ONLY'            // Code lookup — code retriever dominates
  | 'HYBRID'               // Mixed need — both retrievers, weighted
  | 'CLARIFY_FIRST'        // Too ambiguous to retrieve — ask 1 question first
  | 'CLARIFY_AND_RETRIEVE' // Partially clear — fire retrieval + clarify in parallel

export interface ClassificationResult {
  strategy: RetrievalStrategy
  confidence: number            // 0-1
  semanticWeight: number        // 0-1, proportion for semantic retriever
  codeWeight: number            // 0-1, proportion for code retriever
  needsClarification: boolean
  candidateIntents: string[]    // possible intents (for EIG scoring)
  reason: string                // human-readable explanation
}

// ─── Retrieved Documents ──────────────────────────────────────────────────────
export interface RetrievedDoc {
  id: string
  content: string
  source: 'semantic' | 'code' | 'web'
  score: number               // 0-1 raw relevance from retriever
  gradedScore?: number        // 0-5 CRAG-graded relevance
  metadata?: Record<string, unknown>
}

export interface RetrievalResult {
  docs: RetrievedDoc[]
  strategy: RetrievalStrategy
  latencyMs: number
  corrected: boolean            // was CRAG correction cycle applied?
  correctionQuery?: string      // reformulated query if corrected
}

// ─── Clarification / EIG ─────────────────────────────────────────────────────
export interface ClarifyingQuestion {
  question: string
  eigScore: number              // estimated information gain (higher = better)
  eliminatedIntents: string[]   // which intent branches this question kills
  candidateAnswers?: string[]   // optional choices (for multiple-choice UX)
}

// ─── Token Stream ─────────────────────────────────────────────────────────────
export type TokenChunk =
  | { type: 'token';             content: string }
  | { type: 'retrieval_context'; content: string; source: string; corrected: boolean }
  | { type: 'speculative_note';  content: string }  // "generating speculatively..."
  | { type: 'clarify';           question: ClarifyingQuestion }
  | { type: 'tool_start';        toolName: string; toolId: string }
  | { type: 'tool_done';         toolName: string; toolId: string; result: string }
  | { type: 'tool_error';        toolName: string; toolId: string; error: string }
  | { type: 'status';            message: string }
  | { type: 'done' }
  | { type: 'error';             error: string }

// ─── Scheduled Tools ─────────────────────────────────────────────────────────
export interface ScheduledTool {
  id: string
  name: string
  args: Record<string, unknown>
  status: 'pending' | 'running' | 'done' | 'error'
  result?: string
  error?: string
  startedAt: number
  completedAt?: number
}

// ─── Agent Config ─────────────────────────────────────────────────────────────
export interface AdaptiveRagConfig {
  // Key 3: main agent — thinking ON, orchestrates everything
  agentApiKey: string
  agentModel: string
  agentBaseUrl: string

  // Key 4: tool executor — thinking OFF, fast mechanical execution
  toolApiKey: string
  toolModel: string
  toolBaseUrl: string

  cwd: string
  retrievalTimeoutMs?: number   // default: 2000ms
  cragEnabled?: boolean         // default: true
  maxClarifyRounds?: number     // default: 2
}

// ─── Retriever Interface (implement these with your actual retrievers) ────────
export interface IRetriever {
  /** Query the retriever. Must resolve within timeoutMs or throw. */
  query(
    query: string,
    opts: { weight: number; maxDocs?: number; signal?: AbortSignal }
  ): Promise<RetrievedDoc[]>

  /** Optional streaming variant — yields partial results as they arrive */
  queryStream?(
    query: string,
    opts: { weight: number; signal?: AbortSignal }
  ): AsyncIterable<RetrievedDoc>
}

// ─── Chat Message (OpenAI-compatible) ────────────────────────────────────────
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: ToolCall[]
  tool_call_id?: string
  name?: string
}

export interface ToolCall {
  id: string
  type: 'function'
  function: { name: string; arguments: string }
}
