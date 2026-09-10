import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import jobwatch_pipeline as jp


class ClassifyTests(unittest.TestCase):
    def test_status_interview_is_success(self):
        self.assertEqual(jp.classify({"Status": "interview"}), "success")

    def test_yes_columns_are_success_even_with_trailing_header_space(self):
        self.assertEqual(jp.classify({"Status": "sent", "Call? ": "Yes"}), "success")
        self.assertEqual(jp.classify({"Interview?": "yes"}), "success")
        self.assertEqual(jp.classify({"Offer?": "Y"}), "success")

    def test_reached_then_rejected_still_counts_as_success(self):
        self.assertEqual(jp.classify({"Status": "rejection", "Interview?": "Yes"}), "success")

    def test_rejection_before_contact_is_miss(self):
        self.assertEqual(jp.classify({"Status": "rejection"}), "miss")
        self.assertEqual(jp.classify({"Status": "ghosted"}), "miss")

    def test_sent_or_blank_is_pending(self):
        self.assertEqual(jp.classify({"Status": "sent"}), "pending")
        self.assertEqual(jp.classify({}), "pending")
        self.assertEqual(jp.classify({"Status": "in review"}), "pending")


class CategoryTests(unittest.TestCase):
    def test_explicit_category_column_wins(self):
        self.assertEqual(
            jp._category({"Position": "totally ambiguous", "Category": "Human Resources"}),
            "Human Resources",
        )

    def test_strict_match(self):
        self.assertEqual(jp._category({"Position": "Patient Access Representative"}), "Patient Access")

    def test_fallback_handles_typos_and_loose_titles(self):
        self.assertEqual(jp._category({"Position": "Healthcare Administratior"}), "Health Administration")
        self.assertEqual(
            jp._category({"Position": "Administrative Cooridnator"}),
            "Administrative Operations",
        )
        self.assertEqual(
            jp._category({"Position": "HR Operations Coordinator"}),
            "Human Resources",
        )

    def test_fallback_uses_industry_and_role_columns(self):
        self.assertEqual(
            jp._category({"Position": "Coordinator", "Industry": "Public Health"}),
            "Public Health Programs",
        )

    def test_genuinely_unknown_stays_none(self):
        self.assertIsNone(jp._category({"Position": "Role not specified in email"}))


class RollupTests(unittest.TestCase):
    def test_rate_math_and_totals(self):
        rows = [{"Position": "HR Coordinator", "Status": s} for s in
                ("interview", "interview", "interview", "rejection")]
        roll = jp.rollup(rows)
        bucket = roll["categories"]["Human Resources"]
        self.assertEqual(bucket["rate"], 75.0)
        self.assertEqual((roll["rows_total"], roll["reached_total"]), (4, 3))

    def test_min_sample_gate(self):
        rows = [{"Position": "HR Coordinator", "Status": "interview"},
                {"Position": "HR Coordinator", "Status": "rejection"}]
        self.assertIsNone(jp.rollup(rows)["categories"]["Human Resources"]["rate"])

    def test_pending_excluded_from_denominator(self):
        rows = [{"Position": "Patient Access Representative", "Status": s} for s in
                ("interview", "rejection", "rejection", "sent", "sent")]
        bucket = jp.rollup(rows)["categories"]["Patient Access"]
        self.assertEqual(bucket["rate"], 33.3)   # 1 / (1 + 2), the two 'sent' ignored
        self.assertEqual(bucket["pending"], 2)

    def test_unmapped_rows_go_to_unclassified(self):
        roll = jp.rollup([{"Position": "Underwater Welder", "Status": "sent"}])
        self.assertEqual(roll["unclassified"]["applied"], 1)
        self.assertEqual(roll["rows_total"], 1)

    def test_blank_trailing_rows_skipped(self):
        roll = jp.rollup([{"Position": "", "Company": "", "Status": ""}])
        self.assertEqual(roll["rows_total"], 0)


class FetchTests(unittest.TestCase):
    @staticmethod
    def _resp(json_value=None, json_exc=None, text=""):
        r = Mock()
        r.raise_for_status.return_value = None
        r.text = text
        if json_exc is not None:
            r.json.side_effect = json_exc
        else:
            r.json.return_value = json_value
        return r

    def test_list_payload_passes_through(self):
        rows = [{"Position": "HR Coordinator"}]
        with patch("jobwatch_pipeline.requests.get", return_value=self._resp(rows)):
            self.assertEqual(jp.fetch_rows("https://x", "t"), rows)

    def test_error_object_rejected(self):
        with patch("jobwatch_pipeline.requests.get", return_value=self._resp({"error": "boom"})):
            with self.assertRaises(RuntimeError):
                jp.fetch_rows("https://x", "t")

    def test_non_json_response_rejected(self):
        with patch("jobwatch_pipeline.requests.get",
                   return_value=self._resp(json_exc=ValueError, text="<html>login</html>")):
            with self.assertRaises(RuntimeError):
                jp.fetch_rows("https://x", "t")

    def test_missing_config_raises(self):
        with self.assertRaises(RuntimeError):
            jp.fetch_rows("", "")


class SnapshotTests(unittest.TestCase):
    def test_store_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "j.db")
            roll = jp.rollup([{"Position": "HR Coordinator", "Status": "interview"}])
            jp.store_snapshot(db, roll)
            jp.store_snapshot(db, roll)  # same UTC day → upsert, one row
            loaded = jp.load_traction(db)
            self.assertEqual(loaded["rows_total"], 1)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM pipeline_snapshot").fetchone()[0], 1)

    def test_load_traction_empty_db_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(jp.load_traction(str(Path(d) / "fresh.db")))


if __name__ == "__main__":
    unittest.main()
