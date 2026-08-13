/**
 * jarvis/tools/agentTools.ts — Agent Planning & Skill Dispatch
 *
 * Upgrades over v1:
 *  - Skill dispatch uses full examples and concrete instructions
 *  - Agent handler builds richer multi-step plans
 *  - Skills map is extended and documented
 *  - SkillInfo tool added: returns the SKILL.md content for any skill
 */

import { existsSync, readFileSync } from 'fs'
import { join, resolve } from 'path'
import type { JarvisToolDefinition, JarvisToolEntry } from './index.js'

// ─── Skill Catalog ────────────────────────────────────────────────────────────

interface SkillEntry {
  description: string
  toolToUse: string
  example: string
}

const SKILL_CATALOG: Record<string, SkillEntry> = {
  plan: {
    description: 'Create a concise multi-step execution plan. Identify the highest-value first step and potential risks.',
    toolToUse: 'Agent',
    example: 'TodoWrite → Grep → Read → Edit → Bash (verify)',
  },
  summarize: {
    description: 'Summarize the available context into structured bullet points: key facts, risks, open questions, next actions.',
    toolToUse: 'Agent',
    example: 'Read context → synthesize → present findings',
  },
  debug: {
    description: 'Explain the root cause with a minimal verification step. Search for error patterns, read relevant code, run targeted repro.',
    toolToUse: 'Grep + Bash',
    example: 'Grep error string → Read failing file → Bash (repro) → Edit (fix) → Bash (verify)',
  },
  'deck-builder': {
    description: 'Generate a professional PowerPoint presentation (.pptx) from a JSON spec. Supports 6 layouts and multiple themes.',
    toolToUse: 'BuildDeck',
    example: `BuildDeck({ spec: { theme: "midnight", slides: [
  { layout: "title", title: "My Company", subtitle: "2024 Strategy", image: null },
  { layout: "stat", stat: "40%", label: "Cost Reduction", supporting: "vs. previous year" },
  { layout: "content_image", title: "Key Points", bullets: ["Point 1","Point 2","Point 3"], image: null },
  { layout: "closing", title: "Thank You", cta: "contact@example.com" }
] }})`,
  },
  'report-builder': {
    description: 'Generate a Word document (.docx) or PDF report from a JSON spec. Supports sections, tables, code blocks, headers.',
    toolToUse: 'BuildReport',
    example: `BuildReport({ spec: { title: "Q3 Report", author: "Team", sections: [
  { heading: "Executive Summary", body: "Revenue grew 18% YoY..." },
  { heading: "Key Metrics", table: { headers: ["Metric","Value"], rows: [["Revenue","$4.2M"],["Growth","18%"]] } }
] }, formats: ["docx"] })`,
  },
  'dashboard-builder': {
    description: 'Generate an interactive HTML dashboard with charts, KPI cards, and tables. Self-contained single HTML file.',
    toolToUse: 'BuildDashboard',
    example: `BuildDashboard({ spec: { title: "Sales Dashboard", theme: "dark", sections: [
  { type: "kpi", items: [{ label: "Revenue", value: "$4.2M", trend: "+18%" }, { label: "Users", value: "12,400", trend: "+5%" }] },
  { type: "chart", chart_type: "bar", title: "Monthly Sales", labels: ["Jan","Feb","Mar"], datasets: [{ label: "Sales", data: [1.2, 1.8, 2.1], color: "#22d3ee" }] }
] }})`,
  },
  'sheet-builder': {
    description: 'Generate a formatted Excel workbook (.xlsx) or CSV from a structured spec. Supports multiple sheets, headers, styled cells.',
    toolToUse: 'BuildSheet',
    example: `BuildSheet({ spec: { title: "Sales Data", sheets: [
  { name: "Q3 Revenue", headers: ["Month","Revenue","Growth"], rows: [["Jan","$1.2M","12%"],["Feb","$1.8M","50%"],["Mar","$2.1M","17%"]] }
] }})`,
  },
}

function createDefinition(name: string, description: string, required: string[]) {
  return {
    type: 'function' as const,
    function: {
      name,
      description,
      parameters: {
        type: 'object' as const,
        properties: {
          task: { type: 'string', description: 'Task or objective to accomplish' },
          context: { type: 'string', description: 'Additional context or constraints' },
          name: { type: 'string', description: `Skill name. Available: ${Object.keys(SKILL_CATALOG).join(', ')}` },
          input: { type: 'string', description: 'Input or prompt for the skill' },
          goal: { type: 'string', description: 'The overall goal to accomplish' },
          mode: { type: 'string', description: 'Execution mode: fast | thorough | minimal' },
        },
        required,
      },
    },
  }
}

// ─── Agent Handler ────────────────────────────────────────────────────────────

function agentHandler(args: Record<string, unknown>): string {
  const goal = (args.goal as string) ?? (args.task as string) ?? 'complete the request'
  const mode = (args.mode as string) ?? 'fast'
  const context = (args.context as string) ?? ''

  const steps = mode === 'thorough'
    ? [
        '1. Clarify the full scope: read any relevant files, search for related code.',
        '2. Identify all affected components (grep for usages/imports).',
        '3. Form a complete plan with TodoWrite before making any changes.',
        '4. Implement changes in smallest-possible increments.',
        '5. Verify each change with a targeted test or command.',
        '6. Run the full test suite to confirm no regressions.',
        '7. Summarize what changed, why, and any follow-ups needed.',
      ]
    : [
        '1. Identify the minimal scope of the problem.',
        '2. Gather required context with search + targeted reads.',
        '3. Implement the smallest correct change.',
        '4. Verify the result and summarize the outcome.',
      ]

  return [
    `[Agent | mode: ${mode}]`,
    `Goal: ${goal}`,
    context ? `Context: ${context}` : '',
    '',
    'Execution steps:',
    ...steps,
  ].filter(Boolean).join('\n')
}

// ─── Skill Handler ────────────────────────────────────────────────────────────

function skillHandler(args: Record<string, unknown>): string {
  const skillName = ((args.name as string) ?? (args.task as string) ?? 'plan').toLowerCase().trim()
  const input = (args.input as string) ?? ''

  const skill = SKILL_CATALOG[skillName] ?? SKILL_CATALOG['plan']

  return [
    `[Skill: ${skillName}]`,
    skill.description,
    '',
    `Primary tool: ${skill.toolToUse}`,
    '',
    'Example usage:',
    skill.example,
    '',
    input ? `Your input: ${input}` : 'No additional input provided.',
    '',
    `Now execute using: ${skill.toolToUse}`,
  ].join('\n')
}

// ─── SkillInfo Handler ────────────────────────────────────────────────────────

function skillInfoHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const skillName = (args.name as string) ?? ''
  if (!skillName) {
    return `Available skills:\n\n${Object.entries(SKILL_CATALOG).map(([k, v]) => `  ${k.padEnd(20)} — ${v.description.slice(0, 80)}`).join('\n')}`
  }

  // Try to read the actual SKILL.md from disk
  const candidates = [
    join(resolve(cwd, 'jarvis', 'skills', skillName), 'SKILL.md'),
    join(resolve(cwd, '..', 'jarvis', 'skills', skillName), 'SKILL.md'),
  ]

  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return readFileSync(candidate, 'utf8')
    }
  }

  const skill = SKILL_CATALOG[skillName.toLowerCase()]
  if (skill) {
    return `Skill: ${skillName}\n\n${skill.description}\n\nTool: ${skill.toolToUse}\n\nExample:\n${skill.example}`
  }

  return `Skill "${skillName}" not found. Available: ${Object.keys(SKILL_CATALOG).join(', ')}`
}

// ─── Exports ──────────────────────────────────────────────────────────────────

export function createAgentToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition(
        'Agent',
        'Plan and execute a task using a structured agent workflow. Supports fast (4-step) and thorough (7-step) modes.',
        ['task']
      ) as JarvisToolDefinition,
      handler: agentHandler,
    },
    {
      definition: createDefinition(
        'Skill',
        `Invoke a built-in skill. Available: ${Object.keys(SKILL_CATALOG).join(', ')}. Returns the skill description, primary tool, and a concrete usage example.`,
        ['name']
      ) as JarvisToolDefinition,
      handler: skillHandler,
    },
    {
      definition: createDefinition(
        'SkillInfo',
        'Get detailed information about a skill including full SKILL.md content and usage examples. Call with no name to list all skills.',
        []
      ) as JarvisToolDefinition,
      handler: skillInfoHandler,
    },
  ]
}
