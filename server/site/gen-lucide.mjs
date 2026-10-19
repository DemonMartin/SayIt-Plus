// Copies the exact lucide icons the app uses into server/site/lucide.mjs, so the site's app mock
// matches the app 1:1 without a runtime dependency. Hand-drawn stand-ins were tried and read wrong
// (the settings gear looked like a sun). Needs client/node_modules; run: node server/site/gen-lucide.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const pkgDir = path.join(here, '..', '..', 'client', 'node_modules', 'lucide-react')
const dir = path.join(pkgDir, 'dist', 'esm', 'icons')
const pkg = JSON.parse(fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8'))

// Site name -> lucide file. lucide-react exports old names as aliases (Home = house,
// Wand2 = wand-sparkles, CheckCircle2 = circle-check), so the file names differ from the imports.
const map = {
  home: 'house', clock: 'clock', bookOpen: 'book-open', settings: 'settings', info: 'info', wifi: 'wifi', cpu: 'cpu',
  cloud: 'cloud', audioLines: 'audio-lines', sparkles: 'sparkles', wand2: 'wand-sparkles', minus: 'minus', square: 'square',
  x: 'x', mic: 'mic', type: 'type', zap: 'zap', download: 'download', search: 'search', check: 'check',
  checkCircle2: 'circle-check', pencil: 'pencil', plus: 'plus', refreshCw: 'refresh-cw', trash2: 'trash-2', server: 'server',
  user: 'user', stethoscope: 'stethoscope', palette: 'palette', chevronDown: 'chevron-down', chevronUp: 'chevron-up',
  star: 'star', copy: 'copy', play: 'play', rotateCcw: 'rotate-ccw', folderOpen: 'folder-open', externalLink: 'external-link',
  helpCircle: 'circle-help', xCircle: 'circle-x', minusCircle: 'circle-minus', alertCircle: 'circle-alert', send: 'send',
  hardDrive: 'hard-drive', spellCheck: 'spell-check', upload: 'upload', cloudUpload: 'cloud-upload', settings2: 'settings-2',
  // The Kiro IDE's activity bar in the hero (VS Code's explorer / search / source control / extensions).
  files: 'files', gitBranch: 'git-branch', blocks: 'blocks',
  // The Codex desktop app in the hero.
  squarePen: 'square-pen', folder: 'folder', laptop: 'laptop', arrowUp: 'arrow-up', chevronRight: 'chevron-right',
  // The company-network diagram, the model wall and the per-app rules.
  globe: 'globe', shieldCheck: 'shield-check', plug: 'plug', container: 'container', link: 'link', gauge: 'gauge',
  notepadText: 'notepad-text',
}

const out = {}
const missing = []
for (const [name, file] of Object.entries(map)) {
  const p = path.join(dir, `${file}.js`)
  if (!fs.existsSync(p)) {
    missing.push(file)
    continue
  }
  const src = fs.readFileSync(p, 'utf8')
  const m = src.match(/createLucideIcon\(\s*["'][^"']+["']\s*,\s*(\[[\s\S]*\])\s*\);/)
  if (!m) {
    missing.push(`${file}(parse)`)
    continue
  }
  // The node array is plain JS literals: [["path", { d: "...", key: "..." }], ...]
  const nodes = Function(`return ${m[1]}`)()
  out[name] = nodes
    .map(([tag, attrs]) => `<${tag} ${Object.entries(attrs).filter(([k]) => k !== 'key').map(([k, v]) => `${k}="${v}"`).join(' ')}/>`)
    .join('')
}
if (missing.length) {
  console.error(`missing icons: ${missing.join(', ')}`)
  process.exit(1)
}

const body = `// Generated from lucide-react ${pkg.version} (the version the app ships), ISC License, https://lucide.dev.
// Exact copies of the icons used in the SayIt app, so the site's app mock matches it 1:1.
// Regenerate with node server/site/gen-lucide.mjs instead of hand-editing.
export default ${JSON.stringify(out, null, 2)}
`
fs.writeFileSync(path.join(here, 'lucide.mjs'), body, 'utf8')
console.log(`icons ${Object.keys(out).length}, lucide-react ${pkg.version}`)
