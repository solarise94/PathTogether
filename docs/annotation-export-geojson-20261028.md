# 标注导出（QuPath GeoJSON）（2026-10-28）

查看器标注面板头部新增「导出标注」按钮，把**当前用户在当前切片上可见的全部标注**（人工标注与 AI 落标/描绘结果）下载为一个 `.geojson` 文件，可直接导入 QuPath，也可用 Python 等工具做数据分析。

## 1. 入口与权限

- 入口：查看器 → 标注面板 → 头部下载图标。当前切片没有 `slide_id` 时按钮禁用；没有可导出的标注时只提示，不下载。
- 接口：`GET /api/slides/<slide_id>/annotations/export[?format=geojson]`（`format` 只接受 `geojson`，其余 400 `unsupported_format`）。
- 权限与 `GET /api/slides/<slide_id>/annotations` 完全同源：同一读授权、同一标注级可见性过滤、同一作者投影。导出内容恒等于该用户在标注列表里看得到的集合，导出通道不放大可见范围；原始行只为已可见的 `annotation_id` 读取。
- 响应：UTF-8 `application/geo+json` 附件，文件名 `<原始文件名去扩展名>_annotations.geojson`（中文名按 RFC 5987 `filename*`），`Cache-Control: no-store`，`X-Annotation-Count: <条数>`。与裁剪等其他下载一致，不写审计事件。

## 2. 坐标系

坐标是 PathTogether 所服务图像的 **level-0 整数像素**：x 向右、y 向下、原点在左上角。这就是 QuPath 和 OpenSlide 打开同一文件时的 level-0 网格，导入时不需要换算。

- 普通 WSI（svs/ndpi/scn/bif/tiff 等）平台直接服务原始文件，坐标与原文件一致。
- KFB 等需转换的格式：原文件无法被 QuPath/OpenSlide 打开，坐标对应平台转换后的 TIFF（同一像素网格，无裁剪/偏移），请用转换后的文件导入。
- JPEG/BMP 等普通图片：平台按 EXIF 方向转正后显示；若原图带 EXIF 旋转，而外部工具忽略 EXIF，坐标会与外部工具显示的方向不一致。这类图片没有 mpp。

文件顶层的 `pathtogether` 成员记录导出版本、时间、切片信息（`slide_id`、名称、level-0 宽高、`mpp_x/mpp_y/mpp_source`）与坐标系说明；QuPath 读取时忽略该成员。

## 3. 几何映射

| 平台类型 | GeoJSON | QuPath 导入后 |
|---|---|---|
| `rect`（含旧版正方形 `side_px`） | 闭合 `Polygon`（左上起 5 点） | Rectangle |
| `polygon` | 闭合 `Polygon` | Polygon |
| `freehand` | 闭合 `Polygon`（平台按闭合填充绘制；可能自交） | Polygon |
| `arrow` | `LineString`（起点 → 箭头端） | Line（箭头头部不保留） |

几何畸形或已删除的行不导出，计入 `pathtogether.skipped`。

## 4. Feature 字段

Feature `id` = 平台 `annotation_id`（UUID，QuPath 用作对象 ID）。`properties`：

- QuPath 读取的字段：`objectType: "annotation"`；`name` = 标签；`classification: {name, color}`，name 为标签（ASCII `:` 换成全角 `：`，避免 QuPath 生成派生类），color 与查看器的标签哈希色一致；`color` 为对象颜色，AI 结果恒为橙色 `[214,108,10]`，其余同标签色；`isLocked` = 平台位置锁；`metadata` 为全字符串键值，导入 QuPath 后保留在对象上，可随 QuPath 再导出。
- 扁平字段（便于 pandas/GeoPandas）：`annotation_id`、`shape`、`source`（`ai`/`human`）、`ai_generated`、`review_status`（`none`/`pending`/`accepted`/`rejected`）、`label`、`note`、`created_at`/`updated_at`（ISO-8601 UTC）、`author_kind`、`author_label`。
- AI 结果另有 `ai: {skill, model, contour_refined}`（如 `skill: "delineate"` 表示描绘可疑区域，`contour_refined` 表示经过轮廓精修），并同步写入 metadata 的 `ai_skill`/`ai_model`/`ai_contour_refined`。

导出走白名单。provider 主机、用户/会话/运行 ID、幂等键、快照 ID 与框、渲染指纹、revision/change_seq、历史、访客哈希、分享 token 等内部溯源字段一律不导出。

## 5. 导入 QuPath

1. 在 QuPath 中打开同一张切片（见第 2 节说明）。
2. 把 `.geojson` 拖进查看器选择 *Import objects*，或用 *File ▸ Import objects from file...*。QuPath 询问是否更新对象 ID 时，建议选择更新，避免与已有对象重复。
3. 导入后按 classification 分组、着色；AI 结果为橙色，`metadata` 中可见 `source`、`review_status`、`ai_skill` 等。

已用 QuPath 0.7.0 的无头 `QuPath script` 导入验证：平台接口导出与浏览器实际下载的文件（人工矩形、人工多边形、AI 描绘多边形）位置与面积、名称、分类与颜色、AI 橙色和 metadata 均正确；箭头（导入为线段）、锁定状态与 AI 矩形用同一导出模块生成的文件验证，同样正确。

## 6. Python 分析示例

```python
import json
import openslide
from shapely.geometry import shape

fc = json.load(open("slide_annotations.geojson", encoding="utf-8"))
slide = openslide.OpenSlide("slide.svs")
for f in fc["features"]:
    p = f["properties"]
    if not p["ai_generated"]:
        continue
    geom = shape(f["geometry"])            # level-0 像素坐标
    minx, miny, maxx, maxy = map(int, geom.bounds)
    patch = slide.read_region((minx, miny), 0, (maxx - minx, maxy - miny))
    print(p["label"], p["review_status"], geom.area)
```

## 7. 当前范围外

- 只导出当前切片；项目级批量导出、分享页访客导出暂未提供。
- AI 读片过程中的临时视野框（`viewer.highlight` 叠加框）不是标注，不会导出；AI 落标与描绘结果是正式标注，会导出。
