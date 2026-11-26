"""HTTP/WebSocket contract checks, runnable once uv sync installs dependencies."""
import json
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'soundfile', 'qrcode'))

@unittest.skipUnless(AVAILABLE, 'Run uv sync to install API test dependencies')
class APITests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from ktvibes import main
        from ktvibes.queue import State
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        async def idle_worker(*args):
            await asyncio.Event().wait()
        self.patches = [patch.object(main, 'state', State()), patch.object(main, 'CACHE', Path(self.temp.name)),
                        patch.object(main.worker, 'run', idle_worker)]
        for item in self.patches:
            item.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_update_pulls_only_a_clean_checkout(self):
        import subprocess
        root = Path(self.temp.name)
        (root / '.git').mkdir()
        calls = []
        def fake_git(command, **kwargs):
            calls.append(command[3])
            output = {'status': ' M ktvibes/main.py', 'rev-parse': 'abc1234'}.get(command[3], '')
            return subprocess.CompletedProcess(command, 0, output, '')
        with patch.object(self.main, 'ROOT', root), patch('subprocess.run', fake_git):
            with self.assertRaises(SystemExit):
                self.main.update()
            self.assertNotIn('pull', calls)

    def test_lyrics_can_be_searched_and_chosen_by_hand(self):
        import json
        folder = Path(self.temp.name) / 'aaaaaaaaaaa'
        folder.mkdir()
        (folder / 'meta.json').write_text(json.dumps({'artist': 'Simple Plan', 'title': 'Perfect', 'duration': 280}), encoding='utf-8')
        record = {'id': 7, 'trackName': 'Perfect', 'artistName': 'Simple Plan', 'duration': 278, 'syncedLyrics': '[00:10.00]Hey dad look at me'}
        async def search(query, duration):
            self.assertEqual(query, 'simple plan perfect')
            return [record]
        async def fetch(record_id):
            return record if record_id == 7 else None
        with patch.object(self.main.lyrics, 'search', search), patch.object(self.main.lyrics, 'record', fetch):
            options = self.client.get('/api/songs/aaaaaaaaaaa/lyrics', params={'q': ' simple plan perfect '}).json()
            self.assertEqual((options[0]['id'], options[0]['difference'], options[0]['preview']), (7, -2, ['Hey dad look at me']))
            self.assertEqual(self.client.post('/api/songs/aaaaaaaaaaa/lyrics', json={'id': 8}).status_code, 404)
            self.assertEqual(self.client.post('/api/songs/aaaaaaaaaaa/lyrics', json={'id': 7}).status_code, 200)
        self.assertEqual((folder / 'lyrics.lrc').read_text(encoding='utf-8'), record['syncedLyrics'])
        self.assertEqual(json.loads((folder / 'meta.json').read_text(encoding='utf-8'))['lyrics_identity'], ['Simple Plan', 'Perfect'])

    def test_recent_lists_played_songs_newest_first(self):
        import json
        cache = Path(self.temp.name)
        for index, (video_id, stems) in enumerate([('aaaaaaaaaaa', True), ('bbbbbbbbbbb', True), ('ccccccccccc', False), ('ddddddddddd', True)]):
            folder = cache / video_id
            folder.mkdir()
            (folder / 'meta.json').write_text(json.dumps({'artist': '아이유', 'title': f'Song {index}'}), encoding='utf-8')
            if stems:
                (folder / 'no_vocals.wav').touch()
                (folder / 'vocals.wav').touch(); (folder / 'timed.json').write_text('{"lines": [{"t": 1, "text": "a"}]}', encoding='utf-8')  # karaoke-server: singable
        # ddd is prepared but never played; ccc was played but its stems are gone.
        self.main.state.played = {'aaaaaaaaaaa': 1.0, 'bbbbbbbbbbb': 2.0, 'ccccccccccc': 3.0}
        songs = self.client.get('/api/recent').json()
        self.assertEqual([s['id'] for s in songs], ['bbbbbbbbbbb', 'aaaaaaaaaaa'])
        self.assertEqual(songs[0]['artist'], '아이유')

    def test_songbook_lists_numbered_prepared_songs_in_order(self):
        import json
        cache = Path(self.temp.name)
        for video_id, number in [('aaaaaaaaaaa', 10002), ('bbbbbbbbbbb', 10001), ('ccccccccccc', None)]:
            folder = cache / video_id
            folder.mkdir()
            meta = {'artist': '아이유', 'title': video_id, **({'number': number} if number else {})}
            (folder / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
            (folder / 'no_vocals.wav').touch()
            (folder / 'vocals.wav').touch(); (folder / 'timed.json').write_text('{"lines": [{"t": 1, "text": "a"}]}', encoding='utf-8')  # karaoke-server: singable
        songs = self.client.get('/api/songbook').json()
        self.assertEqual([(s['number'], s['id']) for s in songs], [(10001, 'bbbbbbbbbbb'), (10002, 'aaaaaaaaaaa')])
        self.assertIsInstance(songs[0]['prepared'], float)

    def test_delete_song_removes_download_and_history(self):
        import json
        cache = Path(self.temp.name)
        folder = cache / 'aaaaaaaaaaa'
        folder.mkdir()
        (folder / 'meta.json').write_text(json.dumps({'artist': 'IU', 'title': 'Blueming'}), encoding='utf-8')
        (folder / 'no_vocals.flac').touch()
        (folder / 'vocals.flac').touch(); (folder / 'timed.json').write_text('{"lines": [{"t": 1, "text": "a"}]}', encoding='utf-8')  # karaoke-server: singable
        self.main.state.played = {'aaaaaaaaaaa': 1.0}
        self.assertEqual(len(self.client.get('/api/recent').json()), 1)
        self.assertEqual(self.client.delete('/api/songs/aaaaaaaaaaa').status_code, 200)
        self.assertFalse(folder.exists())
        self.assertEqual(self.main.state.played, {})
        self.assertEqual(self.client.get('/api/recent').json(), [])
        self.assertEqual(self.client.delete('/api/songs/aaaaaaaaaaa').status_code, 404)
        self.assertEqual(self.client.delete('/api/songs/..%2F..%2Fetc').status_code, 404)

    def test_delete_refuses_queued_song(self):
        song = {'id': 'abcdefghijk', 'artist': 'IU', 'title': '좋은 날'}
        (Path(self.temp.name) / song['id']).mkdir()
        self.client.post('/api/queue', json=song)
        response = self.client.delete(f"/api/songs/{song['id']}")
        self.assertEqual(response.status_code, 409)
        self.assertTrue((Path(self.temp.name) / song['id']).is_dir())

    def test_ambient_lists_cached_videos(self):
        cache = Path(self.temp.name)
        for video_id in ('bbbbbbbbbbb', 'aaaaaaaaaaa', 'not-an-id'):
            (cache / video_id).mkdir()
            (cache / video_id / 'video.mp4').touch()
            if video_id != 'aaaaaaaaaaa':  # a failed song leaves its video without stems
                for name in ('vocals', 'no_vocals'):
                    (cache / video_id / f'{name}.opus').touch()
        (cache / 'ccccccccccc').mkdir()  # audio only
        self.assertEqual(self.client.get('/api/ambient').json(), ['bbbbbbbbbbb'])

    def test_duplicate_queue_request_conflicts(self):
        song = {'id': 'abcdefghijk', 'artist': 'IU', 'title': '좋은 날'}
        self.assertEqual(self.client.post('/api/queue', json=song).status_code, 201)
        response = self.client.post('/api/queue', json=song)
        self.assertEqual(response.status_code, 409)
        self.assertIn('already', response.json()['detail'])

    def test_unicode_input_validation_and_static_pages(self):
        for route in ('/', '/tv', '/remote', '/static/tv.js', '/api/qr.svg'):
            self.assertEqual(self.client.get(route).status_code, 200)
        song = {'id': 'abcdefghijk', 'artist': '周杰倫', 'title': '晴天 / 좋은 날'}
        response = self.client.post('/api/queue', json=song)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['title'], song['title'])
        self.assertEqual(self.client.post('/api/queue', json={**song, 'id': '../secret'}).status_code, 422)
        self.assertEqual(self.client.post('/api/queue', json={**song, 'artist': '  '}).status_code, 422)

    def test_media_range_and_allowlist(self):
        folder = Path(self.temp.name) / 'abcdefghijk'
        folder.mkdir()
        (folder / 'video.mp4').write_bytes(b'0123456789')
        response = self.client.get('/media/abcdefghijk/video.mp4', headers={'Range': 'bytes=2-5'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'2345')
        self.assertEqual(response.headers['content-type'], 'video/mp4')
        self.assertEqual(self.client.get('/media/abcdefghijk/meta.json').status_code, 404)
        (folder / 'vocals.flac').write_bytes(b'fLaC')
        self.assertEqual(self.client.get('/media/abcdefghijk/vocals').headers['content-type'], 'audio/flac')
        (folder / 'vocals.opus').write_bytes(b'OggS')
        self.assertEqual(self.client.get('/media/abcdefghijk/vocals').headers['content-type'], 'audio/ogg')
        self.assertEqual(self.client.get('/media/abcdefghijk/vocals.flac').status_code, 404)

    def test_websocket_controls(self):
        with self.client.websocket_connect('/ws?role=tv') as tv:
            first = tv.receive_json()
            self.assertTrue(first['player_connected'])
            self.assertRegex(first['build'], r'^[0-9a-f]{12}$')  # pages reload when this changes
            tv.send_json({'action': 'vocal', 'value': .6})
            self.assertEqual(tv.receive_json()['vocal'], .6)
            tv.send_json({'action': 'offset', 'delta': .5})
            self.assertEqual(tv.receive_json()['offset'], .5)
            tv.send_json({'action': 'pause'})
            self.assertFalse(tv.receive_json()['playing'])
            tv.send_json({'action': 'vocal', 'value': 'NaN'})
            self.assertEqual(tv.receive_json()['type'], 'error')

    def test_reactions_reach_only_the_tv(self):
        with self.client.websocket_connect('/ws?role=tv') as tv:
            tv.receive_json()
            with self.client.websocket_connect('/ws?role=remote') as remote:
                tv.receive_json()
                remote.receive_json()
                revision = self.main.state.revision
                remote.send_json({'action': 'react', 'value': '🔥'})
                self.assertEqual(tv.receive_json(), {'type': 'react', 'value': '🔥'})
                self.assertEqual(self.main.state.revision, revision)  # no broadcast, nothing saved
                # karaoke-server: phones may cheer with their own emoji; plain text is still refused.
                remote.send_json({'action': 'react', 'value': '💩'})
                self.assertEqual(tv.receive_json(), {'type': 'react', 'value': '💩'})
                remote.send_json({'action': 'react', 'value': 'spam'})
                self.assertEqual(remote.receive_json()['message'], 'Unknown reaction')

    def test_newest_tv_takes_over(self):
        with self.client.websocket_connect('/ws?role=tv') as old:
            self.assertEqual(old.receive_json()['type'], 'state')
            with self.client.websocket_connect('/ws?role=tv') as new:
                self.assertEqual(new.receive_json()['type'], 'state')
                self.assertEqual(old.receive_json()['message'], 'Another device took over playback.')
                self.assertTrue(self.main.state.player is not None)

    def test_remote_url_uses_configured_port(self):
        with patch.object(self.main, 'PORT', 9999), patch.dict('os.environ', {'KTVIBES_REMOTE_URL': ''}):
            self.assertTrue(self.main.remote_url().endswith(':9999/remote'))
        for configured in ('http://pc:8765', 'http://pc:8765/', 'http://pc:8765/remote'):
            with patch.dict('os.environ', {'KTVIBES_REMOTE_URL': configured}):
                self.assertEqual(self.main.remote_url(), 'http://pc:8765/remote')

    def test_search_marks_prepared_songs(self):
        folder = Path(self.temp.name) / 'aaaaaaaaaaa'
        folder.mkdir()
        (folder / 'meta.json').write_text('{"artist": "Joji", "title": "Glimpse of Us"}', encoding='utf-8')
        (folder / 'no_vocals.wav').touch()
        (folder / 'vocals.wav').touch()
        # karaoke-server: only playable results are listed, so both have confirmed lyrics here
        results = [{'id': 'aaaaaaaaaaa', 'lyrics_match': True}, {'id': 'bbbbbbbbbbb', 'lyrics_match': True}]
        with patch.object(self.main.youtube, 'search', return_value=results), \
             patch.object(self.main, 'lyric_records', AsyncMock(return_value=[])), \
             patch.object(self.main, 'check_lyrics', lambda found, records: None):
            found = self.client.get('/api/search', params={'q': 'joji'}).json()
        self.assertEqual([r['cached'] for r in found], [True, False])

    def test_search_lists_only_playable_versions(self):
        # karaoke-server (user, 2026-11-22): "Show a list that is indeed playable"
        ready = Path(self.temp.name) / 'ccccccccccc'
        ready.mkdir()
        (ready / 'meta.json').write_text('{"artist": "LANY", "title": "Soft"}', encoding='utf-8')
        for name in ('no_vocals.wav', 'vocals.wav'):
            (ready / name).touch()
        (ready / 'timed.json').write_text('{"lines": [{"t": 10, "text": "a"}]}', encoding='utf-8')
        song = lambda i, d, title='Soft': {'id': i, 'title': f'LANY - {title}', 'channel': 'LANY', 'duration': d, 'kind': 'official',
                                           'parsed': {'artist': 'LANY', 'title': title}}
        results = [song('aaaaaaaaaaa', 178), song('bbbbbbbbbbb', 260), song('ccccccccccc', 999, 'Soft (Live)')]
        lrc = '[00:10.00]a\n[02:50.00]b'
        with patch.object(self.main.youtube, 'search', return_value=results), \
             patch.object(self.main, 'lyric_records', AsyncMock(return_value=[{'id': 1, 'artistName': 'LANY', 'trackName': 'Soft', 'duration': 179, 'syncedLyrics': lrc}])):
            found = self.client.get('/api/search', params={'q': 'lany soft'}).json()
        # 178 s matches the 179 s timing; 260 s (a story cut) doesn't; the prepared one with timed lyrics stays,
        # and comes first: our library matches the search (it starts at once, its lyrics are proven)
        self.assertEqual([r['id'] for r in found], ['ccccccccccc', 'aaaaaaaaaaa'])
        self.assertTrue(found[0]['library'])

    def test_search_asks_again_for_the_official_video(self):
        # karaoke-server: 5,000-song study (2026-11-22): no official video of the song on the page -> one more search
        lrc = '[00:05.00]a\n[02:50.00]b'
        lyric = {'id': 'lyricvid001', 'title': 'Drake - One Dance (Lyrics)', 'channel': 'Lyrics Hub', 'duration': 174, 'kind': 'lyric',
                 'parsed': {'artist': 'Drake', 'title': 'One Dance'}}
        mv = {'id': 'officialv01', 'title': 'Drake - One Dance (Official Music Video)', 'channel': 'DrakeVEVO', 'duration': 175,
              'kind': 'official', 'parsed': {'artist': 'Drake', 'title': 'One Dance'}}
        pages = {'drake one dance': [lyric], 'drake one dance official music video': [mv, lyric]}
        with patch.object(self.main.youtube, 'search', side_effect=lambda q, page=0: pages[q]) as search, \
             patch.object(self.main, 'lyric_records', AsyncMock(return_value=[{'id': 1, 'artistName': 'Drake', 'trackName': 'One Dance', 'duration': 174, 'syncedLyrics': lrc}])):
            found = self.client.get('/api/search', params={'q': 'drake one dance'}).json()
            self.assertEqual([r['id'] for r in found], ['officialv01', 'lyricvid001'])
            pages['drake one dance'] = [mv]   # an official video is already there: no second search
            search.reset_mock()
            self.client.get('/api/search', params={'q': 'drake one dance'})
            self.assertEqual(search.call_count, 1)

    def test_a_library_song_without_lyrics_cannot_be_queued(self):
        # karaoke-server (beta 2026-11-22): "I thought we weren't able to queue a song that has no Lyrics?"
        folder = Path(self.temp.name) / 'nnnnnnnnnnn'
        folder.mkdir()
        (folder / 'meta.json').write_text('{"artist": "La Mave", "title": "Dominga"}', encoding='utf-8')
        for name in ('no_vocals.wav', 'vocals.wav'):
            (folder / name).touch()
        response = self.client.post('/api/queue', json={'id': 'nnnnnnnnnnn', 'artist': 'La Mave', 'title': 'Dominga'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('lyrics', response.json()['detail'])

    def test_family_friendly_mode_keeps_explicit_songs_out(self):
        # karaoke-server (user, 2026-11-26): Family-friendly mode leaves out songs Spotify marks explicit
        folder = Path(self.temp.name) / 'xxxxxxxxxxx'
        folder.mkdir()
        (folder / 'meta.json').write_text('{"artist": "A", "title": "Rude", "spotify": {"explicit": true}}', encoding='utf-8')
        for name in ('no_vocals.wav', 'vocals.wav'):
            (folder / name).touch()
        (folder / 'timed.json').write_text('{"lines": [{"t": 1, "text": "a"}]}', encoding='utf-8')
        self.main.state.family = 'on'
        try:
            response = self.client.post('/api/queue', json={'id': 'xxxxxxxxxxx', 'artist': 'A', 'title': 'Rude'})
            self.assertEqual(response.status_code, 409)
            self.assertIn('Family-friendly', response.json()['detail'])
        finally:
            self.main.state.family = 'off'
        self.assertEqual(self.client.post('/api/queue', json={'id': 'xxxxxxxxxxx', 'artist': 'A', 'title': 'Rude'}).status_code, 201)

    def test_library_songs_are_offered_only_when_their_lyrics_line_up(self):
        # karaoke-server: same rule as search results; measured first-line error for prepared songs
        def song(vid, duration, lyrics_duration=None, entry=None, lines=True):
            folder = Path(self.temp.name) / vid
            folder.mkdir()
            meta = {'artist': 'A', 'title': 'T', 'duration': duration}
            if lyrics_duration: meta['lyrics_duration'] = lyrics_duration
            (folder / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
            (folder / 'timed.json').write_text(json.dumps({'entry_error': entry, 'lines': [{'t': 9, 'text': 'x' if lines else ''}]}), encoding='utf-8')
        song('aaaaaaaaaaa', 200, 201)                 # lengths match
        song('bbbbbbbbbbb', 217, 200, entry=0.2)      # longer (an outro), but starts with the singer
        song('ccccccccccc', 200, 209, entry=-7.5)     # "This Is Me": 7.5 s early
        song('ddddddddddd', 200, None, entry=None)    # unknown: benefit of the doubt
        song('eeeeeeeeeee', 200, 200, lines=False)    # no lyrics at all
        self.assertEqual([self.main.lines_up(v) for v in ('aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc', 'ddddddddddd', 'eeeeeeeeeee')],
                         [True, True, False, True, False])

    def test_search_pages(self):
        with patch.object(self.main.youtube, 'search', return_value=[]) as search, \
             patch.object(self.main, 'lyric_records', AsyncMock(return_value=[])):  # karaoke-server: no LRCLIB call
            self.assertEqual(self.client.get('/api/search', params={'q': '晴天', 'page': 2}).status_code, 200)
            search.assert_called_once_with('晴天', 2)
            self.assertEqual(self.client.get('/api/search', params={'q': 'x', 'page': 10}).status_code, 422)
