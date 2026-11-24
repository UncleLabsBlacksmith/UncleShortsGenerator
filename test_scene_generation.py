"""One-scene regeneration integration with fake API and real media encoding."""
import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import shorts_core as core
import scene_generation
from router_retry import RouterHTTPError
from test_shorts import config


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class SceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'project.sqlite3'
        self.folder = self.root / 'day_01'
        self.folder.mkdir()
        self.source = self.root / 'red.mp4'
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:s=240x320:d=1', '-c:v', 'libx264', self.source])
        self.voice = self.root / 'voice.mp3'
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', self.voice])
        scenes = []
        for i in range(1, 4):
            shutil.copyfile(self.source, self.folder / f'{i:02}.mp4')
            shutil.copyfile(self.source, self.folder / f'{i:02}.render.mp4')
            shutil.copyfile(self.voice, self.folder / f'{i:02}.mp3')
            scenes.append({'visual_prompt': f'Scene {i} edited English prompt', 'narration': 'Hello',
                'job_id': f'old-{i}', 'file_path': str(self.folder / f'{i:02}.render.mp4')})
        self.document = {'settings': config(), 'episodes': [{'day': 1, 'title': 'Test', 'scenes': scenes}]}
        core.save_json(self.path, self.document)
        self.posts, self.audio_calls = [], 0
        owner = self
        class Router:
            def request(self, route, payload=None, binary=False):
                if route == '/videos/models':
                    return {'data': [{'id': 'test/video', 'supported_durations': [5]}]}
                if route == '/audio/speech/models':
                    return {'data': []}
                if route == '/videos':
                    owner.posts.append(payload)
                    return {'id': f'new-{len(owner.posts)}'}
                if route.endswith('/content?index=0'):
                    return owner.source.read_bytes()
                if route.startswith('/videos/'):
                    return {'status': 'completed'}
                if route == '/audio/speech':
                    owner.audio_calls += 1
                    return owner.voice.read_bytes()
                raise AssertionError(route)
        self.router = Router()

    def tearDown(self):
        self.temp.cleanup()

    def test_regenerate_scene_three_keeps_other_scenes_and_old_versions(self):
        original = copy.deepcopy(self.document['episodes'][0]['scenes'])
        old_raw = (self.folder / '03.mp4').read_bytes()
        progress = []
        self.router.progress = progress.append
        first = scene_generation.regenerate(self.path, 1, 3, self.router, lambda _: None)
        first_bytes = first.read_bytes()
        p = core.read_json(self.path)
        self.assertEqual(p['episodes'][0]['scenes'][:2], original[:2])
        self.assertEqual(p['episodes'][0]['scenes'][2]['job_id'], 'new-1')
        self.assertEqual(self.posts[0]['prompt'], original[2]['visual_prompt'])
        self.assertEqual((self.folder / '03.mp4').read_bytes(), old_raw)
        self.assertTrue(p['episodes'][0]['final_stale'])
        self.assertFalse(p['episodes'][0]['scenes'][2].get('pending_video_generation'))
        self.assertEqual(progress, [0, 50, 75, 100])
        p['episodes'][0]['scenes'][2]['narration'] = 'New narration'
        core.save_json(self.path, p)
        second = scene_generation.regenerate(self.path, 1, 3, self.router, lambda _: None)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.audio_calls, 2)
        updated = core.read_json(self.path)['episodes'][0]['scenes'][2]
        self.assertEqual(updated['audio_history'][-1]['narration'], 'New narration')

    def test_429_discards_unfinished_job_then_submits_new_job(self):
        record = self.document['episodes'][0]['scenes'][2]
        raw = self.folder / 'replacement.mp4'
        original_request = self.router.request
        def request(route, payload=None, binary=False):
            if route == '/videos/old-3':
                raise RouterHTTPError(429, '{}')
            return original_request(route, payload, binary)
        self.router.request = request
        with patch.object(RouterHTTPError, 'delay', return_value=0):
            core.download_video(self.path, self.document, record, raw, {'prompt': 'Replacement'}, self.router, lambda _: None)
        self.assertEqual(record['job_id'], 'new-1')
        self.assertEqual(record['abandoned_jobs'][0]['job_id'], 'old-3')
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(raw.is_file())

    def test_completed_scene_does_not_submit_any_new_job(self):
        record = self.document['episodes'][0]['scenes'][2]
        core.download_video(self.path, self.document, record, self.folder / '03.mp4', {'prompt': 'unused'}, self.router)
        self.assertEqual(self.posts, [])

    def test_explicit_new_scene_bypasses_legacy_unknown_submission(self):
        scene = self.document['episodes'][0]['scenes'][2]
        scene.pop('job_id')
        scene['submission_pending'] = True
        core.save_json(self.path, self.document)
        scene_generation.regenerate(self.path, 1, 3, self.router, lambda _: None, force_new=True)
        scene = core.read_json(self.path)['episodes'][0]['scenes'][2]
        self.assertEqual(len(self.posts), 1)
        self.assertNotIn('submission_pending', scene)
        self.assertTrue(scene['bypassed_submissions'][0]['submission_pending'])

    def test_explicit_new_scene_bypasses_unknown_new_generation_attempt(self):
        scene = self.document['episodes'][0]['scenes'][2]
        scene['pending_video_generation'] = {'version': 2, 'prompt': 'Old request', 'state': 'submitting'}
        core.save_json(self.path, self.document)
        scene_generation.regenerate(self.path, 1, 3, self.router, lambda _: None, force_new=True)
        scene = core.read_json(self.path)['episodes'][0]['scenes'][2]
        self.assertEqual(scene['job_id'], 'new-1')
        self.assertEqual(scene['bypassed_submissions'][0]['pending_video_generation']['state'], 'submitting')

    def test_rebuild_final_uses_new_scene_raw_and_reports_real_completed_steps(self):
        scene_generation.regenerate(self.path, 1, 3, self.router, lambda _: None)
        new_raw = core.read_json(self.path)['episodes'][0]['scenes'][2]['raw_video_file']
        progress = []
        self.router.progress = progress.append
        with patch.object(core, 'run_ffmpeg', wraps=core.run_ffmpeg) as encode:
            core.generate(self.path, self.router, lambda _: None, only_day=1)
        self.assertTrue(any('-i' in call.args[0] and str(call.args[0][call.args[0].index('-i')+1]) == new_raw for call in encode.call_args_list))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(progress, sorted(progress))
        self.assertFalse(core.read_json(self.path)['episodes'][0].get('final_stale'))

    def test_repeated_429_restarts_are_bounded_and_old_ids_remain_in_history(self):
        record = self.document['episodes'][0]['scenes'][2]
        original_request = self.router.request
        def request(route, payload=None, binary=False):
            if route.startswith('/videos/') and route != '/videos/models':
                raise RouterHTTPError(429, '{}')
            return original_request(route, payload, binary)
        self.router.request = request
        with patch.object(RouterHTTPError, 'delay', return_value=0), self.assertRaisesRegex(RuntimeError, '429'):
            core.download_video(self.path, self.document, record, self.folder / 'new.mp4', {'prompt': 'New'}, self.router, lambda _: None)
        self.assertEqual(len(self.posts), 2)
        self.assertNotIn('job_id', record)
        self.assertEqual([r['job_id'] for r in record['abandoned_jobs']], ['old-3', 'new-1', 'new-2'])
