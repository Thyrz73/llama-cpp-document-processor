"""Keep list polling cheap without caching private snapshots or mutable job state."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import config, jobs
from document_processor.jobs import JobStore


class PublicConfigCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.store.initialize()
        changed = patch.object(app, "store", self.store)
        changed.start()
        self.addCleanup(changed.stop)
        self.client = app.app.test_client()
        self.snapshot = {**config.resolve(app.runtime_defaults()), "system_prompt": "PRIVATE_SYSTEM_PROMPT",
                         "task_templates": {"final": "PRIVATE_TEMPLATE"}, "source_hash": "PRIVATE_HASH"}

    def create(self, number, snapshot=True):
        identifier = f"{number:032x}"
        if snapshot:
            self.store.create_job(identifier, "example.pdf", "Analyze", 1, self.snapshot)
            self.store.update_job(identifier, status="done")
        else:
            with self.store.connect() as con:
                con.execute("INSERT INTO jobs (id, filename, question, status, created_at, updated_at) "
                            "VALUES (?, 'legacy.pdf', 'Analyze', 'done', ?, ?)",
                            (identifier, jobs.utcnow(), jobs.utcnow()))
        return identifier

    def test_warm_lists_neither_select_parse_nor_convert_private_snapshots(self):
        identifier = self.create(1)
        expected = config.public_config(self.snapshot)
        with patch.object(jobs, "public_config", wraps=jobs.public_config) as convert:
            first = self.client.get("/api/jobs").get_json()
            self.assertEqual(convert.call_count, 1)
            statements = []
            with self.store.connect() as con:
                con.set_trace_callback(statements.append)
            try:
                second = self.client.get("/api/jobs").get_json()
            finally:
                with self.store.connect() as con:
                    con.set_trace_callback(None)
            self.assertEqual(convert.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["jobs"][0]["configuration"], expected)
        self.assertEqual(first["jobs"][0]["id"], identifier)
        self.assertFalse(first["has_active_jobs"])
        self.assertFalse(any("snapshot" in statement.lower() for statement in statements))
        self.assertFalse(any(statement.startswith(("UPDATE", "INSERT", "DELETE")) for statement in statements))
        cached = json.dumps(list(self.store._public_configs.values()))
        for private in ("PRIVATE_SYSTEM_PROMPT", "PRIVATE_TEMPLATE", "PRIVATE_HASH", "system_prompt", "task_templates"):
            self.assertNotIn(private, cached)
            self.assertNotIn(private, json.dumps(second))

    def test_cached_controls_are_copied_but_job_progress_is_always_fresh(self):
        identifier = self.create(1)
        first, _ = self.store.list_jobs()
        first[0]["configuration"]["warnings"].append("changed_by_caller")
        first[0]["configuration"]["profile"] = "changed_by_caller"
        self.store.update_job(identifier, status="running", progress_percent=52)
        second, active = self.store.list_jobs()
        self.assertEqual(second[0]["configuration"], config.public_config(self.snapshot))
        self.assertEqual((second[0]["status"], second[0]["progress_percent"]), ("running", 52))
        self.assertTrue(active)

    def test_cache_is_bounded_and_evicted_entries_are_recomputed(self):
        for number in range(1, 5):
            self.create(number)
        with patch.object(jobs, "PUBLIC_CONFIG_CACHE_SIZE", 2), \
             patch.object(jobs, "public_config", wraps=jobs.public_config) as convert:
            self.assertEqual(len(self.store.list_jobs(limit=2)[0]), 2)
            self.assertEqual(convert.call_count, 2)
            self.store.list_jobs(limit=2)
            self.assertEqual(convert.call_count, 2)
            self.assertEqual(len(self.store.list_jobs(limit=4)[0]), 4)
            self.assertEqual(len(self.store._public_configs), 2)
            self.assertEqual(convert.call_count, 4)
            self.store.list_jobs(limit=2)
            self.assertEqual(convert.call_count, 6)
            self.assertEqual(len(self.store._public_configs), 2)

    def test_new_fingerprint_and_startup_migration_invalidate_old_observations(self):
        identifier = self.create(1)
        self.store.list_jobs()
        migrated = {**self.snapshot, "temperature": 0.4, "warnings": ["migrated"]}
        with self.store.connect() as con:
            con.execute("UPDATE job_configs SET snapshot=?, fingerprint=? WHERE job_id=?",
                        (json.dumps(migrated), config.fingerprint(migrated), identifier))
        result, _ = self.store.list_jobs()
        self.assertEqual(result[0]["configuration"], config.public_config(migrated))
        self.store.initialize()
        self.assertFalse(self.store._public_configs)
        self.assertEqual(self.store.list_jobs()[0][0]["configuration"], config.public_config(migrated))

    def test_legacy_snapshot_absence_is_not_cached(self):
        identifier = self.create(1, snapshot=False)
        self.assertNotIn("configuration", self.store.list_jobs()[0][0])
        self.assertFalse(self.store._public_configs)
        self.store.save_config(identifier, self.snapshot)
        self.assertEqual(self.store.list_jobs()[0][0]["configuration"], config.public_config(self.snapshot))

    def test_successful_deletion_invalidates_only_the_deleted_job(self):
        identifier = self.create(1)
        other = self.create(2)
        self.store.list_jobs()
        self.assertIsNone(self.store.delete_job(identifier))
        self.assertFalse(any(key[0] == identifier for key in self.store._public_configs))
        self.assertTrue(any(key[0] == other for key in self.store._public_configs))
        self.assertEqual([job["id"] for job in self.store.list_jobs()[0]], [other])

    def test_active_flag_still_covers_jobs_outside_the_hundred_visible_rows(self):
        oldest = self.create(0, snapshot=False)
        with self.store.connect() as con:
            con.execute("UPDATE jobs SET created_at='2000-01-01', status='running' WHERE id=?", (oldest,))
        for number in range(1, 102):
            self.create(number)
        with patch.object(jobs, "public_config", wraps=jobs.public_config) as convert:
            first = self.client.get("/api/jobs").get_json()
            second = self.client.get("/api/jobs").get_json()
            self.assertEqual(convert.call_count, 100)
        self.assertEqual(first, second)
        self.assertEqual(len(second["jobs"]), 100)
        self.assertTrue(second["has_active_jobs"])
        self.assertNotIn(oldest, [job["id"] for job in second["jobs"]])
        self.store.update_job(oldest, status="failed")
        self.assertFalse(self.client.get("/api/jobs").get_json()["has_active_jobs"])

    def test_concurrent_lists_share_sanitized_cache_entries(self):
        for number in range(1, 7):
            self.create(number)

        def read(_):
            try:
                return self.store.list_jobs()
            finally:
                self.store.close(checkpoint=False)

        with patch.object(jobs, "public_config", wraps=jobs.public_config) as convert, \
             ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(read, range(12)))
        self.assertEqual(convert.call_count, 6)
        self.assertTrue(all(result == results[0] for result in results))


if __name__ == "__main__":
    unittest.main()
