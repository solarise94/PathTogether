/**
 * F2：JPEG2000 源的画质选项描述（tools_slides.html / tools-slides.js /
 * i18n.js / engine.js）。
 *
 * JP2K 源没有「原样搬运」：preserve 与 compact 都是逐 tile 解码后重编码。
 * 默认的「完整 tile 原样搬运」文案只适用于 JPEG 编码源；识别为
 * jpeg2000-reencode 后，两个选项的描述必须换成 JP2K 语义（preserve =
 * 高质量重编码 q96 4:2:2；compact = 固定 compact 参数 q80 4:2:0）。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

const here = dirname(fileURLToPath(import.meta.url));
const pageSrc = readFileSync(
	resolve(here, "../../static/tools/tools-slides.js"), "utf8");
const templateSrc = readFileSync(
	resolve(here, "../../templates/tools_slides.html"), "utf8");
const i18nSrc = readFileSync(
	resolve(here, "../../static/i18n.js"), "utf8");

/** Pull one key's string out of a given language block of static/i18n.js. */
function i18nValue(langBlockMarker, key) {
	const blockAt = i18nSrc.indexOf(langBlockMarker);
	expect(blockAt, `language block ${langBlockMarker} not found`).toBeGreaterThan(-1);
	const at = i18nSrc.indexOf(`"${key}":`, blockAt);
	expect(at, `key ${key} not found after ${langBlockMarker}`).toBeGreaterThan(-1);
	const m = i18nSrc.slice(at).match(/"([^"]*)",/);
	return m ? m[1] : null;
}

describe("quality option description override (F2 JP2K)", () => {
	it("fires only for jpeg2000-reencode probe documents", () => {
		expect(E.qualityOptionDescOverride({ codec: "jpeg2000-reencode" })).toEqual({
			preserve: "tools.quality.preserve.desc.jp2k",
			compact: "tools.quality.compact.desc.jp2k",
		});
		// JPEG / fluorescence / absent codec → the static default text stays
		expect(E.qualityOptionDescOverride({ codec: "jpeg-baseline-passthrough" })).toBeNull();
		expect(E.qualityOptionDescOverride({ modality: "fluorescence" })).toBeNull();
		expect(E.qualityOptionDescOverride(undefined)).toBeNull();
	});

	it("zh + en JP2K descriptions exist and name the locked parameters", () => {
		for (const [marker, q96Word, q80Word] of [
			["\"zh\"", "96", "80"],
			["\"en\"", "96", "80"],
		]) {
			const preserve = i18nValue(marker, "tools.quality.preserve.desc.jp2k");
			const compact = i18nValue(marker, "tools.quality.compact.desc.jp2k");
			// preserve: every tile DECODED + RE-ENCODED, high quality (q96, 4:2:2)
			expect(preserve).toContain("96");
			expect(preserve).toContain("4:2:2");
			expect(/解码/.test(preserve) || /decoded/.test(preserve)).toBe(true);
			expect(/重新编码|re-encoded/.test(preserve)).toBe(true);
			// and it must NOT promise as-is copying
			expect(preserve).not.toContain("原样搬运：完整 tile");
			// compact: fixed compact parameters (q80, 4:2:0)
			expect(compact).toContain("80");
			expect(compact).toContain("4:2:0");
			expect(/重新编码|re-encoded/.test(compact)).toBe(true);
			void q96Word;
			void q80Word;
		}
	});

	it("the page applies the override per probe document and replays on language change", () => {
		// the override fn is wired into renderQualitySection (rerenderForLang
		// calls it on hp-lang-change, so the JP2K text survives语言切换)
		expect(pageSrc).toContain("E.qualityOptionDescOverride(probeDoc())");
		expect(pageSrc).toContain("function applyQualityOptionDescriptions()");
		expect(pageSrc.indexOf("applyQualityOptionDescriptions();"))
			.toBeGreaterThan(-1);
		// the default fallbacks are the pre-existing keys
		expect(pageSrc).toContain("t(o ? o.preserve : 'tools.quality.preserve.desc')");
		expect(pageSrc).toContain("t(o ? o.compact : 'tools.quality.compact.desc')");
		// the option description spans carry stable ids in the template
		expect(templateSrc).toContain('id="quality-preserve-desc"');
		expect(templateSrc).toContain('id="quality-compact-desc"');
		// rerenderForLang re-runs renderQualitySection (idempotent override)
		const rerender = pageSrc.slice(pageSrc.indexOf("function rerenderForLang"));
		expect(rerender.slice(0, rerender.indexOf("\n}"))).toContain("renderQualitySection()");
	});
});
