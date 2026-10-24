/**
 * /tools/slides 页面级可用性修复（2026-10-21 审计 P1-2 … P3-18）。
 *
 * tools-slides.js 是依赖真实 DOM/OPFS 的模块，这里沿用页面既有测试的做法：
 * 读源码锁定接线与 i18n 文案；真实交互由 tests/browser 的 Playwright 驱动
 * 审计脚本覆盖（pagefix 运行目录）。每条断言对应报告里的一个编号：
 *
 *   P1-2  再次选择同一文件/文件夹仍触发 change（清空 input.value）
 *   P1-3  新输入/新结果不继承上一个任务的「已保存」提示
 *   P1-4 / P2-6 / P2-12  页面级忙态（准备起 → 终态，含任务列表开始/续跑）
 *   P2-5  取消是中性状态（`type:'cancelled'` 或 `cancelled:true`），无清理 token
 *   P2-7 / P3-17  运行中的任务行：删除=停止并删除 + 立即反馈；wait 是状态
 *   P2-8  不完整包 / 文件夹 / 空文件夹 / 压缩包各自成句
 *   P2-9  每次新输入（含被拒绝的）先清错误与状态；成功准备清旧状态
 *   P2-10 artifact_missing → 可读文案，任务行不再提供「保存」
 *   P2-11 失败任务的可续跑按钮与文案一致；未知失败不承诺续跑
 *   NEW-a 成功时进度条 100%（两条开始路径 + runner ready 事件）
 *   NEW-b 锁定后已选中的画质仍明显选中
 *   P3-13 拖放高亮计数 / relatedTarget，不在子元素间闪烁
 *   P3-14 「无需转换」提示跟随语言切换
 *   P3-15 磁盘确认取消 = 中性状态 + 隐藏准备卡
 *   P3-16 隐藏 input 移出 Tab 顺序；准备状态 aria-live
 *   P3-18 文件夹多包文案覆盖 MRXS / VMS
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const read = (rel: string) => readFileSync(resolve(root, rel), "utf8");
const pageSrc = read("static/tools/tools-slides.js");
const shellSrc = read("templates/tools_slides.html");
const cssSrc = read("static/tools/tools-slides.css");
const i18nSrc = read("static/i18n.js");

function dictBlock(lang: "zh" | "en"): string {
	if (lang === "zh") {
		return i18nSrc.slice(i18nSrc.indexOf("zh: {"), i18nSrc.indexOf("en: {"));
	}
	return i18nSrc.slice(i18nSrc.indexOf("en: {"), i18nSrc.indexOf("\n  };", i18nSrc.indexOf("en: {")));
}

const dicts = { zh: dictBlock("zh"), en: dictBlock("en") } as const;

function val(lang: "zh" | "en", key: string): string | null {
	const m = dicts[lang].match(
		new RegExp(`"${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`));
	return m ? m[1] : null;
}

describe("P1-2 re-picking the same file / folder", () => {
	it("clears the input value both before opening the picker and after capturing", () => {
		// before opening: the native chooser always sees an empty input
		expect(pageSrc).toMatch(
			/pickFileBtn\.addEventListener\('click', \(ev\) => \{[\s\S]*?els\.fileInput\.value = '';[\s\S]*?els\.fileInput\.click\(\)/);
		expect(pageSrc).toMatch(
			/pickFolderBtn\.addEventListener\('click', \(ev\) => \{[\s\S]*?els\.folderInput\.value = '';[\s\S]*?els\.folderInput\.click\(\)/);
		// after capturing (covers programmatic/drop-invoked change too): the
		// captured File objects stay readable, input.value is reset
		expect(pageSrc).toMatch(
			/async function onFilePicked\(explicitFile\) \{[\s\S]*?els\.fileInput\.value = '';[\s\S]*?guardNewConversion\(\)/);
		expect(pageSrc).toMatch(
			/async function onFolderPicked\(\) \{[\s\S]*?els\.folderInput\.value = '';[\s\S]*?guardNewConversion\(\)/);
	});
});

describe("P1-3 stale save message", () => {
	it("saveMsg is cleared on a new input attempt and on a new ready result", () => {
		expect(pageSrc).toContain("saveMsg: null");
		expect(pageSrc).toMatch(/function beginInputAttempt\(\)[\s\S]*?page\.saveMsg = null;/);
		expect(pageSrc).toMatch(/function resetFlowPanels\(\)[\s\S]*?page\.saveMsg = null;/);
		// both success paths re-render the save status from the reset state
		const drive = pageSrc.slice(pageSrc.indexOf("async function driveConversion"));
		expect(drive).toContain("renderSaveStatus();");
	});
});

describe("P1-4 / P2-6 / P2-12 page-wide busy state", () => {
	it("one busy flag drives pickers, drop zone, convert and job-list actions", () => {
		expect(pageSrc).toContain("busy: false");
		expect(pageSrc).toContain("function setPageBusy(on)");
		const apply = pageSrc.slice(pageSrc.indexOf("function applyBusyState()"));
		expect(apply).toContain("els.pickFileBtn");
		expect(apply).toContain("els.pickFolderBtn");
		expect(apply).toContain("drop-zone-busy");
		expect(apply).toContain("els.convertBtn.disabled = page.busy || !page.prep;");
		expect(apply).toMatch(/action === 'start' \|\| action === 'resume'\) btn\.disabled = page\.busy/);
	});

	it("busy starts with preparation and covers job-list start/resume", () => {
		expect(pageSrc).toMatch(/async function runPrepareFlow[\s\S]*?setPageBusy\(true\);/);
		expect(pageSrc).toMatch(/async function driveConversion\(\) \{[\s\S]*?setPageBusy\(true\);/);
		expect(pageSrc).toMatch(
			/if \(action === 'start' \|\| action === 'resume'\) \{[\s\S]*?setPageBusy\(true\);/);
		// a second job-list start while busy is refused with the typed message
		expect(pageSrc).toMatch(
			/if \(action === 'start' \|\| action === 'resume'\) \{[\s\S]*?if \(page\.busy\) \{[\s\S]*?tools\.err\.job_busy_this_tab/);
	});

	it("input attempts never reset panels while busy", () => {
		expect(pageSrc).toMatch(/async function prepareSource[\s\S]*?if \(page\.busy\) \{[\s\S]*?tools\.drop\.busy/);
		expect(pageSrc).toMatch(/function handleDropData\(dt\) \{[\s\S]*?if \(page\.busy\) \{/);
		expect(pageSrc).toMatch(/async function onDirectoryDropped[\s\S]*?if \(page\.busy\) \{/);
		expect(pageSrc).toMatch(/async function prepareBundleSource[\s\S]*?if \(page\.busy\) \{/);
	});

	it("job_busy_this_tab has a friendly zh/en message", () => {
		expect(val("zh", "tools.err.job_busy_this_tab")).toContain("本标签页已有转换任务");
		expect(val("en", "tools.err.job_busy_this_tab")).toContain("already has a running conversion");
	});
});

describe("P2-5 cancel UX", () => {
	it("both cancelled shapes are recognised on both start paths", () => {
		expect(pageSrc).toMatch(/function isCancelledResult\(result\)[\s\S]*?result\.type === 'cancelled'[\s\S]*?result\.cancelled === true/);
		const drive = pageSrc.slice(pageSrc.indexOf("async function driveConversion"));
		expect(drive).toContain("isCancelledResult(result)");
		const jobAction = pageSrc.slice(pageSrc.indexOf("async function onJobAction"));
		expect(jobAction).toContain("isCancelledResult(result)");
	});

	it("cancel hides the summary and errors and shows a neutral state", () => {
		const fn = pageSrc.slice(
			pageSrc.indexOf("function showCancelledState"),
			pageSrc.indexOf("async function driveConversion"));
		expect(fn).toContain("resetFlowPanels();");      // clears page.prep + errors
		expect(fn).toContain("els.runStatus.textContent = phaseText('cancelled');");
		expect(fn).toContain("tools.run.cancelled.bundle.note");
		expect(fn).toContain("tools.run.cancelled.note");
		// never render internal cleanup tokens
		expect(pageSrc).not.toContain("tools.run.cancel.cleanup");
		expect(val("zh", "tools.run.cancel.cleanup")).toBeNull();
		expect(val("en", "tools.run.cancel.cleanup")).toBeNull();
	});

	it("bundle cancellation tells the user to choose the folder again", () => {
		expect(val("zh", "tools.run.cancelled.bundle.note")).toContain("重新选择文件夹");
		expect(val("en", "tools.run.cancelled.bundle.note")).toContain("choose the folder again");
	});
});

describe("P2-7 / P3-17 running job row", () => {
	it("nextAction 'wait' renders a status, not an enabled no-op button", () => {
		expect(pageSrc).toMatch(
			/if \(primary === 'wait'\) \{[\s\S]*?statusEl\.className = 'job-action-status';[\s\S]*?statusEl\.textContent = t\('tools\.jobs\.action\.wait'\)/);
		expect(pageSrc).not.toMatch(/primary !== 'discard'[\s\S]{0,80}wait/);
	});

	it("deleting a running job says it is stopping first", () => {
		expect(pageSrc).toMatch(/job\.nextAction === 'wait' \|\| !!?job\.active[\s\S]*?tools\.jobs\.stopping/);
		expect(pageSrc).toContain("tools.jobs.action.stopping");
		expect(pageSrc).toContain("tools.jobs.action.stop_discard");
		expect(val("zh", "tools.jobs.action.stop_discard")).toBe("停止并删除");
		expect(val("en", "tools.jobs.action.stopping")).toBe("Stopping…");
	});
});

describe("P2-8 bundle / folder / archive messages", () => {
	it("missing bundle members name what is missing and point at the folder button", () => {
		expect(pageSrc).toMatch(/function prepareErrorMessage\(e\)/);
		expect(pageSrc).toContain("tools.err.bundle_incomplete.mrxs");
		expect(pageSrc).toContain("tools.err.bundle_incomplete.vms");
		expect(val("zh", "tools.err.bundle_incomplete.mrxs"))
			.toMatch(/MRXS 包不完整：缺少 \{missing\}[\s\S]*选择文件夹/);
		expect(val("en", "tools.err.bundle_incomplete.vms"))
			.toMatch(/VMS bundle is incomplete: missing \{missing\}[\s\S]*Choose folder/);
	});

	it("a folder with no MRXS/VMS entry points single files at 选择文件", () => {
		expect(pageSrc).toMatch(/onlyEntryMissing && ctx\.kind === 'folder'[\s\S]*?tools\.input\.folder\.not_bundle/);
		expect(val("zh", "tools.input.folder.not_bundle")).toContain("单文件切片请用「选择文件」");
		expect(val("en", "tools.input.folder.not_bundle")).toContain("Choose file");
	});

	it("empty folder and archives get their own messages", () => {
		expect(pageSrc).toContain("const ARCHIVE_RE");
		expect(pageSrc).toMatch(/ARCHIVE_RE\.test\(file\.name\)[\s\S]*?tools\.input\.archive\.unsupported/);
		expect(pageSrc).toMatch(/!files\.length[\s\S]*?tools\.input\.folder\.empty/);
		expect(pageSrc).toMatch(/!rows\.length[\s\S]*?tools\.input\.folder\.empty/);
		expect(val("zh", "tools.input.archive.unsupported")).toContain("先解压");
		expect(val("en", "tools.input.archive.unsupported")).toContain("extract the archive first");
		expect(val("zh", "tools.input.folder.empty")).toContain("文件夹是空的");
	});

	it("generic unsupported text no longer surfaces the KFB/KFBF-only runner wording", () => {
		expect(pageSrc).toMatch(
			/code === 'unsupported_input' && !\(e && e\.error && e\.error\.kind\)[\s\S]*?return friendly \|\| raw;/);
	});
});

describe("P2-9 clean state per input attempt", () => {
	it("every entry point clears the previous error/status first", () => {
		for (const fn of ["prepareSource", "onFilePicked", "onFolderPicked", "onDirectoryDropped"]) {
			const start = pageSrc.indexOf(`function ${fn}`);
			expect(start).toBeGreaterThan(-1);
			const slice = pageSrc.slice(start, start + 1600);
			expect(slice).toContain("beginInputAttempt()");
		}
		expect(pageSrc).toMatch(/function beginInputAttempt\(\) \{[\s\S]*?clearError\(\);[\s\S]*?setPageMsg\(null\);/);
	});

	it("a successful preparation clears the previous page status", () => {
		expect(pageSrc).toMatch(/setStageMsg\('tools\.stage\.done'\);\s*setPageMsg\(null\);/);
	});
});

describe("P2-10 artifact_missing", () => {
	it("save maps the typed code to a readable message and marks the job", () => {
		expect(pageSrc).toMatch(/errCode\(e\) === 'artifact_missing'[\s\S]*?markArtifactMissing\(jobId\)/);
		expect(pageSrc).toMatch(/function markArtifactMissing\(jobId\)[\s\S]*?page\.missingArtifacts\.add\(jobId\)/);
		expect(pageSrc).toMatch(/page\.missingArtifacts\.has\(page\.readyInfo\.jobId\)/);
		expect(val("zh", "tools.err.artifact_missing")).toContain("请删除任务并重新转换");
		expect(val("en", "tools.err.artifact_missing")).toContain("delete the job and convert again");
	});

	it("the job row stops offering 保存 for a cleaned-up artifact", () => {
		expect(pageSrc).toMatch(
			/primary === 'export' && artifactGone[\s\S]*?tools\.jobs\.artifact_missing/);
	});
});

describe("P2-11 failed jobs: resume + messages", () => {
	it("the row renders the runner-provided resume action with the 继续转换 label", () => {
		expect(pageSrc).toContain("resume: 'tools.jobs.action.resume'");
		expect(val("zh", "tools.jobs.action.resume")).toBe("继续转换");
		expect(val("en", "tools.jobs.action.resume")).toBe("Resume conversion");
	});

	it("unknown failures get a readable message that does not promise resume", () => {
		expect(pageSrc).toContain("function showRunFailure(result)");
		expect(pageSrc).toMatch(/showErrorText\(t\('tools\.run\.failed\.unknown'\)\)/);
		const zh = val("zh", "tools.run.failed.unknown") || "";
		expect(zh).toContain("未知错误");
		expect(zh).not.toMatch(/续跑|继续转换/);
		// recoverable codes still say resume is possible (runner offers it)
		expect(val("zh", "tools.err.io_recoverable")).toContain("续跑");
	});
});

describe("NEW-a terminal progress is 100%", () => {
	it("both start paths set the run bar to 1 and add progress-done", () => {
		const matches = pageSrc.match(
			/setProgress\(els\.runProgress, els\.runBar, els\.runBytes, 1, ''\);\s*els\.runProgress\.classList\.add\('progress-done'\);/g) || [];
		expect(matches.length).toBeGreaterThanOrEqual(2);
		expect(pageSrc).toMatch(/s\.to === 'ready'[\s\S]*?setProgress\(els\.runProgress, els\.runBar, null, 1, null\)/);
		expect(cssSrc).toMatch(/\.progress\.progress-done \.progress-bar \{ transition: none; \}/);
	});
});

describe("job-list run context (summary/stage belong to another job)", () => {
	const jobAction = () => pageSrc.slice(
		pageSrc.indexOf("async function onJobAction"),
		pageSrc.indexOf("async function refreshJobs"));
	const drive = () => pageSrc.slice(
		pageSrc.indexOf("async function driveConversion"),
		pageSrc.indexOf("async function onConvert"));

	it("hides the stage and summary cards of a different job and clears its state", () => {
		expect(jobAction()).toMatch(/const thisPrep = page\.prep && page\.prep\.jobId === job\.id;/);
		expect(jobAction()).toMatch(
			/if \(!thisPrep\) \{[\s\S]*?els\.stageSection\.hidden = true;[\s\S]*?els\.summarySection\.hidden = true;[\s\S]*?page\.prep = null;/);
	});

	it("never locks another job's quality/format controls", () => {
		expect(jobAction()).toMatch(/if \(thisPrep\) \{\s*lockOutputProfile\([\s\S]*?lockEncodingProfile\(/);
	});

	it("names the running job in the run card (folderName||name like the row)", () => {
		expect(jobAction()).toMatch(
			/const jobName = \(job\.source && \(job\.source\.folderName \|\| job\.source\.name\)\) \|\| job\.id;/);
		expect(jobAction()).toContain("setRunJob(jobName, 'running');");
		expect(jobAction()).toContain("setRunJob(jobName, 'done');");
		expect(jobAction()).toContain("setRunJob(jobName, 'failed');");
		// the row list is refreshed once the record is planned/active, so the
		// started job is shown as 「转换中（本标签页）」 rather than 已就绪
		const startBlock = jobAction().slice(jobAction().indexOf("if (action === 'start' || action === 'resume')"));
		expect(startBlock.indexOf("refreshJobs();"))
			.toBeLessThan(startBlock.indexOf("const result = await started.done;"));
	});

	it("does not resurrect the hidden summary when the run ends", () => {
		expect(jobAction()).not.toMatch(/summarySection\.hidden = false/);
		expect(jobAction()).not.toMatch(/stageSection\.hidden = false/);
	});

	it("a manual start names its own job and keeps its summary", () => {
		expect(drive()).toContain("const jobName = prepDisplayName();");
		expect(drive()).toContain("setRunJob(jobName, 'running');");
		expect(drive()).toContain("setRunJob(jobName, 'done');");
		expect(drive()).toContain("setRunJob(jobName, 'failed');");
		expect(pageSrc).toContain("bundleFolderName");
		expect(pageSrc).toMatch(/function prepDisplayName\(\) \{[\s\S]*?page\.bundleFolderName/);
	});

	it("run card has a dedicated job line, re-rendered on language change", () => {
		expect(shellSrc).toContain('id="run-job"');
		const rerender = pageSrc.slice(pageSrc.indexOf("function rerenderForLang"));
		expect(rerender.slice(0, rerender.indexOf("\n}"))).toContain("renderRunJob()");
		expect(val("zh", "tools.run.job.running")).toBe("正在转换：{name}");
		expect(val("en", "tools.run.job.running")).toBe("Converting: {name}");
		expect(val("zh", "tools.run.job.cancelled")).toContain("已取消");
	});
});

describe("quality lock note is only shown for an actually started job", () => {
	it("lock happens after startJob/resumeJob returns, and only for the summary's own job", () => {
		const drive = pageSrc.slice(
			pageSrc.indexOf("async function driveConversion"),
			pageSrc.indexOf("async function onConvert"));
		expect(drive.indexOf("lockEncodingProfile(encodingProfile || E.defaultEncodingProfile())"))
			.toBeGreaterThan(drive.indexOf("await page.runner.startJob"));
		const jobAction = pageSrc.slice(
			pageSrc.indexOf("async function onJobAction"),
			pageSrc.indexOf("async function refreshJobs"));
		expect(jobAction.indexOf("lockEncodingProfile(startEncodingProfile"))
			.toBeGreaterThan(jobAction.indexOf("await page.runner."));
		// locking is guarded by thisPrep: the "已确定" note cannot appear on a
		// summary card that describes another job (the audit screenshot)
		expect(jobAction).toMatch(/if \(thisPrep\) \{[\s\S]*?lockEncodingProfile\(/);
		// prepare/reset start unlocked
		expect(pageSrc).toMatch(/function resetFlowPanels\(\)[\s\S]*?page\.encodingLockedProfile = null;/);
	});
});

describe("NEW-b locked selected radio stays visible", () => {
	it("disabled checked radios keep the accent selection", () => {
		expect(cssSrc).toMatch(/input\[type="radio"\]:disabled:checked \{[\s\S]*?border-color: var\(--accent\);[\s\S]*?var\(--accent\)/);
		expect(cssSrc).toMatch(/input\[type="radio"\]:disabled:not\(:checked\) \{ opacity: 0\.6; \}/);
	});
});

describe("P3-13 drag highlight", () => {
	it("uses a drag counter and relatedTarget instead of a bare dragleave", () => {
		expect(pageSrc).toContain("let dragDepth = 0;");
		expect(pageSrc).toMatch(/addEventListener\('dragenter'[\s\S]*?dragDepth \+= 1;/);
		expect(pageSrc).toMatch(/addEventListener\('dragleave'[\s\S]*?ev\.relatedTarget[\s\S]*?els\.dropZone\.contains\(ev\.relatedTarget\)/);
	});
});

describe("P3-14 direct hint follows the language switch", () => {
	it("stores the hint and re-renders it on hp-lang-change", () => {
		expect(pageSrc).toContain("page.directHint = true;");
		expect(pageSrc).toContain("function renderDirectHint()");
		const rerender = pageSrc.slice(pageSrc.indexOf("function rerenderForLang"));
		expect(rerender.slice(0, rerender.indexOf("\n}"))).toContain("renderDirectHint()");
	});
});

describe("P3-15 disk confirmation cancel", () => {
	it("denying the uncertain-space dialog is neutral and hides the stage card", () => {
		expect(pageSrc).toMatch(
			/els\.stageSection\.hidden = true;[\s\S]*?code === 'disk_precheck_failed'[\s\S]*?if \(info\.uncertain\) \{[\s\S]*?setPageMsg\('tools\.disk\.cancelled\.note'\)/);
	});
});

describe("P3-16 tab order and stage announcements", () => {
	it("both hidden inputs leave the tab order; stage status is a live region", () => {
		expect(shellSrc).toMatch(/id="file-input"[^>]*class="visually-hidden-input" tabindex="-1"/);
		expect(shellSrc).toMatch(/id="folder-input"[^>]*class="visually-hidden-input" tabindex="-1"/);
		expect(shellSrc).toMatch(/id="stage-status" class="stage-status" role="status" aria-live="polite"/);
	});
});

describe("P3-18 folder wording covers MRXS and VMS", () => {
	it("the multiple-bundle message names both formats", () => {
		expect(val("zh", "tools.drop.bundle.multiple")).toContain("MRXS / VMS");
		expect(val("en", "tools.drop.bundle.multiple")).toContain("MRXS / VMS");
	});
});

describe("converter footer support entry (design 2026-10-21 §转换器入口)", () => {
	it("is a plain new-tab link to /support with noopener noreferrer and an aria label", () => {
		const linkAt = shellSrc.indexOf('href="/support"');
		expect(linkAt).toBeGreaterThan(-1);
		const open = shellSrc.lastIndexOf("<a", linkAt);
		const close = shellSrc.indexOf("</a>", linkAt);
		const tag = shellSrc.slice(open, close);
		expect(tag).toContain('target="_blank"');
		expect(tag).toMatch(/rel="[^"]*noopener[^"]*"/);
		expect(tag).toMatch(/rel="[^"]*noreferrer[^"]*"/);
		expect(tag).toContain('data-i18n="tools.support.link"');
		expect(tag).toContain('data-i18n-aria="tools.support.link.aria"');
		// 展示文案用 tools.support.link；页脚内、不在任何卡片/动作流里
		const footer = shellSrc.slice(shellSrc.indexOf("<footer"));
		expect(footer).toContain('href="/support"');
	});

	it("adds no prefetch/preload/dns-prefetch for /support anywhere in <head>", () => {
		const head = shellSrc.slice(shellSrc.indexOf("<head>"), shellSrc.indexOf("</head>"));
		expect(head).not.toContain("/support");
		expect(head).not.toMatch(/prefetch|preload|dns-prefetch/);
		// 页面脚本没有任何支持入口处理器（点击前零 JS 参与）
		expect(pageSrc).not.toContain('"/support"');
		expect(pageSrc).not.toContain("'/support'");
	});

	it("footer styling matches the muted footer with a visible focus ring", () => {
		expect(cssSrc).toMatch(/\.footer-support \{ margin: 6px 0 0; \}/);
		expect(cssSrc).toMatch(/\.footer-support-link \{[\s\S]*?color: var\(--muted\);[\s\S]*?white-space: nowrap;/);
		expect(cssSrc).toMatch(/\.footer-support-link:focus-visible \{[\s\S]*?outline: var\(--focus-ring\);/);
	});

	it("the privacy details carry the support/payment boundary sentence; summary unchanged", () => {
		const privacy = shellSrc.slice(
			shellSrc.indexOf('id="privacy-details"'), shellSrc.indexOf("</details>"));
		expect(privacy).toContain('data-i18n="tools.support.privacy"');
		// summary 本身不改（仍是「需要上传？请前往工作台。」）
		expect(privacy).toMatch(/<summary data-i18n="tools\.privacy\.brief">需要上传？请前往工作台。<\/summary>/);
		expect(privacy).not.toMatch(/<summary[^>]*tools\.support\.privacy/);
		// zh/en 成对且点名「支付服务」边界
		expect(val("zh", "tools.support.privacy")).toContain("支付服务");
		expect(val("en", "tools.support.privacy")).toContain("payment service");
		expect(val("zh", "tools.support.prompt")).toContain("支持项目");
		expect(val("en", "tools.support.link.aria")).toContain("new tab");
	});
});
