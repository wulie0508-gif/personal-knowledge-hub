# Personal Knowledge Hub

Local-first second-brain hub for turning selected reading into a structured,
searchable Obsidian knowledge base. It keeps personal memory, external
research, enterprise material, and raw source evidence explicitly separated so
that an AI can cite useful research without confusing it for the user's views.

## What it does

- Imports selected WeChat Official Account articles, web links, and local files.
- Maintains a text-first Obsidian corpus with source URL, author, date,
  provenance, quality signals, and citations.
- Builds a local SQLite FTS5 index, concept/topic pages, and evidence-aware
  relationships for Obsidian.
- Separates retrieval into `personal_memory`, `professional_reference`,
  `enterprise_internal`, and `source_archive`.
- Keeps runtime data outside the shareable source tree when
  `SECOND_BRAIN_HOME` is configured.

## Quick start

1. Create and activate a virtual environment, then install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

2. Copy `.env.example` if you need environment-specific paths.
3. Set `SECOND_BRAIN_VAULT` to your Obsidian vault when it is not the default
   Documents location.
4. Run `start.cmd` on Windows, then open `http://127.0.0.1:8765`.

The first time you add an Official Account subscription, the archiver will ask
you to scan its login QR code with WeChat. Login credentials stay on the local
machine and must never be committed.

The optional mobile handoff also needs a separately installed local
`wechat-content-router-windows` connector. Set
`WECHAT_CONTENT_ROUTER_ROOT` to that connector's folder; it is intentionally
not bundled with this repository because it interacts with the local WeChat
client.

## WeChat usage

### Reading on this computer

When an Official Account article is opened in the desktop WeChat built-in
browser, the history watcher can detect the new `mp.weixin.qq.com/s/` link and
submit it to the local inbox. It never scans ordinary WeChat chats.

### Reading on a phone

Mobile browsing history is not available to the computer, so use an explicit,
private handoff:

1. In the article's WeChat menu, choose **Send to Friend**.
2. Send the article card to **File Transfer Assistant** (your own account).
3. In the same File Transfer Assistant conversation, send the exact command
   **`存入知识库`**.
4. Keep the computer running with `start.cmd`. The local watcher reads only the
   File Transfer Assistant conversation, collects links received since the
   previous command, and imports them into the local knowledge inbox.

The command is deliberate: forwarding a link alone does not import it. This
prevents unrelated links stored in File Transfer Assistant from entering the
knowledge base. The watcher ignores all other chats and does not upload message
content to a server.

## Architecture and data boundaries

See [ARCHITECTURE_PROPOSAL.md](ARCHITECTURE_PROPOSAL.md) for the corpus,
retrieval, graph, and memory-management design. See
[OPEN_SOURCE_GUIDE.md](OPEN_SOURCE_GUIDE.md) before publishing or migrating a
deployment.

## Validation

```powershell
python -m unittest discover -s tests -v
python corpus_namespace_audit.py
```

The audit is read-only: it identifies legacy local notes that need an explicit
corpus namespace before they are allowed to influence personal retrieval.

## Privacy and publishing

Do not commit `data/`, an Obsidian vault, browser histories, WeChat exports,
SQLite indexes, logs, credentials, QR codes, or `.env` files. The repository's
`.gitignore` is intentionally conservative, but always review `git status`
before publishing.
