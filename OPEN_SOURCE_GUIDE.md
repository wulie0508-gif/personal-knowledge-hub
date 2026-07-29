# 开源与私有部署指南

这个仓库的目标是开源第二大脑的**代码和接口**，而不是发布任何人的阅读记录、Obsidian Vault、企业资料或登录凭证。

## 分享前检查

仓库中可以保留：

- Python 源码、前端静态文件、测试和文档；
- `config/*.example.json`、`.env.example`；
- 完全虚构的示例资料。

仓库中不得保留：

- `data/`、运行日志、SQLite 索引、任务队列和临时文件；
- Obsidian Vault、微信公众号原文、企业资料和图片；
- Cookie、二维码、Token、API Key、邮箱、手机号；
- `.obsidian/workspace*.json` 等可能泄露本机路径的工作区状态。

发布前至少执行：

```powershell
git status --short
rg -n --hidden --glob '!\.git/**' "(sk-[A-Za-z0-9]|Bearer |cookie=|token=|C:\\Users\\)"
python -m unittest discover -s tests -v
```

第一条用于确认没有意外文件；第二条只是基础扫描，发现结果必须人工复核；第三条保证样例与代码可运行。正式发布还应检查 Git 历史、图片 EXIF 和高熵密钥，不要只扫描当前工作树。

## 私有运行目录

设置以下两个环境变量后，运行时数据会离开代码仓库：

```powershell
$env:SECOND_BRAIN_HOME = "D:\SecondBrainRuntime"
$env:SECOND_BRAIN_VAULT = "C:\Users\your-user\Documents\Obsidian Vault"
```

`SECOND_BRAIN_HOME` 会保存索引、队列、报告、日志、缓存、偏好和私有配置。代码仓库继续只保存可分享的程序。

如果当前项目仍使用旧的 `data/` 目录，先只生成迁移计划：

```powershell
python migrate_runtime_home.py --destination "D:\SecondBrainRuntime"
```

确认计划后再复制：

```powershell
python migrate_runtime_home.py --destination "D:\SecondBrainRuntime" --copy
```

迁移工具只复制，不移动或删除旧数据。验证新目录能够启动、检索和重建后，再由用户自行决定是否清理旧目录。

## 语料域

| 域 | 含义 | 是否代表用户 |
|---|---|---:|
| `personal_memory` | 本人写作、项目判断与明确表达 | 是 |
| `professional_reference` | 腾讯研究院、博主、论文和报告 | 否 |
| `enterprise_internal` | 企业资料和组织事实 | 否，代表组织 |
| `authoritative_external` | 政府、标准、官方 API 等权威外部来源 | 否 |
| `source_archive` | 原始证据回溯 | 否 |

导入历史资料前，先做只读审计：

```powershell
python corpus_namespace_audit.py
```

审计会列出仍依赖旧默认规则的本地资料；它不会修改任何 Markdown。完成逐项判断后，再为这些资料补写 `corpus_namespace`。

对于已经确认属于本人作品的原始目录，可先 dry-run：

```powershell
python personal_identity_migration.py --source-root "D:\ReviewedPersonalWorks"
```

核对候选清单后再加 `--apply`。应用前会把原笔记复制到 `SECOND_BRAIN_HOME/backups/`，未匹配来源和已显式分类的笔记不会被修改。

如果同一目录同时包含本人、企业和第三方资料，禁止整目录标成 personal；改用 `corpus_identity_migration.py --rules <private-rules.json>` 按有序规则分流。规则文件应保存在私有运行目录并保持在 Git 之外。

## 本地活动监控与接口安全

默认不启动活动监控。只有明确设置下列变量时，`start.cmd` 才会启动相应 watcher：

```powershell
$env:SECOND_BRAIN_WATCH_WECHAT_HISTORY = "1"
$env:SECOND_BRAIN_WATCH_FILEHELPER = "1"
```

- 微信历史 watcher 只查看桌面微信内置 Chromium History 中新增的公众号 URL，首次运行只建立基线；
- 文件传输助手 watcher 依赖单独安装的侵入式连接器，必须单独授权；
- watcher 状态文件只用于恢复扫描位置，不能作为用户同意监控的凭据；
- “打开/读过”只能表示行为证据，不能自动改变 `stance` 或 `persona_influence`；
- HTTP 服务只能绑定 `127.0.0.1`、`localhost` 或 `::1`，不可直接作为公网服务；
- `GET /api/status` 和检索结果可能包含私有本地路径，因此不要通过反向代理暴露。

## 模型提供方边界

正文、索引与轨迹默认保存在本地；但 AI 整理和视觉 OCR 可以配置云端模型。若启用云端 provider，被提交的内容片段将遵循该 provider 的数据政策。完全离线部署应只启用本地模型或关闭模型辅助。

## 许可证

本仓库已经采用 [MIT License](LICENSE)。发布衍生版本时保留原始版权与许可声明；许可证授权的是代码，不会改变微信公众号文章、企业资料、个人笔记或其他第三方内容各自的版权与使用边界。
