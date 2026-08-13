/**
 * jarvis/queryOptimization.ts — Deep Intent Classifier (v2)
 *
 * Upgrades over v1:
 *  - Uses the router's classifyIntent for full classification
 *  - Returns richer hints with parallelism flag and confidence
 *  - Keeps the original QueryOptimizationHints interface for backward compat
 *  - Adds buildQueryOptimizationPrompt that injects routing hints
 */

import { classifyIntent, type IntentClass } from './router.js'

export interface QueryOptimizationHints {
  intent: string
  intentClass: IntentClass
  confidence: number
  bestToolOrder: string[]
  shouldPlan: boolean
  shouldVerify: boolean
  shouldUseTodo: boolean
  parallelizable: boolean
}

export function optimizeQuery(input: string): QueryOptimizationHints {
  const routing = classifyIntent(input)

  return {
    intent: routing.intent,
    intentClass: routing.intent,
    confidence: routing.confidence,
    bestToolOrder: routing.primaryTools.slice(0, 6),
    shouldPlan: routing.shouldPlanFirst,
    shouldVerify: routing.shouldVerify,
    shouldUseTodo: routing.shouldPlanFirst,
    parallelizable: routing.parallelizable,
  }
}

export function buildQueryOptimizationPrompt(input: string): string {
  const hints = optimizeQuery(input)
  return [
    `Intent: ${hints.intent} (${(hints.confidence * 100).toFixed(0)}% confidence)`,
    `Tool order: ${hints.bestToolOrder.join(' → ')}`,
    `Plan first: ${hints.shouldPlan ? 'yes' : 'no'} | Verify: ${hints.shouldVerify ? 'yes' : 'no'} | Parallel tools: ${hints.parallelizable ? 'yes' : 'no'}`,
  ].join('\n')
}
