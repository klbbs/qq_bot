# QQ / Hermes 本地机器人

保留现有 QQ、ACM 刷题、多平台内容接口和 Hermes 扩展的源码，后续在此仓库维护。公开仓库只保存代码、依赖清单和脱敏配置示例。

## 本次发送保护

- 所有 QQ 发送统一排队：默认全局至少间隔 1 秒，私聊 2 秒、群聊 3 秒；每分钟全局最多 30 条，同一私聊 20 条、同一群 12 条。
- 相同目标、相同内容在 20 秒内复用已有发送回执；收到重复 QQ 事件时不再处理。
- 网络错误暂停 30 秒起，明确限流/登录失效暂停 10 分钟起，连续错误逐次延长，最多 30 分钟。暂停状态会保留到重启后。
- 请求已发出却没有回执时，不自动重发；相同内容 10 分钟内不再尝试。发送队列最多 12 个等待项，等待超过 20 秒明确终止，避免恢复后集中刷屏。
- `<SPLIT>` 默认合并成一条回复；长群消息沿用合并转发。默认保留好友申请供管理员审核。
- 健康接口分别报告“QQ 声称在线”“最近收到消息”“最近确认发送”“当前发送冷却”，不把进程运行等同于收发正常。

这些是本项目可调整的发送预算，并非腾讯公布的安全阈值。不能保证避免 QQ 账号限制。最近一次故障能确认的是 QQ 网络报错和登录态失效，不能单凭这些日志断定腾讯风控。

## 保留的功能

现有命令、窗口化群聊、简短回复、QQ 管理/媒体/分享卡片、管理员权限约束、ACM 图片抽题/去重历史/接收确认/判题/超时放弃、人设库，以及 Bilibili、小黑盒接口均保留。Feishu 等通道继续由本机 Hermes 管理。

`src/qq` 是 QQ 桥接源码；`src/plugins` 是本地 Hermes 插件；`src/content` 是内容接口；`src/acm` 是 ACM 客户端；`vendor/nonebot_plugin_hermes` 保留已安装 0.5.5 的本地修改，避免重装后丢失。

账号、个人目录及服务器地址以 `__QQ_OWNER__`、`__QQ_BOT__`、`__HOME__` 等占位符保存。部署时从本机未跟踪的 `.local-settings.json` 恢复；不要直接运行未渲染的模板。

## 本机使用

在仓库目录执行：

```bash
# 不依赖 QQ 在线，只检查源码、隐私和单元测试
python3 scripts/manage.py check

# 查看真实运行状态（需要本机 QQ 虚拟环境中的 httpx）
~/.local/share/qq-hermes/.venv/bin/python scripts/manage.py status

# 预览将变动的 QQ 文件
~/.local/share/qq-hermes/.venv/bin/python scripts/manage.py deploy

# 一键更新本机 QQ 桥接；自动备份，失败回退
~/.local/share/qq-hermes/.venv/bin/python scripts/manage.py deploy --apply

# 运行目录被其他工具修改后，同步源码回此仓库
# 先提交/保存仓库内正在编辑的代码，避免被运行版本覆盖
~/.local/share/qq-hermes/.venv/bin/python scripts/manage.py capture
```

`deploy` 只更新 QQ 目录并重启 QQ 桥接；判题/抽题或聊天任务正在执行时会退出，稍后重试。它不会部署 ACM 服务器，也不会改本机密钥、Hermes 模型配置或登录态。已有 `delivery-policy.json` 会保留，修改该文件后需重启 QQ 桥接。

此仓库是现有安装的源码维护仓库，**不是包含 QQ 客户端和登录信息的即装即用迁移包**。换机器时需另行安装 Hermes、NapCat 和依赖，配置新环境中的账号与密钥。`requirements/` 提供当前依赖快照；`vendor/` 的本地补丁需单独合并，普通 `pip install` 不会自动应用它们。

每次修改流程：更新源码 → `check` → `deploy --apply` → 检查 `status` → 提交并推送。GitHub 登录凭据由自己的 Git/SSH 配置管理，仓库不保存访问令牌。

## 登录和故障恢复

QQ 显示在线但不回复时，先看 `status` 的收发时间和冷却状态。若登录已失效，在本机 NapCat 管理页重新扫码，选择机器人账号并在手机确认。扫码后重新发送一条消息；不自动补发失联期间的旧回复，以免重复执行操作。

所有运行数据继续保留在本机，包括 `.env`、`.outbound-token`、Hermes 配置、QQ 登录目录、聊天数据库、作答记录、角色卡、图片与附件。它们不会随本仓库上传。

更多说明：[发送保护设计](docs/delivery.md)、[上游来源](THIRD_PARTY_NOTICES.md)。
