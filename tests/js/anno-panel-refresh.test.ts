/**
 * 打开着的标注面板必须随数据刷新重渲（P2 复核 2026-10-18）。
 *
 * 场景原样移植自 dogfood 复核脚本（panel-refresh.mjs，只读留档
 * /mnt/ssd2/tests/run-known-check-rfyRh6/panel-refresh.mjs 及其输出
 * panel-refresh.log）：refreshCurrentAnnotations 更新 currentAnnotations
 * 与画布，却不重渲已打开的面板——删除先渲染乐观态再起刷新，刷新落地后面
 * 板可能滞留「暂无标注」/旧子集。修复后面板随刷新重渲（断言不改，仅
 * harness 路径适配）；真实 app.js 的 refreshCurrentAnnotations 代码切片。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const src = readFileSync(resolve(root, "static/app.js"), "utf8");
const code = src.slice(
	src.indexOf("  function refreshCurrentAnnotations()"),
	src.indexOf("  // 把几何字段写回 item"),
);
const fresh = {
	annotations: [{
		label: "fixture", count: 2,
		items: [{ annotation_id: "fixture-1" }, { annotation_id: "fixture-2" }],
	}],
};

describe("标注面板随刷新重渲（panel-refresh.mjs 场景移植）", () => {
	it("刷新落地后已打开的面板重渲一次", async () => {
		const harness = new Function("apiFetch", `
		 var state={slide:{id:'fixture-slide'}},currentAnnotations={annotations:[]};
		 // annoListReqGen/currentAnnotationsSlide 在生产 app.js 是模块级变量
		 // （被测切片之外），harness 按同语义补声明（review 047 #1 世代计数）
		 var annoListReqGen=0,currentAnnotationsSlide=null;
		 var editDrag=null,kbEditSession=null,editItem=null,editing=false,editBeforeSnapshot=null;
		 var annoPanelOpen=true,panelRenderCount=0;
		 var els={annoBtn:{disabled:false},annoAllBtn:{disabled:false}};
		 function annotationsQueryFor(){return '/fixture';}
		 function rebuildFlatItems(){} function redrawAnnoCanvas(){} function updateAnnoToolbarPos(){}
		 function syncAnnoAllBtns(){} function renderAnnoPanel(){panelRenderCount++;}
		 ${code}
		 return {refresh:refreshCurrentAnnotations,read:()=>({panelRenderCount,currentCount:currentAnnotations.annotations.reduce((n,g)=>n+g.count,0)})};
		`)(async () => ({ ok: true, json: async () => fresh })) as {
			refresh: () => Promise<void>;
			read: () => { panelRenderCount: number; currentCount: number };
		};
		await harness.refresh();
		// 原脚本输出（修复前）：{"panelRenderCount":0,"currentCount":2}
		console.log(JSON.stringify(harness.read()));
		expect(harness.read().panelRenderCount).toBe(1,
			"An open annotation panel must repaint after fetching fresh annotations");
	});
});
