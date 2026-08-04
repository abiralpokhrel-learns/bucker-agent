A code reviewer found concrete issues in your proposed change. Fix them.

Return a single JSON object and nothing else — the same WorkerResult schema
as before, with the corrected "diff" and "summary":

{
  "schema_version": 1,
  "status": "produced" | "blocked" | "no_change_needed",
  "summary": "<what you changed in the revision>",
  "diff": "<unified diff, required when status is produced>",
  "files_touched": ["<paths you changed>"],
  "commands_run": [],
  "blocked_reason": "<required when status is blocked>"
}

Rules:

- Apply every issue the reviewer raised. If an issue is wrong, leave the
  code as-is and say so in the summary — the reviewer is a first pass, not
  the final judge.
- The diff must be a valid unified diff that applies cleanly at the
  workspace root. JSON escaping is mandatory (\" for quotes, \\ for
  backslash, \\n for newlines).
- If the issues make you realize the task is genuinely blocked (missing
  context, nonexistent file), return "blocked" with a specific reason.
- Never invent a plausible-looking diff.

REVIEWER'S ISSUES
-----------------
$critique

OBJECTIVE
---------
$objective

YOUR PREVIOUS RESPONSE
----------------------
$previous
