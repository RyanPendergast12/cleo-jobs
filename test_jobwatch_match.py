import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import jobwatch as jw
import jobwatch_match as m


def job(**kwargs):
    base = {
        "source": "test",
        "title": "Patient Access Representative",
        "company": "Example Health",
        "url": "https://example.com/job",
        "description": "",
        "location": "",
        "work_mode": "",
        "remote": False,
        "salary": "",
    }
    base.update(kwargs)
    return jw.Job(**base)


class CleoMatchTests(unittest.TestCase):
    def setUp(self):
        self.profile = m.load_profile()

    def test_profile_is_cleo_specific(self):
        self.assertEqual(self.profile["bachelors_field"], "Public Health")
        self.assertIn("Phreesia", self.profile["skills"])
        self.assertNotIn("Splunk", self.profile["skills"])

    def test_professional_years_excludes_internships_and_overlap(self):
        self.assertAlmostEqual(m.professional_years(self.profile, date(2026, 9, 9)), 12 / 12)

    def test_required_experience_reads_numeric_and_word_floors(self):
        self.assertEqual(m.required_experience_years("Requires 2+ years of healthcare experience."), 2)
        self.assertEqual(m.required_experience_years("Three years of administrative experience required."), 3)

    def test_required_experience_ignores_preferred_floor(self):
        text = "One year of experience required. Three years of experience preferred."
        self.assertEqual(m.required_experience_years(text), 1)

    def test_production_skills_match(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Patient scheduling, insurance verification, patient intake and Phreesia are required.",
        ), self.profile)
        self.assertEqual(result["requirement_score"], 100)
        self.assertTrue({"Patient scheduling", "Insurance verification", "Patient intake", "Phreesia"} <= set(result["matched"]))

    def test_undocumented_ehr_is_a_gap(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Patient scheduling and Epic electronic health record experience are required.",
        ), self.profile)
        self.assertIn("Patient scheduling", result["matched"])
        # EHR is tracked in readiness analytics but is not claimed in the evidence profile.
        self.assertNotIn("EHR", result["matched"])

    def test_required_cma_is_a_hard_blocker(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Patient scheduling is required. Certified Medical Assistant certification is required.",
        ), self.profile)
        self.assertEqual(result["decision"], "disqualified")
        self.assertEqual(result["score"], 0)
        self.assertTrue(any("Certified Medical Assistant" in note for note in result["eligibility"]))

    def test_required_bls_is_a_hard_blocker(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Patient intake is required. BLS certification is required.",
        ), self.profile)
        self.assertEqual(result["score"], 0)
        self.assertTrue(any("CPR/BLS" in note for note in result["eligibility"]))

    def test_preferred_certification_is_not_a_hard_blocker(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Patient scheduling is required. BLS certification is preferred.",
        ), self.profile)
        self.assertFalse(any("BLOCKER:" in note for note in result["eligibility"]))

    def test_required_nursing_license_is_a_hard_blocker(self):
        result = m.score_job(job(
            title="Patient Care Coordinator",
            work_mode="Remote",
            description="An active registered nurse license is required. Patient communication is required.",
        ), self.profile)
        self.assertEqual(result["score"], 0)
        self.assertTrue(any("clinical license" in note for note in result["eligibility"]))

    def test_bachelors_requirement_is_satisfied(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Bachelor's degree required. Community outreach and resource coordination required.",
        ), self.profile)
        self.assertIn("Bachelor's degree", result["matched"])

    def test_masters_requirement_is_not_satisfied(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Master's degree required. Community outreach and resource coordination required.",
        ), self.profile)
        self.assertIn("Completed master's degree", result["gaps"])
        self.assertLess(result["score"], 70)

    def test_las_vegas_local_is_preferred(self):
        value, note = m.location_preference(job(work_mode="Hybrid", location="Las Vegas, NV"), self.profile)
        self.assertEqual(value, .85)
        self.assertIn("Preferred", note)

    def test_remote_is_preferred(self):
        self.assertEqual(m.location_preference(job(work_mode="Remote"), self.profile)[0], 1.0)

    def test_outside_las_vegas_is_blocked(self):
        value, note = m.location_preference(job(work_mode="Onsite", location="Boston, MA"), self.profile)
        self.assertEqual(value, 0.0)
        self.assertIn("BLOCKER:", note)

    def test_unknown_work_mode_requires_evidence(self):
        result = m.score_job(job(
            description="Patient scheduling, insurance verification and patient intake are required.",
        ), self.profile)
        self.assertEqual(result["decision"], "needs_evidence")
        self.assertLessEqual(result["score"], 69)

    def test_no_salary_floor_is_assumed(self):
        value, note = m.salary_preference(job(salary="$35,000-$40,000 per year"), self.profile)
        self.assertIsNone(value)
        self.assertIn("No salary floor configured", note)

    def test_configured_salary_floor_is_enforced(self):
        profile = json.loads(json.dumps(self.profile))
        profile["preferences"]["minimum_salary_usd"] = 45000
        value, note = m.salary_preference(job(salary="$35,000-$40,000 per year"), profile)
        self.assertEqual(value, 0.0)
        self.assertIn("BLOCKER:", note)

    def test_sparse_posting_cannot_receive_high_confidence_score(self):
        result = m.score_job(job(work_mode="Remote", description="Patient scheduling required."), self.profile)
        self.assertLessEqual(result["score"], 69)
        self.assertEqual(result["confidence"], "limited")

    def test_score_equals_overall_when_not_blocked(self):
        result = m.score_job(job(
            work_mode="Remote",
            description=(
                "Patient scheduling, patient intake, insurance verification, referral verification, "
                "patient communication and Phreesia are required."
            ),
        ), self.profile)
        self.assertEqual(result["score"], result["overall_match"])
        self.assertGreaterEqual(result["score"], 70)

    def test_large_experience_floor_stays_below_apply_threshold(self):
        result = m.score_job(job(
            work_mode="Remote",
            description=(
                "Requires 8 years of healthcare experience. Patient scheduling, patient intake, "
                "insurance verification, referral verification and Phreesia are required."
            ),
        ), self.profile)
        self.assertLess(result["score"], 70)
        self.assertIn("8+ years experience", result["partial"])

    def test_online_enrichment_blocks_outside_location(self):
        initial = job(
            work_mode="Remote",
            description="Patient scheduling, patient intake, insurance verification and Phreesia are required.",
        )
        initial.fit = m.score_job(initial, self.profile)
        result = m.rescore_after_online_enrichment(
            initial,
            "This is a hybrid role in Boston, MA. Patient scheduling and insurance verification are required.",
            self.profile,
        )
        self.assertEqual(result["post_enrichment"]["status"], "blocked")
        self.assertEqual(result["score"], 0)

    def test_online_enrichment_blocks_required_license(self):
        initial = job(
            work_mode="Remote",
            description="Patient scheduling, patient intake, insurance verification and Phreesia are required.",
        )
        initial.fit = m.score_job(initial, self.profile)
        result = m.rescore_after_online_enrichment(
            initial,
            "Remote in the United States. An active registered nurse license is required.",
            self.profile,
        )
        self.assertEqual(result["post_enrichment"]["status"], "blocked")
        self.assertTrue(any("clinical license" in reason for reason in result["post_enrichment"]["reasons"]))

    def test_online_enrichment_can_remain_eligible(self):
        initial = job(
            work_mode="Remote",
            description=(
                "Patient scheduling, patient intake, insurance verification, referral verification, "
                "patient communication and Phreesia are required."
            ),
        )
        initial.fit = m.score_job(initial, self.profile)
        result = m.rescore_after_online_enrichment(
            initial,
            "Remote in the United States. One year of healthcare experience is required. "
            "Patient scheduling, patient intake, insurance verification, referral verification, "
            "patient communication and Phreesia are required.",
            self.profile,
        )
        self.assertEqual(result["post_enrichment"]["status"], "eligible")

    def test_fit_fields_use_role_match_language(self):
        result = m.score_job(job(
            work_mode="Remote",
            description="Patient scheduling, patient intake and insurance verification are required.",
        ), self.profile)
        fields = m.fit_fields(result)
        dashboard = next(field for field in fields if field["name"] == "Match score")
        self.assertIn("Role Match", dashboard["value"])


class TargetFilterTests(unittest.TestCase):
    def test_target_titles_are_included(self):
        for title in (
            "Patient Access Representative",
            "HR Coordinator",
            "Medical Office Assistant",
            "Public Health Program Coordinator",
            "Administrative Coordinator",
        ):
            with self.subTest(title=title):
                self.assertTrue(jw._matches_wanted(title))

    def test_senior_and_licensed_titles_are_excluded(self):
        for title in (
            "Senior HR Coordinator",
            "Patient Access Manager",
            "Registered Nurse Care Coordinator",
        ):
            with self.subTest(title=title):
                self.assertFalse(jw._matches_wanted(title))


class SourceTests(unittest.TestCase):
    def test_linkedin_preserves_partial_results_on_rate_limit(self):
        ok = Mock(status_code=200, ok=True)
        ok.text = """
        <li><a class="base-card__full-link" href="https://linkedin.com/jobs/view/123">
        <h3>Patient Access Representative</h3></a><h4>Clinic</h4>
        <span class="job-search-card__location">Remote</span></li>
        """
        limited = Mock(status_code=429, ok=False)
        with patch.object(jw, "_get", side_effect=[ok, limited]), patch.object(jw.time, "sleep"):
            original_terms = jw.SEARCH_TERMS
            original_markets = jw.LINKEDIN_SEARCH_MARKETS
            try:
                jw.SEARCH_TERMS = ["patient access"]
                jw.LINKEDIN_SEARCH_MARKETS = (("Las Vegas, NV", ""), ("United States", "2"))
                jobs = jw.src_linkedin()
            finally:
                jw.SEARCH_TERMS = original_terms
                jw.LINKEDIN_SEARCH_MARKETS = original_markets
        self.assertEqual(len(jobs), 1)
        self.assertIn("rate limited", jw.SOURCE_STATUS["linkedin"])


if __name__ == "__main__":
    unittest.main()
