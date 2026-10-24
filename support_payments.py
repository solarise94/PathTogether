# -*- coding: utf-8 -*-
"""支付宝「AI 网页应用收款」赞助订单（沙箱，自愿赞助）——服务端权威实现。

设计边界（docs/converter-support-page-design-20261021.md §支付宝 AI 付的后续扩展）：

- 赞助完全自愿、匿名可用：订单**不绑定平台账号**（表无 user 外键），绝不兑换
  AI 额度/存储/功能，不触碰 billing_store / spend_store。赞助与消费额度永久独立。
- 仅 `alipay.trade.page.pay` 下单（AI 网页应用收款唯一默认下单接口）；查询/
  退款/退款查询/关闭走统一收单通用契约（Skill 1.7.5 本地契约）。
- SDK（alipay-sdk-python==3.7.1411，从安装源码核实）：
  - ``from alipay.aop.api.AlipayClientConfig import AlipayClientConfig``
    ``from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient``
  - 页面跳转类 API **必须** ``client.page_execute(request)``（默认 http_method
    "POST" 返回 HTML 表单，需在浏览器渲染并提交；``http_method="GET"`` 返回
    已签名的网关跳转 URL，可作 303 Location 直达收银台；``execute()`` 只用于
    查询/退款/关闭等服务端 API）；
  - Python 使用 **PKCS#1** 私钥（沙箱文件字段 ``appPrivatePkcsKey``），原样
    写入 SDK 配置：不添加 PEM 头尾、不转换格式、不 Base64 重编码；
  - 时间戳由 SDK 自行按 ``yyyy-MM-dd HH:mm:ss`` 生成（DefaultAlipayClient
    ``__prepare_request_params``），业务代码不再拼接时间参数。
- 网关：沙箱 ``https://openapi-sandbox.dl.alipaydev.com/gateway.do``、生产
  ``https://openapi.alipay.com/gateway.do``（Skill 契约/示例明文；生产网关绝不
  作为沙箱缺省值）。CSP ``form-action`` 放开当前模式下单 POST 的**完整 302
  跳转链**精确 origin（网关 → unitradeprod 中转 → 收银台；Chrome 对每一跳都
  执行 form-action——见 ``form_action_origins``）。
- 同步回跳 ``return_url``（GET /support/return）**不是付款成功依据**：只从中取
  ``out_trade_no`` 定位本地订单，状态一律以本地库或服务端 ``alipay.trade.query``
  （SDK 自动验签）为准；页面渲染/拒绝绝不回显同步参数值。
- 异步通知（POST /support/alipay/notify）：POST 表单（非 JSON）；用本项目已安装
  SDK 的验签原语（``SignatureUtils.get_sign_content`` + ``verify_with_rsa``）
  封装验签适配器（禁止手写临时 RSA 实现）；验签通过后按运行时配置快照校验
  ``app_id`` / ``out_trade_no`` / ``total_amount`` / ``seller_id``；只有
  ``TRADE_SUCCESS``/``TRADE_FINISHED`` 且不含退款/关单/分账事件特征
  （``out_biz_no``/``gmt_refund``/``refund_fee``）才认定付款成功；持久化事件表
  幂等；处理成功返回纯文本 ``success``，其余一律 ``failure`` 文本（支付宝会重试）。
- 沙箱本地无公网 HTTPS notify_url ⇒ 按契约**省略** ``notify_url``（不传 None/
  空串/占位 URL），通知处理代码仍然存在；**补偿查单（sweep → trade.query）是
  沙箱验收路径**。生产上线必须配置公网 HTTPS notify_url（部署文档，未上线）。
- 金额一律整数分（BIGINT）；边界上的元字符串用 Decimal 精确换算，全程禁浮点。
- 生产模式 fail-closed：启用 production 而任一生产配置缺失（app id / 应用私钥
  文件路径 / 支付宝公钥文件路径 / 收款方 PID）→ 运行时配置加载失败，支付功能
  保持关闭（404/503），绝不回退示例值、占位密钥或沙箱网关。生产密钥材料只经
  环境变量给的**文件路径**由部署方放置，代码不内嵌、不生成、不转换密钥。

CLI（补偿与运维）::

    python support_payments.py check                     # 校验当前模式配置（不打印密钥）
    python support_payments.py sweep [--min-age-minutes 16]
    python support_payments.py query --out-trade-no <id>
    python support_payments.py close --out-trade-no <id> [--reason expired]

本模块的日志与 CLI 输出**绝不**包含任何密钥材料（.alipay-sandbox.json 的
私钥/公钥值、生产密钥文件内容）。
"""
import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import sys
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlsplit

import psycopg
import psycopg.errors

import pg_store

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 沙箱网关（Skill 1.7.5 alipay-sdk-reminder「检查网关地址」与各 Python 示例注释；
#: AI 网页应用收款沙箱专用，非 AlipayClientConfig(sandbox_debug=True) 的
#: openapi.alipaydev.com——后者是旧版开放平台沙箱，不在本 Skill 契约内）。
GATEWAY_SANDBOX = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
#: 生产网关（仅 production 模式；绝不作为沙箱缺省）。
GATEWAY_PRODUCTION = "https://openapi.alipay.com/gateway.do"

#: 网站支付固定产品码（webpay 契约：必填且固定）。
PRODUCT_CODE_FAST_INSTANT_TRADE_PAY = "FAST_INSTANT_TRADE_PAY"

#: 桌面端「支付状态」弹层内嵌二维码（仍是 alipay.trade.page.pay 同一产品，
#: 只是 biz 参数让收银台渲染可嵌入的裸二维码页；2026-10-22 沙箱实测：
#: standard/auth.htm 返回 200×200 canvas 二维码、无页头页脚、无 X-Frame-Options
#: 且不 frame-bust，iframe 内每 ~18s 经 renewQRCode.json 自动换码，付款后由
#: 收银台自身导航到 return_url）。qr_pay_mode=4 是官方「可嵌入二维码」档位，
#: qrcode_width 只在该模式下生效——两个字段都是已安装 SDK
#: AlipayTradePagePayModel 的显式属性（从安装源码核实，非手拼 biz_content）。
QR_PAY_MODE_EMBED = "4"
QRCODE_WIDTH_EMBED = "200"

#: 下单超时：15 分钟。page.pay 下发**绝对** ``time_expire``（yyyy-MM-dd
#: HH:mm:ss，Asia/Shanghai，= created_at + 15m）而不是相对 ``timeout_express``：
#: 补偿 sweep 只在本地时间越过**同一条**截止线（再加安全余量）后才允许关单，
#: 两侧对齐同一时钟——否则本地 sweep 可能把支付宝侧仍可支付的单提前关掉
#: （paid after local close 的主要来源）。相对超时的语义由绝对时间恢复。
ORDER_TIMEOUT = datetime.timedelta(minutes=15)

#: 下单超时的支付宝侧展示名（不再下发 timeout_express；保留常量仅供文档/测试
#: 断言「biz_content 中不存在该字段」参照）。
ORDER_TIMEOUT_EXPRESS = "15m"

#: 下单 POST 的浏览器落点链（2026-10-21 本机对沙箱实测 Location 头）：
#:   网关 gateway.do --302--> unitradeprod*/appAssign.htm --302--> excashier*
#: Chrome 对 302 重定向链的**每一跳**都执行 CSP form-action，所以支付页
#: form-action 必须精确列出当前模式的全部三台 origin（沙箱/生产各自独立，
#: 无通配符）。
CASHIER_ORIGINS_SANDBOX = (
    "https://openapi-sandbox.dl.alipaydev.com",        # 网关（表单 action）
    "https://unitradeprod-sandbox.dl.alipaydev.com",   # 中转 appAssign.htm
    "https://excashier-sandbox.dl.alipaydev.com",      # 收银台
)
#: 生产链来自官方 page.pay 文档（openapi.alipay.com/gateway.do →
#: unitradeprod.alipay.com → excashier.alipay.com）。本机只有沙箱凭据、无法
#: 实测生产 Location 头——生产上线前必须用生产凭据复测一次跳转链并核对本表
#: （见 HUMAN-SANDBOX-PAYMENT.md；生产模式默认 fail-closed，不会误用本表）。
CASHIER_ORIGINS_PRODUCTION = (
    "https://openapi.alipay.com",
    "https://unitradeprod.alipay.com",
    "https://excashier.alipay.com",
)


def form_action_origins(mode):
    """当前模式下单 POST 跳转链的精确 origin 列表（CSP form-action 值）。"""
    if mode == "sandbox":
        return CASHIER_ORIGINS_SANDBOX
    if mode == "production":
        return CASHIER_ORIGINS_PRODUCTION
    raise SupportPayConfigError("未知支付模式：%r" % (mode,))


#: sweep 本地关单安全余量（秒）：越过 time_expire 截止线后再余量这么久，才
#: 允许 sweep 关单（PENDING 调 close API / NOT_EXIST 本地关单）——吸收支付宝
#: 侧超时判定的延迟与查询缓存，绝不提前关闭仍可支付的单。（值在 _int_env
#: 定义后计算，见下方 SWEEP_CLOSE_MARGIN_SECONDS。）

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 订单标题（固定文案，不收集用户自由输入——赞助期跳过留言审核面）。
ORDER_SUBJECT = "HistoPilot 项目赞助"

#: 金额边界（分）：¥1.00 – ¥500.00；猫咪礼物档：¥6 / ¥18 / ¥68（R2 add-on）。
AMOUNT_MIN_FEN = 100
AMOUNT_MAX_FEN = 50000
AMOUNT_PRESETS_FEN = (600, 1800, 6800)

#: 猫咪礼物档（R2 add-on）：精确金额（分）→ 档位 key。**服务端权威**——档位
#: 只由订单的 amount_fen 推导（回跳页/收据展示用），绝不信任任何客户端
#: 「档位」字段；其他金额（含自定义）→ None（回跳页播放通用开心/爱心反应）。
GIFT_TIERS = (
    (600, "cat_food"),   # 一份猫粮
    (1800, "cat_toy"),   # 猫玩具
    (6800, "mystery"),   # 神秘礼物（光猫）
)


def gift_tier_for_amount(amount_fen):
    """精确金额分 → 礼物档 key；非档位金额（含自定义）→ None。"""
    amount_fen = int(amount_fen)
    for fen, key in GIFT_TIERS:
        if amount_fen == fen:
            return key
    return None


#: 档位 key → 可收集礼物 kind（/support 猫卡的喂猫互动用）。档位礼物与档位
#: 同名；非档位金额（含自定义）→ fish_treat（一条小鱼干）。**服务端权威**：
#: kind 只由本地订单行的 amount_fen 推导，绝不信任客户端字段。
GIFT_COLLECTIBLE_FOR_TIER = {
    "cat_food": "cat_food",
    "cat_toy": "cat_toy",
    "mystery": "mystery",
}
GIFT_COLLECTIBLE_CUSTOM = "fish_treat"


def gift_collectible_for_amount(amount_fen):
    """精确金额分 → 可收集礼物 kind；非档位金额（含自定义）→ fish_treat。"""
    return GIFT_COLLECTIBLE_FOR_TIER.get(
        gift_tier_for_amount(amount_fen), GIFT_COLLECTIBLE_CUSTOM)

#: 订单号：``sp`` + 24 hex（secrets），高熵不可猜、商户侧唯一（PK 兜底）。
_OUT_TRADE_NO_RE = re.compile(r"^[0-9a-z]{26}$")

#: 退款请求号：``rf`` + 24 hex。
_OUT_REQUEST_NO_RE = re.compile(r"^[0-9a-z]{26}$")

#: 元金额输入形态（服务端权威校验）：≤3 位整数 + 可选 1–2 位小数；拒绝
#: 负号/空白/科学计数/多余小数/前导零（"01.00"）等一切其他形态。
_AMOUNT_RE = re.compile(r"^(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,2})?$")

#: 通知事件幂等键：notify_id 缺失时用全参规范化 hash（不含 sign/sign_type）。
_NOTIFY_PARAM_SKIP = frozenset({"sign", "sign_type"})

#: 查询业务状态映射（common-trade 契约：显式映射，其余/缺失 = UNKNOWN）。
_TRADE_STATE_MAP = {
    "TRADE_SUCCESS": "PAID",
    "TRADE_FINISHED": "PAID",
    "WAIT_BUYER_PAY": "PENDING",
    "TRADE_CLOSED": "CLOSED",
}


class SupportPayConfigError(RuntimeError):
    """运行时支付配置缺失/非法（fail-closed：上层保持功能关闭）。"""


class TradeOperationError(RuntimeError):
    """支付宝**明确返回的业务失败**（响应可解析、带失败 code/sub_code）。"""


class TradeOperationUncertainError(TradeOperationError):
    """交易调用**结果未知**：超时/传输中断/响应无法解析。

    与明确业务失败（``TradeOperationError``）严格区分（2026-10-21 评审）：
    结果未知时资金侧必须按「可能已被支付宝受理」处理——退款额度继续占用
    （行状态 unknown），后续退款必须先用**同一** ``out_request_no`` 退款查询
    确认；只有明确业务失败才允许判 failed 并释放额度。
    """


#: 退款**明确业务拒绝** sub_code 白名单（2026-10-21 R1 评审：fail-safe 分类——
#: 白名单之外的**一切**异常/错误码都按「结果未知」处理，额度继续占用）。
#:
#: **准入原则**：白名单只能收录能证明「本请求号（out_request_no）未动任何
#: 资金」的错误码。
#: ``ACQ.DISCORDANT_REPEAT_REQUEST`` 因此**明确不在**白名单（R1 复核）：它
#: 表示同一 out_request_no 之下已存在**参数不同**的请求——先前的请求可能
#: 已被受理，判 failed 会释放额度并放行新号重复退款；该码按结果未知处理
#: （额度占用、同号解决）并记告警（``_alert_refund_discordant_repeat``）。
#:
#: 来源（逐字核对，未猜测）：Skill 本地退款契约与示例
#: （~/.codex/skills/alipay-aipay/references/integration/：
#: ``modules/interface-contract/common-trade-contract.md`` §退款/§退款查询、
#: ``modules/code-examples/python/1-通用接口/统一收单交易退款接口代码示例.md``）
#: **未枚举**业务拒绝码——示例里唯一错误码是 ``isp.unknow-error``（code
#: 20000「系统繁忙」，服务级 → 本来就属结果未知类）。按 Skill 自身的错误码
#: fallback 规则（``modules/troubleshooting-flow.md`` 第 8 条：官方公共错误码
#: /业务接口文档），采用官方 alipay.trade.refund 错误码表
#: （iopenhome.alipay.com/docs/ac/solution_api/trade-refund，2026-10-21 抓取）
#: 中列出的业务拒绝码；``ACQ.SYSTEM_ERROR``（系统错误）与 ``isp.*``/20000 类
#: 服务错误**不在**白名单。
REFUND_DEFINITE_REJECT_SUB_CODES = frozenset({
    "ACQ.INVALID_PARAMETER",            # 参数无效
    "ACQ.SELLER_BALANCE_NOT_ENOUGH",    # 卖家余额不足，退款失败
    "ACQ.REFUND_AMT_NOT_EQUAL_TOTAL",   # 退款金额超限（与交易金额不等）
    "ACQ.REASON_TRADE_BEEN_FREEZEN",    # 交易被冻结
    "ACQ.TRADE_NOT_EXIST",              # 交易不存在
    "ACQ.TRADE_HAS_FINISHED",           # 交易已完成（不可退）
    "ACQ.TRADE_STATUS_ERROR",           # 交易状态非法
    "ACQ.REASON_TRADE_REFUND_FEE_ERR",  # 退款金额无效
    "ACQ.TRADE_NOT_ALLOW_REFUND",       # 不允许退款
    "ACQ.REFUND_FEE_ERROR",             # 退款费用错误
})


def is_definite_refund_rejection(exc):
    """退款调用异常是否为**明确业务拒绝**（fail-safe 分类）。

    只有当异常是 ``TradeOperationError``（且非 ``TradeOperationUncertainError``
    子类）**并且**携带白名单内的显式 ``sub_code`` 时才算。其余一律按「结果
    未知」（额度继续占用、同号解决）：

    - 无 ``sub_code`` 的普通 ``TradeOperationError``
    - ``ACQ.SYSTEM_ERROR`` / ``isp.*`` / ``aop.*`` / code 20000 类服务错误
    - ``ACQ.DISCORDANT_REPEAT_REQUEST``（同号下已有参数不同的请求——先前
      请求可能已被受理；见 ``is_discordant_repeat_rejection``）
    - 白名单之外的未知 ``sub_code``
    - ``TradeOperationUncertainError``（超时/传输中断/响应不可解析）
    - 任何其他异常
    """
    if not isinstance(exc, TradeOperationError):
        return False
    if isinstance(exc, TradeOperationUncertainError):
        return False
    return (getattr(exc, "sub_code", "") or "") in REFUND_DEFINITE_REJECT_SUB_CODES


def is_discordant_repeat_rejection(exc):
    """退款返回 ``ACQ.DISCORDANT_REPEAT_REQUEST``：同一 out_request_no 之下
    已存在**参数不同**的请求——先前请求可能已被受理 → 结果未知（绝不判
    failed/释放额度；额度占用、同号解决，并触发告警）。"""
    return (isinstance(exc, TradeOperationError)
            and not isinstance(exc, TradeOperationUncertainError)
            and (getattr(exc, "sub_code", "") or "")
            == "ACQ.DISCORDANT_REPEAT_REQUEST")


def _alert_refund_discordant_repeat(out_trade_no):
    """警告：退款受理返回 ACQ.DISCORDANT_REPEAT_REQUEST——按结果未知处理
    （额度占用、同号解决）。只记订单号，无参数/密钥。"""
    try:
        _LOG.warning(
            "support-pay ALERT refund discordant repeat request: earlier"
            " request under this out_request_no may have been accepted;"
            " treated as unknown, amount stays occupied, out_trade_no=%s",
            out_trade_no)
    except Exception:
        pass


def _int_env(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


#: sweep 本地关单安全余量（秒）：越过 time_expire 截止线后再余量这么久，才
#: 允许 sweep 关单（PENDING 调 close API / NOT_EXIST 本地关单）——吸收支付宝
#: 侧超时判定的延迟与查询缓存，绝不提前关闭仍可支付的单。
SWEEP_CLOSE_MARGIN_SECONDS = _int_env(
    "SUPPORT_PAY_SWEEP_CLOSE_MARGIN_SECONDS", 120)

#: 状态端点查单节流窗口（秒，与 order_query_throttled 旧字面量 3 同值）：
#: 窗口内的**成功**查单结论可被下一次轮询复用为 confirmed（弹层 0:00 终检
#: 通常落在节流窗口内，复用而不是再次打支付宝）。
STATUS_QUERY_WINDOW_SECONDS = 3

#: 状态端点 expired 判定宽限（秒）：time_expire 截止线（created_at +
#: ORDER_TIMEOUT）之后，确认查单再晚这么多才算 expired——吸收买家在
#: time_expire 前一刻提交、仍在处理中的支付，以及查单→落库延迟。
STATUS_EXPIRED_GRACE_SECONDS = 10


# --------------------------------------------------------------------------- #
# 运行时支付配置（.alipay-sandbox.json / 生产环境变量文件路径）
# --------------------------------------------------------------------------- #
class RuntimeConfig:
    """一次加载、只读的支付配置快照。

    密钥字段仅存内存，``__repr__``/``keys_fingerprint`` 等绝不输出值本身。
    """

    __slots__ = ("mode", "gateway", "app_id", "seller_pid",
                 "app_private_key", "alipay_public_key")

    def __init__(self, mode, gateway, app_id, seller_pid,
                 app_private_key, alipay_public_key):
        self.mode = mode
        self.gateway = gateway
        self.app_id = app_id
        self.seller_pid = seller_pid
        self.app_private_key = app_private_key
        self.alipay_public_key = alipay_public_key

    @property
    def gateway_origin(self):
        """CSP form-action 用的精确 origin（scheme://host[:port]）。"""
        parts = urlsplit(self.gateway)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        return origin

    def describe(self):
        """可安全打印/记录的描述（无任何密钥材料；app_id/pid 非机密但脱敏）。"""
        return {
            "mode": self.mode,
            "gateway_origin": self.gateway_origin,
            "app_id_masked": _mask(self.app_id),
            "seller_pid_masked": _mask(self.seller_pid),
        }

    def __repr__(self):  # 防手滑把配置打进日志
        return "RuntimeConfig(mode=%r, gateway=%r)" % (self.mode, self.gateway)


def _mask(value):
    value = value or ""
    if len(value) <= 4:
        return "***"
    return value[:2] + "***" + value[-2:]


def _read_key_file(path_env):
    """生产模式：读环境变量给的**文件路径**内容（原始文本，只去首尾空白）。

    绝不转换格式、不添加/去除 PEM 头尾之外的内容；文件缺失/不可读 → fail-closed。
    """
    raw_path = (os.environ.get(path_env) or "").strip()
    if not raw_path:
        raise SupportPayConfigError(
            "生产支付配置缺失：未设置 %s" % path_env)
    path = Path(raw_path)
    try:
        if not path.is_file():
            raise SupportPayConfigError(
                "生产支付密钥文件不存在：%s（%s）" % (path_env, _mask(raw_path)))
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SupportPayConfigError(
            "生产支付密钥文件不可读：%s" % path_env) from exc
    value = content.strip()
    if not value:
        raise SupportPayConfigError(
            "生产支付密钥文件为空：%s" % path_env)
    return value


def _require_production_env(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SupportPayConfigError("生产支付配置缺失：未设置 %s" % name)
    return value


def load_runtime_config(mode):
    """按模式加载运行时支付配置；缺失/非法抛 SupportPayConfigError。

    - sandbox：读 ``SUPPORT_ALIPAY_SANDBOX_CONFIG_PATH``（缺省仓库根
      ``.alipay-sandbox.json``，0600、已 gitignore；**绝不**打印/复制其值）。
      Python 使用 ``appPrivatePkcsKey``（PKCS#1）原值；``pid`` 为收款方
      seller_pid（沙箱文件 apps 条目内字段，键名以实际文件结构为准）。
    - production：全部来自环境变量（app id、应用私钥文件、支付宝公钥文件、
      收款方 PID）；任一缺失 → fail-closed。
    """
    if mode == "sandbox":
        base = Path(__file__).resolve().parent
        default_path = base / ".alipay-sandbox.json"
        raw_path = ((os.environ.get("SUPPORT_ALIPAY_SANDBOX_CONFIG_PATH") or "")
                    .strip() or str(default_path))
        path = Path(raw_path)
        try:
            if not path.is_file():
                raise SupportPayConfigError(
                    "沙箱支付配置文件不存在（SUPPORT_ALIPAY_SANDBOX_CONFIG_PATH）")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SupportPayConfigError(
                "沙箱支付配置不可读或非法 JSON") from exc
        apps = data.get("appIds") if isinstance(data, dict) else None
        if not (isinstance(apps, list) and apps
                and isinstance(apps[0], dict)):
            raise SupportPayConfigError(
                "沙箱支付配置缺少可用的 appIds 条目")
        entry = apps[0]
        app_id = str(entry.get("appId") or "").strip()
        app_private_key = str(entry.get("appPrivatePkcsKey") or "").strip()
        alipay_public_key = str(entry.get("alipayPublicKey") or "").strip()
        seller_pid = str(entry.get("pid") or "").strip()
        if not (app_id and app_private_key and alipay_public_key
                and seller_pid):
            raise SupportPayConfigError(
                "沙箱支付配置字段不完整（需要 appId/appPrivatePkcsKey/"
                "alipayPublicKey/pid）")
        return RuntimeConfig("sandbox", GATEWAY_SANDBOX, app_id, seller_pid,
                             app_private_key, alipay_public_key)
    if mode == "production":
        # 生产 fail-closed：四项生产配置缺一不可（密钥只给文件路径，部署方放置；
        # 私钥格式须为 PKCS#1——非 Java SDK 要求，上线前用开放平台密钥工具确认）。
        app_id = _require_production_env("SUPPORT_ALIPAY_PAY_APP_ID")
        seller_pid = _require_production_env("SUPPORT_ALIPAY_PAY_SELLER_PID")
        app_private_key = _read_key_file("SUPPORT_ALIPAY_PAY_APP_PRIVATE_KEY_FILE")
        alipay_public_key = _read_key_file(
            "SUPPORT_ALIPAY_PAY_ALIPAY_PUBLIC_KEY_FILE")
        return RuntimeConfig("production", GATEWAY_PRODUCTION, app_id,
                             seller_pid, app_private_key, alipay_public_key)
    raise SupportPayConfigError("未知支付模式：%r" % (mode,))


#: 运行时配置缓存（按 mode；进程内一次加载。测试用 reset_runtime_cache 复位）。
_RUNTIME_CACHE = {}


def get_runtime(mode):
    """取当前模式配置（带缓存）；不可用抛 SupportPayConfigError。"""
    cfg = _RUNTIME_CACHE.get(mode)
    if cfg is None:
        cfg = load_runtime_config(mode)
        _RUNTIME_CACHE[mode] = cfg
    return cfg


def reset_runtime_cache():
    """清空配置缓存（测试/配置文件更换后使用）。"""
    _RUNTIME_CACHE.clear()


def runtime_ready(mode):
    """功能闸：配置可用返回配置，否则 None（调用方 fail-closed 404/503）。"""
    try:
        return get_runtime(mode)
    except SupportPayConfigError:
        return None


# --------------------------------------------------------------------------- #
# 金额（整数分权威；元字符串 Decimal 精确换算，禁浮点）
# --------------------------------------------------------------------------- #
class InvalidAmount(ValueError):
    """金额输入非法（形态/边界外的任何值）。"""


def parse_amount_fen(raw):
    """元金额字符串 → 整数分。形态与边界外的值一律 InvalidAmount。

    只接受**字符串**输入（HTML 表单字段语义）：非字符串（int/float/None）
    一律拒绝——金额通道不允许 JSON number（JS Number 二进制浮点不可作金额
    来源）。接受 ``6.66`` / ``6.6`` / ``6`` / ``500``；拒绝负数、空白、科学
    计数、三位小数、前导零、``>500``、``<1`` 等。服务端校验权威，前端约束
    只是体验层。
    """
    if not isinstance(raw, str):
        raise InvalidAmount("金额必须是字符串（表单字段）")
    text = raw
    # 不 strip：前后空白等一切「其他形态」一律拒绝（服务端权威校验）
    if not _AMOUNT_RE.fullmatch(text):
        raise InvalidAmount("金额格式非法")
    try:
        fen = int((Decimal(text) * 100).to_integral_exact())
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAmount("金额换算失败") from exc
    if fen < AMOUNT_MIN_FEN or fen > AMOUNT_MAX_FEN:
        raise InvalidAmount("金额超出可支持范围（¥1–¥500）")
    return fen


def fen_to_yuan(fen):
    """整数分 → 元字符串（两位小数；用于下单 total_amount 与回跳凭条）。"""
    fen = int(fen)
    if fen <= 0:
        raise ValueError("金额必须为正整数分")
    return "%d.%02d" % (fen // 100, fen % 100)


def parse_refund_amount_fen(raw):
    """退款金额（元字符串 → 整数分）：字符串 only、最多两位小数、最小 ¥0.01。

    与下单金额解析（``parse_amount_fen``，最小 ¥1）**分离**（2026-10-21 评审）：
    部分退款允许低于 ¥1（如 ¥0.66）。上限不在此校验——调用方必须对照
    「剩余可退」（实付 − 已退/占用中）判断，防止累计超额。
    """
    if not isinstance(raw, str):
        raise InvalidAmount("退款金额必须是字符串（表单/JSON 字段）")
    if not _AMOUNT_RE.fullmatch(raw):
        raise InvalidAmount("退款金额格式非法")
    try:
        fen = int((Decimal(raw) * 100).to_integral_exact())
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAmount("退款金额换算失败") from exc
    if fen < 1:
        raise InvalidAmount("退款金额最小 ¥0.01")
    return fen


# --------------------------------------------------------------------------- #
# SDK 客户端（导入路径/方法名以已安装 alipay-sdk-python 源码为准）
# --------------------------------------------------------------------------- #
def build_client(runtime):
    """按配置快照构建 DefaultAlipayClient（RSA2；不带 logger——SDK 会把
    含签名的完整请求串打进 logger，本集成保持静默）。"""
    from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
    from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient

    cfg = AlipayClientConfig()
    cfg.server_url = runtime.gateway          # 沙箱/生产网关按模式切换
    cfg.app_id = runtime.app_id
    # Python 用 PKCS#1 原值（沙箱 appPrivatePkcsKey 字段/生产 PKCS#1 文件内容），
    # 不做任何格式加工（SDK 自行包装 PEM 头尾）。
    cfg.app_private_key = runtime.app_private_key
    cfg.alipay_public_key = runtime.alipay_public_key
    cfg.sign_type = "RSA2"
    cfg.charset = "utf-8"
    cfg.format = "json"
    return DefaultAlipayClient(alipay_client_config=cfg)


def verify_notify_signature(params, runtime):
    """异步通知验签适配器：封装当前安装 SDK 的验签原语。

    实现（alipay-sdk-python 3.7.1411 源码核实：无独立 rsaCheckV1 门面，等价
    链路为 ``SignatureUtils.get_sign_content`` + ``verify_with_rsa``，与官方
    Java ``AlipaySignature.rsaCheckV1`` 同规则——剔除 ``sign``/``sign_type``
    后按键排序拼 ``k=v&…``，用支付宝公钥验 RSA2 签名）。
    验签前不丢字段、不改字段名/值；任何异常按验签失败处理。
    """
    from alipay.aop.api.util.SignatureUtils import get_sign_content
    from alipay.aop.api.util.SignatureUtils import verify_with_rsa

    sign = params.get("sign") or ""
    if not sign:
        return False
    payload = {k: v for k, v in params.items() if k not in _NOTIFY_PARAM_SKIP}
    try:
        content = get_sign_content(payload)
        return bool(verify_with_rsa(
            runtime.alipay_public_key, content.encode("utf-8"), sign))
    except Exception:  # 密钥/编码/签名形态异常一律按验签失败
        return False


# --------------------------------------------------------------------------- #
# 页面支付表单（page_execute；CSP 收敛：去内联 script/style，外链 JS 自动提交）
# --------------------------------------------------------------------------- #
_FORM_HEAD = '<form name="punchout_form" method="post" action="'
_INLINE_STYLE = ' style="display:none"'


def sanitize_page_form(form_html):
    """把 SDK 返回的支付表单收敛为严格 CSP + 浏览器可提交形态（保持官方布局）。

    SDK（3.7.1411 ``DefaultAlipayClient.__build_form``）输出：
    ``<form name="punchout_form" method="post" action="gateway.do?公共参数
    (含签名)">…biz_content…</form><script>document.forms[0].submit();
    </script>``——公共参数（含 ``charset``/``sign``）在 **action 查询串**，
    body 只有 biz_content。

    2026-10-21 沙箱 headless Chromium 实测（应用同款 form-action 跳转链）：

    - **原始 action 查询串（含裸 ``+``）可以过 Chrome CSP form-action**——
      CSP source 匹配不比较查询串；此前「查询串里的 ``+`` 会被 form-action
      拦截」的判断不成立，当时的拦截来自 302 跳转链上的收银台主机不在
      form-action（已修复，见 ``form_action_origins``）；
    - 公共参数（含 ``charset=utf-8``）**必须留在 action 查询**：网关按查询串
      里的 ``charset`` 决定 POST body 的解码，缺省回退 GBK——把公共参数移进
      body 会让收银台把 UTF-8 的 subject 按 GBK 解成乱码（实测：「项目赞助」
      →「椤圭洰璧炲姪」）。

    因此本函数**不改动** SDK 的 action 与字段布局（等价官方行为），只做
    CSP/体验收敛（形态不符 → raise，调用方 fail-closed，绝不半改半渲染）：

    1. 只保留 ``<form>…</form>`` 片段（尾随内联 submit 脚本绝不渲染——自动
       提交由同源外链 ``/static/support/support_pay.js`` 完成，契约「必须渲染
       并自动提交表单」的等价实现）；
    2. form 加 ``class="pay-form"``（无 JS 手动提交按钮的样式钩子）与
       ``accept-charset="utf-8"``（与查询串 charset 一致的浏览器端声明）；
    3. 去掉 SDK 提交按钮上的内联 style（CSP style-src 'self'）；
    4. 校验 action 查询含 ``charset=utf-8``（防 GBK 回退乱码；缺失即拒绝渲染）。
    """
    html = form_html or ""
    if not html.startswith(_FORM_HEAD):
        raise TradeOperationError("支付表单形态与预期不符（page_execute 输出）")
    end = html.find("</form>")
    if end < 0:
        raise TradeOperationError("支付表单缺少闭合标签")
    form = html[:end + len("</form>")]
    if form.count(_FORM_HEAD) != 1:
        raise TradeOperationError("支付表单出现多个 form 头")

    # 防乱码闸：charset 必须仍在 action 查询（SDK 缺省即含；防未来 SDK/调用
    # 变化把它弄丢——见 docstring 实测记录）
    m = re.search(r'action="([^"]*)"', form)
    query_pairs = dict(parse_qsl(urlsplit(m.group(1)).query,
                                 keep_blank_values=True))
    if query_pairs.get("charset") != "utf-8":
        raise TradeOperationError("支付表单 action 查询缺少 charset=utf-8")

    form = form.replace(_FORM_HEAD,
                        '<form class="pay-form" name="punchout_form"'
                        ' accept-charset="utf-8" method="post" action="', 1)
    form = form.replace(_INLINE_STYLE, "")
    if "<script" in form or "javascript:" in form.lower():
        raise TradeOperationError("支付表单含脚本内容，拒绝渲染")
    return form


def order_time_expire(created_at):
    """订单的支付宝侧绝对超时（``time_expire``）：created_at + ORDER_TIMEOUT，
    格式 ``yyyy-MM-dd HH:mm:ss``、时区 Asia/Shanghai（支付宝网关所在时区）。

    用绝对时间而非相对 ``timeout_express``：补偿 sweep 依据同一截止线（再加
    SWEEP_CLOSE_MARGIN_SECONDS）才关单，两侧对齐，绝不把仍可支付的单提前
    本地关闭。naive datetime 按 UTC 解释（psycopg 对 timestamptz 返回 aware，
    naive 只可能来自手工构造的测试行）。
    """
    created = created_at if created_at.tzinfo \
        else created_at.replace(tzinfo=datetime.timezone.utc)
    deadline = created + ORDER_TIMEOUT
    return deadline.astimezone(_TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _order_close_deadline(created_at):
    """sweep 允许本地关单的最早时刻（aware UTC）：time_expire 截止线 + 余量。"""
    created = created_at if created_at.tzinfo \
        else created_at.replace(tzinfo=datetime.timezone.utc)
    return created + ORDER_TIMEOUT + datetime.timedelta(
        seconds=SWEEP_CLOSE_MARGIN_SECONDS)


def _build_page_pay_request(runtime, order, return_url, notify_url=None,
                            qr_embed=False):
    """构造 ``alipay.trade.page.pay`` 请求（POST 表单与 GET 跳转 URL 共用）。

    ``notify_url``：真实生产/公网联调传入；本地沙箱没有公网 HTTPS 地址时**省略**
    （契约：不传 None/空串/占位 URL）。``return_url`` 来自服务端
    （``_public_canonical_url('/support/return')``），绝不取自请求参数。
    ``time_expire`` 为绝对截止（Asia/Shanghai），见 ``order_time_expire``。
    ``qr_embed=True``：桌面端弹层内嵌二维码页（``/support/pay/<ref>/qr``）专用，
    额外下发 ``qr_pay_mode=4`` + ``qrcode_width``；普通整页收银台
    （``/support/pay/<ref>``）**绝不**携带这两个参数——同一下单接口、同一产品，
    仅 biz 参数差异。
    """
    from alipay.aop.api.domain.AlipayTradePagePayModel import \
        AlipayTradePagePayModel
    from alipay.aop.api.request.AlipayTradePagePayRequest import \
        AlipayTradePagePayRequest

    request = AlipayTradePagePayRequest()
    if return_url:
        request.return_url = return_url
    if notify_url:
        request.notify_url = notify_url
    model = AlipayTradePagePayModel()
    model.out_trade_no = order["out_trade_no"]
    model.total_amount = fen_to_yuan(order["amount_fen"])  # 两位小数字符串
    model.subject = order["subject"]
    model.product_code = PRODUCT_CODE_FAST_INSTANT_TRADE_PAY
    model.time_expire = order_time_expire(order["created_at"])
    if qr_embed:
        model.qr_pay_mode = QR_PAY_MODE_EMBED
        model.qrcode_width = QRCODE_WIDTH_EMBED
    request.biz_model = model
    return request


def build_page_pay_form(runtime, order, return_url, notify_url=None,
                        qr_embed=False):
    """构造 ``alipay.trade.page.pay`` 的 POST 表单 HTML（已收敛 CSP）。"""
    request = _build_page_pay_request(runtime, order, return_url,
                                      notify_url=notify_url, qr_embed=qr_embed)
    form_html = build_client(runtime).page_execute(request)  # POST 表单
    return sanitize_page_form(form_html)


def validate_page_pay_url(url, runtime):
    """校验 SDK 产出的 GET 跳转 URL（iframe 内 303 直达收银台用），形态不符
    一律 ``TradeOperationError``（fail-closed，绝不把半成品 URL 发给浏览器）：

    - 必须以运行时配置的**精确网关 URL** + ``?`` 开头（防拼到他方主机）；
    - 不得含任何空白/CR/LF（URL 进 Location 头，注入面一律拒绝）；
    - 查询串必须含 ``charset=utf-8``——网关按查询串里的 charset 解码请求
      （缺省回退 GBK，UTF-8 的 subject 会成乱码，同 ``sanitize_page_form``
      的闸）；
    - 必须含非空 ``sign``（无签名网关直接拒绝）。
    """
    text = url or ""
    if not text.startswith(runtime.gateway + "?"):
        raise TradeOperationError("支付跳转 URL 网关前缀与配置不符")
    if re.search(r"\s", text):
        raise TradeOperationError("支付跳转 URL 含空白字符")
    query = dict(parse_qsl(urlsplit(text).query, keep_blank_values=True))
    if query.get("charset") != "utf-8":
        raise TradeOperationError("支付跳转 URL 查询缺少 charset=utf-8")
    if not query.get("sign"):
        raise TradeOperationError("支付跳转 URL 缺少签名参数")
    return text


def build_page_pay_url(runtime, order, return_url, notify_url=None,
                       qr_embed=True):
    """构造 ``alipay.trade.page.pay`` 的**签名 GET 跳转 URL**（非表单）。

    供 /support/pay/<ref>/qr 在订单未决时 303 重定向：iframe 内零 HTML/零 JS
    直接跟随到收银台的可嵌入二维码页——不依赖浏览器执行跨站表单自动提交。
    biz 参数与 ``build_page_pay_form`` 完全一致（含 ``qr_embed`` 语义）。
    SDK（3.7.1411）：``page_execute(request, http_method="GET")`` 返回
    ``url + "?" + 公共参数 + "&" + biz 参数``（均 url_encode）。
    """
    request = _build_page_pay_request(runtime, order, return_url,
                                      notify_url=notify_url, qr_embed=qr_embed)
    url = build_client(runtime).page_execute(request, http_method="GET")
    return validate_page_pay_url(url, runtime)


# --------------------------------------------------------------------------- #
# 服务端交易 API（execute；SDK 自动对响应验签）
# --------------------------------------------------------------------------- #
def _execute(runtime, request, response_key):
    """execute + 解析响应；非 10000 抛 TradeOperationError（带 sub_code）。

    SDK ``execute`` 内部已用 ``alipay_public_key`` 验响应签名（``__parse_response``）。
    注意（3.7.1411 实测）：验签开启时 ``execute`` 返回的是**已剥信封**的响应体
    （即 ``{"code":..., "trade_status":...}`` 本身，不再有
    ``alipay_trade_query_response`` 外层键）——两种形态都兼容，绝不信包裹键之外
    的未知结构。

    失败语义（2026-10-21 评审拆分）：
    - 支付宝**明确返回**失败（可解析、code != 10000）→ ``TradeOperationError``
      （带 ``sub_code``）——业务拒绝，可判 failed；
    - 超时/传输中断/响应无法解析（没有可解析的支付宝应答）→
      ``TradeOperationUncertainError``——结果未知，资金侧按可能已受理处理。
    """
    client = build_client(runtime)
    try:
        raw = client.execute(request)
        data = json.loads(raw)
    except TradeOperationError:
        raise
    except Exception as exc:
        raise TradeOperationUncertainError("交易接口调用失败") from exc
    result = data.get(response_key) if isinstance(data, dict) else None
    if not isinstance(result, dict) and isinstance(data, dict) \
            and ("code" in data or "sub_code" in data):
        result = data  # 验签后已剥信封形态
    if not isinstance(result, dict):
        raise TradeOperationUncertainError("交易接口响应形态异常")
    if result.get("code") != "10000":
        err = TradeOperationError(
            "交易接口业务失败：%s" % (result.get("sub_code") or result.get("code")))
        err.sub_code = result.get("sub_code") or ""
        raise err
    return result


def query_trade(runtime, out_trade_no):
    """``alipay.trade.query`` → ``{"state": PAID/PENDING/CLOSED/NOT_EXIST/UNKNOWN,
    "total_amount": str|None, "trade_no": str|None, "trade_status": str|None}``。

    待支付/未知不抛 500；``ACQ.TRADE_NOT_EXIST`` 是下单后未提交/超时清场的正常态。
    """
    from alipay.aop.api.domain.AlipayTradeQueryModel import AlipayTradeQueryModel
    from alipay.aop.api.request.AlipayTradeQueryRequest import \
        AlipayTradeQueryRequest

    request = AlipayTradeQueryRequest()
    model = AlipayTradeQueryModel()
    model.out_trade_no = out_trade_no
    request.biz_model = model
    try:
        result = _execute(runtime, request, "alipay_trade_query_response")
    except TradeOperationError as exc:
        if (getattr(exc, "sub_code", "") or "").endswith("TRADE_NOT_EXIST"):
            return {"state": "NOT_EXIST", "total_amount": None,
                    "trade_no": None, "trade_status": None}
        raise
    state = _TRADE_STATE_MAP.get(result.get("trade_status"), "UNKNOWN")
    return {"state": state,
            "total_amount": result.get("total_amount"),
            "trade_no": result.get("trade_no"),
            "trade_status": result.get("trade_status")}


def close_trade(runtime, out_trade_no):
    """``alipay.trade.close``。成功返回 True；``ACQ.TRADE_NOT_EXIST``（交易
    已不存在/未提交）视为已关，返回 True；其余失败抛 TradeOperationError。"""
    from alipay.aop.api.domain.AlipayTradeCloseModel import AlipayTradeCloseModel
    from alipay.aop.api.request.AlipayTradeCloseRequest import \
        AlipayTradeCloseRequest

    request = AlipayTradeCloseRequest()
    model = AlipayTradeCloseModel()
    model.out_trade_no = out_trade_no
    request.biz_model = model
    try:
        _execute(runtime, request, "alipay_trade_close_response")
    except TradeOperationError as exc:
        if (getattr(exc, "sub_code", "") or "").endswith("TRADE_NOT_EXIST"):
            return True
        raise
    return True


def refund_trade(runtime, order, refund_amount_fen, out_request_no,
                 refund_reason=None):
    """``alipay.trade.refund``。返回 ``{"fund_change": "Y"/"N"/None,
    "refund_fee": str|None}``。

    契约：``code=10000`` 只代表受理成功；``fund_change=Y`` 才通常表示资金变化，
    ``N``/缺失必须用**同一** ``out_request_no`` 走退款查询确认（调用方落库）。
    """
    from alipay.aop.api.domain.AlipayTradeRefundModel import AlipayTradeRefundModel
    from alipay.aop.api.request.AlipayTradeRefundRequest import \
        AlipayTradeRefundRequest

    request = AlipayTradeRefundRequest()
    model = AlipayTradeRefundModel()
    model.out_trade_no = order["out_trade_no"]
    model.refund_amount = fen_to_yuan(refund_amount_fen)
    model.out_request_no = out_request_no
    if refund_reason:
        model.refund_reason = refund_reason
    request.biz_model = model
    result = _execute(runtime, request, "alipay_trade_refund_response")
    return {"fund_change": result.get("fund_change"),
            "refund_fee": result.get("refund_fee"),
            "trade_no": result.get("trade_no")}


def query_refund(runtime, order, out_request_no):
    """``alipay.trade.fastpay.refund.query`` → ``{"refund_status", "refund_amount",
    "trade_no"}``。只有 ``REFUND_SUCCESS`` 表示退款成功。"""
    from alipay.aop.api.domain.AlipayTradeFastpayRefundQueryModel import \
        AlipayTradeFastpayRefundQueryModel
    from alipay.aop.api.request.AlipayTradeFastpayRefundQueryRequest import \
        AlipayTradeFastpayRefundQueryRequest

    request = AlipayTradeFastpayRefundQueryRequest()
    model = AlipayTradeFastpayRefundQueryModel()
    model.out_trade_no = order["out_trade_no"]
    model.out_request_no = out_request_no
    request.biz_model = model
    result = _execute(runtime, request,
                      "alipay_trade_fastpay_refund_query_response")
    return {"refund_status": result.get("refund_status"),
            "refund_amount": result.get("refund_amount"),
            "trade_no": result.get("trade_no")}


# --------------------------------------------------------------------------- #
# 订单仓储（PostgreSQL 权威；金额整数分；状态机见模块 docstring）
# --------------------------------------------------------------------------- #
def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _new_out_trade_no():
    return "sp" + secrets.token_hex(12)


def _new_out_request_no():
    return "rf" + secrets.token_hex(12)


def create_order(runtime, amount_fen, subject=ORDER_SUBJECT,
                 idempotency_key=None):
    """建单（status=created；out_trade_no 服务端生成，冲突重试）。

    ``idempotency_key``：支付页表单每次渲染生成的随机 key（R1 评审 add-on）。
    带 key 时 ``INSERT … ON CONFLICT (idempotency_key) DO NOTHING``——真双击
    的两次并发 POST 只会建一单；key 冲突（他人已用此 key 建单）返回 None，
    由调用方按 key 重查并校验金额/剩余窗口。
    """
    conn = _connect()
    try:
        for _attempt in range(5):
            out_trade_no = _new_out_trade_no()
            try:
                with pg_store.transaction(conn) as c:
                    with c.cursor() as cur:
                        cur.execute(
                            "INSERT INTO support_orders (out_trade_no,"
                            " status, amount_fen, currency, subject, mode,"
                            " app_id, seller_pid, idempotency_key)"
                            " VALUES (%s,'created',%s,'CNY',%s,%s,%s,%s,%s)"
                            " ON CONFLICT (idempotency_key) DO NOTHING"
                            " RETURNING *",
                            (out_trade_no, int(amount_fen), subject,
                             runtime.mode, runtime.app_id, runtime.seller_pid,
                             idempotency_key))
                        return cur.fetchone()
            except psycopg.errors.UniqueViolation:
                # 订单号碰撞（概率≈0）：回滚本事务后换号重试
                conn.rollback()
        raise RuntimeError("订单号生成冲突次数过多")
    finally:
        conn.close()


def latest_session_notice_order(session_refs):
    """会话内值得在 /support 提醒的订单（R2 视觉 B1）。

    优先级：未决（created/paying，「待确认」）> 24h 内 paid（「查看致谢凭条」）。
    只在调用方给的会话 refs 里找（无泄露）。返回 (kind, order|None)，
    kind ∈ {"pending", "paid", None}。
    """
    if not session_refs:
        return None, None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_orders WHERE out_trade_no = ANY(%s)"
                " AND (status IN ('created','paying')"
                "      OR (status='paid' AND paid_at > now() - interval"
                "          '24 hours'))"
                " ORDER BY (CASE WHEN status IN ('created','paying')"
                "           THEN 0 ELSE 1 END), created_at DESC"
                " LIMIT 1",
                (list(session_refs),))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None, None
    return ("pending" if row["status"] in ("created", "paying")
            else "paid"), row


#: /support 猫卡礼物（喂猫互动）最多展示的已支付订单数（有界，防舞台堆积）。
SESSION_GIFT_ORDERS_MAX = 3


def session_paid_gift_orders(session_refs, limit=SESSION_GIFT_ORDERS_MAX):
    """当前会话已支付订单（最新在前，至多 ``limit`` 单）——猫卡礼物来源。

    只在调用方给的会话 refs 里找（无泄露）；未决/已关闭订单一律不算——
    礼物只能来自服务端核实过的付款。按 paid_at 降序（补记/恢复的旧单也以
    入账时间为准），NULL（防御）排最后再按 created_at 兜底。
    """
    if not session_refs:
        return []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_orders WHERE out_trade_no = ANY(%s)"
                " AND status='paid'"
                " ORDER BY paid_at DESC NULLS LAST, created_at DESC"
                " LIMIT %s",
                (list(session_refs), int(limit)))
            return cur.fetchall()
    finally:
        conn.close()


def order_query_throttled(order, seconds=3):
    """距上次服务端查单不足 ``seconds`` 秒 → True（状态轮询节流，R2 B2）。"""
    last = order.get("last_query_at") if order else None
    if not last:
        return False
    last = last if last.tzinfo \
        else last.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.now(datetime.timezone.utc) < (
        last + datetime.timedelta(seconds=seconds))


def status_confirmation(order, now=None):
    """订单行的查询置信度（2026-10-23 评审 P2）→ ``(confirmed, expired)``。

    纯函数（不含运行时归属——调用方已确认可查询时才采信其结果）：

    - ``confirmed`` 仅当订单仍 created/paying，且 stored **最近一次成功**
      查单（``record_trade_query`` 只在查询成功后写 ``last_query_at`` /
      ``last_query_result``，格式 ``"<via>:<STATE>"``）距 ``now`` 不超过
      ``STATUS_QUERY_WINDOW_SECONDS``（``abs``，含未来偏移防御）且 STATE 为
      PENDING/NOT_EXIST。UNKNOWN、金额不符的 PAID（无 transition）、查询
      失败或不可能查单一律不确认——本地 created/paying 绝不因此算「未付」。
    - ``expired`` 仅当 confirmed 且确认查单发生在
      ``created_at + ORDER_TIMEOUT + STATUS_EXPIRED_GRACE_SECONDS`` 之后
      （含相等）：time_expire 截止线 + 宽限，只看服务端/DB 时钟。
    - naive datetime 按 UTC 解释（同 ``order_query_throttled``）；行缺失、
      ``last_query_result`` 缺失/空/非 ``"<via>:<STATE>"`` 形态 →
      ``(False, False)``。
    """
    if not order or order.get("status") not in ("created", "paying"):
        return (False, False)
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif not getattr(now, "tzinfo", None):
        now = now.replace(tzinfo=datetime.timezone.utc)
    last = order.get("last_query_at")
    if not last:
        return (False, False)
    if not getattr(last, "tzinfo", None):
        last = last.replace(tzinfo=datetime.timezone.utc)
    if abs(now - last) > datetime.timedelta(
            seconds=STATUS_QUERY_WINDOW_SECONDS):
        return (False, False)
    result = (order.get("last_query_result") or "").strip()
    if ":" not in result:
        return (False, False)  # 非写入方形态（缺 "<via>:" 前缀）→ 不采信
    state = result.rsplit(":", 1)[-1].strip()
    if state not in ("PENDING", "NOT_EXIST"):
        return (False, False)
    created = order.get("created_at")
    if not getattr(created, "tzinfo", None):
        created = created.replace(tzinfo=datetime.timezone.utc)
    expired = last >= created + ORDER_TIMEOUT + datetime.timedelta(
        seconds=STATUS_EXPIRED_GRACE_SECONDS)
    return (True, expired)


def get_order_by_idempotency_key(idempotency_key):
    """按幂等键取订单（无则 None；key 形态由调用方先行校验）。

    R2-1：key 与原订单永久绑定——调用方绝不清除/改配 key，同 key 一律返回
    原订单（状态路由交给支付页）。R4 add-on：先查建单期列（原表
    idempotency_key），再查后绑定别名（support_order_key_aliases——下单
    会话复用命中的 key），同一 return 形态（完整订单行 dict）。
    """
    if not idempotency_key:
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM support_orders"
                        " WHERE idempotency_key=%s", (idempotency_key,))
            row = cur.fetchone()
            if row is not None:
                return row
            cur.execute(
                "SELECT o.* FROM support_order_key_aliases a"
                " JOIN support_orders o ON o.out_trade_no = a.out_trade_no"
                " WHERE a.idempotency_key=%s", (idempotency_key,))
            return cur.fetchone()
    finally:
        conn.close()


def bind_order_idempotency_key(idempotency_key, out_trade_no):
    """后绑定幂等键 → 订单（单事务；R3 评审 [P2] 修复）。返回绑定是否成立。

    下单会话复用命中时**必须先落别名再返回**：否则同 key 重放/并发请求查
    不到键，会话订单支付后重放会另建第二个可支付订单。

    - key 已被**其他**订单的建单期列持有 → False（绝不改指，防串单）；
    - key 已被别名持有且指向其他订单（ON CONFLICT DO NOTHING 吸收后重查
      确认）→ False；
    - 列/别名已指向本订单（重复绑定/重放）→ True；
    - 新插入别名指向本订单 → True。

    调用方必须在 ``idempotency_key_lock("order", ...)`` 内调用（与并发同
    key 请求串行；绑定本身也有唯一键兜底，锁是防「绑不上又建新单」的第二
    道闸）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT out_trade_no FROM support_orders"
                    " WHERE idempotency_key=%s", (idempotency_key,))
                holder = cur.fetchone()
                if holder is not None:
                    return holder["out_trade_no"] == out_trade_no
                cur.execute(
                    "INSERT INTO support_order_key_aliases"
                    " (idempotency_key, out_trade_no) VALUES (%s,%s)"
                    " ON CONFLICT (idempotency_key) DO NOTHING",
                    (idempotency_key, out_trade_no))
                if cur.rowcount == 1:
                    return True
                cur.execute(
                    "SELECT out_trade_no FROM support_order_key_aliases"
                    " WHERE idempotency_key=%s", (idempotency_key,))
                return cur.fetchone()["out_trade_no"] == out_trade_no
    finally:
        conn.close()


def get_order(out_trade_no):
    if not out_trade_no or not _OUT_TRADE_NO_RE.fullmatch(out_trade_no):
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM support_orders WHERE out_trade_no=%s",
                        (out_trade_no,))
            return cur.fetchone()
    finally:
        conn.close()


def order_matches_runtime(order, runtime):
    """订单是否属于当前运行时配置快照（mode/app_id/seller_pid 全等）。

    2026-10-21 评审（订单/配置归属）：建表单、查单、关单、sweep、退款前必须
    校验——不匹配的订单对当前配置是「他方订单」，一律不触碰支付宝侧。
    """
    return (order["mode"] == runtime.mode
            and order["app_id"] == runtime.app_id
            and order["seller_pid"] == runtime.seller_pid)


#: 下单复用的最小剩余支付窗口（秒）：只有距 time_expire 还剩这么多时间的
#: 未决订单才允许复用（R1 评审 add-on）——否则新建，绝不让人落在即将过期的
#: 收银台上。同用于幂等 key 路径与会话路径。
ORDER_REUSE_MIN_REMAINING_SECONDS = 300


def find_reusable_open_order(session_refs, amount_fen, runtime,
                             exclude_ref=None):
    """下单幂等复用（R1 评审：防重复提交产生多个可支付订单）。

    只在**当前会话**关联的订单号里找：同金额、同运行时快照
    （mode/app_id/seller_pid）、状态 created/paying、且距 time_expire 至少还
    剩 ``ORDER_REUSE_MIN_REMAINING_SECONDS``。命中返回最新一单，否则 None。
    过线（或即将过线）的订单不复用——必须产生新单。

    ``exclude_ref``（R5 评审 add-on，可空）：排除指定订单号（regenerate 的
    「已被替换」防护在处理会话其余同金额未决单时必须跳过旧单本身，否则
    旧单只要还有 ≥5min 剩余就会命中自己）。其余语义不变。
    """
    if not session_refs:
        return None
    cutoff_minutes = max(
        1, int((ORDER_TIMEOUT.total_seconds()
                - ORDER_REUSE_MIN_REMAINING_SECONDS) // 60))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_orders WHERE out_trade_no = ANY(%s)"
                " AND status IN ('created','paying') AND amount_fen=%s"
                " AND mode=%s AND app_id=%s AND seller_pid=%s"
                " AND created_at > now() - (%s || ' minutes')::interval"
                " AND (%s::text IS NULL OR out_trade_no <> %s::text)"
                " ORDER BY created_at DESC LIMIT 1",
                (list(session_refs), int(amount_fen), runtime.mode,
                 runtime.app_id, runtime.seller_pid, cutoff_minutes,
                 exclude_ref, exclude_ref))
            return cur.fetchone()
    finally:
        conn.close()


def list_session_payable_open_orders(session_refs, amount_fen, runtime):
    """会话内同金额、同运行时快照、**仍在支付宝可支付窗口内**的未决订单
    （status created/paying，最新在前）。

    R5 评审（2026-10-23）add-on：会话复用未命中（没有剩余 ≥
    ``ORDER_REUSE_MIN_REMAINING_SECONDS`` 的可复用单）时，凡还在
    ``created_at + ORDER_TIMEOUT + STATUS_EXPIRED_GRACE_SECONDS``（DB
    ``now()`` 口径；与状态端点判「已过期」、regenerate 的 NOT_EXIST 截止线
    同一条，早于 sweep 的关单线 ``+ SWEEP_CLOSE_MARGIN_SECONDS``）之前的同
    金额未决单在支付宝侧
    **仍可支付**——未扫码的二维码在截止前关不掉（NOT_EXIST 不代表已关），
    调用方必须先逐张核实/关单，绝不在旧单仍可支付时直接建新单，否则同一
    会话会留下两张可支付订单。越过窗口线的订单不返回（不可支付、不阻塞
    建新单，留给补偿 sweep 清场）。
    """
    if not session_refs:
        return []
    window_seconds = int(ORDER_TIMEOUT.total_seconds()
                         + STATUS_EXPIRED_GRACE_SECONDS)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_orders WHERE out_trade_no = ANY(%s)"
                " AND status IN ('created','paying') AND amount_fen=%s"
                " AND mode=%s AND app_id=%s AND seller_pid=%s"
                " AND created_at > now() - (%s || ' seconds')::interval"
                " ORDER BY created_at DESC",
                (list(session_refs), int(amount_fen), runtime.mode,
                 runtime.app_id, runtime.seller_pid, window_seconds))
            return cur.fetchall()
    finally:
        conn.close()


def find_session_paid_successor(session_refs, order, runtime):
    """旧单的「会话内已支付后继单」（R6 评审：过期视图 regenerate 不再建新单）。

    只在**当前会话**关联的订单号里找：状态 paid、与 ``order`` 同金额、同运行
    时快照（mode/app_id/seller_pid）、``created_at`` **严格晚于** ``order``
    且订单号不同（排除旧单本身；closed→paid 恢复不改 created_at）→ 返回最新
    一单（``paid_at DESC NULLS LAST, created_at DESC``，同
    ``session_paid_gift_orders`` 的排序），否则 None（refs 空同样 None）。

    语义：规则 A 绝不在旧单仍可支付时创建第二张同金额可支付订单——会话内
    比旧单**更晚**创建的同金额订单只可能在旧单停止可支付之后出现，它就是
    旧单的后继（「A 过期 → 重建 B」的 B）；后继已 paid，说明旧单的支付意图
    已由后继完成（另一标签页付款并本地落库），调用方绝不能再动旧单或建新单。
    比旧单更早创建的同金额 paid 订单（更早一次独立赞助）不是后继，绝不计入。
    """
    if not session_refs:
        return None
    created = order["created_at"]
    if not getattr(created, "tzinfo", None):
        created = created.replace(tzinfo=datetime.timezone.utc)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_orders WHERE out_trade_no = ANY(%s)"
                " AND status='paid' AND amount_fen=%s"
                " AND mode=%s AND app_id=%s AND seller_pid=%s"
                " AND created_at > %s AND out_trade_no <> %s"
                " ORDER BY paid_at DESC NULLS LAST, created_at DESC"
                " LIMIT 1",
                (list(session_refs), int(order["amount_fen"]), runtime.mode,
                 runtime.app_id, runtime.seller_pid, created,
                 order["out_trade_no"]))
            return cur.fetchone()
    finally:
        conn.close()


def mark_paying(out_trade_no):
    """created → paying（打开收银台页面时；幂等）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE support_orders SET status='paying', updated_at=now()"
                    " WHERE out_trade_no=%s AND status='created'",
                    (out_trade_no,))
                return cur.rowcount > 0
    finally:
        conn.close()


#: paid-after-close 告警 logger（错误级；消息只含订单号，绝无参数/密钥）。
_LOG = logging.getLogger("support_payments")


def _alert_paid_after_close(out_trade_no):
    """错误级告警：本地已关单后收到实付凭据（closed → paid 恢复）。

    只记订单号与固定文案——绝不输出任何通知参数、查询响应或密钥材料。
    """
    try:
        _LOG.error(
            "support-pay ALERT paid after local close: order recovered to"
            " paid, out_trade_no=%s", out_trade_no)
    except Exception:
        pass


def _alert_sweep_config_mismatch(out_trade_no):
    """sweep 跳过配置不匹配订单的留痕（只含订单号；无参数/密钥）。

    无条件走模块 logger（不依赖调用方传入 logger）——归属不匹配本身是
    需要运维知晓的异常（订单可能落在另一套配置下无人处理）。
    """
    try:
        _LOG.warning(
            "support-pay sweep skip config mismatch: order belongs to a"
            " different runtime config, out_trade_no=%s", out_trade_no)
    except Exception:
        pass


def _get_order_for_update(cur, out_trade_no):
    """行锁读订单（``SELECT … FOR UPDATE``；形态非法 → None）。"""
    if not out_trade_no or not _OUT_TRADE_NO_RE.fullmatch(out_trade_no):
        return None
    cur.execute("SELECT * FROM support_orders WHERE out_trade_no=%s"
                " FOR UPDATE", (out_trade_no,))
    return cur.fetchone()


def _apply_paid_transition(cur, out_trade_no, trade_no):
    """行锁内推进到 paid（调用方事务内执行；由调用方提交/回滚）。

    - created / paying → paid：正常支付路径；
    - closed → paid：本地曾关单（sweep 超时清场）而支付宝侧实付——**必须**
      恢复（真实收款不可丢）；调用方提交后记 ``paid after local close`` 告警；
    - 已是 paid → 幂等无变更（paid 是吸收态：**绝不**从 paid 变为任何其他
      状态，TRADE_CLOSED/退款/关单一律不动已支付行）；
    - 订单不存在 → (None, False)。

    返回 ``(from_status, from_closed)``；``from_closed=True`` 表示发生了
    closed → paid 恢复。首次 trade_no/paid_at 保留不覆盖（COALESCE）。
    """
    row = _get_order_for_update(cur, out_trade_no)
    if row is None:
        return None, False
    status = row["status"]
    if status == "paid":
        return "paid", False
    cur.execute(
        "UPDATE support_orders SET status='paid',"
        " trade_no=COALESCE(trade_no,%s),"
        " paid_at=COALESCE(paid_at, now()), updated_at=now()"
        " WHERE out_trade_no=%s AND status=%s",
        (trade_no, out_trade_no, status))
    if cur.rowcount != 1:  # 行锁下不可能并发改状态；防御性失败即回滚
        raise RuntimeError("订单状态推进失败（paid）：%s" % out_trade_no)
    return status, status == "closed"


def _apply_closed_transition(cur, out_trade_no, reason):
    """行锁内 created/paying → closed（paid 绝不降级；幂等）。返回是否变更。"""
    cur.execute(
        "UPDATE support_orders SET status='closed',"
        " closed_at=COALESCE(closed_at, now()), closed_reason=%s,"
        " updated_at=now()"
        " WHERE out_trade_no=%s AND status IN ('created','paying')",
        (reason, out_trade_no))
    return cur.rowcount > 0


def mark_paid_idempotent(out_trade_no, trade_no):
    """created/paying/closed → paid（吸收态；幂等）。返回是否有状态变更。

    closed → paid 属于补偿恢复（本地超时清场后支付宝侧实付）：同样允许，
    并记 ``paid after local close`` 错误级告警（只含订单号）。已 paid → False
    且绝不改动。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                from_status, from_closed = _apply_paid_transition(
                    cur, out_trade_no, trade_no)
        if from_closed:
            _alert_paid_after_close(out_trade_no)
        return from_status not in (None, "paid")
    finally:
        conn.close()


def mark_closed_idempotent(out_trade_no, reason):
    """created/paying → closed（paid 绝不降级；幂等）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _apply_closed_transition(cur, out_trade_no, reason)
    finally:
        conn.close()


def record_trade_query(out_trade_no, result_text):
    """补偿查单留痕（sweep / 回跳页查询）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE support_orders SET last_query_at=now(),"
                    " last_query_result=%s, updated_at=now()"
                    " WHERE out_trade_no=%s", (result_text, out_trade_no))
    finally:
        conn.close()


class _NotifyRejected(Exception):
    """通知业务校验未通过（整体回滚事件与状态推进，应答 failure）。"""


def _notify_event_key(params):
    """事件幂等键：notify_id 优先；缺失时用剔除 sign/sign_type 后的规范化
    全参 SHA-256（同一通知重推产生同一键）。"""
    notify_id = (params.get("notify_id") or "").strip()
    if notify_id:
        return "nid:" + notify_id
    canonical = "\n".join(
        "%s=%s" % (k, params[k]) for k in sorted(params)
        if k not in _NOTIFY_PARAM_SKIP)
    return "sha:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert_notify_event(cur, params, order_out_trade_no):
    """事务内持久化通知事件（与状态推进同事务提交/回滚）。

    返回 True=首次插入；False=重复通知（event_key 已存在）。
    """
    notify_id = (params.get("notify_id") or "").strip()
    safe_params = {k: v for k, v in params.items() if k != "sign"}
    cur.execute(
        "INSERT INTO support_notify_events (event_key, notify_id,"
        " out_trade_no, trade_no, trade_status, raw_params)"
        " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (event_key) DO NOTHING",
        (_notify_event_key(params), notify_id or None, order_out_trade_no,
         params.get("trade_no"), params.get("trade_status"),
         json.dumps(safe_params, ensure_ascii=False)))
    return cur.rowcount > 0


def sum_refunded_fen(out_trade_no, exclude_out_request_no=None):
    """累计已受理退款（requested/refunded/unknown 计入，failed 不计）。

    unknown 保守计入——退款查询确认前不能按「没退」放行新的超额退款。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(refund_amount_fen),0) AS s FROM"
                " support_refunds WHERE out_trade_no=%s"
                " AND status IN ('requested','refunded','unknown')"
                " AND (%s::text IS NULL OR out_request_no <> %s::text)",
                (out_trade_no, exclude_out_request_no,
                 exclude_out_request_no))
            row = cur.fetchone()
            return int(row["s"])
    finally:
        conn.close()


def create_refund_row(out_trade_no, refund_amount_fen, requested_by,
                      refund_reason=None, idempotency_key=None):
    """新退款行（out_request_no 服务端生成；主键冲突换号重试）。

    ``idempotency_key``（R2-2，可空）：退款操作幂等键。带 key 时
    ``ON CONFLICT (idempotency_key) DO NOTHING``——并发同 key 只建一行；
    key 冲突返回 None，由调用方按 key 重查并返回原行（绝不新建/新受理）。
    """
    conn = _connect()
    try:
        for _attempt in range(5):
            out_request_no = _new_out_request_no()
            try:
                with pg_store.transaction(conn) as c:
                    with c.cursor() as cur:
                        cur.execute(
                            "INSERT INTO support_refunds (out_request_no,"
                            " out_trade_no, refund_amount_fen, status,"
                            " refund_reason, requested_by, idempotency_key)"
                            " VALUES (%s,%s,%s,'requested',%s,%s,%s)"
                            " ON CONFLICT (idempotency_key) DO NOTHING"
                            " RETURNING *",
                            (out_request_no, out_trade_no,
                             int(refund_amount_fen), refund_reason,
                             requested_by, idempotency_key))
                        return cur.fetchone()
            except psycopg.errors.UniqueViolation:
                # out_trade_no 主键/唯一键之外的碰撞（概率≈0）：换号重试；
                # idempotency_key 冲突不会走到这里（DO NOTHING 吸收）
                conn.rollback()
        raise RuntimeError("退款请求号生成冲突次数过多")
    finally:
        conn.close()


def get_refund(out_request_no):
    if not out_request_no or not _OUT_REQUEST_NO_RE.fullmatch(out_request_no):
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM support_refunds WHERE out_request_no=%s",
                        (out_request_no,))
            return cur.fetchone()
    finally:
        conn.close()


def list_pending_refunds(out_trade_no):
    """未决退款行（status IN ('requested','unknown')，按创建先后）。

    R2-3：``requested`` 与 ``unknown`` 同样对待——都是「受理结果未知/未完结」
    （资金占用中，计入 sum_refunded_fen）：新的退款请求必须先解决它们——
    同一 ``out_request_no`` 退款查询确认；仍未决则同号同额重试受理。决不在
    存在未决行时生成新的退款请求号。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_refunds WHERE out_trade_no=%s"
                " AND status IN ('requested','unknown')"
                " ORDER BY created_at, out_request_no",
                (out_trade_no,))
            return cur.fetchall()
    finally:
        conn.close()


def get_refund_by_idempotency_key(idempotency_key):
    """按退款操作幂等键取退款行（R2-2；无则 None）。

    R4 add-on：先查建行期列（原表 idempotency_key），再查后绑定别名
    （support_refund_key_aliases——未决解析命中的 key）；同一 return 形态
    （完整退款行 dict）。key 与原退款行永久绑定：任何状态都返回原行。
    """
    if not idempotency_key:
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM support_refunds"
                        " WHERE idempotency_key=%s", (idempotency_key,))
            row = cur.fetchone()
            if row is not None:
                return row
            cur.execute(
                "SELECT r.* FROM support_refund_key_aliases a"
                " JOIN support_refunds r ON r.out_request_no = a.out_request_no"
                " WHERE a.idempotency_key=%s", (idempotency_key,))
            return cur.fetchone()
    finally:
        conn.close()


#: 幂等键 advisory lock 命名空间类（两 int 形态 ``pg_advisory_lock(class_id,
#: hashtext(key))``；类号只要求互异，取 0087 表号派生便于溯源）。两 int 形态
#: 与单 bigint 形态（``order_refund_lock`` 的 hashtext 订单锁）处于**不同的
#: 键空间**，永不互相碰撞。
IDEMPOTENCY_LOCK_CLASS_ORDER = 8701
IDEMPOTENCY_LOCK_CLASS_REFUND = 8702


@contextmanager
def idempotency_key_lock(namespace, key):
    """幂等键串行锁（session 级 advisory lock，跨进程互斥；R4 add-on）。

    「按 key 查找 → 复用/解析命中 → 键绑定落库 → 返回」必须整段串行：并发
    同 key 请求在锁上排队，后进者按 key 重查命中列/别名并返回同一行，绝不
    产生第二个可支付订单/第二笔退款。键绑定函数自身有唯一键兜底，本锁防的
    是「绑不上又另建」的窗口。

    - ``namespace`` ∈ {"order", "refund"}（未知命名空间 fail-fast，绝不静默
      互串两套键空间）；
    - key 为空（falsy）→ no-op（键锁只保护键语义；无 key 路径零额外连接）；
    - 锁在**专用连接**上取（session 级，随连接保持），finally 必释放——
      模式同 ``order_refund_lock``；锁序约定恒为 订单锁 → 键锁，无反向路径。
    """
    if namespace == "order":
        lock_class = IDEMPOTENCY_LOCK_CLASS_ORDER
    elif namespace == "refund":
        lock_class = IDEMPOTENCY_LOCK_CLASS_REFUND
    else:
        raise ValueError("未知幂等键锁命名空间：%r" % (namespace,))
    if not key:
        yield
        return
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s, hashtext(%s))",
                        (lock_class, key))
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))",
                            (lock_class, key))
    finally:
        conn.close()


def mark_refunded_and_bind_key(out_request_no, idempotency_key=None,
                               fund_change=None, trade_no=None):
    """退款行置 refunded + 后绑定幂等键（**同一事务**；R3 评审 [P1] 修复）。

    未决退款解析确认退款成功时，状态推进与新请求 key 的别名落库必须原子
    提交：进程在返回后的任何一步（审计写、响应构造）死掉，同 key 重放都按
    key 命中本行（任何状态），绝不产生第二个 out_request_no 重复退款。

    - 状态字段语义同 ``update_refund_status(..., "refunded", ...)``（COALESCE
      保留首次 fund_change/trade_no）——「已退款」是来自支付宝的事实，
      **无条件提交**，与键冲突无关；
    - key 为空：只置状态，返回 True（无键可绑）；
    - key 已被**其他**退款行（建行期列或别名）持有：状态照常提交、别名绝不
      改指（防串行），返回 False——调用方以 409 refund_key_conflict 拒绝
      本次请求（退款事实成立与请求键冲突是两件事）。

    返回 True = key 已绑定到本行（含无 key）。调用方须在订单退款锁与
    ``idempotency_key_lock("refund", ...)`` 内调用。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE support_refunds SET status='refunded',"
                    " fund_change=COALESCE(%s, fund_change),"
                    " trade_no=COALESCE(%s, trade_no), updated_at=now()"
                    " WHERE out_request_no=%s",
                    (fund_change, trade_no, out_request_no))
                if not idempotency_key:
                    return True
                cur.execute(
                    "SELECT out_request_no FROM support_refunds"
                    " WHERE idempotency_key=%s", (idempotency_key,))
                holder = cur.fetchone()
                if holder is not None:
                    return holder["out_request_no"] == out_request_no
                cur.execute(
                    "INSERT INTO support_refund_key_aliases"
                    " (idempotency_key, out_request_no) VALUES (%s,%s)"
                    " ON CONFLICT (idempotency_key) DO NOTHING",
                    (idempotency_key, out_request_no))
                if cur.rowcount == 1:
                    return True
                cur.execute(
                    "SELECT out_request_no FROM support_refund_key_aliases"
                    " WHERE idempotency_key=%s", (idempotency_key,))
                return cur.fetchone()["out_request_no"] == out_request_no
    finally:
        conn.close()


@contextmanager
def order_refund_lock(out_trade_no):
    """每订单退款串行锁（session 级 advisory lock，跨进程互斥）。

    2026-10-21 R1 评审 (b)：未决退款解析 + 累计额度校验 + 退款行创建（额度
    预留）必须串行——两个并发退款 POST 不能同时通过额度检查。支付宝调用
    也在锁内（退款是低频 owner 操作，正确性优先；锁随连接会话保持，
    finally 必释放）。锁键 32 位 hash，理论碰撞只会让不同订单互相等一下，
    不影响正确性。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)",
                        ("support-refund:" + out_trade_no,))
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                    ("support-refund:" + out_trade_no,))
    finally:
        conn.close()


def update_refund_status(out_request_no, status, fund_change=None, trade_no=None):
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE support_refunds SET status=%s,"
                    " fund_change=COALESCE(%s, fund_change),"
                    " trade_no=COALESCE(%s, trade_no), updated_at=now()"
                    " WHERE out_request_no=%s",
                    (status, fund_change, trade_no, out_request_no))
                return cur.rowcount > 0
    finally:
        conn.close()


def reserve_failed_refund_for_retry(out_request_no):
    """failed 行的显式重试：先把行推回 requested（额度预留）并提交（CAS）。

    2026-10-23 评审：``failed`` 行不计入 ``sum_refunded_fen`` /
    ``list_pending_refunds``——若行仍是 ``failed`` 就调支付宝，受理被接受后
    进程在 ``update_refund_status`` 前死掉，行停留在 failed（账本显示 0 预留），
    后续新退款会拿到**新** out_request_no → 同一笔钱退两次。因此重试必须在
    触碰支付宝**之前**把行置回 ``requested`` 并 COMMIT：之后任何时点崩溃，
    行都是未决退款（额度占用、必须先同号解决），不存在「账本已释放 + 资金
    已付出」的窗口。

    条件更新（compare-and-set）只匹配 ``status='failed'`` 的行——预算检查后
    状态已被并发路径改变时返回 False，调用方必须放弃本次重试（不发起任何
    支付宝调用），绝不基于过期状态受理。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE support_refunds SET status='requested',"
                    " updated_at=now()"
                    " WHERE out_request_no=%s AND status='failed'",
                    (out_request_no,))
                return cur.rowcount == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 异步通知处理（纯文本 success/failure 契约）
# --------------------------------------------------------------------------- #
def _is_paid_trade_notification(params):
    """只有付款成功状态、且无退款/关单/分账事件特征，才认定买家付款成功。"""
    if params.get("trade_status") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return False
    if params.get("out_biz_no") or params.get("gmt_refund") \
            or params.get("refund_fee"):
        return False
    return True


def amount_fen_from_text(value):
    """通知/查单金额字符串 → 分（规范化比对，禁浮点）；形态非法返回 None。"""
    text = str(value or "").strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
        return None
    try:
        return int((Decimal(text) * 100).to_integral_exact())
    except (InvalidOperation, ValueError):
        return None


def sanitize_notify_params(params):
    """日志/落库用的脱敏参数副本（剥 sign——验签物料无落库价值）。"""
    return {k: v for k, v in (params or {}).items() if k != "sign"}


def process_notify(params, runtime, logger=None):
    """通知主链路。返回应答纯文本（``success``/``failure``）。

    链路（notify-url 契约）：表单参数 → SDK 验签 → 行锁取订单 + 业务字段校验
    （app_id/out_trade_no/total_amount/seller_id 对照运行时配置快照与订单行）→
    **单事务**：事件幂等插入 + 条件状态推进一起提交；重复通知（事件键已存在）
    → ``success`` 且不再改状态；verified 实付通知对本地已关单的订单做
    closed → paid 恢复并记错误级告警（``paid after local close``）；
    ``TRADE_CLOSED`` 只在未支付态关单——**绝不**把已支付行降级。
    任何异常（含状态推进失败）→ 整体回滚（事件不留半截记录）并应答
    ``failure``，支付宝按非 success 重试；重放时事件与状态推进一起重来。
    """
    try:
        params = {k: v for k, v in dict(params).items()}
        if not verify_notify_signature(params, runtime):
            _log(logger, "alipay notify signature check failed", params)
            return "failure"
        app_id = params.get("app_id") or ""
        seller_id = params.get("seller_id") or ""
        out_trade_no = params.get("out_trade_no") or ""
        duplicate = False
        paid_from_closed = False
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    order = _get_order_for_update(cur, out_trade_no)
                    if order is None or order["app_id"] != app_id \
                            or order["seller_pid"] != seller_id \
                            or order["mode"] != runtime.mode \
                            or amount_fen_from_text(params.get("total_amount")) \
                            != int(order["amount_fen"]):
                        # 不区分具体失配原因（不向通知方泄露内部校验维度）
                        raise _NotifyRejected()
                    if not _insert_notify_event(cur, params, out_trade_no):
                        duplicate = True  # 重复通知：不再改状态，直接确认
                    else:
                        status = params.get("trade_status") or ""
                        if _is_paid_trade_notification(params):
                            _from, paid_from_closed = _apply_paid_transition(
                                cur, out_trade_no, params.get("trade_no"))
                        elif status == "TRADE_CLOSED":
                            # 关单事件：只在未支付态生效（paid 绝不降级）
                            _apply_closed_transition(cur, out_trade_no,
                                                     "notify:TRADE_CLOSED")
                        # WAIT_BUYER_PAY / 其他：只记录事件，等查单
        finally:
            conn.close()
        if duplicate:
            return "success"
        if paid_from_closed:
            _alert_paid_after_close(out_trade_no)  # 提交后告警（回滚不误报）
        _log(logger, "alipay notify accepted", params)
        return "success"
    except _NotifyRejected:
        _log(logger, "alipay notify business check failed", params)
        return "failure"
    except Exception:
        # 事件插入与状态推进同事务：任何异常整体回滚（事件不残留），应答
        # failure 让支付宝重试
        _log(logger, "alipay notify unexpected exception", params,
             include_params=False)
        return "failure"


def _log(logger, message, params, include_params=True):
    """通知侧脱敏日志（失败不能影响应答文本；绝不输出密钥/签名）。"""
    if logger is None:
        return
    try:
        if include_params:
            logger.warning("%s params=%s", message,
                           json.dumps(sanitize_notify_params(params),
                                      ensure_ascii=False, sort_keys=True))
        else:
            logger.warning(message)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 补偿查单 / 关单 sweep（通知不到位的兜底；幂等，可重复执行）
# --------------------------------------------------------------------------- #
def list_stale_open_orders(min_age_minutes=16, max_age_hours=24):
    """sweep 扫描集：超时未决订单（created/paying）+ 待恢复的本地关单
    （closed 且从未 paid，用于 closed → paid 补偿恢复）。

    已 paid 的订单绝不重扫（吸收态）；24h 窗口兜底，避免扫描集无限增长。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM support_orders WHERE ("
                " status IN ('created','paying')"
                " OR (status='closed' AND paid_at IS NULL)"
                ") AND created_at < now() - (%s || '"
                " minutes')::interval AND created_at > now() - (%s || '"
                " hours')::interval ORDER BY created_at",
                (int(min_age_minutes), int(max_age_hours)))
            return cur.fetchall()
    finally:
        conn.close()


def sweep(min_age_minutes=16, logger=None):
    """补偿 sweep：先查单，再按规则关单/补记。返回统计 dict（无敏感信息）。

    - 查得 PAID（金额精确匹配）→ 推进到 paid；本地已关单的实付订单同样恢复
      （closed → paid + ``paid after local close`` 告警）——通知缺失/超时误关
      的补偿路径，真实收款不可丢；
    - 查得 PENDING / NOT_EXIST → **只有**越过 time_expire 截止线
      （created_at + ORDER_TIMEOUT + SWEEP_CLOSE_MARGIN_SECONDS）才关单：
      PENDING 调 close API、NOT_EXIST 直接本地关单；未到线一律跳过——买家
      可能仍在支付，绝不提前关闭；
    - 查得 CLOSED → 本地关单（支付宝侧已关，安全）；
    - 本地已关单且查得非 PAID → 跳过（本地已终态，不重复调 close API）；
    - 订单的 mode/app_id/seller_pid 与当前运行时不匹配 → 跳过并计入
      ``config_mismatch``（不查单、不关单；只记订单号日志）；
    - UNKNOWN / 调用失败 → 跳过留给下轮（幂等，可重复执行）。
    """
    runtime = get_runtime(os.environ.get(
        "SUPPORT_ALIPAY_PAY_MODE", "sandbox").strip() or "sandbox")
    stats = {"scanned": 0, "paid": 0, "closed": 0, "skipped": 0,
             "config_mismatch": 0}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for order in list_stale_open_orders(min_age_minutes=min_age_minutes):
        out_trade_no = order["out_trade_no"]
        if not order_matches_runtime(order, runtime):
            # 订单/配置归属（2026-10-21 评审）：他方配置的订单不查单、不关单；
            # 统计 + 只带订单号的留痕日志，留给对应配置的 sweep 处理。
            stats["config_mismatch"] += 1
            _alert_sweep_config_mismatch(out_trade_no)
            continue
        stats["scanned"] += 1
        locally_closed = order["status"] == "closed"
        try:
            info = query_trade(runtime, out_trade_no)
        except TradeOperationError as exc:
            stats["skipped"] += 1
            _log(logger, "sweep query failed (%s)" % (
                getattr(exc, "sub_code", "") or "error"), None,
                include_params=False)
            continue
        record_trade_query(out_trade_no, "sweep:%s" % (info["state"],))
        if info["state"] == "PAID":
            # 金额必须精确匹配才认账（查询响应同样不可盲信）
            query_fen = amount_fen_from_text(info.get("total_amount"))
            if query_fen == int(order["amount_fen"]):
                if mark_paid_idempotent(out_trade_no, info.get("trade_no")):
                    stats["paid"] += 1
                continue
            _log(logger, "sweep paid amount mismatch", None,
                 include_params=False)
            stats["skipped"] += 1
        elif locally_closed:
            stats["skipped"] += 1  # 已终态且查得非实付：无动作
        elif info["state"] in ("PENDING", "NOT_EXIST"):
            if now_utc < _order_close_deadline(order["created_at"]):
                stats["skipped"] += 1  # 未到 time_expire+余量：不关单
                continue
            try:
                if info["state"] == "NOT_EXIST":
                    # 交易从未提交（页面打开过但未支付/已超时清理）：直接本地关单
                    closed = True
                else:
                    closed = close_trade(runtime, out_trade_no)
            except TradeOperationError as exc:
                stats["skipped"] += 1
                _log(logger, "sweep close failed (%s)" % (
                    getattr(exc, "sub_code", "") or "error"), None,
                    include_params=False)
                continue
            if closed and mark_closed_idempotent(out_trade_no,
                                                 "sweep:expired"):
                stats["closed"] += 1
        elif info["state"] == "CLOSED":
            if mark_closed_idempotent(out_trade_no, "sweep:alipay_closed"):
                stats["closed"] += 1
        else:
            stats["skipped"] += 1
    return stats


# --------------------------------------------------------------------------- #
# CLI（补偿/运维入口；生产调度见 deploy 文档，未接入任何生产部署）
# --------------------------------------------------------------------------- #
def _cli_logger():
    import logging
    return logging.getLogger("support_payments")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="支付宝赞助订单运维（check/sweep/query/close）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="校验当前模式支付配置（不打印任何密钥）")
    p_sweep = sub.add_parser("sweep", help="补偿查单 + 关单（幂等）")
    p_sweep.add_argument("--min-age-minutes", type=int, default=16)
    p_query = sub.add_parser("query", help="查询订单/交易状态")
    p_query.add_argument("--out-trade-no", required=True)
    p_close = sub.add_parser("close", help="关闭未支付交易")
    p_close.add_argument("--out-trade-no", required=True)
    p_close.add_argument("--reason", default="manual")
    args = parser.parse_args(argv)

    mode = (os.environ.get("SUPPORT_ALIPAY_PAY_MODE", "sandbox").strip()
            or "sandbox")
    if args.cmd == "check":
        runtime = load_runtime_config(mode)  # 失败非零退出
        print("config ok: %s" % json.dumps(runtime.describe(), ensure_ascii=False))
        return 0
    runtime = load_runtime_config(mode)
    if args.cmd == "sweep":
        print(json.dumps(sweep(min_age_minutes=args.min_age_minutes,
                               logger=_cli_logger()), ensure_ascii=False))
        return 0
    if args.cmd == "query":
        order = get_order(args.out_trade_no)
        if order is None:
            print("order not found", file=sys.stderr)
            return 1
        # R2-4 订单/配置归属：不匹配 → 非零退出，不调支付宝、不改任何状态
        if not order_matches_runtime(order, runtime):
            print("order belongs to a different runtime config; refused",
                  file=sys.stderr)
            return 1
        info = query_trade(runtime, args.out_trade_no)
        print(json.dumps({"local_status": order["status"],
                          "trade_state": info["state"],
                          "trade_status": info["trade_status"]},
                         ensure_ascii=False))
        record_trade_query(args.out_trade_no, "cli:%s" % info["state"])
        if info["state"] == "PAID":
            # R2-4：与 sweep/回跳页同口径——金额精确匹配才认账补记 paid
            if amount_fen_from_text(info.get("total_amount")) \
                    == int(order["amount_fen"]):
                mark_paid_idempotent(args.out_trade_no, info.get("trade_no"))
            else:
                print("paid amount mismatch; paid not recorded",
                      file=sys.stderr)
        elif info["state"] == "CLOSED":
            mark_closed_idempotent(args.out_trade_no, "cli:alipay_closed")
        return 0
    if args.cmd == "close":
        order = get_order(args.out_trade_no)
        if order is None:
            print("order not found", file=sys.stderr)
            return 1
        # R2-4 订单/配置归属：不匹配 → 非零退出，不调 close、不改状态
        if not order_matches_runtime(order, runtime):
            print("order belongs to a different runtime config; refused",
                  file=sys.stderr)
            return 1
        if close_trade(runtime, args.out_trade_no):
            changed = mark_closed_idempotent(args.out_trade_no,
                                             "cli:%s" % args.reason)
            print("closed (local_updated=%s)" % changed)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
