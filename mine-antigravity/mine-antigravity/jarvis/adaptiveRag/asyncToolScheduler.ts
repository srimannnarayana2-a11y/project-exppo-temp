/**
 * jarvis/adaptiveRag/asyncToolScheduler.ts
 *
 * Non-blocking tool dispatcher for Key 4 (thinking OFF).
 *
 * Pattern: Fire-and-hold
 *   1. Agent decides what tools it needs
 *   2. Scheduler fires them ALL immediately on Key 4
 *   3. Scheduler returns a "hold" — a promise per tool
 *   4. Agent continues streaming while tools run in background
 *   5. When agent needs a result, it awaits the hold
 *
 * For document builders (PPTX/DOCX/Sheet/Dashboard):
 *   - Skeleton file created immediately (< 50ms)
 *   - Content filled when retrieval arrives
 *   - File is ready before agent finishes streaming
 *
 * No I/O blocking:
 *   - File writes: fs.promises only (never writeFileSync)
 *   - Bash: child_process exec with timeout
 *   - Builders: Node subprocess (execFile, non-blocking)
 *   - All paths have AbortSignal propagation
 */

import { exec, execFile } from 'child_process'
import { promises as fsp } from 'fs'
import { resolve, dirname } from 'path'
import { promisify } from 'util'
import type { ScheduledTool, AdaptiveRagConfig } from './types.js'

const execAsync = promisify(exec)

// Internal extension of ScheduledTool with promise handles
// (these are not exposed in the public type)
interface ScheduledToolInternal extends ScheduledTool {
  promise: Promise<string>
  resolve: (r: string) => void
  reject:  (e: Error) => void
}

type ToolHandler = (
  args: Record<string, unknown>,
  config: AdaptiveRagConfig,
  signal: AbortSignal
) => Promise<string>

// ─── Tool Handlers (all async, all non-blocking) ──────────────────────────────

const TOOL_HANDLERS: Record<string, ToolHandler> = {

  async Bash(args, config, signal) {
    const cmd = args.command as string
    const timeout = (args.timeout as number) ?? 30_000
    const ac = new AbortController()
    const cancelOnAbort = () => ac.abort()
    signal.addEventListener('abort', cancelOnAbort, { once: true })
    try {
      const { stdout, stderr } = await execAsync(cmd, {
        cwd: config.cwd,
        timeout,
        maxBuffer: 10 << 20,
        signal: ac.signal,
      })
      return (stdout || stderr || '(no output)').trim()
    } catch (e: unknown) {
      const err = e as Error & { stdout?: string; stderr?: string; code?: number }
      return [err.stdout?.trim(), err.stderr?.trim()].filter(Boolean).join('\n') ||
        `Exit ${err.code ?? 1}: ${err.message}`
    } finally {
      signal.removeEventListener('abort', cancelOnAbort)
    }
  },

  async FileWrite(args, config) {
    const fp      = resolve(config.cwd, args.file_path as string)
    const content = args.content as string
    await fsp.mkdir(dirname(fp), { recursive: true })
    await fsp.writeFile(fp, content, 'utf8')
    const lines = content.split('\n').length
    return `Wrote ${lines} lines to ${args.file_path}`
  },

  async FileRead(args, config) {
    const fp    = resolve(config.cwd, args.file_path as string)
    const text  = await fsp.readFile(fp, 'utf8')
    const lines = text.split('\n')
    const s     = Math.max(0, ((args.start_line as number) ?? 1) - 1)
    const e     = (args.end_line as number) ?? lines.length
    return lines.slice(s, e).map((l, i) => `${String(s + i + 1).padStart(4)} │ ${l}`).join('\n')
  },

  async BuildDeck(args, config, signal) {
    return runBuilder('render_deck.js', 'deck-builder', args, config, signal)
  },

  async BuildReport(args, config, signal) {
    const formats = (args.formats as string[]) ?? ['docx']
    const results: string[] = []
    await Promise.all(formats.map(async fmt => {
      const script = fmt === 'pdf' ? 'build_pdf.js' : 'build_docx.js'
      const result = await runBuilder(script, 'report-builder', { ...args, output_format: fmt }, config, signal)
      results.push(result)
    }))
    return results.join('\n')
  },

  async BuildDashboard(args, config, signal) {
    return runBuilder('build_dashboard.js', 'dashboard-builder', args, config, signal)
  },

  async BuildSheet(args, config, signal) {
    return runBuilder('build_sheet.js', 'sheet-builder', args, config, signal)
  },
}

// ─── Builder Subprocess Runner ────────────────────────────────────────────────

/**
 * Batch mode: write full spec to temp file → spawn node → wait.
 * Used as fallback when streaming is not available.
 */
async function runBuilder(
  script: string,
  skillDir: string,
  args: Record<string, unknown>,
  config: AdaptiveRagConfig,
  signal: AbortSignal
): Promise<string> {
  const scriptPath = resolve(config.cwd, 'jarvis', 'skills', skillDir, 'scripts', script)
  const specPath   = resolve(config.cwd, '.jarvis', 'builder-specs', `spec-${Date.now()}.json`)
  const outputPath = (args.output_path as string) ??
    resolve(config.cwd, '.jarvis', 'output', `output-${Date.now()}.${getExt(skillDir)}`)

  // Write spec and ensure output dir exist in parallel
  await Promise.all([
    fsp.mkdir(dirname(specPath), { recursive: true })
      .then(() => fsp.writeFile(specPath, JSON.stringify(args.spec ?? args, null, 2), 'utf8')),
    fsp.mkdir(dirname(outputPath), { recursive: true }),
  ])

  return new Promise((resolve_, reject) => {
    const child = execFile(
      'node', [scriptPath, specPath, outputPath],
      { cwd: config.cwd, timeout: 120_000, maxBuffer: 5 << 20 },
      (err, stdout, stderr) => {
        if (err) reject(new Error(stderr || err.message))
        else resolve_(stdout.trim() || `Built: ${outputPath}`)
      }
    )
    signal.addEventListener('abort', () => child.kill('SIGTERM'), { once: true })
  })
}

/**
 * Streaming mode: spawn renderer, pipe slide JSON lines to its stdin as they're generated.
 * Returns a { write(line), close(), done } interface.
 * This is the fast path — renders slide-by-slide as LLM streams JSON.
 */
export function createStreamingBuilder(
  type: 'deck' | 'report' | 'dashboard' | 'sheet',
  outputPath: string,
  config: AdaptiveRagConfig,
  signal: AbortSignal
): { write: (line: string) => void; close: () => void; done: Promise<string> } {
  const scriptMap = {
    deck:      'render_deck_stream.js',
    report:    'build_docx_stream.js',
    dashboard: 'build_dashboard.js',  // already writes full file, fast enough
    sheet:     'build_sheet.js',
  }
  const skillMap = {
    deck:      'deck-builder',
    report:    'report-builder',
    dashboard: 'dashboard-builder',
    sheet:     'sheet-builder',
  }

  const scriptPath = resolve(
    config.cwd, 'jarvis', 'skills', skillMap[type], 'scripts', scriptMap[type]
  )

  // For types without a streaming script yet, fall back to batch mode via a pipe trick
  // The child reads from stdin and writes to outputPath
  const child = execFile(
    'node', [scriptPath, outputPath],
    { cwd: config.cwd, timeout: 120_000, maxBuffer: 5 << 20 },
    () => {}  // handled via done promise
  )

  let resolve_!: (r: string) => void
  let reject_!:  (e: Error)  => void
  const done = new Promise<string>((res, rej) => { resolve_ = res; reject_ = rej })

  child.on('close', (code) => {
    if (code === 0) resolve_(`Built: ${outputPath}`)
    else reject_(new Error(`Builder exited with code ${code}`))
  })
  child.on('error', (e) => reject_(e))
  signal.addEventListener('abort', () => child.kill('SIGTERM'), { once: true })

  return {
    write: (line: string) => {
      child.stdin?.write(line + '\n')
    },
    close: () => {
      child.stdin?.end()
    },
    done,
  }
}

function getExt(skillDir: string): string {
  const map: Record<string, string> = {
    'deck-builder': 'pptx', 'report-builder': 'docx',
    'dashboard-builder': 'html', 'sheet-builder': 'xlsx',
  }
  return map[skillDir] ?? 'bin'
}

// ─── Scheduler ────────────────────────────────────────────────────────────────

export class AsyncToolScheduler {
  private readonly running = new Map<string, ScheduledTool>()
  private readonly concurrencyLimit = 4

  constructor(
    private readonly config: AdaptiveRagConfig,
    private readonly globalSignal: AbortSignal
  ) {}

  /**
   * Schedule a tool call. Returns a promise that resolves with the result.
   * The tool runs immediately on Key 4 in the background.
   * Caller can await the promise whenever it needs the result.
   */
  schedule(name: string, args: Record<string, unknown>): Promise<string> {
    const id = `${name}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`

    let resolve_!: (r: string) => void
    let reject_!:  (e: Error)  => void
    const promise = new Promise<string>((res, rej) => { resolve_ = res; reject_ = rej })

    const tool: ScheduledToolInternal = {
      id, name, args,
      status: 'pending',
      startedAt: Date.now(),
      promise, resolve: resolve_, reject: reject_,
    }

    this.running.set(id, tool)
    void this.execute(tool)   // fire-and-forget — caller holds the promise
    return promise
  }

  /**
   * Pre-create a document skeleton immediately.
   * Returns a fill function: call it with content when retrieval is done.
   * This allows the skeleton file to exist on disk < 50ms after the request.
   */
  async preCreateSkeleton(
    type: 'deck' | 'report' | 'dashboard' | 'sheet',
    title: string
  ): Promise<{ path: string; fill: (spec: unknown) => Promise<string> }> {
    const extMap  = { deck: 'pptx', report: 'docx', dashboard: 'html', sheet: 'xlsx' } as const
    const toolMap = { deck: 'BuildDeck', report: 'BuildReport', dashboard: 'BuildDashboard', sheet: 'BuildSheet' } as const
    const ext     = extMap[type]
    const outPath = resolve(this.config.cwd, '.jarvis', 'output', `${title.replace(/\s+/g, '-').toLowerCase()}.${ext}`)

    await fsp.mkdir(dirname(outPath), { recursive: true })
    // Write a placeholder marker file so the output path exists immediately
    await fsp.writeFile(outPath + '.pending', JSON.stringify({ status: 'generating', title, type }), 'utf8')

    const fill = (spec: unknown): Promise<string> =>
      this.schedule(toolMap[type], { spec, output_path: outPath })

    return { path: outPath, fill }
  }

  // Queue of slot-waiters: resolvers that unblock when a slot opens
  private readonly slotWaiters: Array<() => void> = []

  private signalSlotAvailable(): void {
    const next = this.slotWaiters.shift()
    if (next) next()
  }

  private waitForSlot(): Promise<void> {
    return new Promise(resolve_ => this.slotWaiters.push(resolve_))
  }

  private async execute(tool: ScheduledToolInternal): Promise<void> {
    // If at concurrency limit, wait in queue — no busy polling
    while (this.activeCount() >= this.concurrencyLimit) {
      if (this.globalSignal.aborted) {
        tool.status = 'error'
        tool.reject(new Error('Aborted'))
        return
      }
      await this.waitForSlot()
    }

    tool.status = 'running'
    const handler = TOOL_HANDLERS[tool.name]

    if (!handler) {
      tool.status = 'error'
      tool.error = `Unknown tool: ${tool.name}`
      tool.reject(new Error(tool.error))
      this.running.delete(tool.id)
      return
    }

    // Per-tool timeout
    const ac = new AbortController()
    const globalAbort = () => ac.abort()
    this.globalSignal.addEventListener('abort', globalAbort, { once: true })

    const TOOL_TIMEOUT = 120_000
    const timeoutId = setTimeout(() => ac.abort(), TOOL_TIMEOUT)

    try {
      const result = await handler(tool.args, this.config, ac.signal)
      tool.status = 'done'
      tool.result = result
      tool.completedAt = Date.now()
      ;(tool as ScheduledToolInternal).resolve(result)
    } catch (e: unknown) {
      const msg = (e as Error).message
      tool.status = 'error'
      tool.error = msg
      ;(tool as ScheduledToolInternal).reject(new Error(msg))
    } finally {
      clearTimeout(timeoutId)
      this.globalSignal.removeEventListener('abort', globalAbort)
      this.running.delete(tool.id)
      // Wake the next waiter if any
      this.signalSlotAvailable()
    }
  }

  private activeCount(): number {
    return [...this.running.values()].filter(t => t.status === 'running').length
  }

  /** Summary of all scheduled/running tools for display */
  getStatus(): string {
    const tools = [...this.running.values()]
    if (tools.length === 0) return 'No tools running'
    return tools.map(t =>
      `${t.name} [${t.status}] ${t.completedAt ? `${t.completedAt - t.startedAt}ms` : '...'}`
    ).join(' | ')
  }

  /** Cancel all in-flight tool calls */
  abortAll(): void {
    // globalSignal abort propagates to all tool AbortControllers
    for (const tool of this.running.values()) {
      if (tool.status === 'running' || tool.status === 'pending') {
        tool.status = 'error'
        tool.reject(new Error('Aborted'))
      }
    }
    this.running.clear()
  }
}
