/**
 * jarvis/skillRegistry.ts — Skill Registry with Warm Cache (v2)
 *
 * Upgrades over v1:
 *  - Reads SKILL.md content on first load (for /skills command)
 *  - Cache includes TTL: cleared after 5 minutes to pick up new skills
 *  - listJarvisSkills returns skills sorted by name with description extracted
 */

import { readdirSync, existsSync, readFileSync } from 'fs'
import { join, resolve } from 'path'

export interface JarvisSkillDescriptor {
  name: string
  path: string
  hasSkillMarkdown: boolean
  hasScriptsDirectory: boolean
  description: string     // Extracted from SKILL.md frontmatter
  scripts: string[]       // List of script files if scripts/ exists
}

interface CacheEntry {
  skills: JarvisSkillDescriptor[]
  loadedAt: number
}

const CACHE_TTL_MS = 5 * 60 * 1000  // 5 minutes
const skillCache = new Map<string, CacheEntry>()

function extractDescription(skillPath: string): string {
  const mdPath = join(skillPath, 'SKILL.md')
  if (!existsSync(mdPath)) return ''
  try {
    const content = readFileSync(mdPath, 'utf8')
    // Extract description from YAML frontmatter
    const match = content.match(/^---[\s\S]*?description:\s*["']?(.*?)["']?\n/m)
    if (match?.[1]) return match[1].slice(0, 120)
    // Fallback: first non-heading paragraph
    const lines = content.split('\n')
    for (const line of lines) {
      const trimmed = line.trim()
      if (trimmed && !trimmed.startsWith('#') && !trimmed.startsWith('---') && !trimmed.startsWith('name:') && !trimmed.startsWith('description:') && trimmed.length > 20) {
        return trimmed.slice(0, 120)
      }
    }
  } catch { /* ignore */ }
  return ''
}

function listScripts(skillPath: string): string[] {
  const scriptsDir = join(skillPath, 'scripts')
  if (!existsSync(scriptsDir)) return []
  try {
    return readdirSync(scriptsDir)
      .filter(f => /\.(js|ts|py|sh)$/.test(f))
      .sort()
  } catch { return [] }
}

export function listJarvisSkills(
  rootDir: string = resolve(process.cwd(), 'jarvis')
): JarvisSkillDescriptor[] {
  const cacheKey = resolve(rootDir)
  const cached = skillCache.get(cacheKey)

  if (cached && Date.now() - cached.loadedAt < CACHE_TTL_MS) {
    return cached.skills
  }

  const skillsDir = join(rootDir, 'skills')
  if (!existsSync(skillsDir)) {
    skillCache.set(cacheKey, { skills: [], loadedAt: Date.now() })
    return []
  }

  const skills: JarvisSkillDescriptor[] = readdirSync(skillsDir, { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => {
      const skillPath = join(skillsDir, entry.name)
      return {
        name: entry.name,
        path: skillPath,
        hasSkillMarkdown: existsSync(join(skillPath, 'SKILL.md')),
        hasScriptsDirectory: existsSync(join(skillPath, 'scripts')),
        description: extractDescription(skillPath),
        scripts: listScripts(skillPath),
      }
    })
    .sort((a, b) => a.name.localeCompare(b.name))

  skillCache.set(cacheKey, { skills, loadedAt: Date.now() })
  return [...skills]
}

export function clearJarvisSkillsCache(): void {
  skillCache.clear()
}

export function getSkillSummary(rootDir?: string): string {
  const skills = listJarvisSkills(rootDir)
  if (skills.length === 0) return 'No skills found.'
  return skills.map(s => {
    const scripts = s.scripts.length > 0 ? ` [${s.scripts.join(', ')}]` : ''
    const desc = s.description ? `\n    ${s.description}` : ''
    return `  ${s.name}${scripts}${desc}`
  }).join('\n')
}
