# Issue 891 Jobs Wait Discovery Design

## Problem

Long-running tools return job handles and suggest `jobs.wait`, but gateway search relies on
tool names, descriptions, and curated keywords. The current keyword map has no jobs-plane
entries, so natural follow-up phrases around waiting, polling, terminal status, and
cancellation are less reliable than the suggested-next-action loop needs.

Namespace browsing already returns `jobs.wait`, so the remaining gap is ranked query
discovery rather than catalog absence.

## Contract

`tools.search` must make `jobs.wait` easy to find from ordinary wait/poll vocabulary used
after a job-producing tool returns. Searches such as `wait for job`, `poll running job`,
and `suggested next action jobs.wait` should include `jobs.wait` in their matches for a
viewer-visible caller.

The jobs namespace should also have curated keywords for adjacent job tools:

- `jobs.get`: lookup/fetch final job status.
- `jobs.list`: list/filter background jobs.
- `jobs.wait`: wait/poll/retry until completion.
- `jobs.cancel`: cancel/stop/abort a running job.

Do not add `jobs.wait` to `CORE_TOOLS` for this fix. That changes which tools are directly
listed under gateway mode and is broader than the confirmed discovery-quality gap.

## Implementation

Add jobs-plane entries to `TOOL_KEYWORDS` in `src/kdive/mcp/tool_index.py`.

## Testing

Extend `tests/mcp/tools/test_gateway_search.py` with behavior coverage that runs the real
gateway search for the follow-up phrases and asserts `jobs.wait` appears in the returned
matches for a viewer-role caller.
