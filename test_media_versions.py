"""Offline integration tests using real FFmpeg and fake speech responses."""
from pathlib import Path
import shutil
import subprocess
import array
import math
import os
import tempfile
import unittest
from unittest.mock import Mock
import shorts_core as core
import media_versions as versions
from test_shorts import config


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MediaVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clip_versions_')
        self.root = Path(self.temp.name)
        self.path = self.root / 'project.sqlite3'
        self.folder = self.root / 'day_01'
        self.folder.mkdir()
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=240x320:d=1', '-c:v', 'libx264', self.folder / '01.mp4'])
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', self.folder / '01.mp3'])
        self.original_audio = (self.folder / '01.mp3').read_bytes()
        shutil.copyfile(self.folder / '01.mp4', self.folder / 'short.mp4')
        self.document = {'settings': config(), 'episodes': [{'day': 1, 'title': 'Test',
            'file_path': str(self.folder / 'short.mp4'), 'scenes': [{'visual_prompt': 'Blue frame', 'narration': 'Hello'}]}]}
        core.save_json(self.path, self.document)

    def tearDown(self):
        self.temp.cleanup()

    def test_audio_regenerations_and_final_versions_never_overwrite_history(self):
        new_audio = self.root / 'new.mp3'
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=880:duration=1.4', new_audio])
        router = Mock()
        router.request.return_value = new_audio.read_bytes()
        e = self.document['episodes'][0]
        e['render_incomplete'] = True
        s = e['scenes'][0]
        versions.create_audio(self.path, self.document, e, s, 1, router, lambda _: None)
        versions.create_audio(self.path, self.document, e, s, 1, router, lambda _: None)
        self.assertEqual([r['version'] for r in s['audio_history']], [1, 2, 3])
        self.assertEqual(Path(s['audio_history'][0]['file_path']).read_bytes(), self.original_audio)
        self.assertEqual(Path(s['audio_history'][1]['file_path']).read_bytes(), new_audio.read_bytes())
        self.assertEqual((self.folder / '01.mp3').read_bytes(), self.original_audio)
        self.assertEqual(router.request.call_count, 2)
        self.assertTrue(all(call.args[0] == '/audio/speech' for call in router.request.call_args_list))
        first = versions.remix(self.path, 1, log=lambda _: None)
        first_bytes = first.read_bytes()
        second = versions.remix(self.path, 1, log=lambda _: None)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), first_bytes)
        p = core.read_json(self.path)
        self.assertEqual(p['episodes'][0]['video_version'], 3)
        self.assertEqual(p['episodes'][0]['file_path'], str(second))
        self.assertFalse(p['episodes'][0].get('render_incomplete'))
        output = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', str(second)], text=True)
        self.assertIn('audio', output)
        self.assertIn('video', output)
        self.assertAlmostEqual(core.media_duration(second), 5, delta=.2)
        self.assertEqual(p['episodes'][0]['video_history'][-1]['audio_files'], [s['audio_file']])

    def test_failed_speech_keeps_previous_usable_take(self):
        e = self.document['episodes'][0]
        s = e['scenes'][0]
        previous = versions.archive_audio(self.path, self.document, e, s, 1)
        router = Mock()
        router.request.return_value = b''
        with self.assertRaises(ValueError):
            versions.create_audio(self.path, self.document, e, s, 1, router, lambda _: None)
        saved = core.read_json(self.path)['episodes'][0]['scenes'][0]
        self.assertEqual(saved['audio_file'], str(previous))
        self.assertEqual(len(saved['audio_history']), 1)

    def music_fixture(self):
        older = self.folder / 'background_old.wav'
        latest = self.folder / 'background_latest.wav'
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=700:duration=1', older])
        core.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=1000:duration=1', latest])
        os.utime(older, (100, 100))
        self.document['episodes'][0]['music_file'] = str(older)
        self.document['episodes'][0]['music_config'] = {'music_enabled': False, 'music_volume': 15, 'music_duck': True}
        core.save_json(self.path, self.document)
        return latest

    @staticmethod
    def tone_amplitude(video, frequency, start, seconds=.3):
        pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', str(start), '-i', str(video),
            '-t', str(seconds), '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', 'pipe:1'])
        samples = array.array('f')
        samples.frombytes(pcm)
        real = sum(value * math.cos(2 * math.pi * frequency * i / 16000) for i, value in enumerate(samples))
        imag = sum(value * math.sin(2 * math.pi * frequency * i / 16000) for i, value in enumerate(samples))
        return 2 * math.hypot(real, imag) / len(samples)

    def test_background_button_uses_latest_song_even_if_music_was_disabled(self):
        latest = self.music_fixture()
        messages = []
        output = versions.remix(self.path, 1, include_music=True, log=messages.append)
        episode = core.read_json(self.path)['episodes'][0]
        self.assertEqual(episode['music_file'], str(latest))
        self.assertTrue(episode['music_config']['music_enabled'])
        self.assertIn(latest.name, '\n'.join(messages))
        # Verify the actual 1000 Hz background, not merely the existence of an audio stream.
        self.assertGreater(self.tone_amplitude(output, 1000, .5), .001)
        self.assertGreater(self.tone_amplitude(output, 1000, 2.0), .005)
        self.assertGreater(self.tone_amplitude(output, 440, .5), .01)
        self.assertEqual(episode['video_history'][-1]['music_file'], str(latest))

    def test_scene_remix_also_contains_background_and_preserves_voice(self):
        latest = self.music_fixture()
        output = versions.remix(self.path, 1, scene_index=1, include_music=True, log=lambda _: None)
        scene = core.read_json(self.path)['episodes'][0]['scenes'][0]
        self.assertEqual(scene['file_path'], str(output))
        self.assertEqual(scene['video_history'][-1]['music_file'], str(latest))
        self.assertGreater(self.tone_amplitude(output, 1000, .5), .001)
        self.assertGreater(self.tone_amplitude(output, 440, .5), .01)

    def test_requested_background_cannot_silently_skip_missing_or_muted_music(self):
        original = (self.folder / 'short.mp4').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Music'):
            versions.remix(self.path, 1, include_music=True, log=lambda _: None)
        self.assertEqual((self.folder / 'short.mp4').read_bytes(), original)
        self.music_fixture()
        document = core.read_json(self.path)
        document['episodes'][0]['music_config']['music_volume'] = 0
        core.save_json(self.path, document)
        with self.assertRaisesRegex(ValueError, '0'):
            versions.remix(self.path, 1, include_music=True, log=lambda _: None)
        self.assertEqual((self.folder / 'short.mp4').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
