from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from manga_kit import create_app
from manga_kit.config import ConfigError, Settings
from manga_kit.jobs import JobService
from manga_kit.library import LibraryService
from manga_kit.state import StateStore


def abandoned_queue(settings, connection):
    store = StateStore(settings)
    running, _, _ = store.enqueue_job('scan', 'abandoned-running')
    store.start_queued_job(running)
    queued, child, _ = store.enqueue_job('normalization_execute', 'abandoned-queued', child_kind='normalization_check')
    connection.send((running, queued, child))
    connection.close()
    Event().wait(30)


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_root=Path(temporary.name) / 'state')
        self.app = create_app(settings=self.settings)
        self.store, self.jobs = self.app.extensions['state'], self.app.extensions['jobs']
        self.addCleanup(self.jobs.shutdown)

    def other_worker(self, settings=None):
        settings = settings or self.settings
        store = StateStore(settings)
        worker = JobService(LibraryService(settings, store=store), store)
        self.addCleanup(worker.shutdown)
        return worker

    def wait_job(self, identifier, store=None):
        store = store or self.store
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = store.job(identifier, include_result=True)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(.01)
        self.fail('Queue did not finish')

    def blocker(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def block(progress):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timed out')
            return {'done': True}
        identifier = self.jobs.submit_task('fixture', block)
        self.assertTrue(entered.wait(3))
        return identifier, release

    def test_simultaneous_http_submissions_share_one_running_job_and_retry_after_completion(self):
        entered, release = Event(), Event()
        original = self.app.extensions['library'].scan
        def scan(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timed out')
            return original(*args, **kwargs)
        with patch.object(self.app.extensions['library'], 'scan', side_effect=scan) as scanning:
            try:
                with ThreadPoolExecutor(max_workers=8) as senders:
                    responses = list(senders.map(lambda _: self.app.test_client().post('/api/jobs', json={'kind': 'scan'}), range(16)))
                self.assertTrue(entered.wait(3))
                self.assertTrue(all(response.status_code == 202 for response in responses))
                identifiers = {response.json['id'] for response in responses}
                self.assertEqual(len(identifiers), 1)
                self.assertEqual(len(self.store.jobs()), 1)
                self.assertEqual(scanning.call_count, 1)
            finally:
                release.set()
            identifier = identifiers.pop()
            self.assertEqual(self.wait_job(identifier)['status'], 'succeeded')
            retry = self.app.test_client().post('/api/jobs', json={'kind': 'scan'})
            self.assertNotEqual(identifier, retry.json['id'])
            self.assertEqual(self.wait_job(retry.json['id'])['status'], 'succeeded')
            self.assertEqual(scanning.call_count, 2)

    def test_shared_fifo_keeps_different_parameters_and_deduplicates_at_capacity(self):
        blocker, release = self.blocker()
        other, order = self.other_worker(), []
        try:
            identifiers = []
            for index in range(19):
                worker = other if index % 2 == 0 else self.jobs
                identifiers.append(worker.submit_task('source_refresh', lambda progress, i=index: order.append(i) or {'index': i}, key={'index': index}))
            self.assertEqual(self.store.job(identifiers[0])['queue_position'], 2)
            self.assertEqual(self.store.job(identifiers[-1])['queue_position'], 20)
            self.assertEqual(other.submit_task('source_refresh', lambda progress: self.fail('duplicate ran'), key={'index': 0}), identifiers[0])
            with self.assertRaises(ConfigError):
                other.submit_task('source_refresh', lambda progress: {}, key={'index': 20})
            self.assertEqual(order, [])
            self.assertEqual(len(self.store.jobs()), 20)
            html = self.app.test_client().get('/jobs').get_data(as_text=True)
            self.assertIn('順番待ち', html)
        finally:
            release.set()
        for identifier in [blocker, *identifiers]:
            self.assertEqual(self.wait_job(identifier)['status'], 'succeeded')
        self.assertEqual(order, list(range(19)))

    def test_two_worker_instances_atomically_admit_one_job(self):
        _, release = self.blocker()
        other = self.other_worker()
        try:
            with ThreadPoolExecutor(max_workers=8) as senders:
                identifiers = list(senders.map(lambda i: (other if i % 2 else self.jobs).submit_task('ai_generate', lambda progress: {}, key={'series': 'fiction'}), range(20)))
            self.assertEqual(len(set(identifiers)), 1)
            self.assertEqual(len(self.store.jobs()), 2)
        finally:
            release.set()
        self.assertEqual(self.wait_job(identifiers[0])['status'], 'succeeded')

    def test_failed_task_releases_the_queue_and_does_not_expose_private_errors(self):
        _, release = self.blocker()
        def fail(progress):
            raise RuntimeError('private-path-secret')
        first = self.jobs.submit_task('ai_generate', fail, key='fiction')
        second = self.jobs.submit_task('source_refresh', lambda progress: {'done': True}, key='anime')
        release.set()
        failed = self.wait_job(first)
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn('private-path-secret', failed['error'])
        self.assertEqual(self.wait_job(second)['status'], 'succeeded')
        retry = self.jobs.submit_task('ai_generate', lambda progress: {}, key='fiction')
        self.assertNotEqual(retry, first)
        self.assertEqual(self.wait_job(retry)['status'], 'succeeded')

    def test_different_libraries_do_not_share_tasks_or_block_each_other(self):
        _, release = self.blocker()
        settings = replace(self.settings, library_root=Path(self.settings.data_root).parent / 'another-library')
        other = self.other_worker(settings)
        identifier = other.submit_task('fixture', lambda progress: {'independent': True})
        self.assertEqual(self.wait_job(identifier, other.store)['status'], 'succeeded')
        self.assertIsNone(self.store.job(identifier))
        release.set()

    @unittest.skipUnless(os.name == 'posix', 'POSIX process recovery')
    def test_dead_process_slots_are_interrupted_without_replaying_tasks(self):
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=abandoned_queue, args=(self.settings, sender))
        process.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(8))
            abandoned = receiver.recv()
            started = Event()
            identifier = self.jobs.submit_task('fixture', lambda progress: started.set() or {})
            self.assertFalse(started.wait(.2))
            process.terminate()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(self.wait_job(identifier)['status'], 'succeeded')
            self.assertTrue(started.is_set())
            self.assertTrue(all(self.store.job(job)['status'] == 'interrupted' for job in abandoned))
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            receiver.close()

    def test_automation_submissions_share_a_job_without_orphan_pipeline_records(self):
        _, release = self.blocker()
        automation = self.app.extensions['automation']
        with patch.object(automation, 'step', return_value={'done': True}) as step:
            try:
                first = automation.submit()
                self.assertEqual(automation.submit(), first)
                self.assertEqual(self.store.cached_prefix('automation:run:'), {})
            finally:
                release.set()
            self.assertEqual(self.wait_job(first)['status'], 'succeeded')
            self.assertEqual(step.call_count, 1)
            self.assertEqual(len(self.store.cached_prefix('automation:run:')), 1)

    def test_shelf_preview_is_queued_and_keeps_a_review_link_after_navigation(self):
        _, release = self.blocker()
        with patch.object(self.app.extensions['smart_shelves'], 'prepare', return_value={'plan': {'id': 'fictional-shelf-plan'}}) as prepare:
            try:
                first = self.app.test_client().post('/api/tools/shelves/preview/jobs', json={})
                second = self.app.test_client().post('/api/tools/shelves/preview/jobs', json={})
                self.assertEqual((first.status_code, second.status_code), (202, 202))
                self.assertEqual(first.json['id'], second.json['id'])
                self.assertEqual(prepare.call_count, 0)
            finally:
                release.set()
            self.assertEqual(self.wait_job(first.json['id'])['status'], 'succeeded')
            self.assertEqual(prepare.call_count, 1)
            html = self.app.test_client().get('/jobs/'+first.json['id']).get_data(as_text=True)
            self.assertIn('/smart-shelves?plan=fictional-shelf-plan', html)


if __name__ == '__main__':
    unittest.main()
