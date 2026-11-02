"""History removal uses isolated SQLite and storage; no worker or model runs."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import app
from document_processor import jobs


class HistoryDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        for name, value in (("DATA", self.data), ("DB_PATH", self.data / "jobs.sqlite3")):
            changed = patch.object(app, name, value)
            changed.start()
            self.addCleanup(changed.stop)
        app.init_db()
        self.client = app.app.test_client()
        self.identifier = "a" * 32
        self.directory = self.add_job(self.identifier)

    def add_job(self, identifier, status="done"):
        with app.db_conn() as con:
            con.execute("INSERT INTO jobs (id,filename,question,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                        (identifier, "example.pdf", "Analyze", status, app.utcnow(), app.utcnow()))
        directory = app.job_dir(identifier)
        directory.mkdir(parents=True)
        (directory / "rapport_final.md").write_text("# Report", encoding="utf-8")
        return directory

    def delete(self, identifier=None, **kwargs):
        return self.client.post(f"/api/jobs/{identifier or self.identifier}/delete", **kwargs)

    def test_individual_removes_record_files_and_dependent_metrics(self):
        with app.db_conn() as con:
            con.execute("INSERT INTO job_configs VALUES (?, '{}', 'hash', ?)", (self.identifier, app.utcnow()))
            run = con.execute("INSERT INTO job_runs (job_id,started_at,status) VALUES (?,?,'done')",
                              (self.identifier, app.utcnow())).lastrowid
            con.execute("INSERT INTO job_steps (run_id,job_id,stage,step_key,started_at,status) VALUES (?,?,'final','report',?,'done')",
                        (run, self.identifier, app.utcnow()))
        (self.directory / "artifacts" / "hash").mkdir(parents=True)
        (self.directory / "artifacts" / "hash" / "metrics.json").write_text("{}")
        other = self.add_job("b" * 32)
        response = self.delete()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["deleted"])
        self.assertIsNone(response.json["warning_code"])
        self.assertFalse(self.directory.exists())
        self.assertTrue(other.exists())
        with app.db_conn() as con:
            for table in ("job_configs", "job_runs", "job_steps"):
                self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual([row["id"] for row in self.client.get('/api/jobs').json['jobs']], ["b" * 32])
        self.assertEqual(self.client.get(f'/api/jobs/{self.identifier}').status_code, 404)
        self.assertEqual(self.client.post(f'/api/jobs/{self.identifier}/retry').status_code, 404)

    def test_missing_and_invalid_ids(self):
        self.assertEqual(self.delete("f" * 32).status_code, 404)
        self.assertEqual(self.delete("not-an-id").status_code, 400)
        self.assertEqual(self.delete().status_code, 200)
        self.assertEqual(self.delete().status_code, 404)

    def test_queued_running_unknown_and_finishing_jobs_are_protected(self):
        for status in ("queued", "running", "unknown"):
            app.update_job(self.identifier, status=status)
            response = self.delete()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json['error_code'], 'delete_active')
            self.assertTrue(self.directory.exists())
        app.update_job(self.identifier, status="done")
        with app.db_conn() as con:
            con.execute("INSERT INTO job_runs (job_id,started_at,status) VALUES (?,?,'running')",
                        (self.identifier, app.utcnow()))
        self.assertEqual(self.delete().status_code, 409)

    def test_failed_and_cancelled_jobs_can_be_removed(self):
        for index, status in enumerate(("failed", "cancelled")):
            identifier = str(index) * 32
            self.add_job(identifier, status)
            self.assertTrue(self.delete(identifier).json["deleted"])

    def test_missing_directory_and_missing_report_are_tolerated(self):
        (self.directory / "rapport_final.md").unlink()
        self.directory.rmdir()
        self.assertTrue(self.delete().json['deleted'])
        self.add_job(self.identifier)
        (self.directory / "rapport_final.md").unlink()
        self.assertTrue(self.delete().json['deleted'])

    def test_batch_deduplicates_and_reports_partial_success(self):
        active = "b" * 32
        self.add_job(active, "running")
        response = self.client.post('/api/jobs/delete', json={"ids": [self.identifier, active, "f" * 32, self.identifier]})
        self.assertEqual(response.status_code, 207)
        self.assertEqual(len(response.json['results']), 3)
        self.assertEqual([r['deleted'] for r in response.json['results']], [True, False, False])
        self.assertEqual(response.json['results'][1]['error_code'], 'delete_active')
        self.assertEqual(response.json['results'][2]['error_code'], 'job_not_found')
        self.assertEqual(self.client.get(f'/api/jobs/{active}').json['status'], 'running')

    def test_batch_rejects_entire_malformed_input_before_deleting(self):
        for payload in (None, [], {}, {"ids": []}, {"ids": "abc"}, {"ids": [None]},
                        {"ids": [self.identifier, "../../data"]}, {"ids": [self.identifier] * 101}):
            self.assertEqual(self.client.post('/api/jobs/delete', json=payload).status_code, 400)
            self.assertIsNotNone(app.get_job(self.identifier))
        response = self.client.post('/api/jobs/delete', data='{' + ' ' * 8200, content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_complete_batch_success(self):
        self.add_job('b' * 32)
        response = self.client.post('/api/jobs/delete', json={"ids": [self.identifier, 'b' * 32]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(r['deleted'] for r in response.json['results']))
        self.assertEqual(self.client.get('/api/jobs').json['jobs'], [])

    def test_sqlite_failure_keeps_record_and_files_and_batch_continues(self):
        self.add_job('b' * 32)
        with app.db_conn() as con:
            con.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON jobs WHEN OLD.id='" + self.identifier + "' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        response = self.client.post('/api/jobs/delete', json={"ids": [self.identifier, 'b' * 32]})
        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.json['results'][0]['error_code'], 'delete_database')
        self.assertTrue(response.json['results'][1]['deleted'])
        self.assertIsNotNone(app.get_job(self.identifier))
        self.assertTrue((self.directory / 'rapport_final.md').exists())

    def test_cleanup_happens_only_after_commit_and_failure_is_explicit(self):
        def fail_cleanup(directory):
            with sqlite3.connect(app.DB_PATH) as con:
                self.assertIsNone(con.execute('SELECT id FROM jobs WHERE id=?', (self.identifier,)).fetchone())
            raise PermissionError('private path must not be exposed')
        with patch.object(jobs.shutil, 'rmtree', side_effect=fail_cleanup) as cleanup:
            cleanup.avoids_symlink_attacks = True
            response = self.delete()
        self.assertTrue(response.json['deleted'])
        self.assertEqual(response.json['warning_code'], 'delete_files_failed')
        self.assertNotIn('private path', response.get_data(as_text=True))
        self.assertTrue(self.directory.exists())

    def test_commit_failure_rolls_back_without_touching_files(self):
        original = app.db_conn

        @contextmanager
        def failing_commit():
            with original() as con:
                yield con
                raise sqlite3.OperationalError('simulated commit failure')

        with patch.object(app, 'db_conn', failing_commit):
            response = self.delete()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json['error_code'], 'delete_database')
        self.assertIsNotNone(app.get_job(self.identifier))
        self.assertTrue((self.directory / 'rapport_final.md').exists())

    def test_partial_file_cleanup_is_reported_after_durable_deletion(self):
        def partial_cleanup(directory):
            (directory / 'rapport_final.md').unlink()
            raise PermissionError('remaining files could not be removed')

        with patch.object(jobs.shutil, 'rmtree', side_effect=partial_cleanup) as cleanup:
            cleanup.avoids_symlink_attacks = True
            response = self.delete()
        self.assertTrue(response.json['deleted'])
        self.assertEqual(response.json['warning_code'], 'delete_files_failed')
        self.assertIsNone(app.get_job(self.identifier))
        self.assertFalse((self.directory / 'rapport_final.md').exists())

    def test_mounts_and_unsafe_platform_cleanup_are_retained(self):
        with patch.object(Path, 'is_mount', return_value=True):
            self.assertEqual(self.delete().json['warning_code'], 'delete_files_unsafe')
        self.assertTrue((self.directory / 'rapport_final.md').exists())
        with app.db_conn() as con:
            con.execute("INSERT INTO jobs (id,filename,question,status,created_at,updated_at) VALUES (?, 'test.pdf', 'Analyze', 'done', ?, ?)",
                        (self.identifier, app.utcnow(), app.utcnow()))
        with patch.object(jobs.shutil.rmtree, 'avoids_symlink_attacks', False):
            self.assertEqual(self.delete().json['warning_code'], 'delete_files_unsafe')
        self.assertTrue((self.directory / 'rapport_final.md').exists())

    def test_symlink_root_or_descendant_never_removes_shared_data(self):
        shared = self.data / 'shared'
        shared.mkdir()
        (shared / 'private.md').write_text('keep')
        (self.directory / 'shared').symlink_to(shared, target_is_directory=True)
        response = self.delete()
        self.assertTrue(response.json['deleted'])
        self.assertEqual(response.json['warning_code'], 'delete_files_unsafe')
        self.assertEqual((shared / 'private.md').read_text(), 'keep')
        (self.directory / 'shared').unlink()
        (self.directory / 'rapport_final.md').unlink()
        self.directory.rmdir()
        self.add_job(self.identifier)
        (self.directory / 'rapport_final.md').unlink()
        self.directory.rmdir()
        self.directory.symlink_to(shared, target_is_directory=True)
        self.assertEqual(self.delete().json['warning_code'], 'delete_files_unsafe')
        self.assertTrue((shared / 'private.md').exists())

    def test_hardlinked_files_are_retained(self):
        shared = self.data / 'shared.md'
        os.link(self.directory / 'rapport_final.md', shared)
        self.assertEqual(self.delete().json['warning_code'], 'delete_files_unsafe')
        self.assertTrue(shared.exists())
        self.assertTrue((self.directory / 'rapport_final.md').exists())

    def test_symlinked_jobs_parent_is_retained(self):
        original = self.data / 'jobs'
        moved = self.data / 'actual-jobs'
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.delete().json['warning_code'], 'delete_files_unsafe')
        self.assertTrue((moved / self.identifier / 'rapport_final.md').exists())

    def test_cross_origin_removal_is_rejected(self):
        for url, kwargs in ((f'/api/jobs/{self.identifier}/delete', {}),
                            ('/api/jobs/delete', {'json': {'ids': [self.identifier]}})):
            self.assertEqual(self.client.post(url, headers={'Origin': 'https://other.example'}, **kwargs).status_code, 403)
        self.assertIsNotNone(app.get_job(self.identifier))
