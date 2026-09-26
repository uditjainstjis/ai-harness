Feature request: machine-readable output for `todo list`

I want to script around the todo CLI. Please add a `--json` flag to the `list` command that prints the items as a JSON array instead of the human format. Each element should have `index` (1-based, same numbering as the text output), `text`, `priority` and `done`. It would also be great to filter: `--pending` shows only items that are not done (works with and without `--json`; indexes keep their original numbering). An empty list must print `[]` with `--json`. The existing text output must not change.
