"""Offline regression checks; never calls paid APIs or uploads."""
import copy
import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import shorts_core as core


def config():
    return dict(idea='ประวัตินักประดิษฐ์', series='Inventors', days=2, language='Thai', seconds=5,
                text_model='test/text', video_model='test/video', tts_model='test/speech', voice='voice',
                duration=5, resolution='480p', aspect_ratio='9:16', style='documentary',
                start=(datetime.now()+timedelta(days=5)).strftime('%Y-%m-%d 18:00'), timezone='Asia/Bangkok', advanced={})


class FakePlanner:
    def json_chat(self, model, prompt):
        if 'distinct documentary episodes' in prompt:
            return {'episodes': [{'subject': 'Edison', 'title': 'Edison'}, {'subject': 'Tesla', 'title': 'Tesla'}]}
        return {'description': 'AI dramatization', 'tags': ['history'], 'fact_check_notes': 'Verify dates', 'scenes': [{'visual_prompt': 'Historical workshop', 'narration': 'สวัสดี'}]}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shorts_test_')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_and_resume_do_not_duplicate_prompts(self):
        path = core.create_plan(config(), self.root, FakePlanner(), lambda x: None)
        p = core.read_json(path)
        self.assertEqual(len(p['episodes']), 2)
        self.assertNotEqual(p['episodes'][0]['publish_at'], p['episodes'][1]['publish_at'])
        with patch.object(FakePlanner, 'json_chat', side_effect=AssertionError('must not call again')):
            core.complete_prompts(path, FakePlanner())
        self.assertNotIn('api_key', p['settings'])

    def test_topic_only_plan_does_not_automatically_write_episode_scripts(self):
        planner = FakePlanner()
        with patch.object(planner, 'json_chat', wraps=planner.json_chat) as request:
            path = core.create_plan(config(), self.root, planner, lambda _: None, write_prompts=False)
        self.assertEqual(request.call_count, 1)
        episodes = core.read_json(path)['episodes']
        self.assertEqual(len(episodes), 2)
        self.assertTrue(all(not e.get('scenes') for e in episodes))

    def test_manual_episode_write_only_changes_selected_episode(self):
        planner = FakePlanner()
        path = core.create_plan(config(), self.root, planner, lambda _: None, write_prompts=False)
        original = core.read_json(path)['episodes'][0]
        with patch.object(planner, 'json_chat', wraps=planner.json_chat) as request:
            core.complete_prompts(path, planner, lambda _: None, only_day=2)
        self.assertEqual(request.call_count, 1)
        episodes = core.read_json(path)['episodes']
        self.assertEqual(episodes[0], original)
        self.assertTrue(episodes[1]['scenes'])

    def test_unsupported_video_is_rejected(self):
        with self.assertRaises(ValueError):
            core.validate_video(config(), {'supported_durations': [8]})

    def test_advanced_cannot_override_model(self):
        c = config()
        c['advanced'] = {'model': 'other'}
        with self.assertRaises(ValueError):
            core.validate_settings(c)

    def test_music_model_discovery_excludes_speech_only(self):
        models = [{'id': 'google/lyria', 'description': 'Music generation'},
                  {'id': 'speech-only', 'description': 'Speak text aloud'},
                  {'id': 'new/music', 'description': 'Instrumental music'}]
        self.assertEqual([m['id'] for m in core.music_models(models)], ['google/lyria', 'new/music'])

    def test_music_stream_reassembles_base64(self):
        encoded = base64.b64encode(b'RIFF test music').decode()
        lines = []
        for fragment in (encoded[:7], encoded[7:]):
            chunk = {'choices': [{'delta': {'audio': {'data': fragment}}}]}
            lines.extend([('data: ' + json.dumps(chunk) + '\n').encode(), b'\n'])
        lines.extend([b'data: [DONE]\n', b'\n'])
        self.assertEqual(core.decode_music_stream(lines), b'RIFF test music')
        with self.assertRaises(RuntimeError):
            core.decode_music_stream(lines[:-2])
        with self.assertRaises(RuntimeError):
            core.decode_music_stream([b'data: {"error":{"message":"failed"}}\n', b'\n'])

    def test_music_prompt_backfill_and_summary_for_existing_plan(self):
        path = core.create_plan(config(), self.root, FakePlanner(), lambda x: None)
        p = core.read_json(path)
        p['settings'].update(music_enabled=True, music_model='google/lyria', music_volume=15)
        core.save_json(path, p)
        class MusicPlanner:
            calls = 0
            def json_chat(self, model, prompt):
                self.calls += 1
                return {'music_prompt': 'Soft piano, warm strings, 80 BPM.', 'music_summary': 'Warm hopeful music'}
        planner = MusicPlanner()
        core.complete_prompts(path, planner, lambda x: None)
        core.complete_prompts(path, planner, lambda x: None)
        self.assertEqual(planner.calls, 2)
        summary = core.read_json(self.root / 'music_prompts.json')
        self.assertIn('No vocals', summary[0]['request_prompt'])
        self.assertIn('80 BPM', (self.root / 'music_prompts.md').read_text())

    def test_export_xml_handles_spaces_and_ampersands(self):
        path = core.create_plan(config(), self.root / 'space & ไทย', FakePlanner(), lambda x: None)
        p = core.read_json(path)
        video = path.parent / 'test.mp4'
        video.write_bytes(b'fake')
        for e in p['episodes']:
            e['file_path'] = str(video)
        core.save_json(path, p)
        oauth = self.root / 'oauth.json'
        oauth.write_text('{}')
        manifest = core.export_upload(path, oauth, self.root / 'token.json')
        tree = ET.parse(manifest.with_name('upload_task.xml'))
        arguments = tree.find('.//{*}Arguments').text
        self.assertIn('space & ไทย', arguments)
        self.assertEqual(len(core.read_json(manifest)['items']), 2)
        compile(manifest.with_name('run_upload.py').read_text(), 'run_upload.py', 'exec')

    def make_manifest(self):
        manifest = self.root / 'upload_manifest.json'
        token = self.root / 'token.json'
        token.write_text('{}')
        video = self.root / 'video.mp4'
        video.write_bytes(b'test')
        core.save_json(manifest, {'oauth': 'unused', 'token': str(token), 'timezone': 'Asia/Bangkok', 'items': [{'day': 1, 'file_path': str(video), 'publish_at': config()['start']}]})
        return manifest

    def test_upload_success_is_not_repeated(self):
        manifest = self.make_manifest()
        uploader = types.SimpleNamespace(get_authenticated_service=lambda *a, **kw: object(), upload_single_video=lambda *a: 'video123')
        with patch.dict(sys.modules, {'upload_auto': uploader}):
            core.upload_manifest(manifest)
            uploader.upload_single_video = lambda *a: self.fail('duplicate upload')
            core.upload_manifest(manifest)
        self.assertEqual(core.read_json(self.root / 'upload_receipts.json')['1']['video_id'], 'video123')

    def test_ambiguous_upload_requires_review(self):
        manifest = self.make_manifest()
        uploader = types.SimpleNamespace(get_authenticated_service=lambda *a, **kw: object(), upload_single_video=lambda *a: None)
        with patch.dict(sys.modules, {'upload_auto': uploader}):
            with self.assertRaises(RuntimeError):
                core.upload_manifest(manifest)
            uploader.upload_single_video = lambda *a: self.fail('must not retry ambiguous upload')
            with self.assertRaises(RuntimeError):
                core.upload_manifest(manifest)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_ffmpeg_pipeline_and_resume(self):
        c = config()
        source = self.root / 'source.mp4'
        audio = self.root / 'voice.mp3'
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=240x320:d=1', '-c:v', 'libx264', source])
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', audio])
        path = self.root / 'project.sqlite3'
        core.save_json(path, {'settings': c, 'episodes': [{'day': 1, 'title': 'Test', 'scenes': [{'visual_prompt': 'A workshop', 'narration': 'Hello'}]}]})
        class VideoRouter:
            posts = 0
            def request(self, route, payload=None, binary=False):
                if route == '/videos/models':
                    return {'data': [{'id': 'test/video', 'supported_durations': [5]}]}
                if route == '/audio/speech/models':
                    return {'data': []}
                if route == '/videos':
                    self.posts += 1
                    return {'id': 'job1'}
                if route.endswith('/content?index=0'):
                    return source.read_bytes()
                if route.startswith('/videos/'):
                    return {'status': 'completed'}
                if route == '/audio/speech':
                    return audio.read_bytes()
                raise AssertionError(route)
        router = VideoRouter()
        core.generate(path, router, lambda x: None)
        core.generate(path, router, lambda x: None)
        final = self.root / 'day_01' / 'short.mp4'
        self.assertTrue(final.exists())
        thumbnail = Path(core.read_json(path)['episodes'][0]['thumbnail_path'])
        self.assertTrue(thumbnail.exists())
        self.assertEqual(thumbnail.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        self.assertAlmostEqual(core.media_duration(final), 5, delta=.2)
        self.assertEqual(router.posts, 1)
        episode = core.read_json(path)['episodes'][0]
        self.assertFalse(episode.get('render_incomplete'))
        self.assertFalse(core.needs_render_resume(path, episode))
        self.assertIn('Hello', (self.root / 'day_01' / 'captions.srt').read_text())
        # Add music to the existing MP4, then change volume without another paid generation.
        song = self.root / 'song.wav'
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=220:duration=0.8', song])
        p = core.read_json(path)
        p['settings'].update(music_enabled=True, music_model='test/music', music_volume=15, music_duck=True)
        p['episodes'][0]['music_prompt'] = 'Gentle strings, instrumental only.'
        core.save_json(path, p)
        original_request = router.request
        def request(route, payload=None, binary=False):
            if route == '/models?output_modalities=audio':
                return {'data': [{'id': 'test/music', 'description': 'Music generation'}]}
            return original_request(route, payload, binary)
        router.request = request
        with patch.object(router, 'music', return_value=song.read_bytes(), create=True) as paid_music:
            core.generate(path, router, lambda x: None)
            mixed = final.read_bytes()
            core.generate(path, router, lambda x: None)
            self.assertEqual(paid_music.call_count, 1)
            self.assertEqual(mixed, final.read_bytes())
            p = core.read_json(path)
            p['settings']['music_volume'] = 30
            p['settings']['music_duck'] = False
            core.save_json(path, p)
            core.generate(path, router, lambda x: None)
            self.assertEqual(paid_music.call_count, 1)
            self.assertNotEqual(mixed, final.read_bytes())
            self.assertAlmostEqual(core.media_duration(final), 5, delta=.2)
        self.assertEqual(router.posts, 1)


if __name__ == '__main__':
    unittest.main()
