# 聚鑫国际 v3.0.4

面向 Windows 的 Instagram 工作区源码，包含采集、审核、报表、独立养号和单图发帖队列。
桌面使用 Electron，本机 Core 使用 Python。产品版本为 **3.0.4**，源码修订为
**stability-r94**，功能标识为 **2026.10.05-r6.4-ig**。

仓库包含源码与验证工具，不包含预生成安装器，也不代表某次 Windows 构建或安装后验收已通过。

## 构建与验收

公开构建入口为 `.github/workflows/public-windows-verify.yml`，验证实际平铺 Git
检出的提交与源码清单。构建电脑和安装后的目标电脑均须已安装 **Google Chrome**。
环境、入口与结果核对见 [Windows 构建说明](docs/PUBLIC_BUILD.md)。

本机源码检查、合成数据测试、Windows 原生界面、冻结 Core、安装后 API 与最终安装器
SHA-256 是不同证据，不能互相替代。不要伪造 CI 环境变量来取得通过结果；正式公开
源码证明要求真实 GitHub Actions 上下文。详见 [验收边界](RELEASE_VERIFICATION.md)。

## 可选服务

- 云端备份默认关闭。先在自己的 Supabase 项目执行 `cloud/supabase-init.sql`，
  在云端工作区填写 HTTPS 项目地址及发布密钥／anon key，再主动启用并登录。
  启用并登录后，工作区记录与管理素材会上传到所选项目。不要填写 service_role 或 secret key。
- 沉浸式翻译为可选功能，仓库和安装器不包含其专有用户脚本。请从厂商授权渠道自行取得
  支持的原始脚本，在翻译设置选择“导入官方脚本文件”，通过固定哈希校验后再启用并选择服务。
  [集成说明](desktop/vendor/immersive-translate/NOTICE.txt)列明兼容版本和校验值。
  厂商账号、条款、服务费用由使用者自行选择。
- Pexels 等需要凭据的服务须在本机单独安全配置；源码和安装器不提供密钥。

发帖结果不明确时保留未知状态，不自动重复提交。窗口独占、任务租约和权威关闭确认继续生效。
用户数据库、账号登录环境、凭据和构建输出不应提交到公开仓库。

- [验收清单](docs/ACCEPTANCE_CHECKLIST.md)
- [异常恢复](RECOVERY_POLICY.md)
- [存储保护](STORAGE_POLICY.md)
- [数据库迁移边界](docs/PURE_IG_DATABASE_MIGRATION_R5.md)
- [安装器身份](NEWGEN_RELEASE_STATUS.md)

公开源码本身不表示已授予第三方软件再分发权，也不代替项目许可证；再分发前须核对适用权利。
