# Cleo Jobs

A separate job discovery, scoring, Discord approval, resume-tailoring, and application-form workflow for Cleo Navarro. It was built from the same codebase and safety gates as Ryan's JobWatch, but it has its own target roles, evidence profile, database cache keys, Render service name, secrets, and Discord destinations.

This repository does not read or write Ryan's JobWatch database, branches, workflows, secrets, or Discord webhooks.

## Target roles

Cleo Jobs searches for four evidence-supported groups:

- Healthcare and patient operations: patient access, patient services, medical office, referral, scheduling, intake, care, clinic, health-services, medical-records, and health-information roles.
- Human resources: HR assistant/coordinator, people operations, onboarding, benefits, recruiting, and talent coordination.
- Administrative operations: administrative assistant/coordinator, office coordinator, operations coordinator, executive assistant, and program coordinator.
- Public health: public-health programs, community health, health education, outreach, and resource coordination.

The LinkedIn adapter searches both Las Vegas-area local jobs and nationwide remote jobs. Onsite or hybrid jobs outside the configured Las Vegas-area metros are blocked by the match layer.

Senior, manager, supervisor, director, lead, commission-only, and licensed-clinical titles are excluded or blocked when Cleo's supplied resume does not document the required level or credential.

## Evidence profile

The matching engine uses only `profiles/experience.json`, which was derived from `Cleo_Navarro_Resume.docx`.

Supported systems and work include Oracle, Workday, ADP, Phreesia, patient scheduling and intake, insurance and referral verification, onboarding and offboarding, HR records, compliance documentation, records auditing, expense and travel coordination, customer service, POS/payment systems, provider and patient communication, social media, email marketing, crisis support, and community outreach.

The profile does not claim work authorization, sponsorship status, a driver's license, HIPAA certification, CMA, CPR/BLS, medical coding credentials, an EHR/EMR platform, or any clinical license. Postings that require undocumented credentials are blocked or left for explicit review.

No salary floor is assumed. Compensation is shown when available but does not disqualify a job until Cleo chooses a minimum.

## Main components

| File | Purpose |
|---|---|
| `jobwatch.py` | Pulls jobs, deduplicates, suppresses likely ghost jobs, fetches details, scores matches, and posts Discord cards |
| `jobwatch_match.py` | Evidence-bound role, ATS, seniority, location, salary, license, and certification scoring |
| `jobwatch_enrichment.py` | Finds a fuller public copy of a posting and triggers deterministic re-scoring |
| `jobwatch_readiness.py` | Eight-category readiness radar chart for Cleo's target market |
| `jobwatch_trends.py` | Twice-weekly market trend chart |
| `jobwatch_skills_export.py` | Needed-skills CSV based on observed postings |
| `jobwatch_pipeline.py` | Optional callback-rate overlay from an application ledger |
| `jobwatch_apply.py` | Queues only eligible jobs to the application service |
| `jobapply_service.py` | Discord review service for resume drafts and form steps |
| `jobapply_skill.py` | Evidence-bound tailored resume generator and validator |
| `jobapply_browser.py` | Approved employer-form inspection and transmission runtime |
| `profiles/experience.json` | Cleo's structured professional evidence |
| `resume.json` | Readiness-chart sidecar |
| `skills/healthcare-career-strategist/SKILL.md` | Cleo-specific resume and truth rules |
| `render.yaml` | Separate Render service blueprint |

## Safety gates

- Job cards can be posted without enabling applications.
- A job must score at least 70 and have no hard blocker before it enters the private application queue.
- A fuller public posting is checked before application intake. New experience, location, salary, license, or certification blockers stop the application path.
- Resume drafts are evidence-bound and require Discord approval.
- Form inspection is separate from transmission.
- `JOB_APPLY_TRANSMIT_ENABLED=false` prevents employer fields, files, clicks, and submissions.
- ATS transmission requires both the global gate and an explicit platform allowlist.
- Missing or sensitive answers stay blank and require review.
- Employer submission is accepted only when the employer page shows a confirmation receipt.
- An uncertain outcome is never automatically retried.

See `JOB_APPLY.md` for the full state machine and deployment details.

## GitHub Actions setup

Create these repository secrets in the new Cleo Jobs repository. Do not reuse Ryan's Discord destinations or deployment tokens unless that is deliberately intended.

Required for alerts:

- `DISCORD_WEBHOOK_URL`: webhook for Cleo's job-alert channel.

Optional scanner features:

- `JOB_MAP_WEBHOOK_URL`
- `JOB_READINESS_WEBHOOK_URL`
- `NEEDED_SKILLS_WEBHOOK_URL`
- `ADZUNA_APP_ID`
- `ADZUNA_APP_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `GSHEET_WEBAPP_URL`
- `GSHEET_WEBAPP_TOKEN`

Application service:

- `JOB_APPLY_SERVICE_URL`
- `JOB_APPLY_INGEST_TOKEN`

The workflow runs every two hours and uses a `cleo-jobs-db-` cache prefix. A brand-new SQLite database is created on the first run; no copy of Ryan's historical database is included.

## Render setup

`render.yaml` defines a separate `cleo-jobs-apply` service and `cleo-application-data` disk.

Before enabling resume preparation, configure:

- `DISCORD_BOT_TOKEN`
- `JOB_APPLY_CHANNEL_ID`
- `JOB_APPLY_OWNER_ID`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL`
- `JOB_APPLY_PROFILE_JSON`

The private `profiles/apply_profile.json` file is gitignored. Copy its JSON into the Render secret `JOB_APPLY_PROFILE_JSON`. Review all blank questions with Cleo before adding work authorization, sponsorship, salary, availability, consent, demographic, driver's-license, or certification answers.

Keep `JOB_APPLY_TRANSMIT_ENABLED=false` until inspection-only tests pass for Cleo's real job mix.

## Local verification

```bash
python -m pip install -r requirements.txt -r requirements-apply.txt
python -m unittest discover -p 'test_jobwatch*.py' -v
python jobwatch.py --dry
```

The browser runtime has a separate container test:

```bash
docker build -f Dockerfile.jobapply -t cleo-jobs-browser-test .
docker run --rm --network none -e JOB_APPLY_BROWSER_TESTS=1 \
  cleo-jobs-browser-test python -m unittest test_jobwatch_browser_runtime -v
```

## First-run checklist

1. Create the separate private GitHub repository.
2. Push this project to its `main` branch.
3. Add the Cleo-specific Discord webhook and optional feature secrets.
4. Run the test workflow.
5. Run `cleo-jobs` manually with `workflow_dispatch`.
6. Confirm the source summary and inspect several job cards.
7. Tune role keywords and location preferences from real results.
8. Deploy the separate Render service only if resume preparation is wanted.
9. Test employer forms in inspection-only mode.
10. Enable individual ATS transmission only after its real fixtures pass.

