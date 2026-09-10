# Job application approvals

## Production transmission gate

Form inspection and employer transmission are separate deployment capabilities:

- `JOB_APPLY_FORMS_ENABLED=true` enables navigation, read-only form inspection,
  local profile matching, and Discord review cards.
- `JOB_APPLY_TRANSMIT_ENABLED=false` is the default hard stop. While false,
  JobWatch cannot fill employer controls, upload a résumé, click Continue, or
  submit—even for a previously approved or 100% auto-apply candidate.
- `JOB_APPLY_TRANSMIT_ATS` is a comma-separated allowlist of ATS identifiers:
  `greenhouse`, `ashby`, `fountain`, and `subway`. It defaults empty.
  Transmission requires both the global switch and an exact ATS allowlist match.

The gate is enforced at manual form approval, automatic approval, queued browser
execution, and the final execution function. A stale approved step is returned
to review without touching the employer. Keep transmission disabled during live
inspection validation on every ATS.

Recognition does not grant transmission. Workday, Lever, SmartRecruiters,
iCIMS, BambooHR, and LinkedIn are currently inspection/resolution-only and are
not members of the transmission-ready set even if their names are placed in
`JOB_APPLY_TRANSMIT_ATS`.

## Phase 4.1: Greenhouse Voyager controls

Greenhouse's newer React application form ("Voyager") uses controls the plain
form adapter above did not recognize. This adapter now handles them, still
without ever transmitting anything unreviewed:

- An unsupported optional control (for example a plain number input, such as
  an education end-year field) is quietly dropped instead of rejecting the
  whole form. A *required* control of an unsupported type still stops the run
  with a needs-attention error, exactly as before.
- Greenhouse's custom country and yes/no pickers are `input[role=combobox]`
  widgets, not native `<select>` elements. The adapter opens each one just
  long enough to read its option list from the listbox it exposes (`role`
  `option`, wherever in the DOM it is mounted), then closes it again before
  any value is chosen. Filling one clicks the matching option directly; it
  never types free text into the control and hopes the employer's script
  interprets it as a selection.
- React combobox/autosize widgets leave behind decoy inputs (an off-screen
  measuring input, a search proxy pulled out of tab order) that have no
  accessible label. These are identified by `aria-hidden="true"` or
  `tabindex="-1"` and are filtered out before they ever reach validation, so
  they no longer force the whole form to stop. A genuinely required control
  that has no accessible label still stops the run; the adapter does not
  guess what it is asking.
- A required "select all that apply" checkbox group (for example, which
  security clearances a candidate holds) is read as a single field of type
  `checkbox-group`, not as independent checkboxes. Its answer is a list of
  the option values to check; anything not listed is explicitly unchecked
  rather than left in whatever state the employer defaulted it to.

The private profile (`profiles/apply_profile.json` /
`JOB_APPLY_PROFILE_JSON`) gained matching identity fields — `preferred_name`,
`legal_name`, `country`, `city`, `state`, `zip` — alongside the existing
`first_name`/`last_name`/`email`/`phone`/`location`. `preferred_name` falls
back to `first_name` and `legal_name` falls back to the synthesized full name
when left blank, so most owners only need to add the fields their target
employers actually split out separately. Country, city, state and zip are
matched by contact rules the same way `location` already was, so a Greenhouse
combobox asking for "Country" is filled without any explicit `questions`
entry. Authorization, sponsorship, clearance status and clearance level
remain sensitive and are answered only from an explicit `questions` entry, as
before; a checkbox-group question's `value` is a list of the option label(s)
to check (an empty list explicitly checks nothing). `load_profile()` now
validates the profile's shape at load time — a malformed `identity`/`links`
section or a `questions` entry with the wrong value type raises immediately
instead of silently leaving every affected answer blank.

`python -m unittest test_jobwatch_browser_runtime.GreenhouseDryRunTests -v`
(with `JOB_APPLY_BROWSER_TESTS=1` and Chromium installed) exercises the full
inspect → prefill → review path against a fixture carrying all of the above
controls together, and asserts the form reaches a complete review (nothing
missing) with zero submissions and zero transmitted values. It is a synthetic
fixture standing in for the real Voyager markup, not a live run against an
actual employer posting — run the equivalent flow against a real
`job-boards.greenhouse.io` posting in no-submit mode (stop before **Approve &
submit**) before trusting this against production Voyager forms.

## Phase 4.2: Ashby

`jobs.ashbyhq.com` joins the adapter alongside Greenhouse, Subway and
Fountain. Ashby was added by allow-listing its host and declaring its own
upload destination (`.ashbyhq.com`) in `REQUESTABLE` — no field-parsing code
changed. The combobox, checkbox-group, decoy-input-filtering and
unsupported-optional-field handling built for Greenhouse Voyager are generic
(they key off ARIA roles and structure, not Greenhouse-specific markup), so
they apply to Ashby's own custom controls unmodified; a fixture using
deliberately different markup, wording and structure than every Greenhouse
fixture proves this in `test_jobwatch_browser_runtime.AshbyDryRunTests`.

What is genuinely platform-specific, and unverified against a live posting:
Ashby's exact submit-button wording and confirmation-page copy. `NEXT_BUTTONS`
is intentionally left unchanged (Greenhouse-verified strings only) rather than
guessing at Ashby's — an unrecognized button still stops the run safely with
needs-attention before anything is filled or clicked. `RECEIPT_PHRASES` was
widened with plausible Ashby wording; unlike the button list, a miss here
carries no new risk (an unmatched confirmation just leaves a real submission
in the safer `uncertain` state instead of being misrecorded as a receipt).
As with Voyager, run a real `jobs.ashbyhq.com` posting through no-submit mode
before trusting this in production, and correct `NEXT_BUTTONS`/
`RECEIPT_PHRASES` for Ashby if what you see differs.

## Phase 4.3: ATS-wide routing

Application destinations now pass through one registry instead of duplicated
host dictionaries. The registry recognizes direct Greenhouse, Ashby, Fountain,
Subway, Lever, SmartRecruiters, Workday tenant, iCIMS tenant, and BambooHR tenant
URLs. Tenant suffix matching is dot-boundary-safe, HTTPS-only, rejects embedded
credentials and custom ports, and never permits an arbitrary employer host.

LinkedIn is registered as a source board, not a transmitting ATS. A LinkedIn
**Apply** posting may be inspected only long enough to follow its external Apply
link into one of the registered direct ATS families. LinkedIn **Easy Apply**,
authentication, and any external destination outside the registry stop with a
needs-attention card; the browser does not sign in or bypass verification.

For Workday, Lever, SmartRecruiters, iCIMS, and BambooHR, inspection may perform
one unambiguous, navigation-only Apply action when the job page precedes the
form. It never fills a field during that entry step. The generic accessible-form
snapshot then creates the same exact, version-bound Discord review packet used
by Greenhouse and Ashby. Login, verification, unknown controls, multiple Apply
actions, or unexpected destinations still stop safely.

These added families require platform-specific live inspection fixtures before
they may join `TRANSMIT_READY`. Until then, the production gate cannot transmit
to them even if an environment value is mistyped or over-broad.

## Phase 4: one-click auto-apply

The draft card now carries **Approve & auto-apply** alongside **Approve resume
only**. Auto-apply locks the resume exactly as before and additionally records
that the whole form run is pre-authorized, so a strong match can be filled and
submitted without a second click. **Approve resume only** keeps the Phase 3
behaviour: the form is still prefilled, but every step waits for you.

Answers come from a private profile, not from inference. Copy
`profiles/apply_profile.example.json` to `profiles/apply_profile.json` (gitignored)
and fill in the contact details you are willing to send employers. Because that
file is deliberately never committed, a host that deploys from Git will not have
it: there, paste the same JSON into the `JOB_APPLY_PROFILE_JSON` secret
environment variable instead, which takes precedence over the file. Contact rules
cover names, email, phone, location, LinkedIn, GitHub and portfolio links, and a
resume control receives the locked approved resume. Sponsorship, work
authorization, clearance, demographics, consent, pay and availability questions
are matched **only** against explicit `questions` entries you write yourself; an
unmatched question stays blank, is counted on the review card and still needs
**Edit answer**. A select answer must resolve to one of the employer's own
options, and a checkbox is never ticked unless you named that question.

An auto-apply step runs unattended only when the plan has no missing required
answers **and** the match score is at or above `JOB_APPLY_AUTO_SUBMIT_MIN_SCORE`
(default 90). Below that threshold, or with any required answer the profile does
not cover, it falls back to the ordinary review card with the answers already
filled in. Every automatic approval is written to the audit trail as
`form_auto_approve`, and prefilled values are revalidated against the field type
and option list before they are stored, so a bad profile value is dropped rather
than transmitted. All the Phase 3 stop conditions still apply unchanged: a
changed form, newly revealed questions, login, human verification or an ambiguous
outcome halts the run regardless of the threshold.

`job-boards.greenhouse.io` and `boards.greenhouse.io` join the adapter. Each
platform now declares the hosts it may request while the form is open, so
Greenhouse attachment uploads reach its own storage and nothing else. The
snapshot also keeps a hidden `input[type=file]`, since a resume control is
routinely hidden behind a styled Attach button, and falls back to
`aria-labelledby`, a wrapping label, or a file control's own name when there is
no ordinary label. This remains a per-platform adapter. Additional registered
ATS families use the same accessible-control inspection only where their markup
satisfies the strict snapshot contract; unknown or incompatible forms stop with
a needs-attention card. See Phase 4.3.

## Phase 3: opt-in employer form workflow

The bot now includes a separate durable form queue, per-step owner approvals,
Discord answer-entry modals, and an experimental Fountain/Subway browser adapter.
This stage is disabled by default. The existing Python Render service does not
install a browser. Do not enable it until Chromium and its system dependencies
are available. No production application submission has been validated yet.

When enabled, both existing and new `resume_approved` records are queued once.
The bot inspects the employer form, enters only the answers your private profile
supplies (see Phase 4) and posts `form-review.json` in the private approval
channel. Use **Edit answer** with the numbered fields for whatever is left. Checkbox values are `true` or `false`; select fields use
one of the listed option values. Resume upload fields accept
`approved_resume.pdf` or `approved_resume.docx`, referring only to the locked
resume bytes. Unknown personal, legal, demographic and consent answers are not
inferred. Each edit creates a fresh review version and invalidates old buttons.

**Approve & continue** authorizes the exact shown answers and selected uploads
for that employer step, which may create an applicant record. It is not final
application approval. **Approve & submit** appears only for a recognized final
submission button. The browser rechecks the form before filling, and rechecks
for conditional questions before clicking. Any changed controls require further
review; the adapter never fabricates hidden questions or ignores new fields.

The first adapter supports direct `careers.subway.com` and
`us-3.fountain.com` URLs. Aggregator links, other ATS platforms, unfamiliar field
types/buttons, login, or human-verification challenges stop with a needs-attention
card. Use **Set employer URL / reinspect** to provide the exact employer posting
if the scanner supplied an aggregator URL. This is not a generic all-ATS adapter.

If a transmitting step crashes or its result is ambiguous, it becomes
`uncertain`; there is deliberately no automatic retry or reset button. Reconcile
the employer record before making another attempt. A `submitted` state requires
a newly displayed confirmation phrase after an approved final submit action;
the private receipt stores the page URL, confirmation text, time and version.
Fountain's later steps and confirmation still require a controlled live test.

### Browser runtime setup

`Dockerfile.jobapply` remains the container and CI reference. The existing Render
service cannot change runtime after creation, so `render.yaml` preserves its Python
runtime and installs `requirements-browser.txt` plus Chromium during the native build. Render's
native builder cannot grant the root access requested by Playwright's `--with-deps` flag, so system dependencies must come from the managed runtime. `PLAYWRIGHT_BROWSERS_PATH=0` uses Playwright's hermetic installation mode so Chromium is stored inside the deployed Python environment instead of Render's build-only cache. This keeps the current service, bot token,
and persistent disk. Form execution remains disabled during the first browser-
capable deployment. Run a single bot process and retain the persistent disk.

Set `JOB_APPLY_FORMS_ENABLED=true` after browser setup to enable inspection.
Keep `JOB_APPLY_TRANSMIT_ENABLED=false` and `JOB_APPLY_TRANSMIT_ATS` empty
until inspection-only validation is complete. When transmission is deliberately
enabled, list only the ATS identifiers approved for that deployment.
`JOB_APPLY_PROFILE` optionally overrides the answer profile path and
`JOB_APPLY_AUTO_SUBMIT_MIN_SCORE` the unattended-submit threshold.
`JOB_APPLY_BROWSER_DATA` optionally overrides the private profile directory,
which defaults to `browser/` beside the SQLite database. Profiles contain employer
cookies and must never be committed or shared. Include them in private storage
planning; each application has an isolated profile. Resource usage with Chromium
on the 512 MB Render plan is unverified; test before production activation.

Tests: `python -m unittest discover -p 'test_jobwatch*.py' -v`.
Form regressions cover ownership, missing answers, stale approvals, changed
resumes/forms, one-time execution, restart uncertainty, receipts, destination
validation and successive step approval. Browser transmissions are mocked.

## Existing resume pipeline and legacy packet API

Implemented: optional 70%+ intake, blocker exclusion, persistent retry outbox,
private service database, Discord queue/review cards, owner-only version-bound
Apply / Request changes / Skip buttons, restart recovery, and an audit trail.

Phase 2A adds the supplied versioned Claude career skill, evidence-bound draft
validation, one-page PDF and DOCX generation, bounded retries, and prepared-draft
Discord delivery. Phase 2B adds safe API error details, owner-only manual retry,
resume approval, and revision requests that are carried into the next evidence-
bound Claude draft. Preparation remains opt-in and disabled until its API
settings are configured.

The legacy `Apply` packet API still records `approved_waiting_adapter` and does
not submit. Phase 3 uses its own queue and version-bound form review controls.
An installed worker is not evidence of a successful employer application.

## Deployment

Run one long-lived Python 3.12 service from this repository, separate from the
short-lived GitHub Actions scanner. Install `requirements-apply.txt`, then run
`python jobapply_service.py`. Expose port 8080 through an HTTPS reverse proxy;
only `/candidates` needs to be reachable. Use a persistent private volume for
`JOB_APPLY_DB`, for example `/data/applications.sqlite3`. Back it up privately.
Do not commit that database, generated resumes, credentials or answer packets.

Create a Discord application with a bot, install it in your server with View
Channel, Send Messages, Embed Links, Attach Files and Read Message History in
private #job-apply. No privileged message-content intent is needed. Set:

- `DISCORD_BOT_TOKEN`: bot token (not a webhook).
- `JOB_APPLY_CHANNEL_ID`: numeric #job-apply channel ID.
- `JOB_APPLY_OWNER_ID`: numeric Discord user ID of the person authorized to approve Cleo's applications, not the server ID.
- `JOB_APPLY_INGEST_TOKEN`: random secret of at least 32 characters.
- `JOB_APPLY_DB`: absolute private persistent SQLite path.
- `PORT`: optional; defaults to 8080.
- `JOB_APPLY_PREPARE_ENABLED`: set to `true` only after the API settings below are present.
- `ANTHROPIC_API_KEY`: Anthropic API key. A Claude subscription is not an API key.
- `ANTHROPIC_MODEL`: an Anthropic Messages API model ID chosen by the account owner.
- `JOB_APPLY_SKILL_PATH`: optional; defaults to the versioned skill in `skills/`.
- `JOB_APPLY_FORMS_ENABLED`: set `true` after Chromium is healthy to allow inspection.
- `JOB_APPLY_TRANSMIT_ENABLED`: independent hard gate; keep `false` during validation.
- `JOB_APPLY_TRANSMIT_ATS`: comma-separated ATS transmission allowlist; keep empty during validation.
- `JOB_APPLY_BROWSER_DATA`: persistent browser-profile path; use `/data/browser` on Render.
- `JOB_APPLY_AUTO_SUBMIT_MIN_SCORE`: minimum unattended score; defaults to `90`.
- `JOB_APPLY_PROFILE_JSON`: private owner-supplied contact and explicit answer profile.

In GitHub Actions secrets, set `JOB_APPLY_SERVICE_URL` (HTTPS service origin)
and the same `JOB_APPLY_INGEST_TOKEN`. Leave both absent to disable integration.
Normal scanning collects candidates once enabled. Intake is idempotent by the
existing JobWatch fingerprint. Delivery failures stay in the scanner's database
and retry on later runs, even after the ordinary job alert was delivered.
The outbox stores job postings, not personal application answers or resumes.

## Skill adapter contract

When preparation is disabled, cards say `Waiting for the Claude career skill`.
When enabled, one candidate per loop is prepared through the supplied skill. The
worker validates the mandatory evidence audit and evidence IDs, rejects internal
references and unsupported claims, and emits a PDF, DOCX, and JSON evidence packet.

The first generated artifact is intentionally `draft_ready`. It can be approved
as a resume, revised, or skipped, but it cannot be approved as an application.
Resume approval records `resume_approved` and locks the exact artifact digest.
Request changes opens an owner-only modal, stores the revision request privately,
and creates a newly hashed draft. A future form adapter must inspect the employer
form, complete required fields, and call `Store.prepare(...)`. The packet must
include:

- `job_url`: exact queued URL.
- `skill_version`: version of the supplied skill.
- `evidence`: map of evidence IDs to source references.
- `resume_claims`: list of `{value, evidence_ids}` records.
- `answers`: map from form field identifiers to `{value, evidence_ids}` records.
- `form_complete`: true only after required employer fields were inspected.
- `missing_fields`: empty only when all required information is resolved.

The future adapter must verify references against the actual evidence and check
that listed claims match the PDF. The foundation checks structure/references,
not semantic truth or actual form completeness. User-confirmed personal answers
may reference an explicit user-provided profile source. Never infer sponsorship,
legal attestations, demographic answers, or other missing personal information.

The bot attaches the PDF and full JSON answers/evidence packet to a new review
card. Approval binds the exact PDF bytes and packet using SHA-256. Old buttons,
other users, and repeated clicks are rejected. A requested revision invalidates
the old card and requires preparation plus a new approval. Approved packets
cannot be silently edited. Bot restarts restore outstanding review buttons.
If a crash occurs after sending but before saving the message ID, a duplicate
card can appear; database decisions still prevent duplicate approvals.

## Future submission adapter

Do not interpret `approved_waiting_adapter` as submitted. Before connecting a
submission worker, add atomic claim/lease handling, exact digest verification,
form-schema revalidation, employer-specific duplicate checks, receipt storage,
and uncertain-outcome reconciliation. Never retry an ambiguous submission
blindly. New required questions or changed payloads require a new review.
CAPTCHA/login challenges should return a user-action-needed state.

## Phase 2A safety boundaries

- The job description is treated as untrusted data, not instructions.
- A job description under 200 characters is not sent for tailoring.
- Automatic preparation is disabled by default and limited to one candidate per loop.
- Failed preparation is retried at most three times with a 30-minute delay.
- The worker cannot mark an uninspected employer form complete.
- A draft has no legacy Apply button. Approve resume only locks the reviewed
  resume version and cannot transition to application approval. Approve &
  auto-apply additionally pre-authorizes the Phase 3 form run, which still
  refuses to invent an answer, submit below the score threshold, or continue
  through a changed form.
- The exact skill file, PDF, DOCX, evidence packet, and approval version are hashed.
- Employer transmission remains fail-closed unless both transmission settings explicitly allow it.

## Checks and smoke test

`python -m unittest discover -p 'test_jobwatch_apply.py' -v`

After deployment, run the ordinary scanner. A new eligible match should appear
as waiting for the skill and then receive a PDF, DOCX, and evidence packet.
Verify Approve resume, Request changes, and Skip with the configured owner. A
failed preparation should expose a safe error and Retry preparation control.
Re-run without duplicating the candidate. Keep employer transmission disabled
until live inspection-only validation succeeds.
