# Agent workflow

After making any file changes in this repo, stage and commit them with a single-line message:

    git add -A && git commit -m "<one-line message describing the change>"

Rules:
- Always use a single-line message (no body, no blank line, no bullet list).
- Keep it under 72 characters.
- Use imperative mood (e.g. "add X", "fix Y", "refactor Z") — not past tense.
- Do NOT push unless the user explicitly asks.
- Do NOT commit if tests are failing, unless the user explicitly says so.
- If multiple logical changes are made, split into separate commits — one per logical change.
