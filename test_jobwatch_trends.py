from dataclasses import dataclass, field
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jobwatch_trends as trends


@dataclass
class FakeJob:
    key: str
    fit: dict = field(default_factory=dict)

    def fingerprint(self):
        return self.key


class JobTrendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp_dir.name) / "jobs.db")
        self.now = datetime(2026, 9, 3, 15, 7, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ingest_deduplicates_and_classifies_thresholds(self):
        jobs = [
            FakeJob("high", {"score": 70}),
            FakeJob("medium-low", {"score": 50}),
            FakeJob("medium-high", {"score": 69.99}),
            FakeJob("low", {"score": 49}),
            FakeJob("unknown", {"score": None}),
        ]
        self.assertEqual(trends.ingest(jobs, self.db, self.now), 5)
        self.assertEqual(trends.ingest(jobs, self.db, self.now), 0)

        con = trends._db(self.db)
        candidate = trends._candidate(con, self.now)
        con.close()
        self.assertEqual(candidate["total_jobs"], 5)
        self.assertEqual(candidate["high_priority"], 1)
        self.assertEqual(candidate["medium_priority"], 2)

    @patch.object(trends, "post_chart", return_value=False)
    def test_failed_post_keeps_snapshot_pending(self, _post):
        trends.ingest([FakeJob("one", {"score": 88})], self.db, self.now)
        self.assertFalse(trends.maybe_post("https://example.test", self.db, now=self.now))
        con = sqlite3.connect(self.db)
        count = con.execute("SELECT COUNT(*) FROM job_trend_snapshots").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    @patch.object(trends, "post_chart", return_value=True)
    def test_success_stores_history_and_prevents_duplicate_day(self, _post):
        trends.ingest([FakeJob("one", {"score": 88})], self.db, self.now)
        self.assertTrue(trends.maybe_post("https://example.test", self.db, now=self.now))
        self.assertFalse(trends.maybe_post("https://example.test", self.db, now=self.now))
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT total_jobs, high_priority, medium_priority FROM job_trend_snapshots"
        ).fetchone()
        con.close()
        self.assertEqual(row, (1, 1, 0))

    def test_chart_is_png(self):
        rows = [{
            "period_start": "2026-09-01T00:00:00+00:00",
            "period_end": "2026-09-03T15:07:00+00:00",
            "total_jobs": 14,
            "high_priority": 4,
            "medium_priority": 6,
        }]
        self.assertTrue(trends.render_chart(rows).startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()

