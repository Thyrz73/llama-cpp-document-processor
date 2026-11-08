"""Bind cancellation to an execution even when HTTP and worker threads overlap."""

import io
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_resume import ResumeModel

from document_processor import extraction, worker
from document_processor.application import create_app
from document_processor.errors import Cancelled
from document_processor.jobs import JobStore
from document_processor.settings import Settings


class CancellationRaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cancellation-race-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        prompt = root / "prompt.txt"
        prompt.write_text("PRIVATE_PROMPT", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.settings = replace(
                Settings.from_env(),
                data_dir=root / "data",
                system_prompt_file=prompt,
                max_job_attempts=1,
            )
        self.store = JobStore(self.settings.data_dir)
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.models = []
        self.application = create_app(self.settings, self.store, self.model_client)
        self.services = self.application.extensions["document_processor"]
        self.client = self.application.test_client()
        for target, name, result in (
            (
                extraction,
                "extract_pdf",
                ([{"page": 1, "text": "Measurements"}], 1, [], []),
            ),
            (
                extraction,
                "make_chunks",
                [{"text": "Measurements [Page 1]", "pages": [1]}],
            ),
        ):
            patcher = patch.object(target, name, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def model_client(self, snapshot):
        model = ResumeModel("http://unused", "", snapshot, snapshot["system_prompt"])
        model.calls, model.fail_stage = [], None
        self.models.append(model)
        return model

    def create(self):
        response = self.client.post(
            "/api/jobs",
            data={
                "file": (io.BytesIO(b"%PDF-test"), "study.pdf"),
                "question": "Analyze",
            },
        )
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["id"]

    def run_claimed(self, runner, identifier):
        try:
            runner.run_claimed(identifier)
        finally:
            runner.store.close(checkpoint=False)

    def assert_delayed_notification_is_harmless(self, action, *, restart=False):
        identifier = self.create()
        url = f"/api/jobs/{identifier}"
        runner = self.services.worker
        cancelled, deliver = threading.Event(), threading.Event()
        generating, finish = threading.Event(), threading.Event()
        original_notify, original_complete = (
            runner.notify_cancelled,
            ResumeModel.complete,
        )

        def notify(job_id):
            cancelled.set()  # /cancel has committed, but has not notified the worker.
            self.assertTrue(
                deliver.wait(5), "Cancellation notification was not released"
            )
            original_notify(job_id)

        def cancel():
            try:
                return self.application.test_client().post(url + "/cancel")
            finally:
                self.store.close(checkpoint=False)

        def complete(model, *args, **kwargs):
            if not model.calls:
                generating.set()
                self.assertTrue(finish.wait(5), "Generation was not released")
            return original_complete(model, *args, **kwargs)

        with (
            patch.object(runner, "notify_cancelled", side_effect=notify),
            patch.object(ResumeModel, "complete", complete),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            cancelling = pool.submit(cancel)
            try:
                self.assertTrue(cancelled.wait(5), "/cancel did not commit")
                self.assertEqual(self.store.get_job(identifier)["status"], "cancelled")
                self.assertTrue(self.store.read_cancellation(identifier))
                self.assertEqual(self.store.attempt_count(identifier), 0)
                active_store, client, active_runner = self.store, self.client, runner
                if restart:
                    self.store.close()
                    active_store = JobStore(self.settings.data_dir)
                    self.addCleanup(active_store.close)
                    active_store.initialize()
                    application = create_app(
                        self.settings, active_store, self.model_client
                    )
                    client = application.test_client()
                    active_runner = application.extensions["document_processor"].worker
                kwargs = {"json": {"merge_tokens": 2048}} if action == "resume" else {}
                response = client.post(url + "/" + action, **kwargs)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(active_store.claim_next_job(), identifier)
                executing = pool.submit(self.run_claimed, active_runner, identifier)
                self.assertTrue(generating.wait(5), "Resumed generation did not start")
                self.assertFalse(active_store.read_cancellation(identifier))
                deliver.set()
                self.assertEqual(cancelling.result(timeout=5).status_code, 202)
                finish.set()
                executing.result(timeout=5)
            finally:
                deliver.set()
                finish.set()
        job = active_store.get_job(identifier)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual((job["cancel_requested"], job["error_code"]), (0, ""))
        self.assertEqual(active_store.attempt_count(identifier), 1)
        metrics = active_store.metrics(identifier)
        self.assertEqual([run["status"] for run in metrics["runs"]], ["done"])
        self.assertTrue(all(step["status"] == "done" for step in metrics["steps"]))
        effective = active_store.load_config(identifier)
        self.assertEqual(effective["max_attempts"], 1)
        self.assertEqual(
            active_store.load_attempt_config(identifier, metrics["runs"][0]["id"]),
            effective,
        )
        self.assertEqual(self.models[-1].config, effective)
        self.assertEqual(active_runner._signals, {})

    def test_delayed_queued_cancellation_cannot_cancel_resume_or_exhaust_its_allowance(
        self,
    ):
        self.assert_delayed_notification_is_harmless("resume")

    def test_delayed_queued_cancellation_cannot_cancel_retry_or_exhaust_its_allowance(
        self,
    ):
        self.assert_delayed_notification_is_harmless("retry")

    def test_delayed_notification_from_previous_worker_cannot_cancel_resumed_attempt(
        self,
    ):
        self.assert_delayed_notification_is_harmless("resume", restart=True)

    def test_signal_cannot_be_retargeted_if_attempt_changes_after_sqlite_read(self):
        identifier = self.create()
        runner = self.services.worker
        old_started, finish_old = threading.Event(), threading.Event()
        new_started, finish_new = threading.Event(), threading.Event()
        read_cancelled, deliver = threading.Event(), threading.Event()
        original_read = self.store.read_cancellation
        executions = []

        def analyze(job_id, check):
            executions.append(job_id)
            if len(executions) == 1:
                old_started.set()
                self.assertTrue(finish_old.wait(5), "Old attempt was not released")
                raise Cancelled()
            self.assertFalse(check(job_id))
            new_started.set()
            self.assertTrue(finish_new.wait(5), "New attempt was not released")
            self.assertFalse(check(job_id))
            self.store.update_job(job_id, status="done")

        def read(job_id):
            value = original_read(job_id)
            if value:
                read_cancelled.set()
                self.assertTrue(deliver.wait(5), "SQLite result was not released")
            return value

        def notify():
            try:
                runner.notify_cancelled(identifier)
            finally:
                self.store.close(checkpoint=False)

        with (
            patch.object(runner, "run_job", side_effect=analyze),
            patch.object(self.store, "read_cancellation", side_effect=read),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            self.assertEqual(self.store.claim_next_job(), identifier)
            previous = pool.submit(self.run_claimed, runner, identifier)
            try:
                self.assertTrue(old_started.wait(5))
                old_signal = runner._signals[identifier]
                self.assertEqual(
                    self.store.request_cancel(identifier), ("requested", "running")
                )
                notifying = pool.submit(notify)
                self.assertTrue(
                    read_cancelled.wait(5), "Notification did not validate SQLite"
                )
                finish_old.set()
                previous.result(timeout=5)
                response = self.client.post(f"/api/jobs/{identifier}/resume")
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(self.store.claim_next_job(), identifier)
                current = pool.submit(self.run_claimed, runner, identifier)
                self.assertTrue(new_started.wait(5))
                new_signal = runner._signals[identifier]
                self.assertIsNot(old_signal, new_signal)
                deliver.set()
                notifying.result(timeout=5)
                self.assertTrue(old_signal.event.is_set())
                self.assertFalse(new_signal.event.is_set())
                finish_new.set()
                current.result(timeout=5)
            finally:
                finish_old.set()
                finish_new.set()
                deliver.set()
        self.assertEqual(self.store.get_job(identifier)["status"], "done")
        self.assertFalse(self.store.read_cancellation(identifier))
        self.assertEqual(runner._signals, {})

    def test_legitimate_cancellation_interrupts_generation_without_extra_streaming_reads(
        self,
    ):
        identifier = self.create()
        runner = self.services.worker
        generating, continue_stream = threading.Event(), threading.Event()

        def complete(model, *args, **kwargs):
            for _ in range(500):
                model.check_cancelled()
            generating.set()
            self.assertTrue(continue_stream.wait(5), "Streaming was not released")
            model.check_cancelled()
            self.fail("The active generation did not see its cancellation")

        with (
            patch.object(ResumeModel, "complete", complete),
            patch.object(worker, "CANCEL_RECHECK_SECONDS", 3600),
            patch.object(
                self.store, "read_cancellation", wraps=self.store.read_cancellation
            ) as reads,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            self.assertEqual(self.store.claim_next_job(), identifier)
            executing = pool.submit(self.run_claimed, runner, identifier)
            try:
                self.assertTrue(generating.wait(5))
                self.assertEqual(reads.call_count, 1)
                response = self.client.post(f"/api/jobs/{identifier}/cancel")
                self.assertEqual(response.status_code, 202, response.json)
                self.assertEqual(reads.call_count, 2)  # One validation read in /cancel.
                continue_stream.set()
                executing.result(timeout=5)
                self.assertEqual(reads.call_count, 2)
            finally:
                continue_stream.set()
        job = self.store.get_job(identifier)
        self.assertEqual((job["status"], job["cancel_requested"]), ("cancelled", 1))
        self.assertEqual(
            self.store.metrics(identifier)["runs"][0]["status"], "cancelled"
        )

    def test_queued_cancellation_needs_no_active_signal_or_extra_read(self):
        identifier = self.create()
        runner = self.services.worker
        runner._wakeup.clear()
        with patch.object(
            self.store, "read_cancellation", wraps=self.store.read_cancellation
        ) as reads:
            response = self.client.post(f"/api/jobs/{identifier}/cancel")
            self.assertEqual(response.status_code, 202, response.json)
            reads.assert_not_called()
        job = self.store.get_job(identifier)
        self.assertEqual((job["status"], job["cancel_requested"]), ("cancelled", 1))
        self.assertEqual(self.store.attempt_count(identifier), 0)
        self.assertEqual(runner._signals, {})
        self.assertTrue(runner._wakeup.is_set())
        self.assertIsNone(self.store.claim_next_job())


if __name__ == "__main__":
    unittest.main()
