/**
 * §6.3 位置锁 + §6.4 差量保存/冲突（前端契约，后端 vi-lock 86148e5b）：
 *   - 锁切换单独请求（绝不夹带 geom），始终带 expected_revision；
 *   - commitAdminEdit 差量拆分：几何没变只 PATCH note（锁定后改备注不被拒）；
 *   - 409 按错误码区分 annotation_locked ≠ revision_conflict（不再把所有 409
 *     当版本冲突）；冲突保留本地草稿 + 读回服务端版本；
 *   - 松手一次 PATCH（无位移不发）；保存中/已保存/未保存+重试；
 *   - 撤销过锁与 revision；Delete 对锁定无效；临时框 draft.geometryLocked。
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const appSrc = readFileSync(resolve(here, "../../static/app.js"), "utf8");
const shareSrc = readFileSync(resolve(here, "../../static/share.js"), "utf8");

function extractFn(src: string, name: string): string {
	const re = new RegExp(`function ${name}\\([^)]*\\) \\{`);
	const m = re.exec(src);
	expect(m, `${name} 必须存在`).toBeTruthy();
	const start = m!.index;
	let i = src.indexOf("{", start);
	let depth = 0;
	for (; i < src.length; i++) {
		if (src[i] === "{") depth++;
		else if (src[i] === "}") {
			depth--;
			if (depth === 0) return src.slice(start, i + 1);
		}
	}
	throw new Error(`未能截取 ${name}`);
}

describe("§6.3 锁切换（app.js）", () => {
	it("toggleItemGeometryLocked：单独请求，只带 geometry_locked + expected_revision", () => {
		const fn = extractFn(appSrc, "toggleItemGeometryLocked");
		expect(fn).toMatch(/geometry_locked:\s*!!lockTarget/);
		expect(fn).toMatch(/expected_revision/);
		// 锁请求绝不夹带 geom（后端 400 lock_with_geometry）
		expect(fn).not.toMatch(/geom:/);
		expect(fn).not.toMatch(/buildEditGeom/);
		expect(fn).toMatch(/annoIdUrl/); // PATCH /api/annotation/id/<id>
	});

	it("只读身份点解锁 → 说明原因，不发请求", () => {
		const fn = extractFn(appSrc, "toggleItemGeometryLocked");
		expect(fn.indexOf("itemCanEdit(it)")).toBeGreaterThan(-1);
		expect(fn).toMatch(/anno\.lock\.readonly\.tip/);
		const readonlyGuard = fn.slice(0, fn.indexOf("apiFetch"));
		expect(readonlyGuard).toMatch(/return/);
	});

	it("锁命中 409 annotation_locked → 按错误码提示（非版本冲突文案）", () => {
		const fn = extractFn(appSrc, "toggleItemGeometryLocked");
		expect(fn).toMatch(/j\.error === "annotation_locked"/);
		expect(fn).toMatch(/anno\.error\.locked/);
		expect(fn).toMatch(/edit\.conflict/); // 其余 409 才是版本冲突
	});

	it("条目锁判定 itemGeometryLocked（缺省 false）与 canEdit 能力位", () => {
		const fn = extractFn(appSrc, "itemGeometryLocked");
		expect(fn).toMatch(/geometry_locked === true/);
		expect(extractFn(appSrc, "itemCanEdit")).toMatch(/can_edit/);
	});
});

describe("§6.3 锁切换（share.js）", () => {
	it("toggleItemGeometryLocked：PATCH /s/<token>/api/roi/<index>，单独请求不夹带 geom", () => {
		const fn = extractFn(shareSrc, "toggleItemGeometryLocked");
		expect(fn).toMatch(/api\/roi\/" \+ it\.index/);
		expect(fn).toMatch(/geometry_locked:\s*!!lockTarget/);
		expect(fn).toMatch(/expected_revision/);
		expect(fn).not.toMatch(/buildEditGeom/);
		expect(fn).toMatch(/j\.error === "annotation_locked"/);
	});

	it("锁定标记无手柄（editHandles 返回空）且删除入口禁用", () => {
		expect(extractFn(shareSrc, "editHandles")).toMatch(/itemGeometryLocked\(it\)/);
	});
});

describe("§6.3 差量更新（commitAdminEdit 拆分）", () => {
	it("几何没变 → body 只带 note，不带 geom（锁定后改备注不被当成移动）", () => {
		const fn = extractFn(appSrc, "commitAdminEdit");
		expect(fn).toMatch(/geomChanged/);
		expect(fn).toMatch(/var body = \{ note: noteVal \}/);
		expect(fn).toMatch(/if \(geomChanged\) body\.geom = geom/);
		// 新 UI 始终传 expected_revision
		expect(fn).toMatch(/expected_revision/);
	});

	it("409 按错误码区分：annotation_locked（结束编辑说明原因）≠ revision_conflict（保留本地草稿）", () => {
		const fn = extractFn(appSrc, "commitAdminEdit");
		expect(fn).toMatch(/err\.locked = \(j\.error === "annotation_locked"\)/);
		expect(fn).toMatch(/edit\.conflict\.keep/);   // 保留本地草稿
		expect(fn).toMatch(/serverEntry/);             // 读回服务端版本（current）
		// 冲突路径不再无条件清编辑卡（锁分支才结束手势）
		const catchBody = fn.slice(fn.indexOf(".catch(function"));
		const lockedBranch = catchBody.slice(0, catchBody.indexOf("else if"));
		expect(lockedBranch).toMatch(/closeEditCard/);
		expect(lockedBranch).toMatch(/anno\.error\.locked/);
	});

	it("锁定标记编辑卡标题带锁标（openEditCard）", () => {
		expect(extractFn(appSrc, "openEditCard")).toMatch(/itemGeometryLocked\(it\)/);
	});
});

describe("§6.3 差量更新（share.js commitEdit）", () => {
	it("几何没变只发 note；409 锁/版本冲突分路；冲突保留编辑卡与本地草稿", () => {
		const fn = extractFn(shareSrc, "commitEdit");
		expect(fn).toMatch(/geomChanged/);
		expect(fn).toMatch(/var body = \{ note: noteVal \}/);
		expect(fn).toMatch(/j\.error === "annotation_locked"/);
		expect(fn).toMatch(/edit\.conflict\.keep/);
		expect(fn).toMatch(/refreshRoisOnce/); // 读回服务端版本供比较
	});
});

describe("§6.4 松手一次 PATCH + 保存状态（app.js）", () => {
	it("endEditDrag → saveItemGeometryIfChanged；无有效位移不发（geomEqual）", () => {
		const fn = extractFn(appSrc, "endEditDrag");
		expect(fn).toMatch(/saveItemGeometryIfChanged\(d\.item, d\.start\)/);
		const save = extractFn(appSrc, "saveItemGeometryIfChanged");
		expect(save).toMatch(/geomEqual\(beforeGeom, after\)/);
		expect(save).toMatch(/return/); // 相等直接返回，不发 PATCH
	});

	it("手势保存只发 geom 差量 + expected_revision，从不夹带 note", () => {
		const fn = extractFn(appSrc, "sendItemGeometryPatch");
		expect(fn).toMatch(/body\.geom = geom/);
		expect(fn).toMatch(/expected_revision/);
		expect(fn).not.toMatch(/body\.note/);
	});

	it("保存中/已保存/未保存+重试（setAnnoSaveStatus 三态）", () => {
		const fn = extractFn(appSrc, "setAnnoSaveStatus");
		expect(fn).toMatch(/anno\.savestatus\.saving/);
		expect(fn).toMatch(/anno\.savestatus\.saved/);
		expect(fn).toMatch(/anno\.savestatus\.unsaved/);
		expect(fn).toMatch(/annoSaveRetryFn/); // 重试回调
	});

	it("409 annotation_locked → 恢复服务端态并说明；revision_conflict → 保留本地草稿 + 重试", () => {
		const fn = extractFn(appSrc, "handleGeometrySaveError");
		expect(fn).toMatch(/j\.error === "annotation_locked"/);
		expect(fn).toMatch(/refreshCurrentAnnotations/); // 恢复服务端版本
		expect(fn).toMatch(/edit\.conflict\.keep/);       // 冲突保留本地草稿
		expect(fn).toMatch(/serverRev/);                   // 读回的 revision 作重试 CAS
	});

	it("网络等待期间不启动第二个写手势（inflight 闸）", () => {
		const fn = extractFn(appSrc, "sendItemGeometryPatch");
		expect(fn).toMatch(/_inflight/);
	});
});

describe("§6.3 撤销/删除过锁", () => {
	it("undoEditEntry / undoCreateEntry：annotation_locked 专属提示（先解锁）", () => {
		expect(extractFn(appSrc, "undoEditEntry")).toMatch(/anno\.error\.locked\.undo/);
		expect(extractFn(appSrc, "undoCreateEntry")).toMatch(/anno\.error\.locked\.undo/);
		expect(extractFn(appSrc, "sendAnnoPatch")).toMatch(/annotation_locked/);
		expect(extractFn(appSrc, "sendAnnoDelete")).toMatch(/annotation_locked/);
	});

	it("Delete 键对锁定对象无效并说明；只读对象同理", () => {
		const fn = extractFn(appSrc, "onViewerKeydown");
		expect(fn).toMatch(/anno\.lock\.delete\.denied/);
		expect(fn).toMatch(/itemGeometryLocked\(editItem\)/);
	});

	it("deleteAnnoItem 收到 409 annotation_locked → 不当幂等成功，恢复服务端状态", () => {
		const fn = extractFn(appSrc, "deleteAnnoItem");
		expect(fn).toMatch(/res\.errorCode === "annotation_locked"/);
		expect(fn).toMatch(/blocked/);
	});
});

describe("§6.3 临时框 draft.geometryLocked（app.js）", () => {
	it("submitAnnotationDraft 保存矩形草稿时带上锁状态（POST geometry_locked）", () => {
		const fn = extractFn(appSrc, "submitAnnotationDraft");
		expect(fn).toMatch(/state\.roiGeometryLocked/);
		expect(fn).toMatch(/body\.geometry_locked = true/);
	});

	it("exitRoi 丢弃草稿时复位草稿锁（只对当前草稿有效）", () => {
		expect(extractFn(appSrc, "exitRoi")).toMatch(/state\.roiGeometryLocked = false/);
		expect(extractFn(shareSrc, "exitRoi")).toMatch(/state\.roiGeometryLocked = false/);
	});

	it("分享端 saveRoi 同样携带草稿锁", () => {
		const fn = extractFn(shareSrc, "saveRoi");
		expect(fn).toMatch(/roiBody\.geometry_locked = true/);
	});
});

describe("§6.2 键盘（app.js）", () => {
	it("方向键 1px / Shift 10px，合并为一个撤销/提交单元（防抖单 PATCH）", () => {
		const fn = extractFn(appSrc, "onViewerArrowNudge");
		expect(fn).toMatch(/e\.shiftKey \? 10 : 1/);
		expect(fn).toMatch(/kbEditSession/);
		expect(fn).toMatch(/KB_PATCH_DEBOUNCE_MS/);
		const save = extractFn(appSrc, "saveItemGeometryIfChanged");
		expect(save).toBeTruthy();
	});

	it("锁定/只读对象方向键不移动并说明", () => {
		const fn = extractFn(appSrc, "onViewerArrowNudge");
		expect(fn).toMatch(/canDragItem\(editItem\)/);
		expect(fn).toMatch(/anno\.keyboard\.locked/);
	});

	it("Esc 撤销未提交几何（键盘会话恢复 + 编辑态脏几何恢复）", () => {
		const fn = extractFn(appSrc, "onViewerKeydown");
		expect(fn).toMatch(/cancelKbEditSession\(true\)/);
		expect(fn).toMatch(/editBeforeSnapshot\.geom/);
	});

	it("Ctrl/Cmd+Z 撤销链路保持（过锁与 revision 由 409 分支承担）", () => {
		const fn = extractFn(appSrc, "onViewerKeydown");
		expect(fn).toMatch(/performUndo\(\)/);
	});
});
