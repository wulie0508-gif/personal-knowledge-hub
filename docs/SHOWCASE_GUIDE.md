# Repository Showcase Guide

本文件只管理仓库“门面”：GitHub About、Topics、截图命名与隐私检查，不涉及任何功能代码。

## GitHub About

### 中文

本地优先的个人 AI 知识中台：把公众号、网页与本地资料沉淀为可检索、可引用、会建立联系的 Obsidian 第二大脑。

### English

A local-first AI knowledge hub that turns scattered reading into a searchable, citable, and connected Obsidian second brain.

### 推荐的 GitHub About 单行版本

Local-first AI knowledge hub｜把公众号、网页与本地资料变成可检索、可引用、会建立联系的 Obsidian 第二大脑。

## Recommended Topics

- `rag`
- `obsidian`
- `knowledge-management`
- `second-brain`
- `llm`
- `local-first`
- `python`
- `knowledge-graph`

## Screenshot Organization

```text
static/
└─ screenshots/
   ├─ knowledge-graph-overview.jpg
   ├─ web-console.png
   ├─ local-rag-answer.png
   ├─ web-console-placeholder.svg
   └─ local-rag-placeholder.svg
```

命名建议：

- 全部使用小写英文和连字符；
- 文件名表达“界面 + 场景”，避免 `截图1.png`；
- 实际截图优先使用 `.png`，照片或体积较大的图谱可使用高质量 `.jpg`；
- README 使用相对路径，例如：

```markdown
![Knowledge Graph](static/screenshots/knowledge-graph-overview.jpg)
```

## Screenshot Privacy Checklist

公开截图前逐项检查：

- [ ] 没有真实微信号、手机号、邮箱或二维码；
- [ ] 没有 Cookie、Token、API Key、`.env` 内容或浏览器开发者工具请求头；
- [ ] 没有 `C:\Users\<name>` 等真实本地路径；
- [ ] 没有未授权的个人日记、企业内部文档标题或客户名称；
- [ ] 浏览器标签页、通知栏、头像和系统托盘没有泄露身份；
- [ ] 图片 EXIF 中没有定位、设备序列号或拍摄者信息；
- [ ] 示例内容使用虚构或已明确允许公开的数据。

当前 `knowledge-graph-overview.jpg` 的视觉内容只有匿名化节点，且未检测到 EXIF 元数据，可用于公开 README。
