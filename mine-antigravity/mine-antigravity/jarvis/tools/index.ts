/**
 * jarvis/tools/index.ts — Tool Registry with Parallel Execution
 *
 * Upgrades over v1:
 *  - Tool deduplication: single Map-based lookup, no repeated names
 *  - executeJarvisToolsParallel: run multiple tool calls concurrently (capped at 4)
 *  - Includes new codeTools entries
 *  - Exports tool count and categories for the /tools command
 */

import { createFileToolEntries } from './fileTools.js'
import { createSearchToolEntries } from './searchTools.js'
import { createAgentToolEntries } from './agentTools.js'
import { createTodoToolEntries } from './todoTools.js'
import { createRagToolEntries } from './ragTools.js'
import { createBuilderToolEntries } from './builderTools.js'
import { createSheetToolEntries } from './sheetTools.js'
import { createCodeToolEntries } from './codeTools.js'

export interface JarvisToolDefinition {
  type: 'function'
  function: {
    name: string
    description: string
    parameters: {
      type: 'object'
      properties: Record<string, any>
      required: string[]
    }
  }
}

export type JarvisToolHandler = (args: Record<string, unknown>, cwd?: string) => Promise<string> | string

export interface JarvisToolEntry {
  definition: JarvisToolDefinition
  handler: JarvisToolHandler
}

// ─── Build Registry ───────────────────────────────────────────────────────────

const RAW_ENTRIES: JarvisToolEntry[] = [
  ...createFileToolEntries(),
  ...createSearchToolEntries(),
  ...createAgentToolEntries(),
  ...createTodoToolEntries(),
  ...createRagToolEntries(),
  ...createBuilderToolEntries(),
  ...createSheetToolEntries(),
  ...createCodeToolEntries(),
]

// Deduplicate by tool name (first wins — file/search tools take priority)
const JARVIS_TOOL_MAP = new Map<string, JarvisToolEntry>()
for (const entry of RAW_ENTRIES) {
  const name = entry.definition.function.name
  if (!JARVIS_TOOL_MAP.has(name)) {
    JARVIS_TOOL_MAP.set(name, entry)
  }
}

const JARVIS_TOOL_ENTRIES: JarvisToolEntry[] = Array.from(JARVIS_TOOL_MAP.values())
const JARVIS_TOOL_DEFINITIONS: JarvisToolDefinition[] = JARVIS_TOOL_ENTRIES.map(e => e.definition)

// ─── Public API ───────────────────────────────────────────────────────────────

export function createJarvisToolRegistry(): JarvisToolDefinition[] {
  return JARVIS_TOOL_DEFINITIONS
}

export function createJarvisToolRegistryEntries(): JarvisToolEntry[] {
  return JARVIS_TOOL_ENTRIES
}

export function createJarvisToolMap(): Map<string, JarvisToolEntry> {
  return JARVIS_TOOL_MAP
}

// ─── Single Tool Execution ────────────────────────────────────────────────────

export async function executeJarvisToolByName(
  name: string,
  args: Record<string, unknown>,
  cwd: string = process.cwd()
): Promise<string> {
  const tool = JARVIS_TOOL_MAP.get(name)
  if (!tool) return `Unknown tool: ${name}. Available: ${Array.from(JARVIS_TOOL_MAP.keys()).join(', ')}`
  try {
    return await tool.handler(args, cwd)
  } catch (e: unknown) {
    return `Tool error [${name}]: ${(e as Error).message}`
  }
}

// ─── Parallel Execution (max 4 concurrent) ────────────────────────────────────

const MAX_PARALLEL = 4

export async function executeJarvisToolsParallel(
  calls: Array<{ name: string; args: Record<string, unknown> }>,
  cwd: string = process.cwd()
): Promise<Array<{ name: string; result: string }>> {
  const results: Array<{ name: string; result: string }> = []

  // Process in batches of MAX_PARALLEL
  for (let i = 0; i < calls.length; i += MAX_PARALLEL) {
    const batch = calls.slice(i, i + MAX_PARALLEL)
    const batchResults = await Promise.all(
      batch.map(async ({ name, args }) => ({
        name,
        result: await executeJarvisToolByName(name, args, cwd),
      }))
    )
    results.push(...batchResults)
  }

  return results
}

// ─── Tool Metadata ────────────────────────────────────────────────────────────

export const TOOL_COUNT = JARVIS_TOOL_MAP.size

export function getToolSummary(): string {
  const categories = {
    'File I/O': ['Read', 'Write', 'FileWrite', 'Edit', 'FileEdit', 'Bash', 'LS'],
    'Search': ['Grep', 'Glob', 'WebFetch', 'WebSearch'],
    'Agent': ['Agent', 'Skill', 'SkillInfo', 'TodoWrite'],
    'RAG': ['NvidiaRagRetrieve'],
    'Builders': ['BuildDashboard', 'BuildDeck', 'BuildReport', 'BuildSheet'],
    'Code': ['CodeAnalyze', 'RunTests', 'FormatCode'],
  }

  const lines: string[] = [`${TOOL_COUNT} tools registered:\n`]
  for (const [cat, tools] of Object.entries(categories)) {
    const available = tools.filter(t => JARVIS_TOOL_MAP.has(t))
    if (available.length > 0) {
      lines.push(`  ${cat.padEnd(12)} ${available.join(', ')}`)
    }
  }
  return lines.join('\n')
}
