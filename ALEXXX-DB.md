# alexxx-db fork notes

This is alexxx-db's fork of [databricks/app-templates](https://github.com/databricks/app-templates). `main` tracks upstream exactly; alexxx-db work lives on branches.

## Remotes

| Remote | URL | Use |
|---|---|---|
| `origin` | `git@github.com:alexxx-db/databricks-app-templates.git` | Our fork: push branches here |
| `upstream` | `https://github.com/databricks/app-templates.git` | Source of truth: never push |

```bash
git remote add upstream https://github.com/databricks/app-templates.git   # once per clone
```

## Syncing with upstream

```bash
git fetch upstream
git checkout main
git merge --ff-only upstream/main   # fails if main has diverged: don't commit to main
git push origin main
```

Then rebase open branches: `git rebase main <branch>`.

## Branches

| Prefix | Meaning |
|---|---|
| `fix/`, `feat/`, `docs/`, `chore/` | Generic changes intended for upstream. Branch from `upstream/main`, one topic per branch. |
| `entrada/` | alexxx-db-only work that won't go upstream (client or vertical demos, alexxx-db branding). |

Keep upstreamable changes free of client names, workspace URLs, and alexxx-db-specific config so they can be offered as-is.

## Contributing upstream

- **One topic per PR**, branched from `upstream/main`. Small fixes go straight to a PR; for anything large (new templates, new tooling, docs), open an issue first to check the maintainers want it.
- **`appkit-*` templates are generated** from AppKit's template (`appkit generate:app-templates`). Fix them in AppKit too, or the next regeneration reverts the change.
- **Agent templates** share synced files: follow `.claude/AGENTS.md` (edit `.scripts/source/` or `.claude/skills/`, then run the sync scripts).
- Run the relevant tests before opening a PR: `.scripts/classic-app-tests` for classic templates, `.scripts/agent-integration-tests` for agent templates.

## Upstream status of fork work

| Branch | Contents | Upstream |
|---|---|---|
| `fix/classic-template-deps` | Shiny `htmltools<0.7` crash fix; caps on unbounded requirements | Ready for a PR |
| `fix/flask-secret-key` | Flask apps no longer fall back to a hardcoded session key | Ready for a PR |
| `chore/user-api-scope-names` | `dashboards.genie` → `genie`, `files.files` → `files`, `serving.serving-endpoints` → `model-serving` | Ready for a PR (also needs the AppKit fix) |
| `feat/showcases` | `retail-customer-assistant`, `hls-clinical-explorer`, `aml-alert-triage`, `city-311-operations`, `factory-oee-maintenance`, `telco-network-care` showcases | Fork-only for now; generic enough to propose later |
| `tier3-guides` (Tiers 1–3) | Classic template READMEs + `databricks.yml` generator, smoke tests and CI; five Streamlit integration templates; `docs/` guides | Propose in an issue first, then split into PRs |

## Vertical showcases (`entrada/`)

Rules for packaging alexxx-db demos as showcase templates:

- **Data must be redistributable.** Generate synthetic data (Faker, `spark.range`) at setup time. Don't commit third-party documents, and never MIMIC/PhysioNet data (its data use agreement forbids redistribution).
- **Keep files under 10 MB.** That's the Databricks Apps per-file limit and keeps clones small. Seed data is generated or downloaded at setup, not committed.
- **Follow the showcase format:** app + `databricks.yml` + setup job/pipeline + README runbook (see `inventory-intelligence/` or `saas-tracker/`).
- **No workspace-specific values** (endpoint names, hosts, user names) in committed files.

## Local AI tooling

`.agents/`, `.cursor/`, `.gemini/`, `.github/skills/`, `.kiro/`, `.opencode/`, `.windsurf/`, `.ai-dev-kit/` and `GEMINI.md` are installed by the Databricks AI Dev Kit and are untracked. They're local copies; updating the dev kit overwrites them.
