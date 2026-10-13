"""Public product documentation and crawler discovery; never enumerate user data."""
from dataclasses import dataclass
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Blueprint, Response, abort, render_template, request, current_app

import agreement_store


@dataclass(frozen=True)
class Page:
    path: str
    title: str
    summary: str


#: 域名事实（2026-10-13 声明）：canonical 主域名一律来自注入的 origin()
#: （生产=PUBLIC_BASE_URL=histopilot.cn，绝不在此硬编码）；histopilot.com 是
#: 同一服务与账号的国际优化线路（不是独立站点）；pt.solarise94.fun 已弃用
#: （nginx 308 保留路径跳主域名）。两个辅助域名作字面常量供声明引用。
INTERNATIONAL_HOST = "histopilot.com"
DEPRECATED_HOST = "pt.solarise94.fun"


GUIDES = {
    "overview": {
        "title": "HistoPilot 功能指南",
        "intro": "在同一个工作台查看切片、制作标记，与同伴协作，并使用 AI 辅助观察。",
        "sections": (
            ("查看与整理", "登录工作台上传切片；在某个文件夹内导入的切片默认加入该文件夹，也可改选其他位置。刷新后回到上次所在的文件夹与页；通过文件夹、缩略图与搜索查找文件，在多分辨率视窗中平移、缩放。支持的输入格式与压缩方式以文件识别结果为准。"),
            ("标注与讨论", "创建矩形、箭头或多边形标记，调整位置与边界、锁定几何并编辑备注。通过切片和标记的备注交流观察依据。"),
            ("AI 读片助手", "向助手提问，使用 AI 导航与区域观察；明确请求“@描绘”或“帮我圈出来”时可生成标记。AI 使用需要账号授权、服务配置和可用额度。结果需由专业人员复核。"),
            ("测量与插件分析", "按研究需求接入测量和分析插件，开展计数、测量与区域计量分析。具体插件能力取决于部署配置与账号权限。"),
            ("协作与分享", "分享面板设置接收者和权限；只读与允许标注分别控制。撤销分享会结束对方的访问。页面中的协作演示使用虚构参与者，不是在线聊天服务。"),
            ("切片转换", "无需登录即可在浏览器本地识别、转换并保存支持的切片。只有主动选择上传后，文件才进入工作台。详见转换指南。"),
        ),
    },
    "conversion": {
        "title": "切片格式转换指南",
        "intro": "在本机浏览器把支持的切片转换成 OME-TIFF 或经典金字塔 TIFF，再保存或上传。",
        "sections": (
            ("输入与完整文件包", "工具识别 KFB、KFBF、SVS、SCN、NDPI、通用 TIFF 和 BMP/JPEG；MRXS、VMS 使用完整文件夹包，必须保留主文件及伴随数据。不是所有同后缀文件或压缩方式都支持，识别页会明确说明。不要靠修改扩展名转换文件。"),
            ("选择输出目标", "适合 QuPath：默认 OME-TIFF（.ome.tif），支持 Bio-Formats 生态读取多分辨率层级。兼容 OpenSlide 工具：经典金字塔 TIFF（.tif），面向 ASAP/OpenSlide；QuPath 默认读取器可能只显示单一分辨率。"),
            ("转换、校验与保存", "选择文件或完整文件包，核对识别结果与输出设置，开始转换并检查结果。转换由本机浏览器执行，进度和可续跑状态以工具界面为准。输出保留金字塔分辨率结构，荧光数据使用多通道 OME-TIFF。"),
            ("本地处理与上传的区别", "本地转换无需账号，不自动发送切片内容；页面脚本、WASM 等工具资源仍需下载。保存到本机后可在其他软件打开；上传到工作台是另一个明确操作，需要登录并受配额与文件验证限制。"),
            ("转换与服务器读取", "网页工具负责本地生成输出文件；工作台服务负责验证和读取已上传文件。这是两个独立环节，转换成功不代表任意源格式或任意第三方软件都兼容。"),
        ),
    },
    "collaboration": {
        "title": "切片协作与分享指南",
        "intro": "和你的同伴一起查阅切片、标注并讨论。",
        "sections": (
            ("建立共享", "在工作台选择切片，打开分享面板设置共享范围与权限，再把分享入口交给接收者。对方必须满足该分享的访问条件。"),
            ("围绕观察交流", "通过同一张切片与标记定位区域，在备注中说明观察依据。允许标注的共享可让接收者参与标记；只读共享不能新增或修改标注。"),
            ("修改与结束共享", "分享管理中调整范围或撤销共享。拥有链接不等于拥有永久权限，也不自动获得 AI 调用或下载权限。"),
        ),
    },
}


def public_pages():
    """One source for the HTML map, XML sitemap and LLM discovery links."""
    pages = [
        Page("/", "HistoPilot", "产品介绍、开始使用、AI 观察与协作演示。"),
        Page("/guide", "功能指南", GUIDES["overview"]["intro"]),
        Page("/guide/conversion", "切片转换指南", GUIDES["conversion"]["intro"]),
        Page("/guide/collaboration", "协作指南", GUIDES["collaboration"]["intro"]),
        Page("/tools/slides", "本地切片转换工具", "无需登录的浏览器本地切片识别、转换、校验与保存。"),
        Page("/demo", "公开 Demo", "无需登录体验发布的示例切片；不提供个人文件上传。"),
        Page("/site-map", "完整公开网页地图", "公开页面与账户入口，以及面向自动客户端的文档。"),
    ]
    for doc in agreement_store.builtin_documents():
        pages.append(Page("/legal/%s/%s" % (doc["slug"], doc["version"]),
                          doc["title"], "公开协议文稿；以页面公布的版本和原文为准。"))
    return tuple(pages)


ACCOUNT_PAGES = (
    Page("/login", "登录工作台", "登录后进入工作台；不向爬虫提供账户内容。"),
    Page("/register", "注册账号", "开放注册时验证邮箱并设置密码；开放状态以页面为准。"),
    Page("/registration-help", "注册帮助", "验证邮件、注册状态与联系作者的说明。"),
)
DISCOVERY_PATHS = frozenset({"/robots.txt", "/sitemap.xml", "/llms.txt", "/llms-full.txt"})
PRIVATE_PREFIXES = ("/app", "/admin", "/api/", "/internal/", "/r/", "/share/",
                    "/login", "/register", "/verify-email", "/activate", "/logout")


def domains_section(base):
    """Domains 声明（/llms.txt 与 /llms-full.txt 共用；短、事实性、英文）。"""
    return ["## Domains", "",
            "- Canonical origin: %s; all canonical page URLs use this origin."
            % base,
            "- %s serves the same service and accounts over the international "
            "optimized network route; it is not a separate site."
            % INTERNATIONAL_HOST,
            "- %s is deprecated and redirects to %s." % (DEPRECATED_HOST, base),
            ""]


def create_blueprint(origin):
    bp = Blueprint("public_site", __name__)

    @bp.get("/favicon.ico")
    def favicon():
        return current_app.send_static_file("brand/favicon.ico")

    def text_response(body, mimetype="text/plain"):
        resp = Response(body, mimetype=mimetype)
        resp.headers["Cache-Control"] = "public, max-age=300"
        resp.headers["Link"] = '</llms.txt>; rel="describedby"; type="text/markdown"'
        return resp

    def markdown_guide(key):
        guide = GUIDES[key]
        lines = ["# " + guide["title"], "", "> " + guide["intro"], ""]
        for title, body in guide["sections"]:
            lines.extend(["## " + title, "", body, ""])
        lines.extend(["## 页面导航", "",
                      "- [网站地图](%s/site-map): 全部公开页面" % origin(),
                      "- [切片转换工具](%s/tools/slides): 在浏览器本地转换" % origin(),
                      "- [LLM 索引](%s/llms.txt): 按需阅读的文档索引" % origin(), ""])
        return "\n".join(lines)

    @bp.get("/robots.txt")
    def robots():
        # 默认不爬未知资源；发布的页面与必需静态资源显式放行。
        lines = ["User-agent: *", "Disallow: /", "Allow: /static/", "Allow: /favicon.ico$",
                 "Allow: /legal/", "Allow: /guide/", "Disallow: /*?"]
        lines.extend("Allow: " + p.path + "$" for p in public_pages())
        lines.extend("Allow: " + p + "$" for p in sorted(DISCOVERY_PATHS | {"/index.md", "/guide.md"}))
        lines.extend("Disallow: " + p for p in PRIVATE_PREFIXES)
        lines.extend(["", "Sitemap: %s/sitemap.xml" % origin(),
                      "# Public documentation for AI assistants: %s/llms.txt" % origin(), ""])
        return text_response("\n".join(lines))

    @bp.get("/sitemap.xml")
    def sitemap():
        root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for page in public_pages():
            SubElement(SubElement(root, "url"), "loc").text = origin() + page.path
        return text_response(tostring(root, encoding="unicode", xml_declaration=False), "application/xml")

    @bp.get("/site-map")
    def site_map():
        domains_note = (
            "主域名（canonical）是 %s，页面与站点地图的规范链接一律使用它。"
            "%s 是同一服务的国际线路（账号通用，不是独立站点）；"
            "%s 已弃用，自动跳转主域名。"
            % (origin(), INTERNATIONAL_HOST, DEPRECATED_HOST))
        return render_template("public_guide.html", title="公开网页地图",
                               intro="全部公开产品页面、文档与账户入口。个人切片、分享令牌和内部 API 不列入公开地图。",
                               sections=(("域名说明", domains_note),),
                               pages=public_pages(), accounts=ACCOUNT_PAGES,
                               canonical=origin() + request.path)

    @bp.get("/guide")
    @bp.get("/guide/<name>")
    def guide(name="overview"):
        if name not in GUIDES:
            abort(404)
        data = GUIDES[name]
        return render_template("public_guide.html", title=data["title"], intro=data["intro"],
                               sections=data["sections"], pages=(), accounts=(),
                               markdown_path="/guide.md" if name == "overview" else "/guide/%s.md" % name,
                               canonical=origin() + request.path)

    @bp.get("/index.md")
    @bp.get("/guide.md")
    @bp.get("/guide/<name>.md")
    def guide_md(name="overview"):
        if name not in GUIDES:
            abort(404)
        return text_response(markdown_guide(name), "text/markdown")

    @bp.get("/legal/<slug>.md")
    def legal_md(slug):
        doc = agreement_store.get_builtin_document(slug)
        if not doc:
            abort(404)
        return text_response(doc["content"], "text/markdown")

    @bp.get("/llms.txt")
    def llms():
        base = origin()
        body = ["# HistoPilot", "", "> A pathology slide viewer, collaboration workbench and browser-local slide converter. / 病理切片查看、协作与本地格式转换。", "",
                "Read the sitemap for all indexable public pages. The guides describe product features and limitations. Private slides, account data and internal APIs are not public documentation; do not crawl or bypass their access controls.", ""]
        body.extend(domains_section(base))
        body.extend(["## Website map", "",
                "- [Complete public website map](%s/site-map): Human-readable map, including account entry points." % base,
                "- [XML sitemap](%s/sitemap.xml): All indexable public pages; no private slide identifiers." % base,
                "## Product documentation", "",
                "- [Feature guide](%s/guide.md): Viewing, folders, annotations, AI requirements and sharing." % base,
                "- [Slide conversion](%s/guide/conversion.md): Supported inputs, complete bundles, OME-TIFF vs pyramidal TIFF, local processing and upload boundaries." % base,
                "- [Collaboration](%s/guide/collaboration.md): Sharing scope, annotation permissions, notes and revocation." % base,
                "- [Full text](%s/llms-full.txt): Combined public guides and page links." % base,
                "## Public application pages", "",
                "- [Homepage](%s/): Product overview and demonstrations." % base,
                "- [Local converter](%s/tools/slides): Browser-based tool; sign-in is not required to convert and save locally." % base,
                "- [Demo](%s/demo): Published demo slides; no private uploads." % base,
                "## Policies", ""])
        for slug in agreement_store.SLUG_TO_TYPE:
            doc = agreement_store.get_builtin_document(slug)
            body.append("- [%s](%s/legal/%s.md): Authoritative public policy text." % (doc["title"], base, slug))
        return text_response("\n".join(body) + "\n", "text/markdown")

    @bp.get("/llms-full.txt")
    def llms_full():
        body = ["# HistoPilot — public product documentation", "",
                "This file contains public product guides only; it does not grant access to slides or accounts.", ""]
        body.extend(domains_section(origin()))
        for key in GUIDES:
            body.append(markdown_guide(key))
        body.extend(["## Complete public page map", ""])
        body.extend("- [%s](%s%s): %s" % (p.title, origin(), p.path, p.summary) for p in public_pages())
        return text_response("\n".join(body) + "\n", "text/markdown")

    return bp
