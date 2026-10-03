/**
 * U2 页面简化（/tools/slides）的纯 helper 与源码级 wiring 约定：
 *
 *  - 入口统一：file input 与 drop 都进入同一 prepareSource 流程；一次只接受
 *    一个切片文件；目录/.mrxs/.dat → MRXS 完整包提示；.svs → 尚未支持；
 *  - 扩展名只是提示（engine.INPUT_EXTENSION_HINTS），识别靠文件头魔数表
 *    （engine.SUPPORTED_MAGICS / magicSupported）——两者同在一处维护；
 *  - 画质（U3 编码档）与输出格式同一合同：prepared 可改并落盘、开始即锁定、
 *    重开以任务记录为准、荧光不提供有损模式；
 *  - strict 无损与 compact 互斥必须在 UI 阻止（核心另有类型化拒绝兜底）；
 *  - 磁盘预估跟随所选编码取上界；
 *  - 上传阶段文案与计划对齐（排队 → 上传至腾讯云 → 工作台接收 → 校验/发布
 *    → 可查看），zh/en 成对。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(resolve(here, "../..", rel), "utf8");
const shellSrc = read("templates/tools_slides.html");
const pageSrc = read("static/tools/tools-slides.js");
const cssSrc = read("static/tools/tools-slides.css");
const uploadSrc = read("static/tools/tools-slides-upload.js");
const i18nSrc = read("static/i18n.js");
const runnerSrc = read("static/tools/slide-transform/runner.js");

describe("input extension hints (engine.js, hint only — never identification)", () => {
	it("bundle inputs (.mrxs/.dat) are hinted as bundles", () => {
		expect(E.inputExtensionHint("slide.mrxs")).toBe("bundle");
		expect(E.inputExtensionHint("SLIDE.MRXS")).toBe("bundle");
		expect(E.inputExtensionHint("index.dat")).toBe("bundle");
	});

	it("SVS gets no extension hint: the TIFF header + bounded sniff identify it", () => {
		expect(E.inputExtensionHint("scan.svs")).toBeNull();
		expect(E.inputExtensionHint("SCAN.SVS")).toBeNull();
		expect(pageSrc).not.toMatch(/hint === 'svs'/);
	});

	it("supported and unknown extensions get no hint (header decides)", () => {
		expect(E.inputExtensionHint("a.kfb")).toBeNull();
		expect(E.inputExtensionHint("b.kfbf")).toBeNull();
		expect(E.inputExtensionHint("c.txt")).toBeNull();
		expect(E.inputExtensionHint("")).toBeNull();
	});

	it("hints and identification live in one place, next to the magic table", () => {
		const engineSrc = read("static/tools/slide-transform/engine.js");
		const magicsPos = engineSrc.indexOf("SUPPORTED_MAGICS");
		const hintsPos = engineSrc.indexOf("INPUT_EXTENSION_HINTS");
		expect(magicsPos).toBeGreaterThan(-1);
		expect(hintsPos).toBeGreaterThan(magicsPos);
		expect(hintsPos - magicsPos).toBeLessThan(3000);
	});
});

describe("U2 default view (template structure)", () => {
	it("drop zone + pick button + hidden-but-focusable file input, one accept list", () => {
		expect(shellSrc).toContain('id="drop-zone"');
		expect(shellSrc).toContain('id="pick-file-btn"');
		expect(shellSrc).toMatch(/<input id="file-input" name="file" type="file" accept="\.kfb,\.kfbf,\.svs" class="visually-hidden-input"/);
		expect(cssSrc).toContain(".visually-hidden-input");
		expect(cssSrc).not.toMatch(/#file-input\s*\{[^}]*display:\s*none/);
		expect(cssSrc).toContain(".drop-zone.dragover");
	});

	it("one-line local explanation with the full privacy items folded", () => {
		expect(shellSrc).toContain('id="privacy-details"');
		expect(shellSrc).toContain('data-i18n="tools.privacy.brief"');
		expect(shellSrc).toContain('data-i18n="tools.privacy.note"');
	});

	it("distinct prepare and convert stages, preparation cancellable", () => {
		expect(shellSrc).toContain('id="stage-section"');
		expect(shellSrc).toContain('id="prepare-cancel-btn"');
		expect(shellSrc).toContain('id="run-section"');
		expect(pageSrc).toContain("async function onPrepareCancel()");
		expect(pageSrc).toContain("els.prepareCancelBtn.addEventListener('click'");
	});

	it("config summary card with output summary, quality and two actions", () => {
		expect(shellSrc).toContain('id="summary-section"');
		expect(shellSrc).toContain('id="summary-grid"');
		expect(shellSrc).toContain('id="output-summary-text"');
		expect(shellSrc).toContain('data-i18n="tools.run.upload"');
		expect(shellSrc).toContain('id="more-options"');
		expect(pageSrc).toContain("renderSummary()");
	});

	it("advanced items are collapsed inside 更多选项; the disk confirm is not", () => {
		const more = shellSrc.slice(shellSrc.indexOf('id="more-options"'));
		const moreEnd = more.indexOf("</details>");
		const inner = more.slice(0, moreEnd);
		for (const id of ["channel-section", "format-section", "profile-section",
			"policy-section", "probe-section", "estimate-section"]) {
			expect(inner).toContain(`id="${id}"`);
		}
		// the space confirmation is a modal dialog outside any collapsible
		const dialog = shellSrc.slice(shellSrc.indexOf('id="disk-dialog"'));
		expect(dialog).toContain("<dialog");
		expect(dialog).not.toContain("<details");
	});

	it("job records are a collapsed details card that auto-opens with jobs", () => {
		expect(shellSrc).toMatch(/<details class="card" id="jobs-details">/);
		expect(pageSrc).toMatch(/if \(jobs\.length && !els\.jobsDetails\.open\) els\.jobsDetails\.open = true;/);
	});

	it("explicit 保存到电脑, never claiming the OPFS artifact is saved", () => {
		expect(shellSrc).toContain('data-i18n="tools.result.save"');
		expect(shellSrc).toMatch(/data-i18n="tools\.result\.not\.saved\.warn"[^<]*>/);
	});
});

describe("U2 entry flow (page source wiring)", () => {
	it("file input and drop converge on one prepareSource flow", () => {
		expect(pageSrc).toContain("async function prepareSource(fileList)");
		expect(pageSrc).toContain("els.fileInput.addEventListener('change', () => { onFilePicked(); })");
		// onFilePicked is the shared next step of the manual and dropped path
		expect(pageSrc).toMatch(/async function prepareSource\(fileList\)[\s\S]*?await onFilePicked\(file\)/);
		expect(pageSrc).toMatch(/async function onFilePicked\(explicitFile\)[\s\S]*?await runProbeFlow\(\)/);
		// handoff uses the same flow too
		expect(pageSrc).toMatch(/async function takeHandoffFile\(file\)[\s\S]*?await runProbeFlow\(\)/);
	});

	it("drop navigation is prevented anywhere on the page", () => {
		expect(pageSrc).toMatch(/for \(const type of \['dragover', 'drop'\]\)[\s\S]*?ev\.preventDefault\(\)/);
		expect(pageSrc).toContain("handleDropData(ev.dataTransfer)");
	});

	it("multiple files: visible message, nothing silently dropped or started", () => {
		expect(pageSrc).toMatch(/prepareSource\(fileList\)[\s\S]*?files\.length > 1[\s\S]*?tools\.drop\.multiple/);
		expect(pageSrc).toMatch(/onFilePicked\(explicitFile\)[\s\S]*?list\.length > 1[\s\S]*?tools\.drop\.multiple/);
	});

	it("a drop while converting is deflected with a message, not a panel reset", () => {
		// the file input is disabled during a run; the drop path needs the same
		// guard so resetFlowPanels cannot tear down the live progress UI
		expect(pageSrc).toMatch(/if \(page\.running\) \{[\s\S]*?tools\.drop\.busy/);
		expect(i18nSrc).toContain('"tools.drop.busy"');
		expect(i18nSrc).toContain('"tools.drop.busy": "A conversion is running');
	});

	it("directory/.mrxs/.dat drops explain before any copy", () => {
		expect(pageSrc).toMatch(/entry\.isDirectory[\s\S]*?tools\.drop\.bundle/);
		expect(pageSrc).toMatch(/hint === 'bundle'[\s\S]*?tools\.drop\.bundle/);
	});

	it("cancelled preparation is a typed state, not an error panel", () => {
		expect(pageSrc).toMatch(/code === 'cancelled'[\s\S]*?tools\.stage\.cancelled/);
		// the runner rejects pending prepare requests as cancelled on cancel
		expect(runnerSrc).toContain("_rejectPending('cancelled', E.ERROR_CODES.CANCELLED)");
		expect(runnerSrc).toContain("_rejectPending(reason, code = E.ERROR_CODES.IO_RECOVERABLE)");
	});
});

describe("U2 quality wiring (page source, mirrors the output-profile contract)", () => {
	it("template offers preserve (default) and compact with the plan wording", () => {
		expect(shellSrc).toContain('id="quality-preserve" name="encoding" value="preserve-source-v1" checked');
		expect(shellSrc).toContain('id="quality-compact" name="encoding" value="compact-jpeg-v1"');
		expect(shellSrc).toContain('data-i18n="tools.quality.preserve"');
		expect(shellSrc).toContain('data-i18n="tools.quality.compact"');
	});

	it("prepare passes only a brightfield quality choice; fluorescence stays undefined", () => {
		expect(pageSrc)
			.toContain("encodingProfile: sniffedModality === 'brightfield' ? selectedEncodingProfile() : undefined");
		expect(pageSrc).toMatch(/function encodingProfileForModality\(modality\)[\s\S]*?modality === 'brightfield' \? selectedEncodingProfile\(\) : undefined/);
	});

	it("fresh start passes the selection; list-start only for this job's visible group", () => {
		expect(pageSrc).toMatch(/startJob\(page\.file, \{[\s\S]*?encodingProfile,/);
		expect(pageSrc).toMatch(/startJob\(null, \{[\s\S]*?encodingProfile: startEncodingProfile,/);
		expect(pageSrc).toContain("(action === 'start' && thisPrep && !els.qualityFieldset.hidden)");
		expect(pageSrc).toContain("await page.runner.resumeJob(job.id)");
	});

	it("a radio change after prepare is persisted into the prepared record", () => {
		expect(pageSrc).toContain("setPreparedEncodingProfile(page.prep.jobId, ev.target.value)");
		expect(pageSrc).toMatch(/input\[name="encoding"\][\s\S]*?onEncodingChange/);
		expect(pageSrc).toMatch(/onEncodingChange\(ev\)[\s\S]*?page\.encodingLockedProfile \|\| els\.qualityFieldset\.hidden/);
	});

	it("a started job locks the choice and shows its actual quality", () => {
		expect(pageSrc).toContain("lockEncodingProfile(encodingProfile || E.defaultEncodingProfile())");
		expect(pageSrc).toContain("els.qualityFieldset.disabled = !!locked;");
		expect(pageSrc).toContain("t('tools.quality.locked'");
		expect(pageSrc).toMatch(/t\('tools\.result\.encoding'\)[\s\S]*?encodingLabel\(/);
	});

	it("strict-lossless and compact are mutually exclusive in the UI as well", () => {
		expect(pageSrc).toMatch(/compactRadio\.disabled = strictSel/);
		expect(pageSrc).toMatch(/strictRadio\.disabled = compactSel/);
		expect(pageSrc).toMatch(/selectedPolicy\(\) === 'strict-lossless'[\s\S]*?quality-preserve[\s\S]*?checked = true/);
		expect(pageSrc).toMatch(/ev\.target\.value === E\.ENCODING_PROFILES\.COMPACT[\s\S]*?policy-allow-edge[\s\S]*?checked = true/);
	});

	it("the disk estimate follows the chosen encoding", () => {
		expect(pageSrc).toMatch(/diskNeedBytes\(est, \{[\s\S]*?encoding: selectedEncodingProfile\(\)/);
		expect(pageSrc).toMatch(/onEncodingChange\(ev\)[\s\S]*?renderEstimate\(\)/);
	});

	it("job rows call out the non-default (compact) encoding only", () => {
		expect(pageSrc).toMatch(/job\.encodingProfile === E\.ENCODING_PROFILES\.COMPACT[\s\S]*?tools\.jobs\.encoding/);
	});
});

describe("U2 i18n (zh/en pairs for the new concepts)", () => {
	it("quality labels carry the exact plan meanings", () => {
		expect(i18nSrc).toContain('"tools.quality.preserve": "保留画质（推荐）"');
		expect(i18nSrc).toContain('"tools.quality.preserve.desc": "尽量保留源切片的原有编码：完整 tile 原样搬运，边缘不完整 tile 可能被重编码（该部分有损）；不是像素级无损。"');
		expect(i18nSrc).toContain('"tools.quality.compact": "更小文件（有损）"');
		expect(i18nSrc).toContain('"tools.quality.compact.desc": "每个 tile 解码后按固定参数重新编码（仅明场）：文件通常变小，但节省幅度可能有限；不适合颜色定量用途。"');
		expect(i18nSrc).toContain('"tools.quality.preserve": "Keep source quality (recommended)"');
		expect(i18nSrc).toContain('"tools.quality.compact": "Smaller file (lossy)"');
	});

	it("upload stages follow the plan: 排队 → 上传至腾讯云 → 工作台接收 → 校验/发布 → 可查看", () => {
		expect(i18nSrc).toContain('"tools.upload.stage.queued": "排队"');
		expect(i18nSrc).toContain('"tools.upload.stage.cos": "上传至腾讯云"');
		expect(i18nSrc).toContain('"tools.upload.stage.workbench": "工作台接收"');
		expect(i18nSrc).toContain('"tools.upload.stage.validate": "校验/发布"');
		expect(i18nSrc).toContain('"tools.upload.stage.viewable": "可查看"');
		expect(i18nSrc).toContain('"tools.upload.stage.queued": "Queued"');
		expect(i18nSrc).toContain('"tools.upload.stage.cos": "Uploading to Tencent Cloud"');
		expect(i18nSrc).toContain('"tools.upload.stage.workbench": "Receiving in the workbench"');
		expect(i18nSrc).toContain('"tools.upload.stage.validate": "Validating/publishing"');
		expect(i18nSrc).toContain('"tools.upload.stage.viewable": "Viewable"');
	});

	it("the tool-page uploader maps engine stages onto those five labels", () => {
		expect(uploadSrc).toContain("TOOL_STAGE_KEY");
		expect(uploadSrc).toContain("waiting_space: 'tools.upload.stage.queued'");
		expect(uploadSrc).toContain("uploading: 'tools.upload.stage.cos'");
		expect(uploadSrc).toContain("downloading: 'tools.upload.stage.workbench'");
		expect(uploadSrc).toContain("validating: 'tools.upload.stage.validate'");
		expect(uploadSrc).toContain("viewable: 'tools.upload.stage.viewable'");
		expect(uploadSrc).toContain("setStageTxt(jobId, t('tools.upload.stage.cos'))");
	});

	it("drop hints explain MRXS bundles and SVS in both languages", () => {
		expect(i18nSrc).toContain('"tools.drop.multiple"');
		expect(i18nSrc).toContain('"tools.drop.bundle"');
		expect(i18nSrc).toContain('"tools.drop.svs"');
		expect(i18nSrc).toMatch(/"tools\.drop\.bundle": "[^"]*完整包[^"]*"/);
		expect(i18nSrc).toMatch(/"tools\.drop\.bundle": "[^"]*bundle[^"]*"/);
	});
});
