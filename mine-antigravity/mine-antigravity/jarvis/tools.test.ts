import { test, expect } from 'bun:test'
import { createJarvisToolRegistry, executeJarvisToolByName } from './tools/index.js'
import { listJarvisSkills } from './skillRegistry.js'

const tempDir = '/tmp/jarvis-tools-test'

function resetTempDir() {
  const fs = require('fs')
  const path = require('path')
  if (fs.existsSync(tempDir)) {
    fs.rmSync(tempDir, { recursive: true, force: true })
  }
  fs.mkdirSync(tempDir, { recursive: true })
  return tempDir
}

test('creates a registry with the core tool names', () => {
  const registry = createJarvisToolRegistry()
  const names = registry.map(tool => tool.function.name)

  expect(names).toContain('Read')
  expect(names).toContain('Write')
  expect(names).toContain('Edit')
  expect(names).toContain('Bash')
  expect(names).toContain('Grep')
  expect(names).toContain('Glob')
  expect(names).toContain('LS')
  expect(names).toContain('WebFetch')
  expect(names).toContain('NvidiaRagRetrieve')
})

test('executes Read and Write tools against a temporary workspace', async () => {
  const cwd = resetTempDir()
  const filePath = `${cwd}/hello.txt`

  const writeResult = await executeJarvisToolByName('Write', { file_path: filePath, content: 'hello\nworld' }, cwd)
  expect(writeResult).toContain('Wrote')

  const readResult = await executeJarvisToolByName('Read', { file_path: filePath }, cwd)
  expect(readResult).toContain('hello')
  expect(readResult).toContain('world')
})

test('routes builder-focused skill names to the builder tool workflow', async () => {
  const result = await executeJarvisToolByName('Skill', { name: 'dashboard-builder', input: 'Create a KPI dashboard' }, '/tmp')
  expect(result).toContain('BuildDashboard')
  expect(result).toContain('dashboard')
})

test('builds a spreadsheet artifact from a simple sheet spec', async () => {
  const cwd = resetTempDir()
  const spec = {
    sheets: [
      {
        name: 'Summary',
        columns: ['Name', 'Score'],
        rows: [['Ada', 10], ['Linus', 8]],
      },
    ],
  }

  const result = await executeJarvisToolByName('BuildSheet', { spec, output_path: `${cwd}/summary.xlsx` }, cwd)
  expect(result).toContain('Built sheet')
  expect(result).toContain('summary.xlsx')
})

test('discovers the builder skills from the Jarvis skills directory', () => {
  const skills = listJarvisSkills('/workspaces/mine-antigravity/mine-antigravity/jarvis')
  const names = skills.map(skill => skill.name)
  expect(names).toContain('dashboard-builder')
  expect(names).toContain('deck-builder')
  expect(names).toContain('report-builder')
  expect(names).toContain('sheet-builder')
})
