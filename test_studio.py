"""SQLite persistence and real Tk autosave checks, with no network calls."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import shorts_core as core
import shorts_studio
from studio_store import Library, protect_secret
from test_shorts import config, FakePlanner


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='studio_db_')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_pipeline_writes_sqlite_and_resumes_without_json_project(self):
        path = self.root / 'project.sqlite3'
        core.save_json(path, {'settings': config(), 'episodes': []})
        core.create_plan(config(), self.root, FakePlanner(), lambda _: None, project_path=path)
        self.assertEqual(len(core.read_json(path)['episodes']), 2)
        self.assertFalse((self.root / 'project.json').exists())
        with patch.object(FakePlanner, 'json_chat', side_effect=AssertionError('must not regenerate')):
            core.complete_prompts(path, FakePlanner(), lambda _: None)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_api_key_encrypted_for_windows_user(self):
        secret = 'unit-test-secret-not-a-real-api-key'
        protected = protect_secret(secret)
        self.assertNotIn(secret, protected)
        self.assertEqual(protect_secret(protected, decrypt=True), secret)

    def test_key_check_requires_authenticated_endpoint_and_catalog_loads_all(self):
        router = core.Router('fake-unit-test-key')
        with patch.object(router, 'request', return_value={'data': {'usage': 2}}) as request:
            self.assertEqual(router.check_key()['usage'], 2)
            request.assert_called_once_with('/key')
        router.key = ''
        with patch.object(router, 'request') as request, self.assertRaises(ValueError):
            router.check_key()
        request.assert_not_called()
        with patch.object(router, 'request', return_value={'data': [{'id': 'music/test', 'description': 'Music generation'}]}) as request:
            result = router.catalog()
            self.assertIn('all', result)
            self.assertEqual(len(result['music']), 1)
            self.assertEqual(request.call_count, 5)

    def test_episode_music_model_override_and_review_before_any_video_post(self):
        path = self.root / 'project.sqlite3'
        c = config()
        c.update(music_enabled=False, music_model='default/music', music_review_required=True)
        e = {'day': 1, 'title': 'Episode', 'scenes': [{'visual_prompt': 'Workshop', 'narration': 'Hello'}],
             'music_prompt': 'Piano', 'music_config': {'music_enabled': True, 'music_model': 'episode/music'}}
        core.save_json(path, {'settings': c, 'episodes': [e]})
        class Router:
            def request(self, route, *args, **kwargs):
                if route == '/videos/models':
                    return {'data': [{'id': 'test/video', 'supported_durations': [5]}]}
                if route == '/models?output_modalities=audio':
                    return {'data': [{'id': 'episode/music', 'description': 'Music generation'}]}
                raise AssertionError('Should stop before any video or music POST')
        with self.assertRaisesRegex(ValueError, 'Prompt'):
            core.generate(path, Router(), lambda _: None)
        self.assertEqual(core.episode_settings(c, e)['music_model'], 'episode/music')


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='studio_gui_')
        self.app = shorts_studio.Studio(self.temp.name)
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_typing_autosaves_and_api_defaults_reused_for_new_series(self):
        self.app.vars['series'].set('ซีรีส์นักประดิษฐ์')
        self.app.vars['text_model'].set('saved/text-model')
        self.app.idea_editor.insert('1.0', 'ประวัตินักประดิษฐ์ 30 คน')
        self.app.update()
        self.app.after(750, self.app.quit)
        self.app.mainloop()
        stored = core.read_json(self.app.project)
        self.assertEqual(stored['settings']['series'], 'ซีรีส์นักประดิษฐ์')
        self.assertIn('30', stored['settings']['idea'])
        self.app.create_series('Second series')
        self.assertEqual(self.app.vars['text_model'].get(), 'saved/text-model')
        self.assertEqual(len(self.app.notebook.tabs()), 7)
        self.assertFalse(hasattr(self.app, 'area'))
        self.app.close()
        self.app = shorts_studio.Studio(self.temp.name)
        self.app.withdraw()
        self.app.update()
        self.assertEqual(self.app.vars['series'].get(), 'Second series')
        self.assertEqual(self.app.vars['text_model'].get(), 'saved/text-model')

    def test_scene_fields_and_per_episode_music_autosave_without_json_editor(self):
        p = {'settings': dict(shorts_studio.defaults(), **config()), 'episodes': [
            {'day': 1, 'title': 'Edison', 'subject': 'Edison', 'publish_at': config()['start'],
             'scenes': [{'visual_prompt': 'Workshop', 'narration': 'Hello'}]}]}
        core.save_json(self.app.project, p)
        self.app.open_series(self.app.project, 1)
        self.app.update()
        visual, narration = self.app.scene_editors[0]
        narration.delete('1.0', 'end')
        narration.insert('1.0', 'เสียงพากย์ที่แก้ไข')
        self.app.music_vars['music_enabled'].set(True)
        self.app.music_vars['music_model'].set('episode/music')
        self.app.music_editor.insert('1.0', 'Soft strings, no vocals')
        self.app.update()
        self.app.after(750, self.app.quit)
        self.app.mainloop()
        e = core.read_json(self.app.project)['episodes'][0]
        self.assertEqual(e['scenes'][0]['narration'], 'เสียงพากย์ที่แก้ไข')
        self.assertEqual(e['music_config']['music_model'], 'episode/music')
        self.assertEqual(e['music_prompt'], 'Soft strings, no vocals')
        self.assertNotIn('music_approved', e)
        self.assertFalse(hasattr(self.app, 'editor'))

    def test_check_button_loads_models_and_persists_cache(self):
        catalog = {section: [{'id': section + '/test'}] for section in ('text', 'video', 'speech', 'music', 'audio', 'all')}
        self.app.vars['api_key'].set('fake-check-key')
        with patch.object(core.Router, 'check_key', return_value={'usage': 2, 'limit_remaining': 8}) as check, patch.object(core.Router, 'catalog', return_value=catalog) as load:
            self.app.check_key_button.invoke()
            self.app.after(500, self.app.quit)
            self.app.mainloop()
        check.assert_called_once()
        load.assert_called_once()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.checked_key, 'fake-check-key')
        self.assertTrue(any(value.startswith('text/test | ') for value in self.app.combos['text_model']['values']))
        self.assertEqual(self.app.library.get('catalog')['all'][0]['id'], 'all/test')
        self.app.vars['api_key'].set('different-test-key')
        self.assertIn('ยังไม่ได้ตรวจ', self.app.key_status.get())

    def test_calendar_and_time_picker_autosaves_selected_value(self):
        picker = next(w for w in self.app.schedule_card.winfo_children() if isinstance(w, shorts_studio.DateTimePicker))
        picker.open()
        picker.year, picker.month = 2028, 2
        picker.select_day(29)
        picker.hour.set('18')
        picker.minute.set('45')
        picker.close()
        self.app.after(750, self.app.quit)
        self.app.mainloop()
        self.assertEqual(core.read_json(self.app.project)['settings']['start'], '2028-02-29 18:45')

    def test_oauth_picker_validates_desktop_client_and_autosaves(self):
        path = Path(self.temp.name) / 'oauth-test.json'
        import json
        path.write_text(json.dumps({'installed': {k: 'test' for k in ('client_id', 'client_secret', 'auth_uri', 'token_uri')}}), encoding='utf-8')
        self.app.notebook.select(self.app.pages['upload'])
        with patch.object(shorts_studio.filedialog, 'askopenfilename', return_value=str(path)):
            self.app.oauth_picker_button.invoke()
        self.assertEqual(core.read_json(self.app.project)['settings']['oauth'], str(path.resolve()))
        path.write_text('{"token": "wrong-file"}', encoding='utf-8')
        self.app.vars['oauth'].set('previous.json')
        with patch.object(shorts_studio.filedialog, 'askopenfilename', return_value=str(path)), patch.object(shorts_studio.messagebox, 'showerror') as error:
            self.app.pick_oauth()
        error.assert_called_once()
        self.assertEqual(self.app.vars['oauth'].get(), 'previous.json')
        with patch.object(shorts_studio.filedialog, 'askopenfilename', return_value=''):
            self.app.pick_oauth()
        self.assertEqual(self.app.vars['oauth'].get(), 'previous.json')

    def test_single_video_mode_persists_one_episode_and_toolbar_has_only_creation(self):
        with patch.object(shorts_studio.simpledialog, 'askstring', return_value='One video'):
            self.app.single_video_button.invoke()
        self.app.flush()
        self.assertEqual(self.app.document['settings']['mode'], 'single')
        self.assertEqual(self.app.vars['days'].get(), '1')
        self.assertEqual(self.app.idea_editor.get('1.0', 'end').strip(), 'One video')
        self.app.vars['days'].set('7')
        self.app.flush()
        saved = core.read_json(self.app.project)
        self.assertEqual(saved['settings']['days'], '1')
        self.app.open_series(self.app.project)
        self.assertEqual(self.app.vars['days'].get(), '1')
        self.assertEqual(len(self.app.new_series_button.master.winfo_children()), 2)
        self.assertTrue(str(self.app.plan_button).startswith(str(self.app.pages['settings'])))

    def test_manual_write_button_targets_selected_episode_only(self):
        document = {'settings': shorts_studio.defaults(), 'episodes': [
            {'day': 1, 'title': 'One', 'subject': 'One'}, {'day': 2, 'title': 'Two', 'subject': 'Two'}]}
        core.save_json(self.app.project, document)
        self.app.open_series(self.app.project, 2)
        with patch.object(core, 'complete_prompts') as write:
            self.app.write_episode_button.invoke()
            self.app.after(250, self.app.quit)
            self.app.mainloop()
        self.assertEqual(write.call_args.kwargs['only_day'], 2)
        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)
        self.assertFalse(any(isinstance(w, shorts_studio.ttk.Button) and w.cget('text') == 'เขียนบทที่เหลือ' for w in walk(self.app)))

    def test_ai_prompt_requires_apply_and_completed_scene_narration_is_editable(self):
        document = {'settings': shorts_studio.defaults(), 'episodes': [{'day': 1, 'title': 'Test',
            'scenes': [{'visual_prompt': 'Original English prompt', 'narration': 'Hello', 'job_id': 'old-job'}]}]}
        core.save_json(self.app.project, document)
        self.app.open_series(self.app.project, 1)
        visual, narration = self.app.scene_editors[0]
        self.assertEqual(str(visual.cget('state')), 'normal')
        self.assertEqual(str(narration.cget('state')), 'normal')
        self.app.vars['api_key'].set('fake-ai-edit-key')
        self.app.vars['text_model'].set('fake/text')
        self.app.edit_scene_prompt(1)
        dialog = self.app.ai_prompt_dialog
        dialog['instructions'].insert('1.0', 'เปลี่ยนมุมกล้อง')
        with patch.object(core.Router, 'json_chat', return_value={'visual_prompt': 'New English close-up prompt'}), patch.object(core.Router, 'request', side_effect=AssertionError('must not generate video')):
            dialog['generate'].invoke()
            self.app.after(300, self.app.quit)
            self.app.mainloop()
        self.assertEqual(visual.get('1.0', 'end').strip(), 'Original English prompt')
        self.assertEqual(dialog['proposal'].get('1.0', 'end').strip(), 'New English close-up prompt')
        dialog['apply'].invoke()
        self.assertEqual(core.read_json(self.app.project)['episodes'][0]['scenes'][0]['visual_prompt'], 'New English close-up prompt')
        narration.delete('1.0', 'end')
        narration.insert('1.0', 'Edited speech')
        self.app.flush()
        self.assertEqual(core.read_json(self.app.project)['episodes'][0]['scenes'][0]['narration'], 'Edited speech')

    def test_progress_bar_is_green_determinate_and_reports_percent(self):
        self.assertEqual(str(self.app.progress.cget('mode')), 'determinate')
        self.assertEqual(shorts_studio.ttk.Style(self.app).lookup('Green.Horizontal.TProgressbar', 'background'), '#22c55e')
        self.app.report_progress(37.5)
        self.app.after(150, self.app.quit)
        self.app.mainloop()
        self.assertEqual(self.app.progress_value.get(), 37.5)
        self.assertEqual(self.app.progress_text.get(), '38%')

    def test_render_button_shows_resume_for_saved_error_or_legacy_progress(self):
        document = {'settings': shorts_studio.defaults(), 'episodes': [
            {'day': 1, 'title': 'One', 'scenes': [{'visual_prompt': 'A', 'narration': 'Hello'}]}]}
        for progress in ('new', 'error', 'legacy'):
            with self.subTest(progress=progress):
                episode = document['episodes'][0]
                episode.pop('render_incomplete', None)
                episode['scenes'][0].pop('job_id', None)
                if progress == 'error':
                    episode['render_incomplete'] = True
                elif progress == 'legacy':
                    episode['scenes'][0]['job_id'] = 'existing-job'
                core.save_json(self.app.project, document)
                self.app.open_series(self.app.project, 1)
                self.assertEqual(self.app.render_episode_button.cget('text'), 'สร้างคลิปตอนนี้' if progress == 'new' else 'สร้างคลิปต่อ')

    def test_search_finds_series_and_episodes_across_all_projects(self):
        first = self.app.project
        core.save_json(first, {'settings': dict(shorts_studio.defaults(), series='Inventors'), 'episodes': [
            {'day': 2, 'title': 'Edison', 'subject': 'แสงสว่าง'}, {'day': 12, 'title': 'Tesla'}]})
        self.app.library.register(first, 'Inventors')
        self.app.create_series('Robotics')
        second = self.app.project
        core.save_json(second, {'settings': dict(shorts_studio.defaults(), series='Robotics'), 'episodes': [
            {'day': 1, 'title': 'Walking Robot', 'subject': 'Humanoid'}]})
        self.app.open_series(first)
        def search(text, scope='ทั้งหมด'):
            self.app.search_query.set(text)
            self.app.search_scope.set(scope)
            self.app.apply_search()
            return [(Path(path), day) for path, day, kind in self.app.nodes.values() if kind == 'episode']
        self.assertEqual(search('robot'), [(second, 1)])
        result_node = next(node for node, (_, _, kind) in self.app.nodes.items() if kind == 'episode')
        self.app.tree.selection_set(result_node)
        self.app.select_node()
        self.assertEqual(self.app.project, second)
        self.assertEqual(self.app.day, 1)
        self.assertEqual(search('EDISON'), [(first, 2)])
        self.assertEqual(search('แสงสว่าง', 'ตอน'), [(first, 2)])
        self.assertEqual(search('EP 02', 'ตอน'), [(first, 2)])
        self.assertEqual(search('Inventors', 'Series'), [(first, 2), (first, 12)])
        self.assertEqual(search('not found'), [])
        self.assertIn('ไม่พบ', self.app.search_result.get())
        self.app.clear_search()
        self.assertEqual(sum(kind == 'episode' for _, _, kind in self.app.nodes.values()), 3)

    def test_search_preserves_unsaved_edits_and_can_open_matching_episode(self):
        self.app.vars['series'].set('Edited Series')
        self.app.idea_editor.insert('1.0', 'An unsaved idea')
        self.app.search_query.set('Edited')
        self.app.apply_search()
        self.assertIn('unsaved idea', core.read_json(self.app.project)['settings']['idea'])
        self.assertTrue(self.app.tree.get_children())
        self.app.search_query.set('does not exist')
        self.app.apply_search()
        self.assertFalse(self.app.tree.get_children())
        self.assertIn('unsaved idea', self.app.idea_editor.get('1.0', 'end'))

    def test_model_selection_displays_prices_but_saves_only_raw_api_id(self):
        self.app.catalog['video'] = [{'id': 'example/video', 'pricing_skus': {'per-video-second': '0.5'},
                                     'supported_durations': [5], 'supported_resolutions': ['480p']}]
        self.app.build_form()
        combo = self.app.combos['video_model']
        self.assertIn('$0.5', combo['values'][0])
        combo.current(0)
        combo.event_generate('<<ComboboxSelected>>')
        self.app.update()
        self.assertEqual(self.app.vars['video_model'].get(), 'example/video')
        self.assertEqual(self.app.vars['duration'].get(), '5')
        self.app.flush()
        self.assertEqual(core.read_json(self.app.project)['settings']['video_model'], 'example/video')

    def test_token_picker_does_not_use_oauth_client_as_token_destination(self):
        path = str(Path(self.temp.name) / 'client.json')
        self.app.vars['oauth'].set(path)
        previous = self.app.vars['token'].get()
        with patch.object(shorts_studio.filedialog, 'asksaveasfilename', return_value=path), patch.object(shorts_studio.messagebox, 'showerror') as error:
            self.app.token_picker_button.invoke()
        error.assert_called_once()
        self.assertEqual(self.app.vars['token'].get(), previous)
        destination = str(Path(self.temp.name) / 'youtube-token.json')
        with patch.object(shorts_studio.filedialog, 'asksaveasfilename', return_value=destination):
            self.app.pick_token()
        self.assertEqual(core.read_json(self.app.project)['settings']['token'], destination)

    def test_scene_navigation_preserves_edits_and_position_after_rebuild(self):
        document = {'settings': shorts_studio.defaults(), 'episodes': [
            {'day': 1, 'title': 'Test', 'scenes': [{'visual_prompt': str(i), 'narration': 'Hello'} for i in range(3)]}]}
        core.save_json(self.app.project, document)
        self.app.open_series(self.app.project, 1)
        self.app.notebook.select(self.app.pages['scenes'])
        self.assertTrue(self.app.scene_prev.instate(['disabled']))
        self.app.scene_next.invoke()
        self.app.scene_editors[1][1].insert('end', ' edited')
        self.app.flush()
        self.app.build_form()
        self.assertEqual(self.app.notebook.select(), str(self.app.pages['scenes']))
        self.assertEqual(self.app.scene_selector.current(), 1)
        self.assertEqual(self.app.scene_position.get(), '2 / 3')
        self.assertIn('edited', self.app.scene_editors[1][1].get('1.0', 'end'))
        self.app.scene_next.invoke()
        self.assertTrue(self.app.scene_next.instate(['disabled']))

    def test_workflow_tabs_fit_without_page_scrolling(self):
        p = {'settings': dict(shorts_studio.defaults(), **config()), 'episodes': [
            {'day': 1, 'title': 'Edison', 'subject': 'Edison', 'publish_at': config()['start'],
             'scenes': [{'visual_prompt': 'Workshop', 'narration': 'Hello'} for _ in range(12)]}]}
        core.save_json(self.app.project, p)
        self.app.open_series(self.app.project, 1)
        self.app.geometry('1280x850')
        self.app.deiconify()
        self.app.update()
        self.assertEqual(len(self.app.scene_selector['values']), 12)
        for key, page in self.app.pages.items():
            self.app.notebook.select(page)
            self.app.update()
            def walk(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from walk(child)
            for child in walk(page):
                if child.winfo_ismapped() and isinstance(child, (shorts_studio.ttk.Button, shorts_studio.tk.Text)):
                    self.assertLessEqual(child.winfo_rooty()+child.winfo_height(), page.winfo_rooty()+page.winfo_height()+3, f'{key}: widget clipped at bottom')
                    self.assertLessEqual(child.winfo_rootx()+child.winfo_width(), page.winfo_rootx()+page.winfo_width()+3, f'{key}: widget clipped at right')

    def test_completed_scene_and_final_have_thumbnail_and_play_without_regeneration(self):
        folder = self.app.project.parent / 'day_01'
        folder.mkdir()
        for name in ('01.render.mp4', 'short.mp4'):
            video = folder / name
            video.write_bytes(b'fixture: player mocked')
            image = shorts_studio.tk.PhotoImage(master=self.app, width=192, height=256)
            image.put('#336699', to=(0, 0, 192, 256))
            image.write(str(video.with_name(video.stem + '_thumbnail.png')), format='png')
        episode = {'day': 1, 'title': 'Test', 'file_path': str(folder / 'short.mp4'),
                   'scenes': [{'visual_prompt': 'A', 'narration': 'Hello', 'file_path': str(folder / '01.render.mp4')},
                              {'visual_prompt': 'B', 'narration': 'World'}]}
        core.save_json(self.app.project, {'settings': shorts_studio.defaults(), 'episodes': [episode]})
        with patch.object(core, 'ensure_thumbnail', side_effect=AssertionError('existing thumbnails should be reused')):
            self.app.open_series(self.app.project, 1)
            self.app.update()
        ready = [v for v in self.app.media_views if v['stamp'] is not None]
        self.assertEqual(len(ready), 2)
        for view in ready:
            self.assertTrue(view['label'].cget('image'))
            with patch.object(shorts_studio.os, 'startfile') as play:
                view['play'].invoke()
            play.assert_called_once_with(view['video'])
        waiting = next(v for v in self.app.media_views if v['title'] == 'Scene 02')
        self.assertTrue(waiting['play'].instate(['disabled']))
        self.assertTrue(str(self.app.render_episode_button).startswith(str(self.app.pages['summary'])))
        self.assertTrue(str(self.app.schedule_card).startswith(str(self.app.pages['upload'])))

    def test_live_log_is_visible_during_worker_and_survives_popup_reopen(self):
        import threading
        gate = threading.Event()
        def worker():
            self.app.log('Planning episode 1')
            gate.wait(3)
            self.app.log('Planning complete')
        try:
            self.app.work(worker)
            self.app.after(250, self.app.quit)
            self.app.mainloop()
            self.assertTrue(self.app.busy)
            self.assertIn('Planning episode 1', self.app.logs.get('1.0', 'end'))
            self.assertIn('Planning episode 1', self.app.popup_logs.get('1.0', 'end'))
            self.app.status_popup.destroy()
            self.app.show_status()
            self.assertIn('Planning episode 1', self.app.popup_logs.get('1.0', 'end'))
        finally:
            gate.set()
            self.app.after(500, self.app.quit)
            self.app.mainloop()
        self.assertFalse(self.app.busy)
        self.assertIn('Planning complete', self.app.logs.get('1.0', 'end'))

    def test_prompt_translation_popup_does_not_change_source_and_reuses_cache(self):
        self.app.vars['api_key'].set('fake-translation-key')
        self.app.vars['text_model'].set('fake/text')
        original = self.app.style_editor.get('1.0', 'end')
        with patch.object(core.Router, 'json_chat', return_value={'translation': 'คำแปลทดสอบ'}) as request:
            self.app.translate_prompt(self.app.style_editor)
            self.app.after(300, self.app.quit)
            self.app.mainloop()
            self.assertIn('คำแปลทดสอบ', self.app.translation_cache.values())
            self.app.translate_prompt(self.app.style_editor)
            self.app.after(150, self.app.quit)
            self.app.mainloop()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.app.style_editor.get('1.0', 'end'), original)

    def test_open_latest_video_folder_uses_latest_video_directory(self):
        folder = self.app.project.parent / 'day_01' / 'versions'
        folder.mkdir(parents=True)
        (folder / 'final_v002.mp4').write_bytes(b'fixture')
        with patch.object(shorts_studio.os, 'startfile') as open_folder:
            self.app.open_latest_video_folder()
        open_folder.assert_called_once_with(str(folder))

    def test_oauth_check_displays_green_status_and_resets_when_files_change(self):
        channel = {'id': 'channel-test', 'title': 'Test channel'}
        with patch.object(shorts_studio.youtube_actions, 'authenticated_channel', return_value=(None, channel)):
            self.app.oauth_check_button.invoke()
            self.app.after(300, self.app.quit)
            self.app.mainloop()
        self.assertIn('Test channel', self.app.oauth_message.get())
        self.assertEqual(str(self.app.oauth_label.cget('foreground')), '#16803c')
        self.app.vars['token'].set('another-token.json')
        self.assertEqual(str(self.app.oauth_label.cget('foreground')), '#666666')

    def test_publish_selected_episode_makes_persistent_copy_link_available(self):
        document = {'settings': shorts_studio.defaults(), 'episodes': []}
        for day in (1, 2):
            video = self.app.project.parent / f'final-{day}.mp4'
            video.write_bytes(b'fixture')
            document['episodes'].append({'day': day, 'title': f'EP {day}', 'file_path': str(video), 'scenes': []})
            image = shorts_studio.tk.PhotoImage(master=self.app, width=192, height=256)
            image.write(str(video.with_name(video.stem + '_thumbnail.png')), format='png')
        core.save_json(self.app.project, document)
        self.app.open_series(self.app.project, 1)
        self.app.upload_selector.current(1)
        receipt = {'state': 'uploaded', 'video_id': 'id-2', 'url': 'https://www.youtube.com/watch?v=id-2'}
        def upload(path, day, privacy, log):
            saved = core.read_json(path)
            saved['episodes'][1]['youtube_uploads'] = [receipt]
            core.save_json(path, saved)
            return receipt, {'id': 'channel-test', 'title': 'Test channel'}
        with patch.object(shorts_studio.youtube_actions, 'publish_now', side_effect=upload) as publish:
            self.app.publish_button.invoke()
            self.app.after(450, self.app.quit)
            self.app.mainloop()
        self.assertEqual(publish.call_args.args[1:3], (2, 'public'))
        self.assertEqual(self.app.day, 2)
        self.assertFalse(self.app.copy_youtube_button.instate(['disabled']))
        self.app.copy_youtube_button.invoke()
        self.assertEqual(self.app.clipboard_get(), receipt['url'])
        self.app.build_form()
        self.assertEqual(self.app.youtube_url.get(), receipt['url'])


if __name__ == '__main__':
    unittest.main()
