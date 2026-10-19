// Copies the brand logos shown on the site into server/web/img/logos/ (served as <img>, so two
// logos that both define a gradient id="a" cannot collide the way inline SVG would).
//
// The owner asked for the real logos instead of a line of small text (2026-10-16). Sources, pinned:
//   npm pack @lobehub/icons-static-svg@1.95.1   (MIT; AI brands, Kiro, Codex, Cursor)
//   Microsoft's own Fluent product icons:        res-1.cdn.office.net/.../brand-icons/product/svg/{teams,outlook}_48x1.svg
//   simple-icons@13.21.0 (CC0) for wechat / tencentqq, simple-icons@10.0.0 for visualstudiocode
//     (both were removed from later releases at the brand owners' request; recoloured below).
// The logos stay the property of their owners; they only say which services SayIt connects to.
//
// usage: node server/site/gen-logos.mjs <dir>
//   <dir>/lobe/package/icons/*.svg   (the extracted lobehub tarball)
//   <dir>/ext/{teams,outlook,vscode,wechat,qq}.svg
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const src = process.argv[2]
if (!src) {
  console.error('usage: node server/site/gen-logos.mjs <dir with lobe/ and ext/>')
  process.exit(1)
}
const out = path.join(here, '..', 'web', 'img', 'logos')
const lobe = (n) => path.join(src, 'lobe', 'package', 'icons', `${n}.svg`)
const ext = (n) => path.join(src, 'ext', `${n}.svg`)

// name -> [file, fill for single-colour marks]. Colour marks keep their own colours.
const INK = '#0b0b0c'
const LOGOS = {
  // Apps with a built-in per-app rule (client: builtinApp.*)
  teams: [ext('teams')],
  outlook: [ext('outlook')],
  kiro: [lobe('kiro-color')],
  codex: [lobe('codex-color')],
  vscode: [ext('vscode'), '#007ACC'],
  cursor: [lobe('cursor'), INK],
  wechat: [ext('wechat'), '#07C160'],
  qq: [ext('qq'), INK],
  // Speech recognition and AI cleanup providers (client: asrProviderCatalog / aiProviderCatalog)
  doubao: [lobe('doubao-color')],
  qwen: [lobe('qwen-color')],
  mimo: [lobe('xiaomimimo'), '#FF6900'],
  openai: [lobe('openai'), INK],
  gemini: [lobe('gemini-color')],
  groq: [lobe('groq'), '#F55036'],
  // The colour variant is lime (#C8FF00), unreadable on a white tile.
  openrouter: [lobe('openrouter'), INK],
  deepseek: [lobe('deepseek-color')],
  zhipu: [lobe('zhipu-color')],
  ollama: [lobe('ollama'), INK],
  // Local models: Qwen3-ASR (qwen), SenseVoice and Fun-ASR (Alibaba Tongyi Lab), Parakeet and Nemotron (NVIDIA)
  alibaba: [lobe('alibaba-color')],
  nvidia: [lobe('nvidia-color')],
}

fs.mkdirSync(out, { recursive: true })
const missing = []
for (const [name, [file, fill]] of Object.entries(LOGOS)) {
  if (!fs.existsSync(file)) {
    missing.push(`${name} (${file})`)
    continue
  }
  let svg = fs.readFileSync(file, 'utf8')
  svg = svg
    .replace(/<\?xml[^>]*>\s*/g, '')
    .replace(/<title>[\s\S]*?<\/title>/g, '')
    // lobehub ships width/height="1em" and an inline style meant for React; an <img> needs neither.
    .replace(/<svg([^>]*)>/, (m, attrs) => {
      let a = attrs.replace(/\s(width|height|style|role)="[^"]*"/g, '')
      if (fill) a = a.replace(/\sfill="[^"]*"/g, '') + ` fill="${fill}"`
      if (!/xmlns=/.test(a)) a += ' xmlns="http://www.w3.org/2000/svg"'
      return `<svg${a}>`
    })
  if (fill) svg = svg.replace(/fill="currentColor"/g, `fill="${fill}"`)
  fs.writeFileSync(path.join(out, `${name}.svg`), svg.trim() + '\n', 'utf8')
}
if (missing.length) {
  console.error(`missing: ${missing.join(', ')}`)
  process.exit(1)
}
console.log(`logos ${Object.keys(LOGOS).length} -> server/web/img/logos/`)
