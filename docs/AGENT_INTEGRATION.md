# Agent Integration Contract

Personal Knowledge Hub exposes two local-only reads. An Agent should call them in this order.

## 1. Load the compact personal context

CLI:

```powershell
python knowledge_agent_cli.py context --max-chars 6000
```

HTTP:

```text
GET http://127.0.0.1:8765/api/context?max_chars=6000
```

Rules for the caller:

- `confirmed_self` may be described as user-authored memory;
- `explicit_preferences` are explicit behavioural feedback, not universal beliefs;
- `observed_trajectory` is an attention signal only;
- never infer agreement, mastery or authorship from a page view;
- do not preload external corpora merely to personalise an answer.

## 2. Recall details only when required

CLI:

```powershell
python knowledge_agent_cli.py recall "我过去如何判断 Agent 记忆"
python knowledge_agent_cli.py recall "我过去如何判断 Agent 记忆" --include-evidence
```

HTTP:

```text
GET /api/recall?q=我过去如何判断Agent记忆
GET /api/recall?q=我过去如何判断Agent记忆&include_evidence=true
```

Result fields:

- `memories`: user-authored `personal_memory`;
- `enterprise_facts`: organisation-owned facts;
- `evidence`: separately labelled external research;
- `identity.represents_user`: the authoritative identity flag;
- `temporal`: the best available publication or curation date;
- `citation`: title, URL, date and local note path;
- `boundary`: a mandatory warning when no personal memory exists;
- `context_budget`: confirms that no full article or archive fallback was loaded.

## 3. Direct scoped retrieval

```powershell
python knowledge_agent_cli.py search "query" --scope personal
python knowledge_agent_cli.py search "query" --scope professional
python knowledge_agent_cli.py search "query" --scope enterprise
python knowledge_agent_cli.py search "query" --scope authoritative
python knowledge_agent_cli.py search "query" --scope archive
```

Use direct search for evidence gathering, not as a replacement for the two-step personal context protocol.

## Failure behaviour

- Empty `memories` means no matching user-authored record was found.
- External evidence must remain in `evidence`; never rewrite it as a memory.
- Missing date is returned as `date_kind=unknown`.
- Archive retrieval is opt-in; the recall endpoint does not silently load it.
- The HTTP service is intentionally limited to local loopback addresses.
