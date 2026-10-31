# 1.0.0 发布检查

检查日期：2026-10-31。依据 [AstrBot 插件开发原则](https://docs.astrbot.app/dev/star/plugin-new.html#开发原则)与[发布说明](https://docs.astrbot.app/dev/star/plugin-publish.html)。

| 检查项 | 结果 |
| --- | --- |
| 元数据 | 插件名、展示名、作者、1.0.0 版本、GitHub 仓库地址齐全；声明 AstrBot `>=4.29.1,<5` 与 `aiocqhttp` 平台 |
| 依赖与文档 | Python 依赖已写入 requirements.txt；README 包含安装、指令、配置、FFmpeg/浏览器/STT 要求、部署方式与限制 |
| 数据存储 | 使用公开的 `StarTools.get_data_dir`，凭据和缓存仍在 `data/plugin_data/astrbot_plugin_bilicard`，更新不会覆盖账号数据 |
| 日志与网络 | 运行时代码使用 AstrBot 的 logger；HTTP 使用 aiohttp；敏感凭据、Base64 和临时媒体链接脱敏 |
| 许可与仓库内容 | MIT 代码许可证；预览素材注明来源；排除密钥、账号凭据、二维码、视频/音频缓存和 Python 缓存 |
| 基础检查 | Ruff 格式与基础正确性检查通过；46 项既有必要检查通过 |
| 卡片预览 | 使用真实视频元信息、封面与热门评论重新渲染普通卡片及总结示例；卡片右上角图标已移除 |

必要检查覆盖链接/BV/QQ 小程序、JSON/XML/CQ 分享、合并转发、外层触发权限、扫码凭据处理与加密、摘要编排及失败降级、时长与大小限制、本地/Base64/HTTP 回调发送、EACCES 自动切换与媒体清理。

此前真实媒体验证：B 站 DASH 音视频经 FFmpeg 合成为 H.264/AAC MP4，退出上下文后删除；单独音轨提取为 212.395 秒、6,796,708 字节、16 kHz 单声道 PCM WAV，处理后临时文件与目录均删除。

测试中的 SDK 边界及 STT/LLM 响应使用隔离类型或固定数据，并核对 AstrBot 4.29.1 源码接口。未连接使用者的实际 QQ 协议端，未执行人工扫码确认或真实 STT/LLM 调用。总结预览明确标记演示文本，实际效果取决于配置的服务。

此文件记录发布前自查，不表示 AstrBot 插件市场已审核或上架。市场提交通过 AstrBot Cloud 完成。
