import { mkdirSync, writeFileSync } from 'fs'
import { dirname, resolve } from 'path'
import type { JarvisToolDefinition, JarvisToolEntry } from './index.js'

function createDefinition(name: string, description: string, required: string[]) {
  return {
    type: 'function' as const,
    function: {
      name,
      description,
      parameters: {
        type: 'object' as const,
        properties: {
          todos: { type: 'array', items: { type: 'string' }, description: 'Todo list entries' },
          task: { type: 'string', description: 'Single todo item' },
          notes: { type: 'string', description: 'Optional notes' }
        },
        required
      }
    }
  }
}

function todoWriteHandler(args: Record<string, unknown>, cwd = process.cwd()): string {
  const todoItems = (args.todos as unknown[] | undefined)?.map(item => String(item)) ?? []
  const task = args.task as string | undefined
  if (task && todoItems.length === 0) todoItems.push(task)
  const notes = (args.notes as string) ?? ''
  const outDir = resolve(cwd, '.jarvis')
  mkdirSync(outDir, { recursive: true })
  writeFileSync(resolve(outDir, 'todos.json'), JSON.stringify({ todos: todoItems, notes }, null, 2), 'utf8')
  return `Saved ${todoItems.length} todo item(s) to .jarvis/todos.json`
}

export function createTodoToolEntries(): JarvisToolEntry[] {
  return [
    {
      definition: createDefinition('TodoWrite', 'Persist a todo list for the current task.', ['todos']) as JarvisToolDefinition,
      handler: todoWriteHandler
    }
  ]
}
