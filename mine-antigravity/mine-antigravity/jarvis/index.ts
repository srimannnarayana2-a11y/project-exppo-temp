/**
 * jarvis/index.ts — Central Tool Registry & Execution Router (v2)
 *
 * Upgrades over v1:
 *  - Map-based O(1) dispatch (no duplicate switch/Set)
 *  - Re-exports new modules: cache, router, sessionManager
 *  - executeJarvisTool returns null only for truly unknown tools
 */

import { createJarvisToolRegistry, executeJarvisToolByName } from './tools/index.js'
import { listJarvisSkills } from './skillRegistry.js'

export { clearAllCaches, getCacheStats } from './cache.js'
export { classifyIntent, buildRoutingPrompt } from './router.js'
export { getToolSummary, TOOL_COUNT } from './tools/index.js'
export {
  saveSession, loadSession, clearSession,
  estimateTokens, getContextStatus, prepareHistoryForApiCall,
} from './sessionManager.js'

/**
 * Combined list of tool schemas exported to the LLM.
 */
export const JARVIS_TOOLS = createJarvisToolRegistry()
export const JARVIS_SKILLS = listJarvisSkills()

/**
 * Dynamic router: dispatches model tool calls to their handlers.
 * Returns null only for tools not in the Jarvis registry (enables fallback).
 */
export async function executeJarvisTool(
  name: string,
  args: Record<string, unknown>
): Promise<string | null> {
  const result = await executeJarvisToolByName(name, args, process.cwd())
  // If the tool is unknown, executeJarvisToolByName returns a message starting with "Unknown tool:"
  if (result.startsWith('Unknown tool:')) return null
  return result
}