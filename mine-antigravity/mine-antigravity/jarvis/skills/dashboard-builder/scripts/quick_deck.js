// render_deck.js
// Usage: node render_deck.js <spec.json> <output.pptx>
// spec.json shape: { "theme": "midnight", "slides": [ {layout, ...fields}, ... ] }
// See ../references/layout_schema.md for the field spec per layout.

const path = require("path");
const fs = require("fs");
const pptxgen = require("pptxgenjs");

const THEMES = JSON.parse(fs.readFileSync(path.join(__dirname, "themes.json"), "utf8"));

const W = 13.33, H = 7.5; // LAYOUT_WIDE inches
const MARGIN = 0.6;

function requireFields(slide, fields) {
  for (const f of fields) {
    if (slide[f] === undefined || slide[f] === null || slide[f] === "") {
      throw new Error(`layout "${slide.layout}" missing required field "${f}"`);
    }
  }
}

function resolveImage(p) {
  if (!p) return null;
  const abs = path.isAbsolute(p) ? p : path.resolve(process.cwd(), p);
  if (!fs.existsSync(abs)) throw new Error(`image not found: ${p} (resolved ${abs})`);
  return abs;
}

// ---------- Layout: title ----------
function layoutTitle(pres, s, t) {
  requireFields(s, ["title"]);
  const slide = pres.addSlide();
  const hasImage = !!s.image;
  const textW = hasImage ? 6.6 : W - MARGIN * 2;

  slide.background = { color: t.bg };

  if (hasImage) {
    slide.addImage({
      path: resolveImage(s.image),
      x: 7.33, y: 0, w: 6.0, h: H,
      sizing: { type: "cover", w: 6.0, h: H },
    });
  }

  if (s.kicker) {
    slide.addText(s.kicker.toUpperCase(), {
      x: MARGIN, y: 1.6, w: textW, h: 0.4,
      fontFace: t.fontBody, fontSize: 14, color: t.accent, charSpacing: 2, bold: true, margin: 0,
    });
  }

  slide.addText(s.title, {
    x: MARGIN, y: 2.1, w: textW, h: 2.2,
    fontFace: t.fontTitle, fontSize: 44, color: t.text, bold: true, valign: "top", margin: 0,
  });

  if (s.subtitle) {
    slide.addText(s.subtitle, {
      x: MARGIN, y: 4.3, w: textW, h: 1.0,
      fontFace: t.fontBody, fontSize: 18, color: t.textMuted, margin: 0,
    });
  }
}

// ---------- Layout: section (divider) ----------
function layoutSection(pres, s, t) {
  requireFields(s, ["heading"]);
  const slide = pres.addSlide();
  slide.background = { color: t.bgAlt };

  if (s.index) {
    slide.addText(s.index, {
      x: MARGIN, y: 1.5, w: 3, h: 1.2,
      fontFace: t.fontTitle, fontSize: 40, color: t.accent, bold: true, margin: 0,
    });
  }

  slide.addText(s.heading, {
    x: MARGIN, y: 2.8, w: W - MARGIN * 2, h: 1.6,
    fontFace: t.fontTitle, fontSize: 36, color: t.text, bold: true, margin: 0,
  });

  if (s.subheading) {
    slide.addText(s.subheading, {
      x: MARGIN, y: 4.3, w: W - MARGIN * 2 - 3, h: 0.8,
      fontFace: t.fontBody, fontSize: 16, color: t.textMuted, margin: 0,
    });
  }
}

// ---------- Layout: content_image ----------
function layoutContentImage(pres, s, t) {
  requireFields(s, ["title", "bullets", "image"]);
  const slide = pres.addSlide();
  slide.background = { color: t.bg };
  const imageSide = s.image_side === "left" ? "left" : "right";
  const imgW = 5.8;
  const textX = imageSide === "left" ? imgW + 0.9 : MARGIN;
  const imgX = imageSide === "left" ? 0 : W - imgW;
  const textW = W - imgW - 0.9 - MARGIN;

  slide.addImage({
    path: resolveImage(s.image),
    x: imgX, y: 0, w: imgW, h: H,
    sizing: { type: "cover", w: imgW, h: H },
  });

  slide.addText(s.title, {
    x: textX, y: 0.8, w: textW, h: 1.2,
    fontFace: t.fontTitle, fontSize: 28, color: t.text, bold: true, margin: 0,
  });

  const bulletItems = s.bullets.map((b, i) => ({
    text: b,
    options: {
      bullet: true, breakLine: i !== s.bullets.length - 1,
      fontFace: t.fontBody, fontSize: 16, color: t.textMuted, paraSpaceAfter: 14,
    },
  }));
  slide.addText(bulletItems, { x: textX, y: 2.2, w: textW, h: H - 3.0, margin: 0, valign: "top" });
}

// ---------- Layout: stat ----------
function layoutStat(pres, s, t) {
  requireFields(s, ["stat", "label"]);
  const slide = pres.addSlide();
  slide.background = { color: t.bg };

  slide.addText(s.stat, {
    x: MARGIN, y: 2.0, w: W - MARGIN * 2, h: 2.4,
    fontFace: t.fontTitle, fontSize: 96, color: t.accent, bold: true, align: "center", margin: 0,
  });

  slide.addText(s.label, {
    x: MARGIN, y: 4.4, w: W - MARGIN * 2, h: 0.8,
    fontFace: t.fontBody, fontSize: 20, color: t.text, align: "center", margin: 0,
  });

  if (s.supporting) {
    slide.addText(s.supporting, {
      x: 2.5, y: 5.2, w: W - 5, h: 0.8,
      fontFace: t.fontBody, fontSize: 14, color: t.textMuted, align: "center", margin: 0,
    });
  }
}

// ---------- Layout: quote ----------
function layoutQuote(pres, s, t) {
  requireFields(s, ["quote"]);
  const slide = pres.addSlide();
  slide.background = { color: t.bgAlt };

  slide.addText(`"${s.quote}"`, {
    x: 2.0, y: 2.0, w: W - 4.0, h: 2.8,
    fontFace: t.fontTitle, fontSize: 30, italic: true, color: t.text, align: "center", valign: "middle", margin: 0,
  });

  if (s.attribution) {
    slide.addText(`— ${s.attribution}`, {
      x: 2.0, y: 4.9, w: W - 4.0, h: 0.6,
      fontFace: t.fontBody, fontSize: 16, color: t.accent, align: "center", margin: 0,
    });
  }
}

// ---------- Layout: closing ----------
function layoutClosing(pres, s, t) {
  requireFields(s, ["heading"]);
  const slide = pres.addSlide();
  slide.background = { color: t.bg };

  slide.addText(s.heading, {
    x: MARGIN, y: 2.6, w: W - MARGIN * 2, h: 1.2,
    fontFace: t.fontTitle, fontSize: 40, color: t.text, bold: true, align: "center", margin: 0,
  });

  if (s.subtext) {
    slide.addText(s.subtext, {
      x: 2.5, y: 3.9, w: W - 5.0, h: 0.8,
      fontFace: t.fontBody, fontSize: 18, color: t.textMuted, align: "center", margin: 0,
    });
  }

  if (s.cta) {
    const btnW = 3.2, btnH = 0.65;
    slide.addShape("roundRect", {
      x: (W - btnW) / 2, y: 4.9, w: btnW, h: btnH,
      rectRadius: 0.12, fill: { color: t.accent }, line: { type: "none" },
    });
    slide.addText(s.cta, {
      x: (W - btnW) / 2, y: 4.9, w: btnW, h: btnH,
      fontFace: t.fontBody, fontSize: 16, bold: true, color: t.bg, align: "center", valign: "middle", margin: 0,
    });
  }
}

const LAYOUTS = {
  title: layoutTitle,
  section: layoutSection,
  content_image: layoutContentImage,
  stat: layoutStat,
  quote: layoutQuote,
  closing: layoutClosing,
};

function main() {
  const [, , specPath, outPath] = process.argv;
  if (!specPath || !outPath) {
    console.error("Usage: node render_deck.js <spec.json> <output.pptx>");
    process.exit(1);
  }
  const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
  const themeName = spec.theme || "midnight";
  const t = THEMES[themeName];
  if (!t) throw new Error(`unknown theme "${themeName}". Available: ${Object.keys(THEMES).join(", ")}`);

  const pres = new pptxgen();
  pres.defineLayout({ name: "WIDE", width: W, height: H });
  pres.layout = "WIDE";

  spec.slides.forEach((s, i) => {
    const fn = LAYOUTS[s.layout];
    if (!fn) throw new Error(`slide ${i}: unknown layout "${s.layout}". Available: ${Object.keys(LAYOUTS).join(", ")}`);
    fn(pres, s, t);
  });

  pres.writeFile({ fileName: outPath }).then(() => {
    console.log(`Wrote ${outPath} (${spec.slides.length} slides, theme "${themeName}")`);
  });
}

main();