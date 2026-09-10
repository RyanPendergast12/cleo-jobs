import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import re
import unittest
import jobwatch_display as display

# Load pure production functions without importing scraper/network dependencies.
tree = ast.parse(Path(__file__).with_name("jobwatch.py").read_text(encoding="utf-8"))
names = {"Job", "_extract_experience", "to_embed", "_embed_char_count", "_discord_embed_batches", "ghost_score", "_age_days"}
nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
ns = dict(dataclass=dataclass, field=field, display=display, re=re, datetime=datetime,
          timezone=timezone, matching=__import__("jobwatch_match"), workplace=__import__("jobwatch_workplace"), SOURCE_COLOR={}, DISCORD_MAX_EMBEDS=10,
          DISCORD_MAX_EMBED_CHARS=5500, MAX_AGE_DAYS=30, MIN_DESC_CHARS=220,
          REPOST_GHOST_THRESHOLD=2, VAGUE=re.compile(r"\brockstar\b"))
exec(compile(ast.Module(body=nodes, type_ignores=[]), "jobwatch.py", "exec"), ns)
Job = ns["Job"]

def job(**kw):
    return Job(source="test", title=kw.pop("title", "Security Engineer"),
               company="Example", url="https://example.com/jobs/1", **kw)

class DisplayTests(unittest.TestCase):
    def test_staff_in_description_is_not_seniority(self):
        self.assertEqual(display.experience("GRC Analyst", "Support our staff and senior leadership."), "")

    def test_structured_seniority_preserved(self):
        self.assertEqual(display.experience("Analyst", "5 years of experience", "Mid-level"), "Mid-level")

    def test_title_level_and_years(self):
        self.assertEqual(display.experience("Junior Analyst"), "Junior")
        self.assertEqual(display.experience("Analyst", "Requires 3+ years of experience."), "3+ years of experience")

    def test_company_age_is_not_experience(self):
        self.assertEqual(display.experience("Analyst", "Founded 20 years ago."), "")

    def test_technical_remote_terms_are_not_work_arrangements(self):
        for text in ("Protect remote access.", "Operate distributed systems.", "Remote access engineer"):
            with self.subTest(text=text):
                self.assertFalse(display.is_remote(text))
                self.assertEqual(display.work_mode(job(description=text)), "Not specified")

    def test_explicit_remote(self):
        for text in ("Remote", "Remote - US", "This position is remote.", "You can work from home."):
            with self.subTest(text=text):
                self.assertTrue(display.is_remote(text))

    def test_negative_remote(self):
        for text in ("This role is not remote.", "Remote work is not available.", "No remote work."):
            with self.subTest(text=text):
                self.assertFalse(display.is_remote(text))
                self.assertNotEqual(display.work_mode(job(remote=True, description=text)), "Remote")

    def test_hybrid_overrides_remote(self):
        self.assertEqual(display.work_mode(job(remote=True, work_mode="Hybrid")), "Hybrid")
        self.assertEqual(display.work_mode(job(remote=True, description="This is a hybrid role.")), "Hybrid")

    def test_onsite_overrides_search_flag(self):
        self.assertEqual(display.work_mode(job(remote=True, work_mode="Onsite")), "Onsite")

    def test_usd_salary_without_invented_period(self):
        self.assertEqual(display.compensation("USD 93800-120000"), "$93,800–$120,000 USD")
        self.assertEqual(display.compensation("USD 40-55 / hour"), "$40–$55 USD / hour")
        self.assertEqual(display.compensation("EUR 70000-88000"), "EUR 70000-88000")
        self.assertEqual(display.compensation(""), "Not listed")

    def test_summary_skips_company_intro(self):
        summary=display.role_summary("Founded in 2006, Example is a managed services provider. Responsibilities: Monitor security alerts. Requirements: Experience with Splunk and Python.")
        self.assertNotIn("Founded", summary)
        self.assertIn("Monitor security alerts", summary)
        self.assertIn("Splunk", summary)

    def test_html_summary(self):
        self.assertIn("Investigate incidents", display.role_summary("<h2>Responsibilities</h2><ul><li>Investigate incidents and document findings.</li></ul>"))

    def test_no_invented_summary(self):
        self.assertEqual(display.role_summary("We are a leading technology company."), "Open the posting for responsibilities and requirements.")

    def test_clip_word_boundary(self):
        result=display.clip("Monitor security alerts and investigate incidents", 28)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 28)
        self.assertNotIn("invest", result)

    def test_missing_salary_is_neutral(self):
        embed=ns["to_embed"](job(ghost_flags=["no salary"]))
        self.assertNotIn("⚠", embed["title"])
        self.assertIn({"name":"Compensation","value":"Not listed","inline":True}, embed["fields"])
        _, flags, _=ns["ghost_score"](job(), None)
        self.assertNotIn("no salary", flags)

    def test_actual_warning_remains(self):
        self.assertIn("⚠", ns["to_embed"](job(ghost_flags=["stale 45d"]))["title"])

    def test_embed_retains_link_and_linebreak(self):
        embed=ns["to_embed"](job(salary="USD 93800-120000"))
        self.assertEqual(embed["url"], "https://example.com/jobs/1")
        self.assertIn("**Example**\n", embed["description"])

    def test_remote_restriction_excerpt(self):
        self.assertIn("must reside", display.remote_details("You must reside in the United States. Monitor alerts."))

    def test_batch_limits(self):
        jobs=[job(description="Requirements: " + "Python " * 200, ghost_flags=["stale 45d"]) for _ in range(30)]
        batches=list(ns["_discord_embed_batches"](jobs))
        self.assertEqual(sum(map(len,batches)),30)
        for batch in batches:
            self.assertLessEqual(len(batch),10)
            self.assertLessEqual(sum(ns["_embed_char_count"](e) for e in batch),5500)

if __name__ == "__main__":
    unittest.main()


