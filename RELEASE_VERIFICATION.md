# 聚鑫国际 v3.0.4 验证说明

本文件定义验收要求，不记录历史交付结论，也不表示当前 Windows 构建已完成。

## 必须分开的证据

1. 公开源码身份：schema 2 清单、实际 Git HEAD、GitHub Actions 提交与仓库身份一致。
2. 源码门禁：所有可执行代码、业务回归、版本、依赖和构建约束通过；source-only 输出明确
   标注 DESKTOP_ARTIFACTS_CHECK=NOT_RUN，不能视为完整构建成功。
3. Windows 原生验证：实际 Chrome、Electron 原生 UI、隐藏采集恢复 UI、窗口所有权及
   权威关闭证明通过；合成适配器不替代第三方服务实测。
4. 冻结运行时：实际打包 Core、模型／OpenVINO、本地服务、资源布局等门禁通过。
5. 最终安装器：唯一的准确 NSIS Setup、SHA-256、安装后应用、实际冻结 Core 和认证 API
   恢复链路一致；便携包、编译输出或构建前 fixture 不替代安装后验证。

核对 `Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe.sha256`、实际 Setup
的 SHA-256 以及 `installer-output/LATEST_SUCCESS.txt`。对应报告必须绑定相同源码身份
和最终安装器，不能复用其他提交、旧截图或另一份安装器的通过记录。

## 运行边界

验收使用隔离合成数据库、账号、网页和回执，不应打开用户数据库或对真实 Instagram
账号发帖、点赞、修改资料。云端和可选翻译未配置或不可用时，保持准确状态。发帖与 Pexels 已移除，替代验收必须证明归档完整、接口不存在、活动占用不被抢占。
离线适配器测试可以证明边界和控制流，不能证明真实服务已可用。

失败、缺失或跳过的必需阶段阻止声明安装器验收通过。工作流运行结果及其产物才是当次
执行证据；本仓库不预填成功状态、运行 ID 或最终安装器哈希。

参见 [Windows 构建](docs/PUBLIC_BUILD.md)、[源码身份](docs/CI_SOURCE_IDENTITY.md)
和 [验收清单](docs/ACCEPTANCE_CHECKLIST.md)。
