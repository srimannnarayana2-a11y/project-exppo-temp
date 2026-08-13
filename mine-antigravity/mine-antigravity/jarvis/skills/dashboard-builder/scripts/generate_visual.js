// generate_visual.js
// Creates an abstract gradient + centered icon PNG, for slides that need a visual
// but have no photo available (no internet fetch, no user upload).
// Usage: node generate_visual.js <out.png> <theme> <icon_name> <width_px> <height_px>
// icon_name = any react-icons/fa icon name, e.g. "FaSolarPanel", "FaChartLine"

const fs = require("fs");
const path = require("path");
const sharp = require("sharp");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const FA = require("react-icons/fa");

const THEMES = JSON.parse(fs.readFileSync(path.join(__dirname, "themes.json"), "utf8"));

function hexToRgb(hex) {
  const n = parseInt(hex, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

async function main() {
  const [, , outPath, themeName, iconName, wArg, hArg] = process.argv;
  if (!outPath || !themeName) {
    console.error("Usage: node generate_visual.js <out.png> <theme> [iconName] [width] [height]");
    process.exit(1);
  }
  const t = THEMES[themeName];
  if (!t) throw new Error(`unknown theme "${themeName}"`);
  const W = parseInt(wArg || "1200", 10);
  const H = parseInt(hArg || "1350", 10);

  const c1 = hexToRgb(t.bgAlt);
  const c2 = hexToRgb(t.cardBg);
  const accent = t.accent;

  // Layered radial-gradient-ish SVG background (diagonal linear gradient + soft accent blob)
  const svgBg = `
  <svg width="${W}" height="${H}" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="rgb(${c1.r},${c1.g},${c1.b})"/>
        <stop offset="100%" stop-color="rgb(${c2.r},${c2.g},${c2.b})"/>
      </linearGradient>
    </defs>
    <rect width="${W}" height="${H}" fill="url(#g)"/>
    <circle cx="${W * 0.78}" cy="${H * 0.22}" r="${W * 0.32}" fill="#${accent}" opacity="0.14"/>
    <circle cx="${W * 0.18}" cy="${H * 0.82}" r="${W * 0.22}" fill="#${accent}" opacity="0.10"/>
  </svg>`;

  const layers = [{ input: Buffer.from(svgBg) }];

  const IconComp = FA[iconName] || FA.FaShapes;
  if (IconComp) {
    const iconSize = Math.round(Math.min(W, H) * 0.28);
    const svgIcon = ReactDOMServer.renderToStaticMarkup(
      React.createElement(IconComp, { size: iconSize, color: `#${accent}` })
    );
    const iconPngBuf = await sharp(Buffer.from(
      `<svg xmlns="http://www.w3.org/2000/svg" width="${iconSize}" height="${iconSize}">${svgIcon.replace(/^<svg[^>]*>|<\/svg>$/g, "")}</svg>`
    )).png().toBuffer();
    layers.push({
      input: iconPngBuf,
      left: Math.round((W - iconSize) / 2),
      top: Math.round((H - iconSize) / 2),
    });
  }

  await sharp({ create: { width: W, height: H, channels: 4, background: { r: 0, g: 0, b: 0, alpha: 0 } } })
    .composite(layers)
    .png()
    .toFile(outPath);

  console.log(`Wrote ${outPath} (${W}x${H}, theme "${themeName}", icon "${iconName || "none"}")`);
}

main().catch((e) => { console.error(e); process.exit(1); });