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

第一条用于确认没有意外文件；第二条只是基础扫描，发现结果必须人工复核；第三条保证样例与代码可运行。

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
| `personal_memory` | 本人写作、项目、明确读过与批注 | 是 |
| `professional_reference` | 腾讯研究院、博主、论文和报告 | 否 |
| `enterprise_internal` | 企业资料和组织事实 | 否，代表组织 |
| `authoritative_external` | 政府、标准、官方 API 等权威外部来源 | 否 |
| `source_archive` | 原始证据回溯 | 否 |

导入历史资料前，先做只读审计：

```powershell
python corpus_namespace_audit.py
```

审计会列出仍依赖旧默认规则的本地资料；它不会修改任何 Markdown。完成逐项判断后，再为这些资料补写 `corpus_namespace`。

## 许可证

在公开发布前，由仓库所有者选择许可证。若尚未决定，保持私有或明确标记“未授权再分发”；不要默认把代码当作可自由商用。
