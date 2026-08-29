# Issue tracker: GitHub

Issues and specs for this repository live in GitHub Issues for `williamxhero/provider_broker_proj`. Use the `gh` CLI for issue operations.

## Conventions

- Publishing a spec means creating a GitHub issue.
- Fetching a ticket means reading the issue and its comments.
- Infer the repository from the configured `origin` remote.
- Pull requests are not treated as a request or triage surface.

## Common operations

- Create: `gh issue create --title "..." --body-file <file>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open`
- Comment: `gh issue comment <number> --body "..."`
- Close: `gh issue close <number> --comment "..."`
