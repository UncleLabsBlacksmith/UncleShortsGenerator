"""YouTube publish and accounting tests; no network or external publication."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from decimal import Decimal
import shorts_core as core
import youtube_actions as yt
from clip_costs import Ledger, project_summary
from test_shorts import config


class YoutubeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'project.sqlite3'
        self.video = self.path.parent / 'final_v001.mp4'
        self.video.write_bytes(b'fixture (upload mocked)')
        self.document = {'settings': dict(config(), oauth='client.json', token='token.json'),
            'episodes': [{'day': 1, 'title': 'Test', 'file_path': str(self.video), 'publish_at': '2099-01-01 12:00'}]}
        core.save_json(self.path, self.document)
        self.service = Mock()
        self.service.channels.return_value.list.return_value.execute.return_value = {'items': [{'id': 'channel-1', 'snippet': {'title': 'Test channel'}}]}
        self.service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {'id': 'video-1', 'status': {'privacyStatus': 'public'}})
        self.auth = patch('upload_auto.get_authenticated_service', return_value=self.service)
        self.auth_mock = self.auth.start()

    def tearDown(self):
        self.auth.stop()
        self.temp.cleanup()

    def test_oauth_check_calls_channel_endpoint_without_interactive_login(self):
        _, channel = yt.authenticated_channel('client.json', 'token.json')
        self.assertEqual(channel['title'], 'Test channel')
        self.auth_mock.assert_called_once_with('client.json', 'token.json', allow_interactive=False)
        self.service.channels.return_value.list.assert_called_once_with(part='snippet', mine=True)

    def test_publish_now_ignores_schedule_persists_link_and_deduplicates(self):
        receipt, _ = yt.publish_now(self.path, 1, 'public', lambda _: None)
        body = self.service.videos.return_value.insert.call_args.kwargs['body']
        self.assertEqual(body['status']['privacyStatus'], 'public')
        self.assertNotIn('publishAt', body['status'])
        self.assertEqual(receipt['url'], 'https://www.youtube.com/watch?v=video-1')
        saved = core.read_json(self.path)['episodes'][0]['youtube_uploads'][0]
        self.assertEqual(saved['state'], 'uploaded')
        repeated, _ = yt.publish_now(self.path, 1, 'public', lambda _: None)
        self.assertEqual(repeated['url'], receipt['url'])
        self.assertEqual(self.service.videos.return_value.insert.call_count, 1)

    def test_private_upload_can_be_made_public_without_reupload(self):
        self.service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {'id': 'video-1', 'status': {'privacyStatus': 'private'}})
        yt.publish_now(self.path, 1, 'private', lambda _: None)
        self.service.videos.return_value.update.return_value.execute.return_value = {'status': {'privacyStatus': 'public'}}
        receipt, _ = yt.publish_now(self.path, 1, 'public', lambda _: None)
        self.assertEqual(receipt['privacy'], 'public')
        self.assertEqual(self.service.videos.return_value.insert.call_count, 1)

    def test_uncertain_failure_is_not_retried_automatically(self):
        self.service.videos.return_value.insert.return_value.next_chunk.side_effect = TimeoutError('unknown result')
        with self.assertRaises(TimeoutError):
            yt.publish_now(self.path, 1, log=lambda _: None)
        self.assertEqual(core.read_json(self.path)['episodes'][0]['youtube_uploads'][0]['state'], 'needs_review')
        with self.assertRaises(ValueError):
            yt.publish_now(self.path, 1, log=lambda _: None)
        self.assertEqual(self.service.videos.return_value.insert.call_count, 1)

    def test_invalid_oauth_never_attempts_upload(self):
        self.auth_mock.return_value = None
        with self.assertRaises(ValueError):
            yt.publish_now(self.path, 1, log=lambda _: None)
        self.service.videos.return_value.insert.assert_not_called()


class CostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'project.sqlite3'
        self.document = {'settings': config(), 'episodes': [{'day': 1, 'title': 'One'}, {'day': 2, 'title': 'Two'}]}
        core.save_json(self.path, self.document)
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_estimates_unknowns_and_shared_cost_do_not_double_count(self):
        self.ledger.record('text', actual='.20', identifier='shared')
        self.ledger.record('video', day=1, estimate='1.5', identifier='job')
        self.ledger.record('video', day=1, actual='1.25', identifier='job')
        self.ledger.record('speech', day=1, actual='0', identifier='free')
        self.ledger.record('music', day=1, identifier='unknown')
        self.ledger.record('video', day=2, actual='5', identifier='other')
        report = self.ledger.summary(1, 2)
        self.assertEqual(report['text']['actual'], Decimal('.10'))
        self.assertEqual(report['video']['actual'], Decimal('1.25'))
        self.assertEqual(report['video']['estimate'], 0)
        self.assertEqual(report['video']['count'], 1)
        self.assertEqual(report['speech']['unknown'], 0)
        self.assertEqual(report['music']['unknown'], 1)
        core.save_json(self.path, self.document)
        self.assertEqual(Ledger(self.path).summary(1, 2), report)

    def test_router_records_reported_cost_and_current_video_estimate(self):
        router = core.Router('fake', project=self.path, day=1, catalog={'video': [
            {'id': 'test/video', 'pricing_skus': {'per-video-second': '.5'}}]})
        router.record_cost('/chat/completions', {'model': 'test/text'}, {'id': 'gen-1', 'usage': {'cost': .03}})
        router.record_cost('/videos', {'model': 'test/video', 'duration': 5}, {'id': 'job'})
        report = self.ledger.summary(1, 2)
        self.assertEqual(report['text']['actual'], Decimal('.03'))
        self.assertEqual(report['video']['estimate'], Decimal('2.5'))

    def test_historical_assets_without_receipts_remain_unknown(self):
        self.document['episodes'][0]['scenes'] = [{'job_id': 'legacy', 'narration': 'Hello'}]
        report = project_summary(self.path, self.document, {}, 1)
        self.assertEqual(report['video']['unknown'], 1)
        self.assertEqual(report['text']['unknown'], 1)

    def test_refresh_pending_generation_replaces_estimate_with_actual_cost(self):
        self.ledger.record('speech', day=1, estimate='.50', identifier='gen-speech')
        router = Mock()
        router.request.return_value = {'data': {'total_cost': '.42'}}
        self.ledger.refresh(router, lambda _: None)
        summary = self.ledger.summary(1, 2)
        self.assertEqual(summary['speech']['actual'], Decimal('.42'))
        self.assertEqual(summary['speech']['estimate'], 0)
        self.assertEqual(summary['speech']['count'], 1)

    def test_binary_speech_response_records_generation_header_for_later_cost_lookup(self):
        router = core.Router('fake', project=self.path, day=1)
        response = Mock()
        response.headers = {'X-Generation-Id': 'gen-audio'}
        response.read.return_value = b'audio-fixture'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(core.urllib.request, 'urlopen', return_value=context):
            result = router.request('/audio/speech', {'model': 'test/tts', 'input': 'Hello'}, binary=True)
        self.assertEqual(result, b'audio-fixture')
        lookup = Mock()
        lookup.request.return_value = {'data': {'total_cost': '.05'}}
        self.ledger.refresh(lookup, lambda _: None)
        lookup.request.assert_called_once_with('/generation?id=gen-audio')
        self.assertEqual(self.ledger.summary(1, 2)['speech']['actual'], Decimal('.05'))


if __name__ == '__main__':
    unittest.main()
