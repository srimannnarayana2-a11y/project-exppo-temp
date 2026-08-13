// toolkit.js — composable pptxgenjs helpers. NOT a rigid renderer.
//
// Usage pattern (write your own script, this is a library you require()):
//
//   const pptxgen = require("pptxgenjs");
//   const tk = require("./toolkit.js");
//   const pres = new pptxgen();
//   pres.defineLayout({ name: "WIDE", width: tk.W, height: tk.H });
//   pres.layout = "WIDE";
//   const theme = tk.loadTheme("midnight");
//
//   tk.addTitleSlide(pres, theme, { title: "...", subtitle: "..." });   // fast helper
//
//   const slide = pres.addSlide();                                      // or go fully custom —
//   slide.background = { color: theme.bg };                             // raw pptxgenjs API,
//   slide.addText("anything at all", { x: 1, y: 1, w: 5, h: 1, ... });   // no restrictions
//
//   tk.addStatSlide(pres, theme, { stat: "70%", label: "..." });
//
//   pres.writeFile({ fileName: "output.pptx" });
//
// The helpers below cover common slide patterns fast. They are a starting point,
// not a whitelist — mix them freely with raw slide.addText/addImage/addShape/addChart/addTable
// calls for anything a helper doesn't cover (multi-column layouts, native pptxgenjs charts,
// custom shapes, animations-via-build-steps, whatever the request actually needs).

const fs = require("fs");
const path = require("path");

const THEMES = JSON.parse(fs.readFileSync(path.join(__dirname, "themes.json"), "utf8"));

const W = 13.33, H = 7.5; // LAYOUT_WIDE inches — export so scripts can define the same layout
const MARGIN = 0.6;

function loadTheme(name) {
  const t = THEMES[name];
  if (!t) throw new Error(`unknown theme "${name}". Available: ${Object.keys(THEMES).join(", ")}. Or just build a theme object inline — {bg, bgAlt, text, textMuted, accent, cardBg, fontTitle, fontBody} — nothing requires using themes.json.`);
  return t;
}

function resolveImage(p) {
  if (!p) return null;
  const abs = path.isAbsolute(p) ? p : path.resolve(process.cwd(), p);
  if (!fs.existsSync(abs)) throw new Error(`image not found: ${p} (resolved ${abs})`);
  return abs;
}

// ---------- addTitleSlide ----------
// opts: { title, subtitle?, kicker?, image? }
function addTitleSlide(pres, t, opts) {
  const slide = pres.addSlide();
  const hasImage = !!opts.image;
  const textW = hasImage ? 6.6 : W - MARGIN * 2;
  slide.background = { color: t.bg };
  if (hasImage) {
    slide.addImage({ path: resolveImage(opts.image), x: 7.33, y: 0, w: 6.0, h: H, sizing: { type: "cover", w: 6.0, h: H } });
  }
  if (opts.kicker) {
    slide.addText(opts.kicker.toUpperCase(), { x: MARGIN, y: 1.6, w: textW, h: 0.4, fontFace: t.fontBody, fontSize: 14, color: t.accent, charSpacing: 2, bold: true, margin: 0 });
  }
  slide.addText(opts.title, { x: MARGIN, y: 2.1, w: textW, h: 2.2, fontFace: t.fontTitle, fontSize: 44, color: t.text, bold: true, valign: "top", margin: 0 });
  if (opts.subtitle) {
    slide.addText(opts.subtitle, { x: MARGIN, y: 4.3, w: textW, h: 1.0, fontFace: t.fontBody, fontSize: 18, color: t.textMuted, margin: 0 });
  }
  return slide; // returned so the caller can keep adding to it if they want
}

// ---------- addSectionSlide (divider) ----------
// opts: { heading, index?, subheading? }
function addSectionSlide(pres, t, opts) {
  const slide = pres.addSlide();
  slide.background = { color: t.bgAlt };
  if (opts.index) {
    slide.addText(opts.index, { x: MARGIN, y: 1.5, w: 3, h: 1.2, fontFace: t.fontTitle, fontSize: 40, color: t.accent, bold: true, margin: 0 });
  }
  slide.addText(opts.heading, { x: MARGIN, y: 2.8, w: W - MARGIN * 2, h: 1.6, fontFace: t.fontTitle, fontSize: 36, color: t.text, bold: true, margin: 0 });
  if (opts.subheading) {
    slide.addText(opts.subheading, { x: MARGIN, y: 4.3, w: W - MARGIN * 2 - 3, h: 0.8, fontFace: t.fontBody, fontSize: 16, color: t.textMuted, margin: 0 });
  }
  return slide;
}

// ---------- addContentImageSlide ----------
// opts: { title, bullets: [...], image, image_side?: "left"|"right" }
function addContentImageSlide(pres, t, opts) {
  const slide = pres.addSlide();
  slide.background = { color: t.bg };
  const imageSide = opts.image_side === "left" ? "left" : "right";
  const imgW = 5.8;
  const textX = imageSide === "left" ? imgW + 0.9 : MARGIN;
  const imgX = imageSide === "left" ? 0 : W - imgW;
  const textW = W - imgW - 0.9 - MARGIN;
  slide.addImage({ path: resolveImage(opts.image), x: imgX, y: 0, w: imgW, h: H, sizing: { type: "cover", w: imgW, h: H } });
  slide.addText(opts.title, { x: textX, y: 0.8, w: textW, h: 1.2, fontFace: t.fontTitle, fontSize: 28, color: t.text, bold: true, margin: 0 });
  const bulletItems = opts.bullets.map((b, i) => ({
    text: b,
    options: { bullet: true, breakLine: i !== opts.bullets.length - 1, fontFace: t.fontBody, fontSize: 16, color: t.textMuted, paraSpaceAfter: 14 },
  }));
  slide.addText(bulletItems, { x: textX, y: 2.2, w: textW, h: H - 3.0, margin: 0, valign: "top" });
  return slide;
}

// ---------- addContentSlide (no image, just title + bullets, full width) ----------
// opts: { title, bullets: [...] }
function addContentSlide(pres, t, opts) {
  const slide = pres.addSlide();
  slide.background = { color: t.bg };
  slide.addText(opts.title, { x: MARGIN, y: 0.7, w: W - MARGIN * 2, h: 1.1, fontFace: t.fontTitle, fontSize: 30, color: t.text, bold: true, margin: 0 });
  const bulletItems = opts.bullets.map((b, i) => ({
    text: b,
    options: { bullet: true, breakLine: i !== opts.bullets.length - 1, fontFace: t.fontBody, fontSize: 18, color: t.textMuted, paraSpaceAfter: 16 },
  }));
  slide.addText(bulletItems, { x: MARGIN, y: 2.0, w: W - MARGIN * 2, h: H - 2.8, margin: 0, valign: "top" });
  return slide;
}

// ---------- addStatSlide ----------
// opts: { stat, label, supporting? }
function addStatSlide(pres, t, opts) {
  const slide = pres.addSlide();
  slide.background = { color: t.bg };
  slide.addText(opts.stat, { x: MARGIN, y: 2.0, w: W - MARGIN * 2, h: 2.4, fontFace: t.fontTitle, fontSize: 96, color: t.accent, bold: true, align: "center", margin: 0 });
  slide.addText(opts.label, { x: MARGIN, y: 4.4, w: W - MARGIN * 2, h: 0.8, fontFace: t.fontBody, fontSize: 20, color: t.text, align: "center", margin: 0 });
  if (opts.supporting) {
    slide.addText(opts.supporting, { x: 2.5, y: 5.2, w: W - 5, h: 0.8, fontFace: t.fontBody, fontSize: 14, color: t.textMuted, align: "center", margin: 0 });
  }
  return slide;
}

// ---------- addQuoteSlide ----------
// opts: { quote, attribution? }
function addQuoteSlide(pres, t, opts) {
  const slide = pres.addSlide();
  slide.background = { color: t.bgAlt };
  slide.addText(`"${opts.quote}"`, { x: 2.0, y: 2.0, w: W - 4.0, h: 2.8, fontFace: t.fontTitle, fontSize: 30, italic: true, color: t.text, align: "center", valign: "middle", margin: 0 });
  if (opts.attribution) {
    slide.addText(`— ${opts.attribution}`, { x: 2.0, y: 4.9, w: W - 4.0, h: 0.6, fontFace: t.fontBody, fontSize: 16, color: t.accent, align: "center", margin: 0 });
  }
  return slide;
}

// ---------- addClosingSlide ----------
// opts: { heading, subtext?, cta? }
function addClosingSlide(pres, t, opts) {
  const slide = pres.addSlide();
  slide.background = { color: t.bg };
  slide.addText(opts.heading, { x: MARGIN, y: 2.6, w: W - MARGIN * 2, h: 1.2, fontFace: t.fontTitle, fontSize: 40, color: t.text, bold: true, align: "center", margin: 0 });
  if (opts.subtext) {
    slide.addText(opts.subtext, { x: 2.5, y: 3.9, w: W - 5.0, h: 0.8, fontFace: t.fontBody, fontSize: 18, color: t.textMuted, align: "center", margin: 0 });
  }
  if (opts.cta) {
    const btnW = 3.2, btnH = 0.65;
    slide.addShape("roundRect", { x: (W - btnW) / 2, y: 4.9, w: btnW, h: btnH, rectRadius: 0.12, fill: { color: t.accent }, line: { type: "none" } });
    slide.addText(opts.cta, { x: (W - btnW) / 2, y: 4.9, w: btnW, h: btnH, fontFace: t.fontBody, fontSize: 16, bold: true, color: t.bg, align: "center", valign: "middle", margin: 0 });
  }
  return slide;
}

// ---------- addBlankSlide ----------
// Just gives you a themed-background slide and hands it back — for anything fully custom.
function addBlankSlide(pres, t, bg) {
  const slide = pres.addSlide();
  slide.background = { color: bg || t.bg };
  return slide;
}

module.exports = {
  W, H, MARGIN,
  loadTheme,
  resolveImage,
  addTitleSlide,
  addSectionSlide,
  addContentImageSlide,
  addContentSlide,
  addStatSlide,
  addQuoteSlide,
  addClosingSlide,
  addBlankSlide,
};