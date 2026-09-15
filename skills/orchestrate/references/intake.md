# Intake adapters

Goal for every incoming item, whatever its source: **key**, **title**, **what done looks like**, **source**. Fetch the body yourself rather than delegating it — you already have the tracker context, and pulling it here keeps the worker's context clean for the actual work.

If a source is unreachable, park that one item with the reason and keep going. One dead tracker should never stall a batch.

## GitHub

`gh` covers issues and PRs. Check auth once (`gh auth status`) — note the token's scopes, since a token without `repo` will read public issues and silently fail on private ones.

```bash
gh issue view 412 --json number,title,body,labels,assignees,url
gh issue view 412 --repo owner/name --json number,title,body,url    # cross-repo
gh pr view 77 --json number,title,body,url,headRefName
```

Key as `GH-<number>`. Labels are worth reading: they often carry the done-condition (`needs-test`, `regression`) more reliably than the body.

For a batch, one call beats N:

```bash
gh issue list --label bug --state open --limit 20 --json number,title,url
```

## GitLab

`glab` mirrors the same shape when installed:

```bash
glab issue view 412 --output json
```

## Jira

No official CLI ships by default. In order of preference: a connected Atlassian MCP tool; then the REST API if the user has `JIRA_BASE_URL` and a token in the environment; then ask for pasted text.

```bash
curl -sS -u "$JIRA_USER:$JIRA_TOKEN" \
  "$JIRA_BASE_URL/rest/api/3/issue/PROJ-412?fields=summary,description,status,labels" | jq .
```

Never ask the user to type a token into the conversation, and never echo one. If credentials aren't already in the environment, the pasted-text path is the correct fallback — it is faster than a credential detour and keeps secrets out of the transcript.

Jira descriptions come back as Atlassian Document Format, not Markdown. Extract the text content rather than passing raw ADF JSON into a worker brief; the nesting will eat the worker's context for no benefit.

Key as the Jira key verbatim (`PROJ-412`).

## Notion

Requires a connected Notion MCP tool — there is no standard CLI. With one, fetch the page by id or URL and use the page title as the title. Without one, ask for pasted text; a Notion URL alone is not fetchable.

Notion pages are frequently specs rather than issues, so the done-condition is often absent. Ask for it rather than inventing one, and prefer splitting a long spec into several items over briefing one worker with a whole document.

Key as `NOT-<short id>` or a slug from the title.

## Plain descriptions and discovered bugs

Work also arrives as "the login page hangs on Safari" or from something a worker tripped over mid-task. Same four fields; you write them.

Two things matter more here than with tracker items. Get an explicit done-condition, because there is no issue body to fall back on — "fixed" is not one, "Safari 17 loads the page in under 2s and the existing suite still passes" is. And when a worker discovers the bug, record who found it and in which branch in the new item's notes, since the reproduction usually lives in that worker's worktree and will be lost when it is cleaned up.

Key as a short slug: `login-safari-hang`.

## Normalizing the brief

Whatever the source, the worker gets prose, not JSON. A dumped API payload wastes the worker's context on field names and metadata that have nothing to do with the fix.

```
Issue GH-412: Login page hangs on Safari

Users on Safari 17 see the login page spin indefinitely. Chrome and Firefox
are unaffected. Reported against v2.4.1.

Done when: Safari 17 loads /login in under 2s and the existing suite passes.
Source: https://github.com/owner/name/issues/412
```

Strip tracker boilerplate — templates, checkbox scaffolding, "thanks for filing" comments. Keep reproduction steps, versions, and error text verbatim; those are the parts a worker cannot reconstruct.
