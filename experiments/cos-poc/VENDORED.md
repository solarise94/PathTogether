# Vendored 第三方产物记录（cos-js-sdk-v5）

> 本目录文件是第三方构建产物，逐字节来自官方仓库，不得手工修改。
> 修改需求只能通过"换锁定版本重新下载 + 更新本记录"满足。

## 清单

| 文件 | 版本 | 来源 URL | SHA-256 | 许可证 |
|---|---|---|---|---|
| `vendor/cos-js-sdk-v5-1.10.1.min.js` | 1.10.1 | https://raw.githubusercontent.com/tencentyun/cos-js-sdk-v5/v1.10.1/dist/cos-js-sdk-v5.min.js | `16a5fa25ad090647e9e5e928fb260e5a9d8ddf8a063cedb5a3a7cbc2105a66e7` | MIT |
| `vendor/LICENSE-cos-js-sdk-v5.txt` | （随 v1.10.1 tag） | https://raw.githubusercontent.com/tencentyun/cos-js-sdk-v5/v1.10.1/LICENSE | `fc10b4b2fe85a360257d40e65192594976aa0ea72c4c8f555043e029564a9f18` | MIT |

## 版本选择依据

- 选定 tag `v1.10.1`：下载当时官方仓库最新的**稳定** tag；`v1.11.0-*` 均为 beta 预发布，不用于 PoC。
- 版本号与官方仓库 `package.json` 的 `"version": "1.10.1"` 交叉核对一致；dist 文件内嵌版本串 `cos-js-sdk-v5-1.10.1`（构建时注入）。
- 通过 git tag 固定下载 URL（`raw.githubusercontent.com/.../v1.10.1/...`），不使用 `latest`/`master`/CDN 漂移源。

## 校验方法

```bash
sha256sum experiments/cos-poc/vendor/cos-js-sdk-v5-1.10.1.min.js \
           experiments/cos-poc/vendor/LICENSE-cos-js-sdk-v5.txt
# 与上表比对；不一致则立即删除并重新获取，禁止继续使用。
```

## 许可证义务

MIT（版权 2017-present 腾讯云）。分发或再打包时保留 LICENSE 文件副本（已随目录提交），产品若采纳候选 B，Phase 4 的本地托管副本须沿用同一锁定版本与校验流程（合同 docs/cos-direct-upload-audit-plan.md §3.2"锁版本、本地托管"）。

## 与合同条款的对应

- 合同 §3.2（候选 B）：浏览器使用"官方 `cos-js-sdk-v5` + 单 key STS"高级分块上传，SDK 必须**本地托管且锁版本**——即本目录，不从公网 CDN 加载。
- 分流调研 §0 D2：不引入 Uppy/tusd/Companion，PoC 只用官方 `cos-js-sdk-v5`。
