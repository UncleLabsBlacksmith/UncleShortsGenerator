"""Retry rejection, polling, cancellation and ambiguous submission tests; no waits/API calls."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch
import shorts_core as core
from router_retry import RouterHTTPError, RetryCancelled, open_with_retry
from test_shorts import config


def rejected(status=429, headers=None, metadata=None):
    body = {'error': {'code': status, 'message': 'Retry shortly', 'metadata': metadata or {
        'limit_source': 'openrouter_admission_control', 'headers': {'Retry-After': '10'}}}}
    return urllib.error.HTTPError(core.API + '/videos', status, 'rejected', headers or {}, io.BytesIO(json.dumps(body).encode()))


class RetryTests(unittest.TestCase):
    def test_nested_retry_after_retries_rejected_post_then_succeeds(self):
        request = urllib.request.Request(core.API + '/videos', data=b'{}')
        stop, log = Mock(), Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        response = object()
        with patch('urllib.request.urlopen', side_effect=[rejected(), response]) as api, patch('router_retry.random.uniform', return_value=0):
            self.assertIs(open_with_retry(request, 180, stop, log), response)
        stop.wait.assert_called_once_with(10)
        self.assertEqual(api.call_count, 2)
        self.assertIn('429', log.call_args.args[0])

    def test_header_is_honored_and_retries_are_bounded(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with patch('urllib.request.urlopen', side_effect=[rejected(headers={'Retry-After': '35'}), rejected()]) as api, patch('router_retry.random.uniform', return_value=0):
            with self.assertRaises(RouterHTTPError):
                open_with_retry(urllib.request.Request(core.API + '/videos/job'), 180, stop, Mock(), max_retries=1)
        stop.wait.assert_called_once_with(35)
        self.assertEqual(api.call_count, 2)

    def test_stop_cancels_backoff_without_second_request(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with patch('urllib.request.urlopen', side_effect=rejected()) as api:
            with self.assertRaises(RetryCancelled):
                open_with_retry(urllib.request.Request(core.API + '/videos', data=b'{}'), 180, stop, Mock())
        self.assertEqual(api.call_count, 1)

    def test_ambiguous_paid_post_is_not_retried(self):
        for error in (TimeoutError('unknown result'), rejected(status=503), rejected(status=402)):
            with self.subTest(error=error):
                with patch('urllib.request.urlopen', side_effect=error) as api:
                    with self.assertRaises((TimeoutError, RouterHTTPError)):
                        open_with_retry(urllib.request.Request(core.API + '/videos', data=b'{}'), 180, threading.Event(), Mock())
                self.assertEqual(api.call_count, 1)

    def test_poll_uses_same_job_url_when_rate_limited(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {}
        response.read.return_value = b'{"id":"job-existing","status":"completed"}'
        router = core.Router('fake', stop=stop, retry_log=Mock())
        with patch('urllib.request.urlopen', side_effect=[rejected(), response]) as api:
            self.assertEqual(router.request('/videos/job-existing')['status'], 'completed')
        self.assertTrue(all(call.args[0].full_url.endswith('/videos/job-existing') for call in api.call_args_list))
        self.assertTrue(all(call.args[0].get_method() == 'GET' for call in api.call_args_list))

    def test_only_definite_rejection_clears_submission_pending(self):
        for error, remains_pending in [(RouterHTTPError(429, '{}'), False), (TimeoutError('unknown'), True)]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'project.sqlite3'
                core.save_json(path, {'settings': config(), 'episodes': [{'day': 1, 'title': 'Test',
                    'scenes': [{'visual_prompt': 'Workshop', 'narration': 'Hello'}]}]})
                class Router:
                    def request(self, route, payload=None, binary=False):
                        if route == '/videos/models':
                            return {'data': [{'id': 'test/video', 'supported_durations': [5]}]}
                        if route == '/audio/speech/models':
                            return {'data': []}
                        if route == '/videos':
                            raise error
                        raise AssertionError(route)
                with self.assertRaises((RouterHTTPError, TimeoutError)):
                    core.generate(path, Router(), lambda _: None)
                scene = core.read_json(path)['episodes'][0]['scenes'][0]
                self.assertEqual(bool(scene.get('submission_pending')), remains_pending)
                self.assertNotIn('job_id', scene)
                episode = core.read_json(path)['episodes'][0]
                self.assertTrue(episode['render_incomplete'])
                self.assertTrue(core.needs_render_resume(path, episode))


if __name__ == '__main__':
    unittest.main()
