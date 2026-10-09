/**
 * HP_AnnoGeometry（static/anno-geometry.js）纯几何 helper 单测。
 * §5.4 polygon 完整性与 §6.2 旋转/镜像投影的算法基座：
 *   - 点列外接框 / 平移 / 等比缩放 / 越界回移（freehand 与 polygon 共用）；
 *   - 简单多边形自交校验（「完成轮廓编辑」时与服务端同一规则，不静默修正）；
 *   - 矩形四顶点投影 + 四边形命中（旋转/镜像下不能拿两个对角点拼 AABB）。
 */
import { describe, expect, it } from "vitest";
import geo from "../../static/anno-geometry.js";

const AG = geo as unknown as {
	pointsBBox(points: number[][]): { x: number; y: number; w: number; h: number } | null;
	pointSegDist(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number;
	pointInPolygon(px: number, py: number, pts: number[][]): boolean;
	segmentsIntersect(a1: number[], a2: number[], b1: number[], b2: number[]): boolean;
	polygonSelfIntersects(pts: number[][]): boolean;
	translatePoints(points: number[][], dx: number, dy: number): number[][];
	scalePointsAbout(points: number[][], ax: number, ay: number, s: number): number[][];
	pointsShiftIntoRect(points: number[][], w: number | null, h: number | null): { dx: number; dy: number } | null;
	rectQuad(x: number, y: number, w: number, h: number, f: (x: number, y: number) => { x: number; y: number }): number[][];
	pointInQuad(px: number, py: number, quad: number[][], tol?: number): boolean;
	quadBBox(quad: number[][]): { x: number; y: number; w: number; h: number } | null;
	clonePoints(points: number[][]): number[][];
};

describe("anno-geometry：点列基础（freehand/polygon 共用）", () => {
	it("pointsBBox：三角形外接框；空列 → null", () => {
		expect(AG.pointsBBox([[100, 100], [400, 100], [250, 400]]))
			.toEqual({ x: 100, y: 100, w: 300, h: 300 });
		expect(AG.pointsBBox([])).toBeNull();
		expect(AG.pointsBBox(null as unknown as number[][])).toBeNull();
	});

	it("translatePoints / clonePoints：返回新数组，不改入参", () => {
		const src = [[1, 2], [3, 4]];
		const out = AG.translatePoints(src, 10, -5);
		expect(out).toEqual([[11, -3], [13, -1]]);
		expect(src).toEqual([[1, 2], [3, 4]]);
		const c = AG.clonePoints(src);
		c[0][0] = 99;
		expect(src[0][0]).toBe(1);
	});

	it("scalePointsAbout：以锚点等比缩放（scale 2，锚 0,0）", () => {
		expect(AG.scalePointsAbout([[1, 1], [2, 2]], 0, 0, 2)).toEqual([[2, 2], [4, 4]]);
		// scale <= 0 → 原样副本（不产生 NaN 扩散）
		expect(AG.scalePointsAbout([[1, 1]], 0, 0, 0)).toEqual([[1, 1]]);
	});

	it("pointsShiftIntoRect：越界整体回移（保持形状，不裁剪点）", () => {
		// 左上越界 → 平移回 [0, W/H] 内
		expect(AG.pointsShiftIntoRect([[-5, -3], [15, 20]], 100, 100)).toEqual({ dx: 5, dy: 3 });
		// 右下越界 → 负向平移
		expect(AG.pointsShiftIntoRect([[90, 90], [110, 120]], 100, 100)).toEqual({ dx: -10, dy: -20 });
		// 未越界 → null（调用方不做无谓变换）
		expect(AG.pointsShiftIntoRect([[0, 0], [10, 10]], 100, 100)).toBeNull();
	});
});

describe("anno-geometry：polygon 自交校验（§6.2 完成轮廓编辑的闸）", () => {
	const TRIANGLE = [[0, 0], [100, 0], [50, 80]];
	const BOWTIE = [[0, 0], [100, 100], [100, 0], [0, 100]]; // 蝶形自交

	it("简单三角形不自交；蝶形自交", () => {
		expect(AG.polygonSelfIntersects(TRIANGLE)).toBe(false);
		expect(AG.polygonSelfIntersects(BOWTIE)).toBe(true);
	});

	it("点数 <3 不判自交（数量不足由调用方另行拒绝）", () => {
		expect(AG.polygonSelfIntersects([[0, 0], [10, 10]])).toBe(false);
	});

	it("segmentsIntersect：对边相交 vs 不相交/相离", () => {
		expect(AG.segmentsIntersect([0, 0], [10, 10], [0, 10], [10, 0])).toBe(true);
		// 相离线段不相交（共享端点由 polygonSelfIntersects 的相邻边跳过承担）
		expect(AG.segmentsIntersect([0, 0], [10, 10], [20, 0], [30, 10])).toBe(false);
	});
});

describe("anno-geometry：矩形四顶点投影（§6.2 旋转/镜像）", () => {
	it("未旋转：quad 为屏幕轴对齐四角", () => {
		const quad = AG.rectQuad(0, 0, 100, 50, (x, y) => ({ x: x * 2, y: y * 2 }));
		expect(quad).toEqual([[0, 0], [200, 0], [200, 100], [0, 100]]);
	});

	it("旋转 90°（图像→屏幕旋转投影）：四顶点各自投影，不拼 AABB", () => {
		// 模拟图像坐标 (x,y) 旋转 90° 后的屏幕坐标 (H - y, x)
		const H = 400;
		const quad = AG.rectQuad(10, 20, 30, 40, (x, y) => ({ x: H - y, y: x }));
		expect(quad).toEqual([[380, 10], [380, 40], [340, 40], [340, 10]]);
		const bb = AG.quadBBox(quad);
		expect(bb).toEqual({ x: 340, y: 10, w: 40, h: 30 });
	});

	it("pointInQuad：内部命中；边线 ≤ tol 命中；外部不命中", () => {
		const quad = [[0, 0], [100, 0], [100, 100], [0, 100]];
		expect(AG.pointInQuad(50, 50, quad)).toBe(true);
		expect(AG.pointInQuad(103, 50, quad, 6)).toBe(true);   // 边线容差
		expect(AG.pointInQuad(103, 50, quad)).toBe(false);     // 无容差不命中
		expect(AG.pointInQuad(150, 50, quad, 6)).toBe(false);
	});
});

describe("anno-geometry：距离与点在多边形", () => {
	it("pointSegDist：垂足在段内取垂距，在段外取端点距", () => {
		expect(AG.pointSegDist(5, 7, 0, 0, 10, 0)).toBe(7);
		expect(AG.pointSegDist(-4, 0, 0, 0, 10, 0)).toBe(4);
	});

	it("pointInPolygon：射线法（凹多边形）", () => {
		const concave = [[0, 0], [100, 0], [100, 100], [50, 40], [0, 100]];
		expect(AG.pointInPolygon(10, 10, concave)).toBe(true);
		expect(AG.pointInPolygon(50, 90, concave)).toBe(false); // 底部凹口内
	});
});
