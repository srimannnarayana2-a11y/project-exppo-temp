/**
 * jarvis/tools/builderTools.ts — Builder Tool Handlers (v2)
 *
 * Upgrades over v1:
 *  - dashboard-builder now calls build_dashboard.js (Node) instead of build_dashboard.py (Python)
 *  - report-builder calls build_docx.js or build_pdf.js (Node) — no Python dependency
 *  - Workspace root discovery is cached
 *  - Better error messages with actionable hints
 *  - spec can be passed inline as a JS object (no serialization needed)
 */

import { spawnSync } from 'child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { dirname, join, resolve } from 'path'
import type { JarvisToolDefinition, JarvisToolEntry } from './index.js'

// ─── Workspace Root Cache ─────────────────────────────────────────────────────

let _workspaceRootCache: string | null = null

function findWorkspaceRoot(startDir: string): string {
  if (_workspaceRootCache) return _workspaceRootCache

  const skillDirs = ['deck-builder', 'dashboard-builder', 'report-builder', 'sheet-builder']
  const candidates = [resolve(startDir), resolve(process.cwd())]
  const seen = new Set<string>()

  for (const base of candidates) {
    let current = base
    while (true) {
      if (seen.has(current)) break
      seen.add(current)
      if (skillDirs.some(d => existsSync(join(current, 'jarvis', 'skills', d)))) {
        _workspaceRootCache = current
        return current
      }
      const parent = dirname(current)
      if (parent === current) break
      current = parent
    }
  }

  return resolve(startDir)
}

// ─── Spec Resolution ──────────────────────────────────────────────────────────

/**
 * Robustly resolve the spec from args.
 *
 * Handles:
 *  1. args.spec_path — direct file path to a JSON file
 *  2. args.spec as object — already parsed (ideal case from NVIDIA NIM)
 *  3. args.spec as valid JSON string
 *  4. args.spec as a file path string
 *  5. args.spec as a near-JSON string (relaxed parsing via eval-safe fallback)
 *
 * NEVER throws a cryptic error — always includes what was received.
 */
function resolveSpec(args: Record<string, unknown>, cwd: string): { specPath: string; spec: unknown } {
  // Case 1: explicit file path argument
  if (args.spec_path) {
    const p = resolve(cwd, args.spec_path as string)
    if (!existsSync(p)) {
      throw new Error(`spec_path not found: ${args.spec_path}\nSearched: ${p}`)
    }
    return { specPath: p, spec: JSON.parse(readFileSync(p, 'utf8')) }
  }

  // Case 2: no spec provided at all
  if (args.spec === undefined || args.spec === null) {
    throw new Error(
      'No spec provided. Pass a JSON object as the "spec" argument.\n' +
      'Example: BuildDeck({ spec: { theme: "midnight", slides: [...] } })'
    )
  }

  let spec: unknown

  // Case 3: spec is already a parsed object (best case — NVIDIA NIM passes objects)
  if (typeof args.spec === 'object') {
    spec = args.spec
  } else {
    // spec is a string — try to parse it
    const raw = String(args.spec).trim()

    // Case 4: check if it's a file path (no braces, has extension or looks like a path)
    if (!raw.startsWith('{') && !raw.startsWith('[')) {
      const candidate = resolve(cwd, raw)
      if (existsSync(candidate)) {
        try {
          spec = JSON.parse(readFileSync(candidate, 'utf8'))
        } catch {
          throw new Error(`Found file at ${raw} but could not parse as JSON.`)
        }
      } else {
        throw new Error(
          `spec string does not look like JSON and "${raw}" is not a valid file path.\n` +
          'Expected either:\n' +
          '  - A JSON object: { "theme": "midnight", "slides": [...] }\n' +
          '  - A path to a JSON file: /path/to/spec.json'
        )
      }
    } else {
      // Case 5: looks like JSON — try standard parse first, then relaxed
      try {
        spec = JSON.parse(raw)
      } catch {
        // Attempt relaxed parse: fix common LLM JSON mistakes
        // - trailing commas before } or ]
        // - single quotes around strings
        // - unquoted keys
        try {
          const relaxed = raw
            .replace(/,\s*([\]}])/g, '$1')             // trailing commas
            .replace(/([{,]\s*)(\w+)\s*:/g, '$1"$2":') // unquoted keys
            .replace(/'/g, '"')                         // single → double quotes
          spec = JSON.parse(relaxed)
        } catch {
          // Last resort: show a useful error
          const preview = raw.length > 200 ? raw.slice(0, 200) + '…' : raw
          throw new Error(
            `Could not parse spec as JSON.\n` +
            `Received (first 200 chars): ${preview}\n\n` +
            'Common fixes:\n' +
            '  - Remove trailing commas (JSON does not allow them)\n' +
            '  - Use double quotes " not single quotes \'\n' +
            '  - Ensure all keys are quoted: "theme" not theme'
          )
        }
      }
    }
  }

  // Write resolved spec to a temp file for the script to read
  const tempDir = join(cwd, '.jarvis', 'builder-specs')
  mkdirSync(tempDir, { recursive: true })
  const tempPath = join(tempDir, `spec-${Date.now()}.json`)
  writeFileSync(tempPath, JSON.stringify(spec, null, 2), 'utf8')
  return { specPath: tempPath, spec }
}

function resolveOutput(args: Record<string, unknown>, cwd: string, defaultName: string): string {
  if (args.output_path) return resolve(cwd, args.output_path as string)
  const outDir = join(cwd, '.jarvis', 'outputs')
  mkdirSync(outDir, { recursive: true })
  return join(outDir, defaultName)
}

// ─── Command Runner ───────────────────────────────────────────────────────────

function runNodeScript(scriptPath: string, specPath: string, outputPath: string, cwd: string): string {
  if (!existsSync(scriptPath)) {
    return `ERROR: Script not found: ${scriptPath}\nCheck that jarvis/skills are properly installed.`
  }

  mkdirSync(dirname(outputPath), { recursive: true })

  const result = spawnSync('node', [scriptPath, specPath, outputPath], {
    cwd,
    encoding: 'utf8',
    shell: false,
    timeout: 60_000,
  })

  const output = [result.stdout, result.stderr].filter(Boolean).join('\n').trim()

  if (result.status === 0) {
    return output || `Completed → ${outputPath}`
  }

  throw new Error(output || `Exited with code ${result.status ?? 'unknown'}`)
}

// ─── Shared Tool Definition Builder ──────────────────────────────────────────

function createDefinition(name: string, description: string, required: string[]): JarvisToolDefinition {
  return {
    type: 'function',
    function: {
      name,
      description,
      parameters: {
        type: 'object',
        properties: {
          spec: {
            oneOf: [
              { type: 'string', description: 'JSON spec as a string' },
              { type: 'object', description: 'JSON spec as an object' },
            ],
          },
          spec_path: { type: 'string', description: 'Path to a JSON spec file' },
          output_path: { type: 'string', description: 'Where to write the generated file (default: .jarvis/outputs/)' },
          formats: { type: 'array', items: { type: 'string' }, description: 'Output formats (pdf, docx)' },
          theme: { type: 'string', description: 'Theme: midnight|paper|forest|ocean|corporate|neon (deck), dark|light|corporate (dashboard)' },
          title: { type: 'string', description: 'Optional title override' },
        },
        required,
      },
    },
  }
}

// ─── Handlers ─────────────────────────────────────────────────────────────────

function buildDashboardHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const root = findWorkspaceRoot(cwd)
  const { specPath } = resolveSpec(args, cwd)
  const outputPath = resolveOutput(args, cwd, `dashboard-${Date.now()}.html`)
  const script = join(root, 'jarvis', 'skills', 'dashboard-builder', 'scripts', 'build_dashboard.js')

  try {
    return runNodeScript(script, specPath, outputPath, cwd)
  } catch (e) {
    return `Dashboard build failed: ${(e as Error).message}\n\nSpec path: ${specPath}\nScript: ${script}`
  }
}

function buildDeckHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const root = findWorkspaceRoot(cwd)
  const { specPath } = resolveSpec(args, cwd)
  const outputPath = resolveOutput(args, cwd, `deck-${Date.now()}.pptx`)
  const script = join(root, 'jarvis', 'skills', 'deck-builder', 'scripts', 'render_deck.js')

  try {
    return runNodeScript(script, specPath, outputPath, cwd)
  } catch (e) {
    return `Deck build failed: ${(e as Error).message}\n\nSpec path: ${specPath}\nScript: ${script}`
  }
}

function buildReportHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const root = findWorkspaceRoot(cwd)
  const { specPath } = resolveSpec(args, cwd)

  const formats = Array.isArray(args.formats)
    ? (args.formats as string[])
    : [String(args.formats ?? 'docx')]

  const outputs: string[] = []

  for (const fmt of formats) {
    const normalized = fmt.toLowerCase()
    if (!['pdf', 'docx'].includes(normalized)) continue

    const outputPath = resolveOutput(args, cwd, `report-${Date.now()}.${normalized}`)
    const script = normalized === 'docx'
      ? join(root, 'jarvis', 'skills', 'report-builder', 'scripts', 'build_docx.js')
      : join(root, 'jarvis', 'skills', 'report-builder', 'scripts', 'build_pdf.js')

    try {
      runNodeScript(script, specPath, outputPath, cwd)
      outputs.push(outputPath)
    } catch (e) {
      return `Report build failed (${normalized}): ${(e as Error).message}`
    }
  }

  return outputs.length > 0
    ? `Built report${outputs.length > 1 ? 's' : ''}: ${outputs.join(', ')}`
    : 'No valid formats specified. Use "docx" or "pdf".'
}

function buildSheetHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const root = findWorkspaceRoot(cwd)
  const { specPath } = resolveSpec(args, cwd)

  const formats = Array.isArray(args.formats)
    ? (args.formats as string[])
    : [String(args.formats ?? 'xlsx')]

  const ext = formats.includes('csv') ? 'csv' : 'xlsx'
  const outputPath = resolveOutput(args, cwd, `sheet-${Date.now()}.${ext}`)
  const script = join(root, 'jarvis', 'skills', 'sheet-builder', 'scripts', 'build_sheet.js')

  try {
    return runNodeScript(script, specPath, outputPath, cwd)
  } catch (e) {
    return `Sheet build failed: ${(e as Error).message}\n\nSpec path: ${specPath}\nScript: ${script}`
  }
}

// ─── Exports ──────────────────────────────────────────────────────────────────

export function createBuilderToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition(
        'BuildDashboard',
        'Generate an interactive HTML dashboard with KPI cards, charts (bar/line/pie), and tables from a JSON spec. Themes: dark | light | corporate.',
        ['spec']
      ),
      handler: buildDashboardHandler,
    },
    {
      definition: createDefinition(
        'BuildDeck',
        'Generate a professional PowerPoint (.pptx) from a JSON spec. Supports 6 layouts (title/section/content_image/stat/quote/closing) and 6 themes (midnight/paper/forest/ocean/corporate/neon).',
        ['spec']
      ),
      handler: buildDeckHandler,
    },
    {
      definition: createDefinition(
        'BuildReport',
        'Generate a Word document (.docx) or PDF from a JSON spec. Supports sections with body text, tables, code blocks, bullet lists, and stat highlights.',
        ['spec']
      ),
      handler: buildReportHandler,
    },
    {
      definition: createDefinition(
        'BuildSheet',
        'Generate a formatted Excel workbook (.xlsx) or CSV from a structured spec. Supports multiple sheets, styled headers, alternating rows, freeze-top, and auto-filter.',
        ['spec']
      ),
      handler: buildSheetHandler,
    },
  ]
}
