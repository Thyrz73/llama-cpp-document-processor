"""Keep original snapshots immutable and pin durable revisions to retry attempts."""

import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from document_processor import config, database, jobs
from document_processor.errors import ProcessingError
from document_processor.jobs import JobStore

REVISION_TABLES = (
    "job_config_revisions",
    "job_config_selections",
    "job_run_config_revisions",
)


class ConfigurationRevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(Path(temporary.name))
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.job_id = "a" * 32
        self.original = {
            "max_attempts": 8,
            "analysis_tokens": 256,
            "merge_tokens": 512,
            "final_tokens": 1024,
            "warnings": [],
            "system_prompt": "PRIVATE_PROMPT — é",
            "templates": {"final": "PRIVATE_TEMPLATE"},
            "source_hash": "PRIVATE_HASH",
            "historical_extension": {"unchanged": [None, 1]},
        }
        self.store.create_job(self.job_id, "example.pdf", "Analyze", 1, self.original)

    def counts(self):
        with self.store.connect() as con:
            return [
                con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in REVISION_TABLES
            ]

    def revised(self, **changes):
        return {**copy.deepcopy(self.original), "merge_tokens": 2048, **changes}

    def select(self, revision):
        self.store.update_job(self.job_id, status="failed")
        self.assertEqual(
            self.store.requeue(self.job_id, 8, config_revision=revision)[0], "queued"
        )

    def attempt(self):
        recorder = self.store.start_attempt(self.job_id, jobs.utcnow())
        recorder.finish("failed")
        return recorder.run_id

    def test_distinct_revisions_append_without_changing_the_original_or_selection(self):
        with self.store.connect() as con:
            before = tuple(con.execute("SELECT * FROM job_configs").fetchone())
        first, second = self.revised(), self.revised(final_tokens=4096)
        self.assertEqual(self.store.save_config_revision(self.job_id, first), 1)
        self.assertEqual(self.store.save_config_revision(self.job_id, second), 2)
        self.assertEqual(self.store.load_config(self.job_id), self.original)
        self.assertEqual(self.store.load_config(self.job_id, revision=1), first)
        self.assertEqual(self.store.load_config(self.job_id, revision=2), second)
        self.assertEqual(self.counts(), [2, 0, 0])
        with self.store.connect() as con:
            self.assertEqual(
                tuple(con.execute("SELECT * FROM job_configs").fetchone()), before
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_config(self.job_id, second)
        # Returned dictionaries cannot mutate any saved snapshot.
        loaded = self.store.load_config(self.job_id, revision=1)
        loaded["templates"]["final"] = "changed"
        self.assertEqual(self.store.load_config(self.job_id, revision=1), first)

    def test_duplicate_snapshots_reuse_references_including_the_original(self):
        self.assertEqual(self.store.save_config_revision(self.job_id, self.original), 0)
        revised = self.revised()
        self.assertEqual(self.store.save_config_revision(self.job_id, revised), 1)
        reordered = dict(reversed(list(revised.items())))
        self.assertEqual(self.store.save_config_revision(self.job_id, reordered), 1)
        self.select(1)
        self.attempt()
        self.attempt()
        self.assertEqual(self.counts(), [1, 1, 2])
        self.assertEqual(self.store.load_config(self.job_id), revised)

    def test_selection_and_attempts_keep_their_own_configuration_across_restarts(self):
        initial = self.attempt()
        first, second = self.revised(), self.revised(final_tokens=4096)
        self.store.save_config_revision(self.job_id, first)
        self.store.save_config_revision(self.job_id, second)
        self.select(1)
        self.store.close()
        restarted = JobStore(self.store.data_dir)
        self.addCleanup(restarted.close)
        restarted.initialize()
        self.assertEqual(restarted.load_config(self.job_id), first)
        self.assertEqual(restarted.claim_next_job(), self.job_id)
        interrupted = restarted.start_attempt(self.job_id, jobs.utcnow())
        restarted.close()
        restarted.initialize()
        self.assertEqual(restarted.get_job(self.job_id)["status"], "queued")
        self.assertEqual(restarted.load_config(self.job_id), first)
        recovered = restarted.start_attempt(self.job_id, jobs.utcnow())
        recovered.finish("failed")
        self.select(2)
        latest = self.attempt()
        self.assertEqual(restarted.load_config(self.job_id), second)
        self.assertEqual(restarted.load_config(self.job_id, revision=0), self.original)
        for run, expected in (
            (initial, self.original),
            (interrupted.run_id, first),
            (recovered.run_id, first),
            (latest, second),
        ):
            self.assertEqual(restarted.load_attempt_config(self.job_id, run), expected)
        self.assertEqual(
            restarted.metrics(self.job_id)["runs"][2]["status"], "interrupted"
        )

    def test_plain_retries_retain_the_selected_configuration_and_can_select_original(
        self,
    ):
        self.store.save_config_revision(self.job_id, self.revised())
        self.select(1)
        revised_run = self.attempt()
        self.store.update_job(self.job_id, status="cancelled")
        self.assertEqual(self.store.requeue(self.job_id, 0), ("queued", 1))
        self.assertEqual(self.store.load_config(self.job_id), self.revised())
        self.select(0)
        original_run = self.attempt()
        self.assertEqual(self.store.load_config(self.job_id), self.original)
        self.assertEqual(
            self.store.load_attempt_config(self.job_id, revised_run), self.revised()
        )
        self.assertEqual(
            self.store.load_attempt_config(self.job_id, original_run), self.original
        )

    def test_conflicts_and_retry_limits_do_not_change_selection(self):
        self.store.save_config_revision(self.job_id, self.revised())
        self.assertEqual(
            self.store.requeue(self.job_id, 8, config_revision=1), ("conflict", 0)
        )
        self.assertEqual(self.store.load_config(self.job_id), self.original)
        self.select(1)
        self.attempt()
        limited = self.revised(max_attempts=1)
        self.store.save_config_revision(self.job_id, limited)
        self.store.update_job(self.job_id, status="failed")
        self.assertEqual(
            self.store.requeue(self.job_id, 8, config_revision=2), ("limit", 1)
        )
        self.assertEqual(self.store.load_config(self.job_id), self.revised())
        self.assertEqual(self.store.get_job(self.job_id)["status"], "failed")

    def test_missing_foreign_and_invalid_revisions_are_rejected_without_queueing(self):
        other_id = "b" * 32
        self.store.create_job(other_id, "b.pdf", "Analyze", 1, self.original)
        self.store.save_config_revision(other_id, self.revised())
        self.store.update_job(self.job_id, status="failed")
        for revision, error in (
            (1, LookupError),
            (-1, ValueError),
            (True, ValueError),
            ("1", ValueError),
        ):
            with self.subTest(revision=revision), self.assertRaises(error):
                self.store.requeue(self.job_id, 8, config_revision=revision)
        self.assertEqual(self.store.get_job(self.job_id)["status"], "failed")
        with self.assertRaises(LookupError):
            self.store.load_attempt_config(other_id, self.attempt())
        with self.assertRaises(LookupError):
            self.store.save_config_revision("missing", self.revised())

    def test_revision_integrity_failures_are_private_and_cannot_be_selected(self):
        self.store.save_config_revision(self.job_id, self.revised())
        self.store.update_job(self.job_id, status="failed")
        for corrupted in (
            json.dumps(self.revised(merge_tokens=4096)),
            "PRIVATE_INVALID_JSON",
        ):
            with self.subTest(corrupted=corrupted):
                with self.store.connect() as con:
                    con.execute(
                        "UPDATE job_config_revisions SET snapshot=? WHERE job_id=?",
                        (corrupted, self.job_id),
                    )
                for action in (
                    lambda: self.store.load_config(self.job_id, revision=1),
                    lambda: self.store.save_config_revision(
                        self.job_id, self.revised()
                    ),
                    lambda: self.store.requeue(self.job_id, 8, config_revision=1),
                ):
                    with self.assertRaises(ProcessingError) as raised:
                        action()
                    self.assertEqual(raised.exception.code, "config_integrity")
                    self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertEqual(self.store.get_job(self.job_id)["status"], "failed")
        self.assertEqual(self.store.load_config(self.job_id), self.original)

    def test_original_and_pinned_revision_integrity_are_checked_independently(self):
        original_run = self.attempt()
        self.store.save_config_revision(self.job_id, self.revised())
        self.select(1)
        revised_run = self.attempt()
        with self.store.connect() as con:
            con.execute("UPDATE job_configs SET fingerprint='corrupt'")
        with self.assertRaises(ProcessingError):
            self.store.load_attempt_config(self.job_id, original_run)
        with self.assertRaises(ProcessingError):
            self.store.save_config_revision(
                self.job_id, self.revised(final_tokens=4096)
            )
        self.assertEqual(
            self.store.load_attempt_config(self.job_id, revised_run), self.revised()
        )
        with self.store.connect() as con:
            con.execute("UPDATE job_config_revisions SET fingerprint='corrupt'")
        with self.assertRaises(ProcessingError):
            self.store.load_attempt_config(self.job_id, revised_run)

    def test_public_cache_tracks_selection_without_private_or_metadata_fields(self):
        self.store.list_jobs()
        before = self.store.get_job(self.job_id)
        self.store.save_config_revision(self.job_id, self.revised())
        self.assertTrue(self.store._public_configs)  # Appending alone does not select.
        self.select(1)
        self.assertFalse(self.store._public_configs)
        result = self.store.list_jobs()[0][0]
        self.assertEqual(result["configuration"], config.public_config(self.revised()))
        self.assertEqual(set(self.store.get_job(self.job_id)), set(before))
        encoded = json.dumps([result, list(self.store._public_configs.values())])
        for private in (
            "PRIVATE",
            "templates",
            "system_prompt",
            "source_hash",
            "revision",
        ):
            self.assertNotIn(private, encoded)
        self.attempt()
        self.assertEqual(
            set(self.store.metrics(self.job_id)["runs"][0]),
            {
                "id",
                "job_id",
                "started_at",
                "finished_at",
                "status",
                "duration_ms",
                "queue_ms",
                "provenance",
            },
        )

    def test_other_store_cache_observes_committed_selection_and_warm_lists_stay_cheap(
        self,
    ):
        other = JobStore(self.store.data_dir)
        self.addCleanup(other.close)
        other.list_jobs()
        self.store.save_config_revision(self.job_id, self.revised())
        self.select(1)
        self.assertEqual(
            other.list_jobs()[0][0]["configuration"],
            config.public_config(self.revised()),
        )
        statements = []
        with other.connect() as con:
            con.set_trace_callback(statements.append)
        try:
            other.list_jobs()
        finally:
            with other.connect() as con:
                con.set_trace_callback(None)
        self.assertFalse(
            any("snapshot" in statement.lower() for statement in statements)
        )
        self.select(0)
        self.assertEqual(
            other.list_jobs()[0][0]["configuration"],
            config.public_config(self.original),
        )

    def test_legacy_jobs_without_snapshots_keep_default_limits_and_original_fallback(
        self,
    ):
        legacy = "c" * 32
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO jobs (id,filename,question,status,created_at,updated_at) "
                "VALUES (?, 'old.pdf', 'Analyze', 'failed', ?, ?)",
                (legacy, jobs.utcnow(), jobs.utcnow()),
            )
        self.assertIsNone(self.store.load_config(legacy))
        self.assertIsNone(self.store.load_config("missing"))
        self.assertEqual(self.store.requeue(legacy, 0), ("limit", 0))
        self.assertEqual(self.store.requeue(legacy, 1), ("queued", 0))
        recorder = self.store.start_attempt(legacy, jobs.utcnow())
        recorder.finish("failed")
        self.assertIsNone(self.store.load_attempt_config(legacy, recorder.run_id))
        with self.assertRaises(LookupError):
            self.store.save_config_revision(legacy, self.revised())
        self.store.save_config(legacy, self.original)
        self.assertEqual(self.store.load_config(legacy), self.original)
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_concurrent_appends_reuse_identical_snapshots_and_allocate_distinct_revisions(
        self,
    ):
        def save(snapshot):
            try:
                return self.store.save_config_revision(self.job_id, snapshot)
            finally:
                self.store.close(checkpoint=False)

        first, second = self.revised(), self.revised(final_tokens=4096)
        with ThreadPoolExecutor(max_workers=4) as pool:
            revisions = list(pool.map(save, [first, second, first, second] * 3))
        self.assertEqual(set(revisions), {1, 2})
        self.assertEqual(len(set(revisions[::2])), 1)
        self.assertEqual(len(set(revisions[1::2])), 1)
        self.assertEqual(self.counts(), [2, 0, 0])

    def test_attempt_and_its_configuration_reference_commit_atomically(self):
        self.store.save_config_revision(self.job_id, self.revised())
        self.select(1)
        with self.store.connect() as con:
            con.execute(
                "CREATE TRIGGER fail_reference BEFORE INSERT ON job_run_config_revisions "
                "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.start_attempt(self.job_id, jobs.utcnow())
        self.assertEqual(self.store.attempt_count(self.job_id), 0)
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_queue_state_and_revision_selection_roll_back_together(self):
        self.store.save_config_revision(self.job_id, self.revised())
        self.select(1)
        self.store.save_config_revision(self.job_id, self.revised(final_tokens=4096))
        self.store.update_job(self.job_id, status="failed")
        before = self.store.get_job(self.job_id)
        self.store.list_jobs()
        with self.store.connect() as con:
            con.execute(
                "CREATE TRIGGER fail_selection BEFORE INSERT ON job_config_selections "
                "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.requeue(self.job_id, 8, config_revision=2)
        self.assertEqual(self.store.get_job(self.job_id), before)
        self.assertEqual(self.store.load_config(self.job_id), self.revised())
        self.assertTrue(self.store._public_configs)

    def test_foreign_keys_reject_cross_job_references_and_delete_cascades(self):
        self.store.save_config_revision(self.job_id, self.revised())
        self.select(1)
        self.attempt()
        other = "b" * 32
        self.store.create_job(other, "other.pdf", "Analyze", 1, self.original)
        self.store.save_config_revision(other, self.revised())
        recorder = self.store.start_attempt(other, jobs.utcnow())
        recorder.finish("failed")
        for sql, parameters in (
            ("INSERT INTO job_config_selections VALUES (?, ?)", (other, 2)),
            (
                "INSERT INTO job_run_config_revisions VALUES (?, ?, ?)",
                (recorder.run_id, self.job_id, 1),
            ),
            ("DELETE FROM job_config_revisions WHERE job_id=?", (self.job_id,)),
        ):
            with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as con:
                con.execute(sql, parameters)
        self.store.update_job(self.job_id, status="failed")
        self.assertIsNone(self.store.delete_job(self.job_id))
        self.assertEqual(self.counts(), [1, 0, 0])
        with self.store.connect() as con:
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])


class ConfigurationRevisionMigrationTests(unittest.TestCase):
    def test_v4_upgrade_preserves_rows_and_historical_attempts_without_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(directory)
            self.addCleanup(store.close)
            with patch.object(database, "MIGRATIONS", database.MIGRATIONS[:4]):
                database.initialize(store.db_path)
            original = {
                "max_attempts": 3,
                "merge_tokens": 512,
                "system_prompt": "PRIVATE",
            }
            store.create_job("a", "old.pdf", "Old request", 1, original)
            with store.connect() as con:
                run_id = con.execute(
                    "INSERT INTO job_runs (job_id,started_at,status) VALUES ('a',?,'failed')",
                    (jobs.utcnow(),),
                ).lastrowid
                before = {
                    table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
                    for table in ("jobs", "job_configs", "job_runs", "job_steps")
                }
            database.initialize(store.db_path)
            database.initialize(store.db_path)
            with store.connect() as con:
                for table, rows in before.items():
                    self.assertEqual(
                        [tuple(row) for row in con.execute(f"SELECT * FROM {table}")],
                        rows,
                    )
                for table in REVISION_TABLES:
                    self.assertEqual(
                        con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                    )
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
            self.assertEqual(store.load_attempt_config("a", run_id), original)
            store.save_config_revision("a", {**original, "merge_tokens": 1024})
            store.update_job("a", status="failed")
            store.requeue("a", 0, config_revision=1)
            self.assertEqual(store.load_attempt_config("a", run_id), original)
            # An untracked, already populated schema can safely record v5 again.
            with store.connect() as con:
                con.execute("DELETE FROM schema_migrations WHERE version=5")
            database.initialize(store.db_path)
            self.assertEqual(store.load_config("a")["merge_tokens"], 1024)

    def test_failed_revision_migration_rolls_back_all_tables_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            migrate = dict(database.MIGRATIONS)[5]

            def broken(con):
                migrate(con)
                raise sqlite3.OperationalError("simulated failure")

            with (
                patch.object(
                    database, "MIGRATIONS", database.MIGRATIONS[:4] + ((5, broken),)
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                database.initialize(path)
            with database.connect(path) as con:
                tables = {
                    row[0] for row in con.execute("SELECT name FROM sqlite_master")
                }
                self.assertFalse(set(REVISION_TABLES) & tables)
                self.assertNotIn("job_runs_identity", tables)
                self.assertEqual(
                    con.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    4,
                )
            database.initialize(path)
            with database.connect(path) as con:
                self.assertEqual(
                    con.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    database.MIGRATIONS[-1][0],
                )


if __name__ == "__main__":
    unittest.main()
