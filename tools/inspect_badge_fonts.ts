// Offline evidence from the same CanvasKit compiler used by the CLI.
// This tool accepts synthetic verification inputs only, not Sheets or profiles.
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { compile, resolveTemplateFonts } from "../freshcoat/packages/coatfile/src/index";
import { missingGlyphs } from "../freshcoat/packages/engine/src/index";
import { openRenderer, createLoader } from "../freshcoat/packages/cli/src/renderer";
import { readTemplate } from "../freshcoat/packages/cli/src/template-file";
import { createParagraphEngine } from "../freshcoat/packages/engine/src/paragraph-layout";

const [file, valuesFile, side = "front"] = process.argv.slice(2);
const values = JSON.parse(await readFile(valuesFile, "utf8"));
const io = { cwd: process.cwd(), stdout() {}, stderr() {} };
const { template, directory } = await readTemplate(io, file);
let remoteRequests = 0;
const report = await resolveTemplateFonts(template, {
  guessGoogle: false,
  load: createLoader(io, directory),
  fetch: async () => { remoteRequests++; throw new Error("Remote font fetching is disabled"); },
});
if (report.missing.length || report.guessed.length || remoteRequests)
  throw new Error("Font resolution was not fully local and declared");
const renderer = await openRenderer(io, { root: directory, build: "default", fonts: report.fonts });
try {
  const faces = renderer.fonts.get("Google Sans")!;
  if (faces.length !== 2 || renderer.fonts.size !== 1) throw new Error("Expected only Google Sans 400/700");
  const buffers = faces.map(bytes => bytes.slice().buffer);
  const engine = createParagraphEngine(renderer.ck, new Map([["Google Sans", faces]]));
  const matchedWeights: any[] = [];
  try {
    for (const [index, weight] of [400, 700].entries()) {
      const expected = renderer.ck.Typeface.MakeFreeTypeFaceFromData(buffers[index])!;
      const singleFaceEngine = createParagraphEngine(renderer.ck, new Map([["Google Sans", [faces[index]]]]));
      try {
        const sample = "Élodie O'Connor & José D’Ambrósio";
        const font = { family: "Google Sans", weight, size: 48 };
        const width = engine.measureSpanWidth(sample, font);
        const intended = singleFaceEngine.measureSpanWidth(sample, font);
        if (Math.abs(width - intended) > 0.001) throw new Error(`Weight ${weight} matched the wrong face: ${width} vs ${intended}`);
        const text = weight === 700 ? values.displayName : values.ticketTier;
        if (Array.from(expected.getGlyphIDs(text)).some(id => id === 0)) throw new Error("Selected face requires glyph fallback");
        matchedWeights.push({ weight, family: expected.getFamilyName(), exact_face_advances: true,
          missing_glyphs: [], probe_advance: width });
      } finally { singleFaceEngine.dispose(); expected.delete(); }
    }
    if (matchedWeights[0].probe_advance === matchedWeights[1].probe_advance)
      throw new Error("400 and 700 did not resolve to distinct font advances");
  } finally { engine.dispose(); }
  const compiled = compile(template, values, { width: template.width, height: template.height, frameNames: [side] });
  const frame = compiled.frames.find(f => f.name === side)!;
  const commands = renderer.compile(frame.root, {
    width: compiled.width, height: compiled.height, fonts: frame.assets.fonts, images: frame.assets.images,
  });
  if (missingGlyphs(commands).length) throw new Error("Glyphs are missing");
  const text: any[] = [];
  function visit(cmd: any) {
    if (cmd.op === "drawGroup" || cmd.op === "drawMasked") cmd.children.forEach(visit);
    if (cmd.op !== "drawText") return;
    const { layout } = cmd;
    const actual = layout.lines.map((l: any) => l.text).join("");
    if (cmd.id === "name" && (actual !== values.displayName || layout.lines.length !== 1))
      throw new Error("The full name was wrapped, truncated or ellipsized");
    for (const line of layout.lines) for (const span of line.spans) {
      if (span.font.family !== "Google Sans" || ![400, 700].includes(span.font.weight))
        throw new Error("Unexpected font family or weight");
      if (cmd.id === "name" && (span.x < 72 || span.x + span.width > 1128))
        throw new Error("Name exceeds its existing box");
    }
    text.push({ id: cmd.id, characters: [...actual].length, lines: layout.lines.length,
      family: layout.font.family, weight: layout.font.weight, size: layout.font.size,
      full_name_verified: cmd.id === "name" ? actual === values.displayName : undefined });
  }
  commands.forEach(visit);
  if (!text.some(t => t.id === "name") || !text.some(t => t.weight === 400))
    throw new Error("Name or tier text missing");
  console.log(JSON.stringify({ declared: report.declared, missing: report.missing, guessed: report.guessed,
    remote_requests: remoteRequests, missing_glyphs: [], matched_weights: matchedWeights,
    loaded_faces: [...renderer.fonts].map(([family, faces]) => ({ family, sha256: faces.map(
      bytes => createHash("sha256").update(bytes).digest("hex")) })), text }));
} finally { renderer.dispose(); }
