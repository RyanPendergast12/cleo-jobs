# Cleo Experience-Based Matching

`profiles/experience.json` is the source of truth for automated match scoring and tailored-resume evidence. It contains only facts from Cleo's supplied resume plus explicitly configured preferences.

## Current scope

- Target market: healthcare administration, patient access and medical office operations, HR coordination, administrative operations, and public-health coordination.
- Location: remote roles or onsite/hybrid roles in the Las Vegas area.
- Salary: no minimum has been assumed.
- Credentials: no clinical license, Certified Medical Assistant credential, CPR/BLS certification, or medical coding credential is documented.
- Sensitive eligibility facts: work authorization, sponsorship, driver's license, availability, travel, demographic, and consent answers remain unset.

## Score meaning

The score is a deterministic evidence comparison, not a hiring probability.

- Required skills use full weight; preferred skills use half weight.
- Production evidence earns full credit. Resume-only reported skills receive limited credit.
- Required years are compared against non-internship paid-role months without double-counting overlaps.
- A completed bachelor's requirement is satisfied by Cleo's B.S. in Public Health. Field-specific requirements still require review.
- Required undocumented licenses and certifications are hard blockers.
- Remote work is preferred. Onsite/hybrid work outside approved Las Vegas-area aliases is a hard blocker.
- Unknown salary is shown but does not block while the configured minimum remains zero.
- Sparse descriptions are capped rather than inflated to a confident score.

## Updating evidence

Add only facts Cleo can verify. Preserve the exact organization, title, dates, action, scope, and limitations. Add aliases in `jobwatch_match.py` and `jobwatch_readiness.py` when a new supported skill must be recognized.

The private contact and application-answer profile is `profiles/apply_profile.json`. It is gitignored and should be supplied to Render through `JOB_APPLY_PROFILE_JSON`.

