import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import unittest.mock
from ktvibes.lyrics import parse_lrc
from ktvibes.youtube import is_music, parse_title
from ktvibes.queue import State

class LyricsTests(unittest.TestCase):
    def test_unicode_multistamp_and_offset(self):
        self.assertEqual(parse_lrc('[ar:周杰倫]\n[offset:-500]\n[00:02.50][00:04.000]你好 안녕\n[00:01.00]'),
                         [{'t': .5, 'text': ''}, {'t': 2.0, 'text': '你好 안녕'}, {'t': 3.5, 'text': '你好 안녕'}])

    def test_title_drops_pipe_suffixes_like_official_performance_video(self):
        # karaoke-server: these suffixes broke the LRCLIB lookup ("Saranggola | Official Performance Video").
        self.assertEqual(parse_title('Ben&Ben - Saranggola | Official Performance Video'), {'artist': 'Ben&Ben', 'title': 'Saranggola'})
        self.assertEqual(parse_title('IV OF SPADES - Mundo | Official Video'), {'artist': 'IV OF SPADES', 'title': 'Mundo'})
        self.assertEqual(parse_title('Zack Tabudlo - Binibini | Lyric Visualizer'), {'artist': 'Zack Tabudlo', 'title': 'Binibini'})
        self.assertEqual(parse_title('India.Arie - Video'), {'artist': 'India.Arie', 'title': 'Video'})  # a song named "Video" stays

    def test_search_results_are_labelled_and_official_videos_come_first(self):
        # karaoke-server: kinds for the remote's labels, and a stable re-order (YouTube's order kept within a kind).
        from unittest import mock
        from ktvibes import youtube
        self.assertEqual(youtube.kind('Bruno Mars - Die With A Smile (Official Lyric Video)', 'Bruno Mars', 'Bruno Mars'), 'lyric')
        self.assertEqual(youtube.kind('Die With A Smile', 'Lady Gaga - Topic'), 'audio')
        self.assertEqual(youtube.kind('Adele - Hello (Live at the BBC)', 'Adele', 'Adele'), 'live')
        self.assertEqual(youtube.kind('Taylor Swift - Shake It Off', 'TaylorSwiftVEVO', 'Taylor Swift'), 'official')
        self.assertEqual(youtube.kind('someone sings shallow', 'random guy'), 'other')
        # learned from our library's real titles
        for title, want in [('Evanescence - Bring Me To Life (Official HD Music Video) ft. Paul McCoy', 'official'),
                            ('LANY - When Did You Stop Loving Me? (Music Video)', 'official'),
                            ('LANY - Prettiest Thing I’ve Ever Seen (Official)', 'official'),
                            ('Ben&Ben - Saranggola | Official Performance Video', 'official'),
                            ('Remember Me (Lullaby) (From "Coco"/Sing-Along)', 'lyric'),
                            ('ITS RAINING TACOS! (Roblox Music Video)', 'other'),
                            ('LANY - Stuck (Official Lyric Video)', 'lyric'),
                            ("Elvis Presley - Can't Help Falling In Love (Official Audio)", 'audio')]:
            self.assertEqual(youtube.kind(title), want, title)
        entries = [{'id': f'aaaaaaaaaa{n}', 'title': t, 'channel': c, 'duration': 200} for n, (t, c) in enumerate([
            ('Artist - Song (Lyrics)', 'lyrics hub'), ('Artist - Song (Live)', 'Artist'), ('Artist - Song (Official Music Video)', 'Artist'),
            ('Artist - Song (Official Audio)', 'Artist'), ('Artist - Song lyric video', 'other hub')])]
        class FakeYDL:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, *a, **k): return {'entries': entries}
        with mock.patch('yt_dlp.YoutubeDL', FakeYDL):
            got = youtube.search('artist song')
        self.assertEqual([r['kind'] for r in got], ['official', 'lyric', 'lyric', 'audio', 'live'])
        self.assertEqual([r['id'] for r in got][1:3], ['aaaaaaaaaa0', 'aaaaaaaaaa4'])  # lyric videos keep YouTube's order

    def test_title_metadata_preserves_original_script(self):
        self.assertEqual(parse_title('周杰倫 - 晴天 (Official MV)'), {'artist': '周杰倫', 'title': '晴天'})
        self.assertEqual(parse_title('아이유 - 좋은 날 (Official Audio)'), {'artist': '아이유', 'title': '좋은 날'})
        self.assertEqual(parse_title('周杰倫 Jay Chou【晴天 Sunny Day】-Official Music Video'), {'artist': '周杰倫 Jay Chou', 'title': '晴天 Sunny Day'})
        self.assertEqual(parse_title('봄날', '방탄소년단 - Topic'), {'artist': '방탄소년단', 'title': '봄날'})
        self.assertEqual(parse_title('卓文萱 Genie Chuo&曹格 Gary Chaw【梁山伯與茱麗葉】華視偶像劇「戀愛女王」片尾曲', '滾石唱片 ROCK RECORDS'),
                         {'artist': '卓文萱 Genie Chuo&曹格 Gary Chaw', 'title': '梁山伯與茱麗葉'})
        # "Song - Artist" on the artist's own channel, with a remaster tag
        self.assertEqual(parse_title('Numb (Official Music Video) [4K UPGRADE] – Linkin Park', 'Linkin Park'), {'artist': 'Linkin Park', 'title': 'Numb'})
        self.assertEqual(parse_title('Adele - Hello', 'AdeleVEVO'), {'artist': 'Adele', 'title': 'Hello'})
        self.assertEqual(parse_title('Simple Plan - Perfect // Lyrics'), {'artist': 'Simple Plan', 'title': 'Perfect'})
        self.assertEqual(parse_title('Simple Plan - Perfect (Lyrics) 🎵'), {'artist': 'Simple Plan', 'title': 'Perfect'})

class YouTubeErrorTests(unittest.TestCase):
    def test_bot_check_is_explained(self):
        from ktvibes import youtube
        error = Exception("ERROR: [youtube] kXYiU_JCYtU: Sign in to confirm you’re not a bot. Use --cookies-from-browser")
        self.assertIn('KTVIBES_COOKIES_FROM_BROWSER', youtube.explain(error))
        self.assertEqual(youtube.explain(Exception('HTTP Error 404')), 'HTTP Error 404')
        with unittest.mock.patch.dict('os.environ', {'KTVIBES_COOKIES_FROM_BROWSER': 'firefox'}):
            self.assertEqual(youtube.base_options()['cookiesfrombrowser'], ('firefox',))

class MusicFilterTests(unittest.TestCase):
    def test_keeps_songs_and_drops_other_videos(self):
        self.assertTrue(is_music('BTS - Dynamite (Official MV)', 223))
        self.assertTrue(is_music('晴天', None))
        for title in ('BTS Dynamite Reaction!!', 'Dynamite Dance Practice', 'Dynamite (Instrumental)', 'Pink Venom #shorts'):
            self.assertFalse(is_music(title, 200), title)
        self.assertFalse(is_music('周杰倫最好聽的20首歌曲', 4000))
        self.assertFalse(is_music('Intro', 30))

class QueueTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("karaoke-server: Classic theme and pronunciation guides removed")
    async def test_guide_cycles_only_through_modes_with_content(self):
        state = State()
        a = await state.add('abcdefghijk', 'IU', '좋은 날')
        a['status'], a['duration'] = 'ready', 100
        a['lyrics'] = [{'t': 0, 'text': '좋은', 'units': [['좋', 0, 1, 'jo', ''], ['은', 1, 2, 'eun', '']]}]
        state.promote()
        self.assertEqual(state.guides(), ['off', 'latin'])
        await state.control({'action': 'guide', 'value': 'cycle'})
        self.assertEqual(state.guide, 'off')
        await state.control({'action': 'guide', 'value': 'cycle'})
        self.assertEqual(state.guide, 'latin')
        with self.assertRaises(ValueError):
            await state.control({'action': 'guide', 'value': 'klingon'})
        state.guide = 'hangul'  # a mode this Korean song has nothing for
        await state.control({'action': 'guide', 'value': 'cycle'})
        self.assertEqual(state.guide, 'latin')

    async def test_pause_and_skip_say_who(self):
        # karaoke-server (user, 2026-11-22): "When someone paused ... it feels like the tv just lagged ... identify who"
        state = State()
        state.claim_singer('Ana', 'phone-ana-1')
        a = await state.add('abcdefghijk', 'A', 'First'); a['status'] = 'ready'
        state.promote()
        await state.control({'action': 'pause', 'singer': 'Ana', 'device': 'phone-ana-1'})
        self.assertEqual(state.snapshot()['paused_by'], 'Ana')
        await state.control({'action': 'play', 'singer': 'Ana', 'device': 'phone-ana-1'})
        self.assertEqual(state.snapshot()['paused_by'], '')
        await state.control({'action': 'pause', 'singer': 'Ana', 'device': 'someone-else-1'})  # not Ana's phone
        self.assertEqual(state.snapshot()['paused_by'], 'Someone')

    async def test_scenery_for_lyric_and_audio_videos(self):
        # karaoke-server (user, 2026-11-22): "for 'Audio' type we should also use scenery, similar to the Lyric video"
        class Pool:
            def take(self, seconds): return {"file": "aaaaaaaaaaa.mp4", "start": 8.0}
        state = State(); state.scenery = Pool()
        for vid, ok in (("abcdefghijk", True), ("bbbbbbbbbbb", False)):
            item = await state.add(vid, 'A', 'T'); item.update(status='ready', duration=200, scenery_ok=ok)
        state.promote()
        self.assertEqual(state.current["scenery"]["file"], "aaaaaaaaaaa.mp4")   # a lyric or audio video
        state.position = 5; await state.control({'action': 'skip'}); state.transition_until = 0; state.promote()
        self.assertNotIn("scenery", state.current)                              # an official video keeps its own

    async def test_video_always_shows(self):
        # karaoke-server (user, 2026-11-22): "simplify the display ... remove blur and hide"
        state = State()
        await state.control({'action': 'video_mode', 'value': 'blur'})   # an older phone or a TV restoring its setting
        self.assertEqual(state.video_mode, 'show')
        state.profiles['ana'] = {'video_mode': 'hide'}
        a = await state.add('abcdefghijk', 'A', 'First', 'Ana'); a['status'] = 'ready'
        state.promote()
        self.assertEqual(state.video_mode, 'show')

    async def test_renaming_a_singer_updates_their_songs(self):
        # karaoke-server (beta 2026-11-22): "When I change my name in realtime, does it update what is shown in the TV?"
        state = State()
        state.claim_singer('Vince', 'phone-vince-1'); state.claim_singer('Ana', 'phone-ana-1')
        a = await state.add('abcdefghijk', 'A', 'First', 'Vince'); a['status'] = 'ready'
        b = await state.add('bbbbbbbbbbb', 'B', 'Second', 'Ana')
        c = await state.add('ccccccccccc', 'C', 'Third', 'vince')
        state.promote()
        state.profiles['vince'] = {'key': 2}
        state.claim_singer('Vincent', 'phone-vince-1')
        self.assertEqual([state.current['singer'], b['singer'], c['singer']], ['Vincent', 'Ana', 'Vincent'])
        self.assertEqual(state.profiles.get('vincent'), {'key': 2})
        self.assertNotIn('vince', state.singers)
        # the phone's old name was freed meanwhile (idle, or renamed on an older version): its songs still follow it
        d = await state.add('ddddddddddd', 'D', 'Fourth', 'Vincent', 'phone-vince-1')
        state.singers.pop('vincent')
        state.claim_singer('John', 'phone-vince-1')
        self.assertEqual(d['singer'], 'John')
        self.assertEqual(b['singer'], 'Ana')

    async def test_lyrics_vote_after_each_song(self):
        # karaoke-server (user, 2026-11-22): everyone votes "lyrics in sync?"; the countdown is 10 s and drops to 3 s
        # once every singer with a song in the queue has voted
        import time, json, tempfile
        from pathlib import Path
        from ktvibes import queue
        state = State()
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        state.path = Path(folder.name) / 'queue.json'
        (Path(folder.name) / 'abcdefghijk').mkdir()
        (Path(folder.name) / 'abcdefghijk' / 'meta.json').write_text('{"artist": "A", "title": "First"}')
        (Path(folder.name) / 'abcdefghijk' / 'lyrics.lrc').write_text('[00:01.00]a')
        state.claim_singer('Ana', 'phone-ana-1'); state.claim_singer('Ben', 'phone-ben-1')
        first = await state.add('abcdefghijk', 'A', 'First', 'Ana')
        second = await state.add('bbbbbbbbbbb', 'B', 'Second', 'Ben')
        for item in (first, second):
            item['status'], item['duration'], item['lyrics'] = 'ready', 200, [{'t': 1, 'text': 'a'}]
        state.promote()
        state.position = 120
        await state.control({'action': 'skip'})
        vote = state.snapshot()['vote']
        self.assertEqual((vote['title'], vote['needed'], vote['voted_needed']), ('First', 2, 0))
        self.assertAlmostEqual(state.transition_until - time.time(), queue.VOTE_WAIT, delta=1)
        await state.control({'action': 'lyrics_vote', 'key': first['key'], 'value': 'down', 'why': 'late', 'device': 'phone-ana-1'})
        await state.control({'action': 'lyrics_vote', 'key': first['key'], 'value': 'up', 'device': 'someone-else'})  # anyone may vote
        self.assertGreater(state.transition_until - time.time(), queue.VOTE_FAST + 1)  # still waiting for Ben
        await state.control({'action': 'lyrics_vote', 'key': first['key'], 'value': 'down', 'device': 'phone-ben-1'})
        self.assertLessEqual(state.transition_until - time.time(), queue.VOTE_FAST)
        vote = state.snapshot()['vote']
        self.assertEqual((vote['up'], vote['down'], vote['voted_needed']), (1, 2, 2))
        saved = json.loads((Path(folder.name) / 'abcdefghijk' / 'meta.json').read_text())['lyrics_votes']
        self.assertEqual((saved[0]['up'], saved[0]['down'], saved[0]['why']), (1, 2, {'late': 1}))
        from ktvibes import worker
        self.assertEqual(worker.vote_verdict(Path(folder.name) / 'abcdefghijk', {'lyrics_votes': saved}), 'flagged')
        with self.assertRaises(ValueError):
            await state.control({'action': 'lyrics_vote', 'key': 'old', 'value': 'up', 'device': 'phone-ana-1'})
        # while the next song plays, a phone can still vote on the first one...
        await state.control({'action': 'lyrics_vote', 'key': first['key'], 'value': 'up', 'device': 'late-phone-1'})
        saved = json.loads((Path(folder.name) / 'abcdefghijk' / 'meta.json').read_text())['lyrics_votes']
        self.assertEqual(saved[-1]['up'], 2)
        # ...but once that one ends, only it can be voted on (user, 2026-11-22)
        state.position = 120
        await state.control({'action': 'skip'})
        with self.assertRaises(ValueError):
            await state.control({'action': 'lyrics_vote', 'key': first['key'], 'value': 'up', 'device': 'late-phone-2'})
        self.assertEqual(state.snapshot()['vote']['title'], 'Second')
        # a song skipped early isn't asked about
        third = await state.add('ccccccccccc', 'C', 'Third', 'Ana')
        third['status'], third['duration'], third['lyrics'] = 'ready', 200, [{'t': 1, 'text': 'a'}]
        state.transition_until = 0; state.promote()
        self.assertEqual(state.current['title'], 'Third')
        state.position = 5
        await state.control({'action': 'skip'})
        self.assertIsNone(state.snapshot()['vote'])  # not asked about, and "Second" is no longer voted on

    async def test_breather_only_between_songs(self):
        import time
        state = State()
        first = await state.add('abcdefghijk', 'A', 'First')
        first['status'] = 'ready'
        state.promote()
        # From an empty stage the song starts at once; its intro carries the title card.
        self.assertLessEqual(state.transition_until, time.time())
        second = await state.add('bbbbbbbbbbb', 'B', 'Second')
        second['status'] = 'ready'
        await state.control({'action': 'skip'})
        self.assertIs(state.current, second)
        self.assertAlmostEqual(state.transition_until - time.time(), 4, delta=0.5)
        # A song that was still preparing when the last one ended starts without a pause once ready.
        await state.control({'action': 'skip'})
        third = await state.add('ccccccccccc', 'C', 'Third')
        state.transition_until = 0
        third['status'] = 'ready'
        state.promote()
        self.assertLessEqual(state.transition_until, time.time())

    @unittest.skip("karaoke-server: Classic theme and pronunciation guides removed")

    async def test_classic_breather_leaves_room_for_the_score(self):
        import time
        state = State()
        await state.control({'action': 'theme', 'value': 'classic'})
        songs = []
        for vid in ('abcdefghijk', 'bbbbbbbbbbb', 'ccccccccccc'):
            song = await state.add(vid, 'A', vid)
            song['status'] = 'ready'
            songs.append(song)
        state.promote()
        # A song sung to the end: the score (5 s) shows, then the 예약곡 board (4 s).
        await state.control({'action': 'ended', 'key': songs[0]['key']})
        self.assertIs(state.current, songs[1])
        self.assertAlmostEqual(state.transition_until - time.time(), 9, delta=0.5)
        # A skipped song gets no score, so only the board.
        await state.control({'action': 'skip'})
        self.assertAlmostEqual(state.transition_until - time.time(), 4, delta=0.5)

    async def test_seek_and_lyric_scale_are_clamped(self):
        state = State()
        with self.assertRaises(ValueError):
            await state.control({'action': 'seek', 'position': 5})
        a = await state.add('abcdefghijk', 'A', 'First')
        a['status'], a['duration'] = 'ready', 100
        state.promote()
        with self.assertRaisesRegex(ValueError, 'paused or skipped'):  # karaoke-server: no seeking
            await state.control({'action': 'seek', 'position': 50})
        self.assertEqual((state.position, state.seek_id), (0, 0))
        await state.control({'action': 'lyric_scale', 'value': 9})
        self.assertEqual(state.lyric_scale, 2.5)
        await state.control({'action': 'music', 'value': -1})
        self.assertEqual(state.music, 0)
        await state.control({'action': 'speed', 'value': 3})
        self.assertEqual(state.speed, 1.0)  # karaoke-server: speed removed (2026-11-22); always 100%
        await state.control({'action': 'key', 'value': -9})
        self.assertEqual(state.key, -6)

    async def test_display_settings_take_known_values_or_cycle(self):
        state = State()
        await state.control({'action': 'video_mode', 'value': 'blur'})
        await state.control({'action': 'lyric_mode', 'value': 'two'})
        self.assertEqual((state.video_mode, state.lyric_mode), ('show', 'two'))  # karaoke-server: Blur/Hide removed, video always shows
        await state.control({'action': 'video_mode', 'value': 'cycle'})
        self.assertEqual(state.video_mode, 'show')
        with self.assertRaises(ValueError):
            await state.control({'action': 'lyric_mode', 'value': 'sideways'})
        self.assertEqual(state.snapshot()['lyric_mode'], 'two')

    async def test_duplicates_rejected_until_song_leaves_stage(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        with self.assertRaises(ValueError):
            await state.add('abcdefghijk', 'A', 'Again')
        a['status'] = 'ready'
        state.promote()
        self.assertIs(state.current, a)
        self.assertIn('abcdefghijk', state.played)
        await state.control({'action': 'offset', 'delta': .5})
        await state.control({'action': 'skip'})
        self.assertIsNone(state.current)
        self.assertEqual(state.offset, 0)
        b = await state.add('abcdefghijk', 'A', 'Encore')
        self.assertNotEqual(a['key'], b['key'])

    async def test_singer_rides_with_the_song_and_survives_restart(self):
        # karaoke-server: each queued song carries who sings it, through promote, undo and a restart.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            state = State()
            state.restore(path, seed=lambda: {})
            a = await state.add('abcdefghijk', 'Queen', 'Bohemian Rhapsody', 'Anna')
            await state.add('12345678901', 'ABBA', 'Dancing Queen', 'Ben')
            await state.add('zyxwvutsrqp', 'Toto', 'Africa')  # an old client sends no singer
            self.assertEqual([i['singer'] for i in state.snapshot()['upcoming']], ['Anna', 'Ben', ''])
            a['status'] = 'ready'
            state.promote()
            self.assertEqual(state.current['singer'], 'Anna')
            await state.broadcast()
            again = State()
            again.restore(path, seed=lambda: {})
            self.assertEqual([(i['title'], i['singer']) for i in again.upcoming],
                             [('Bohemian Rhapsody', 'Anna'), ('Dancing Queen', 'Ben'), ('Africa', '')])

    def test_singer_name_is_tidied_and_capped(self):
        from pydantic import ValidationError
        from ktvibes.main import Song
        self.assertEqual(Song(id='abcdefghijk', artist='Queen', title='Song', singer='  Anna   Marie ').singer, 'Anna Marie')
        self.assertEqual(Song(id='abcdefghijk', artist='Queen', title='Song').singer, '')
        with self.assertRaises(ValidationError):
            Song(id='abcdefghijk', artist='Queen', title='Song', singer='x' * 41)

    async def test_each_singer_gets_their_own_mix_key_and_speed(self):
        # karaoke-server: Anna likes 20% vocals, Bea 50%; each song plays with its singer's settings.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            state = State(); state.restore(path, seed=lambda: {})
            async def sing(video_id, singer):
                item = await state.add(video_id, 'Artist', 'Song ' + video_id[:2], singer)
                item['status'] = 'ready'
                if state.current:
                    state.advance()
                else:
                    state.promote()
                self.assertEqual(state.current['singer'], singer)
            await sing('aaaaaaaaaaa', 'Anna')
            await state.control({'action': 'vocal', 'value': 0.2})
            await state.control({'action': 'key', 'value': -2})
            await sing('bbbbbbbbbbb', 'Bea')
            self.assertEqual((state.key, state.speed), (0, 1.0))  # a first-timer starts as recorded
            await state.control({'action': 'vocal', 'value': 0.5})
            await state.control({'action': 'speed', 'value': 0.9})
            await sing('ccccccccccc', 'anna')  # same singer, any case
            self.assertEqual((state.vocal, state.key, state.speed), (0.2, -2, 1.0))
            await sing('ddddddddddd', 'Bea')
            self.assertEqual((state.vocal, state.key, state.speed), (0.5, 0, 1.0))  # karaoke-server: speed removed, always 100%
            await sing('eeeeeeeeeee', '')  # no name: room settings, nothing saved
            await state.control({'action': 'vocal', 'value': 0.9})
            self.assertEqual(state.profiles['bea']['vocal'], 0.5)
            await state.broadcast()
            again = State(); again.restore(path, seed=lambda: {})
            self.assertEqual({k: again.profiles['anna'][k] for k in ('vocal', 'key', 'speed')}, {'vocal': 0.2, 'key': -2, 'speed': 1.0})

    async def test_singer_profile_covers_display_settings_too(self):
        state = State()
        for video_id, singer in (('aaaaaaaaaaa', 'Anna'), ('bbbbbbbbbbb', 'Bea')):
            (await state.add(video_id, 'A', 'S', singer))['status'] = 'ready'
        state.promote()
        await state.control({'action': 'lyric_scale', 'value': 2.0})
        await state.control({'action': 'video_mode', 'value': 'blur'})
        await state.control({'action': 'lyric_mode', 'value': 'scroll'})
        await state.control({'action': 'music', 'value': 0.6})
        state.advance()                                   # Bea: first time, keeps the room as it is
        await state.control({'action': 'video_mode', 'value': 'show'})
        await state.control({'action': 'lyric_scale', 'value': 1.0})
        (await state.add('ccccccccccc', 'A', 'S', 'Anna'))['status'] = 'ready'
        state.advance()                                   # Anna again: all her settings come back
        self.assertEqual((state.lyric_scale, state.video_mode, state.lyric_mode, state.music), (2.0, 'show', 'scroll', 0.6))  # karaoke-server: video always shows
        self.assertEqual(state.profiles['bea']['video_mode'], 'show')

    async def test_a_phone_edits_its_own_profile_not_the_singer_on_stage(self):
        # karaoke-server: PC moves vocals to 51% during Vince's song: PC's profile changes, Vince's song and profile don't.
        state = State()
        state.claim_singer('Vince', 'phone-v'); state.claim_singer('PC', 'phone-pc')
        (await state.add('aaaaaaaaaaa', 'A', 'S', 'Vince'))['status'] = 'ready'
        (await state.add('bbbbbbbbbbb', 'A', 'S', 'PC'))['status'] = 'ready'
        state.promote()
        await state.control({'action': 'vocal', 'value': 0.2, 'singer': 'Vince', 'device': 'phone-v'})  # Vince, live
        await state.control({'action': 'vocal', 'value': 0.51, 'singer': 'PC', 'device': 'phone-pc'})   # PC, saved for later
        await state.control({'action': 'key', 'value': 3, 'singer': 'pc', 'device': 'phone-pc'})
        self.assertEqual(state.vocal, 0.2)                           # Vince's song keeps sounding as Vince set it
        self.assertEqual(state.profiles['vince']['vocal'], 0.2)
        self.assertEqual((state.profiles['pc']['vocal'], state.profiles['pc']['key']), (0.51, 3))
        self.assertIn('profiles', state.snapshot())
        state.advance()                                               # PC's turn: PC's settings load
        self.assertEqual((state.vocal, state.key), (0.51, 3))
        with self.assertRaisesRegex(ValueError, 'reload'):            # a phone that doesn't hold the name can't edit it
            await state.control({'action': 'vocal', 'value': 0.9, 'singer': 'Vince', 'device': 'phone-pc'})
        self.assertEqual(state.profiles['vince']['vocal'], 0.2)

    def test_version_is_semver_and_broadcast(self):
        # karaoke-server: ktvibes/VERSION is our SemVer, sent with the state so the TV and remotes can show it.
        import re
        from ktvibes.queue import VERSION
        self.assertRegex(VERSION, r'^\d+\.\d+\.\d+$')
        self.assertEqual(State().snapshot()['version'], VERSION)

    async def test_shout_outs_and_photos_pop_up_on_the_tv_with_a_name(self):
        # karaoke-server: fun extras. Always named, 60 characters max, one per phone per 10 s, photos in memory only.
        import ktvibes.queue as q
        state = State(); state.claim_singer('Vince', 'phone-v')
        tv, sent = object(), []
        async def fake_send(ws, message):
            sent.append((ws, message)); return True
        state.send = fake_send; state.player = tv; state.clients = {tv: 'tv', 'remote-1': 'remote'}
        with self.assertRaisesRegex(ValueError, 'Pick your name'):
            await state.control({'action': 'shout', 'value': 'hi', 'singer': 'Vince', 'device': 'someone-else'})
        await state.control({'action': 'shout', 'value': '  You   got this! ' + 'x' * 80, 'singer': 'vince', 'device': 'phone-v'})
        shout = next(m for ws, m in sent if ws is tv and m['type'] == 'shout')
        self.assertEqual((shout['name'], len(shout['text'])), ('Vince', 60))
        self.assertTrue(shout['text'].startswith('You got this! '))
        self.assertTrue(any(ws == 'remote-1' and m == {'type': 'fun', 'kind': 'shout', 'id': shout['id'], 'name': 'Vince'} for ws, m in sent))
        with self.assertRaisesRegex(ValueError, 'Wait'):
            await state.control({'action': 'shout', 'value': 'again', 'singer': 'Vince', 'device': 'phone-v'})
        item = await state.add_photo(  # photos have their own cooldown, so one goes straight after a shout-out
b'\xff\xd8\xff' + b'0' * 100, 'image/jpeg', 'Vince', 'phone-v')
        self.assertIn(item['photo'], state.photos)
        self.assertEqual(state.photos[item['photo']]['name'], 'Vince')
        with self.assertRaisesRegex(ValueError, 'Wait'):                 # but not two photos within 3 s
            await state.add_photo(b'\xff\xd8\xff2', 'image/jpeg', 'Vince', 'phone-v')
        state.fun_last['photo:phone-v'] -= q.PHOTO_COOLDOWN
        with self.assertRaisesRegex(ValueError, 'too big'):
            await state.add_photo(b'0' * (q.PHOTO_MAX_BYTES + 1), 'image/jpeg', 'Vince', 'phone-v')
        state.photos[item['photo']]['at'] -= q.PHOTO_KEEP + 1          # the night is over: pruned on the next photo
        state.fun_last['photo:phone-v'] -= q.PHOTO_COOLDOWN
        await state.add_photo(b'\xff\xd8\xff1', 'image/jpeg', 'Vince', 'phone-v')
        self.assertNotIn(item['photo'], state.photos)
        await state.control({'action': 'hide', 'id': shout['id']})
        self.assertEqual(sent[-1], (tv, {'type': 'hide', 'id': shout['id']}))

    async def test_queue_plays_in_order_and_cannot_be_reordered(self):
        # karaoke-server: first come, first served.
        state = State()
        a = await state.add('aaaaaaaaaaa', 'A', 'First')
        b = await state.add('bbbbbbbbbbb', 'B', 'Second')
        with self.assertRaisesRegex(ValueError, "can't be moved"):
            await state.control({'action': 'reorder', 'keys': [b['key'], a['key']]})
        self.assertEqual([i['title'] for i in state.upcoming], ['First', 'Second'])
        with self.assertRaisesRegex(ValueError, "can't be removed"):    # and it is read-only: no removing either
            await state.control({'action': 'remove', 'key': a['key']})
        self.assertEqual(len(state.upcoming), 2)

    def test_slow_steps_get_an_expected_length_that_learns(self):
        # karaoke-server: separating/syncing report no progress, so the remote's ring is filled from these.
        import ktvibes.worker as w
        saved = dict(w.STEP_RATE)
        try:
            item = {}
            w.expect_step(item, "separating", 200)
            self.assertAlmostEqual(item["expect"], 0.36 * 200, places=1)
            self.assertAlmostEqual(item["since"], time.time(), delta=2)
            w.learn_step("separating", 30, 200)                      # this run went faster: 0.15 s per second
            self.assertAlmostEqual(w.STEP_RATE["separating"], 0.6 * 0.36 + 0.4 * 0.15, places=4)
            w.expect_step(item, "syncing", None)                     # unknown length: a typical song
            self.assertGreaterEqual(item["expect"], 3)
        finally:
            w.STEP_RATE.update(saved)

    async def test_next_songs_download_ahead_one_file_at_a_time(self):
        # karaoke-server: while a song separates, the next PREFETCH_AHEAD queued songs get their audio and video.
        import ktvibes.worker as w
        state = State()
        items = [await state.add(f'song{n}abcde'[:11], 'A', f'S{n}') for n in range(5)]
        items[0]['status'] = 'separating'                       # the worker is busy with this one
        busy = []
        def fake_audio(vid, folder, progress=None):
            busy.append(vid); assert len([b for b in busy if b]) == 1  # never two downloads at once
            (folder / 'audio.m4a').write_bytes(b'a'); busy.remove(vid); return {'duration': 200, 'source_title': vid}
        def fake_video(vid, folder, progress=None):
            (folder / 'video.mp4').write_bytes(b'v')
        with tempfile.TemporaryDirectory() as folder, \
             unittest.mock.patch.object(w.youtube, 'download', fake_audio), \
             unittest.mock.patch.object(w.youtube, 'download_video', fake_video), \
             unittest.mock.patch.object(w, 'PREFETCH_PAUSE', 0.01):
            cache = Path(folder)
            task = asyncio.create_task(w.prefetch(state, cache))
            await asyncio.sleep(1.0); task.cancel()
            done = {i['id']: ((cache / i['id'] / 'audio.m4a').is_file(), (cache / i['id'] / 'video.mp4').is_file()) for i in items}
            self.assertEqual([done[i['id']] for i in items[1:4]], [(True, True)] * 3)   # the next three
            self.assertEqual(done[items[4]['id']], (False, False))                       # not the fourth
            self.assertEqual(done[items[0]['id']], (False, False))                       # nor the one the worker has
            self.assertEqual(json.loads((cache / items[1]['id'] / 'meta.json').read_text())['duration'], 200)

    async def test_own_emoji_cheers_are_single_emoji_only(self):
        # karaoke-server: phones can cheer with their own emoji, but never with plain text.
        from ktvibes.queue import one_emoji
        for good in ['😎', '👍🏽', '🇵🇭', '👨\u200d🎤', '❤️', '1️⃣', '🫶']:
            self.assertTrue(one_emoji(good), good)
        for bad in ['hi', 'a😎', '', '12', '<b>', '😎' * 5, None, 3]:
            self.assertFalse(one_emoji(bad), bad)
        state = State(); tv, sent = object(), []
        async def fake_send(ws, message):
            sent.append(message); return True
        state.send = fake_send; state.player = tv
        await state.control({'action': 'react', 'value': '🦄'})
        self.assertEqual(sent[-1], {'type': 'react', 'value': '🦄'})
        with self.assertRaises(ValueError):
            await state.control({'action': 'react', 'value': 'spam text'})

    def test_state_counts_connected_phones_not_the_tv(self):
        # karaoke-server: the TV shows how many phones have the remote open.
        state = State()
        state.clients = {'tv-socket': 'tv', 'a': 'remote', 'b': 'remote'}
        self.assertEqual(state.snapshot()['phones'], 2)
        del state.clients['a']
        self.assertEqual(state.snapshot()['phones'], 1)

    def test_sixty_fps_videos_get_a_thirty_fps_copy(self):
        # karaoke-server: the original stays; only >30 fps videos get video-30.mp4.
        import subprocess
        from ktvibes import youtube
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            for fps, name in ((60, 'a'), (30, 'b')):
                (d / name).mkdir()
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=320x180:rate={fps}', '-t', '2',
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(d / name / 'video.mp4')], check=True)
            self.assertTrue(youtube.make_30fps(d / 'a'))
            self.assertAlmostEqual(youtube.video_fps(d / 'a' / 'video-30.mp4'), 30, delta=0.5)
            self.assertAlmostEqual(youtube.video_fps(d / 'a' / 'video.mp4'), 60, delta=0.5)   # original untouched
            self.assertFalse(youtube.make_30fps(d / 'b'))
            self.assertFalse((d / 'b' / 'video-30.mp4').exists())

    async def test_metrics_record_plays_cheers_and_fun_without_private_text(self):
        # karaoke-server: local metrics in <cache>/metrics.jsonl; shout-out text is never stored.
        from ktvibes import metrics
        with tempfile.TemporaryDirectory() as folder:
            metrics.setup(Path(folder), '9.9.9')
            state = State(); state.claim_singer('Vince', 'phone-v')
            async def fake_send(ws, message): return True
            state.send = fake_send; state.player = object()
            (await state.add('aaaaaaaaaaa', 'A', 'First', 'Vince'))['status'] = 'ready'
            state.promote(); state.position = 42
            await state.control({'action': 'skip'})
            await state.control({'action': 'react', 'value': '🦄'})
            await state.control({'action': 'shout', 'value': 'my secret message', 'singer': 'Vince', 'device': 'phone-v'})
            rows = [json.loads(l) for l in (Path(folder) / 'metrics.jsonl').read_text().splitlines()]
            played = next(r for r in rows if r['event'] == 'song_played')
            self.assertEqual((played['song'], played['completed'], played['position'], played['v']), ('aaaaaaaaaaa', False, 42, '9.9.9'))
            self.assertEqual(next(r for r in rows if r['event'] == 'cheer')['custom'], True)
            self.assertEqual(next(r for r in rows if r['event'] == 'shout')['chars'], len('my secret message'))
            self.assertNotIn('secret', (Path(folder) / 'metrics.jsonl').read_text())
            old = metrics.METRICS_MAX_BYTES
            try:
                metrics.METRICS_MAX_BYTES = 10
                metrics.record('after_rotation')
                self.assertTrue((Path(folder) / 'metrics.jsonl.1').exists())
            finally:
                metrics.METRICS_MAX_BYTES = old
            metrics.setup(Path(folder) / 'missing' / 'dir', 'x')
            metrics.record('cannot_write')  # must not raise
            metrics._path = None

    async def test_lyrics_for_the_song_on_stage_apply_in_the_background_newest_wins(self):
        # karaoke-server: the phone gets its answer at once; the TV switches when the words are timed.
        import threading
        from unittest import mock
        from ktvibes import main
        LRC = {1: "[00:01.00]first version", 2: "[00:01.00]second version"}
        gates = {1: threading.Event(), 2: threading.Event()}
        def slow_timing(folder, raw, vocals, lines, lyric_duration=None):
            gates[1 if "first" in raw else 2].wait(5)
            return lines
        async def record(version):
            return {"syncedLyrics": LRC[version]}
        with tempfile.TemporaryDirectory() as folder:
            song = Path(folder) / "aaaaaaaaaaa"; song.mkdir()
            (song / "meta.json").write_text(json.dumps({"artist": "A", "title": "T", "duration": 100}))
            state = State()
            async def no_broadcast(): pass
            state.broadcast = no_broadcast
            state.current = {"id": "aaaaaaaaaaa", "key": "k", "title": "T", "lyrics": [], "lyrics_rev": 0}
            with mock.patch.object(main, "CACHE", Path(folder)), mock.patch.object(main, "state", state), \
                 mock.patch.object(main.lyrics, "record", record), mock.patch.object(main.worker, "timed_lyrics", slow_timing):
                first = await asyncio.wait_for(main.choose_lyrics("aaaaaaaaaaa", main.LyricChoice(id=1)), 2)
                self.assertTrue(first["applying"])
                self.assertIn("aaaaaaaaaaa", state.snapshot()["lyrics_applying"])
                self.assertEqual(state.current["lyrics_rev"], 0)            # not swapped yet
                await main.choose_lyrics("aaaaaaaaaaa", main.LyricChoice(id=2))
                gates[2].set()
                for _ in range(100):
                    if state.current["lyrics_rev"]: break
                    await asyncio.sleep(0.02)
                gates[1].set()
                await asyncio.gather(*main._lyric_tasks)
            self.assertEqual(state.current["lyrics_rev"], 1)                # the older version didn't overwrite it
            self.assertEqual(state.current["lyrics"][0]["text"], "second version")
            self.assertEqual(state.lyrics_applying, {})
            self.assertIn("second version", (song / "lyrics.lrc").read_text())

    def test_covers_are_labelled_but_songs_named_cover_are_not(self):
        # karaoke-server: real YouTube titles (2026-11-21)
        from ktvibes.youtube import kind, KIND_ORDER
        for title in ("Teddy Swims - Someone You Loved (Lewis Capaldi Cover)", "Tadhana // Up Dharma Down (Cover)",
                      "Lewis Capaldi - Someone You Loved // Brittany Maggs cover", "Someone You Loved - Lewis Capaldi (Cover By: Davina Michelle)",
                      'KZ Tandingan covers "Tadhana" (Up Dharma Down) on Wish 107.5 Bus', "TADHANA | Up Dharma Down (Soulful R&B cover) #REINEMUSIC"):
            self.assertEqual(kind(title), "cover", title)
        for title, want in (("Cover Me - Bruce Springsteen Lyrics", "lyric"), ("Twenty One Pilots - Take Cover", "other"),
                            ("The Strokes - Under Cover of Darkness (Official Video)", "official")):
            self.assertEqual(kind(title), want, title)
        self.assertGreater(KIND_ORDER["cover"], KIND_ORDER["other"])

    def test_shortest_official_video_of_a_song_comes_first(self):
        # karaoke-server: long official cuts often have a story intro that throws the lyrics off
        from ktvibes.youtube import order_results
        r = lambda i, kind, title, d: {"id": i, "kind": kind, "duration": d, "parsed": {"title": title}}
        out = order_results([r("lyric", "lyric", "Soft", 200), r("long", "official", "Soft", 420), r("other", "official", "Thick and Thin", 300),
                             r("short", "official", "Soft!", 215), r("nodur", "official", "Soft", None)])
        self.assertEqual([x["id"] for x in out], ["short", "long", "nodur", "other", "lyric"])
        out = order_results([r("a", "official", "Telephone ft. Beyoncé", 571), r("b", "official", "Telephone ft. Beyoncé (Explicit) ft. Beyoncé", 572),
                             r("c", "official", "Telephone Feat Beyoncé", 243), r("d", "official", "Poker Face", 214)])
        self.assertEqual([x["id"] for x in out], ["c", "a", "b", "d"])

    def test_matched_length_lyrics_cannot_jump_a_verse(self):
        # karaoke-server: lyrics from a recording as long as the video only get nudged (Elvis was moved 47 s late).
        import numpy as np, soundfile as sf
        from ktvibes import worker, lyrics
        rate = 16000; hop = 0.05
        # vocals: a phrase every 8 s from 8 s on, as the LRC says; the melody repeats, so a far shift also "fits"
        y = np.zeros(rate * 200, np.float32)
        for start in range(8, 190, 8): y[start * rate:(start + 3) * rate] = 0.3 * np.sin(np.arange(3 * rate) * 0.2)
        raw = "\n".join(f"[{s // 60:02d}:{s % 60:02d}.00]line {i}" for i, s in enumerate(range(8, 190, 8)))
        with tempfile.TemporaryDirectory() as folder:
            sf.write(f"{folder}/vocals.wav", y, rate)
            seen = {}
            real = lyrics.find_shift
            def spy(lines, energy, *a, **k):
                seen.update(k); return real(lines, energy, *a, **k)
            with unittest.mock.patch.object(lyrics, "find_shift", spy), unittest.mock.patch.object(worker.align, "align", lambda *a: None):
                worker.timed_lyrics(Path(folder), raw, Path(f"{folder}/vocals.wav"), lyrics.parse_lrc(raw), 201.0)
            self.assertEqual((seen.get("limit"), seen.get("far")), (worker.MATCHED_LENGTH, False))
            seen.clear(); (Path(folder) / "timed.json").unlink()
            with unittest.mock.patch.object(lyrics, "find_shift", spy), unittest.mock.patch.object(worker.align, "align", lambda *a: None):
                worker.timed_lyrics(Path(folder), raw, Path(f"{folder}/vocals.wav"), lyrics.parse_lrc(raw), 230.0)
            self.assertEqual(seen, {})   # lengths differ (a story intro?): the full search, as before

    def test_results_with_matching_lyrics_come_first(self):
        # karaoke-server: goal (user, 2026-11-21): top = the official video, with synced lyrics that match it
        from ktvibes.youtube import mark_lyrics, order_results, song_name
        r = lambda i, kind, title, d: {"id": i, "kind": kind, "title": title, "duration": d, "parsed": {"title": title}}
        results = [r("story", "official", "Telephone ft. Beyoncé", 571), r("lyric", "lyric", "Telephone", 221),
                   r("edit", "official", "Telephone", 243), r("live", "live", "Telephone", 221), r("other", "official", "Poker Face", 238)]
        mark_lyrics(results, [(song_name("Telephone"), 221.0), (song_name("Telephone"), 244.0), (song_name("Poker Face (Remix)"), 400.0)])
        self.assertEqual([x["id"] for x in order_results(results)], ["edit", "lyric", "story", "other", "live"])
        self.assertEqual(song_name("Bad Blood (Taylor's Version) - From The Vault ft. Kendrick"), "badblood")
        self.assertFalse(next(x for x in results if x["id"] == "live")["lyrics_match"])  # same length, still live
        from ktvibes.youtube import record_song
        self.assertEqual(record_song("Lady Gaga - Telephone ft. Beyoncé (Official Music Video)", "Lady Gaga"), "telephone")
        self.assertEqual(record_song("Bad - Day", "Daniel Powter"), "bad")   # a song that has " - " in it stays
        mark_lyrics(results, [])  # LRCLIB down: the order is as before
        self.assertEqual([x["id"] for x in order_results(results)][:2], ["edit", "story"])

    def test_tv_and_award_show_performances_are_live(self):
        # karaoke-server: 5,000-song study (2026-11-22): these were labelled official, so their lyrics counted as matching
        from ktvibes.youtube import kind
        for title, channel in (("Eminem & Rihanna Perform “Love the Way You Lie / Not Afraid” at 2010 VMAs | MTV", "MTV"),
                               ("Justin Bieber Performs 'Love Yourself' on the ELLEN SHOW", "TheEllenShow"),
                               ('Billie Eilish - What Was I Made For? (From The Motion Picture "Barbie") [2024 GRAMMY Performance]', "BillieEilishVEVO"),
                               ("Jimin: Who | The Tonight Show Starring Jimmy Fallon", "The Tonight Show Starring Jimmy Fallon"),
                               ("Teddy Swims – “Bad Dreams” [2024 Billboard Music Awards]", "Teddy Swims")):
            self.assertEqual(kind(title, channel, channel), "live", title)
        for title, channel in (("정국 (Jung Kook) 'Seven (feat. Latto)' Official Performance Video", "HYBE LABELS"),
                               ("Ben&Ben - Leaves (Official Music Video)", "Ben&Ben"), ("The Weeknd - Save Your Tears (Official Music Video)", "TheWeekndVEVO")):
            self.assertEqual(kind(title, channel), "official", title)

    def test_5000_song_study_fixes(self):
        # karaoke-server (2026-11-22): an "Unofficial Music Video" is not the record's own video; a name that starts
        # with brackets has a name; two disagreeing labels for one timing: the one that ends just after the last line
        from ktvibes.youtube import kind, song_name
        from ktvibes.lyrics import real_lengths
        self.assertEqual(kind("Eraserheads - With A Smile (Unofficial Music Video HQ)", "x"), "other")
        self.assertEqual(kind("Chris Brown ft. Ludacris - Wet The Bed (Unofficial Music Video)", "ChrisBrownVEVO"), "other")
        self.assertEqual(song_name("(Not) The One"), "nottheone")
        self.assertEqual(song_name("(I Just) Died In Your Arms (Official Video)"), "ijustdiedinyourarms")
        self.assertEqual(song_name("(Official Video) Hello"), "")
        rec = lambda d: {"duration": d, "syncedLyrics": "[00:21.96]a\n[04:01.55]b"}
        records = [rec(246), rec(462)]
        real_lengths(records)
        self.assertEqual([r["length"] for r in records], [246.0, 246.0])

    def test_lrclib_answers_are_kept_and_stand_in_when_it_is_down(self):
        # karaoke-server (beta 2026-11-22): LRCLIB slowed to 2-12 s; searches gave up and showed nothing playable
        import asyncio, tempfile, httpx
        from pathlib import Path
        from ktvibes import lyrics
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        lyrics.set_lrclib_cache(Path(folder.name)); self.addCleanup(lyrics.set_lrclib_cache, None)
        calls = []
        async def answer(self, request):
            calls.append(str(request.url))
            if len(calls) > 1:
                raise httpx.ConnectTimeout("down")
            return httpx.Response(200, json=[{"id": 1}], request=request)
        async def get():
            async with lyrics.lrclib() as client:
                return (await client.get("/api/search", params={"q": "palagi"})).json()
        with unittest.mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", answer):
            self.assertEqual(asyncio.run(get()), [{"id": 1}])   # asked, and kept
            self.assertEqual(asyncio.run(get()), [{"id": 1}])   # under a day old: not asked again
            self.assertEqual(len(calls), 1)
            for kept in Path(folder.name).iterdir():
                import os; os.utime(kept, (0, 0))                # make it old
            self.assertEqual(asyncio.run(get()), [{"id": 1}])   # LRCLIB down: the old answer stands in
            self.assertEqual(len(calls), 2)
        lyrics.set_lrclib_cache(Path(folder.name))                  # at startup, answers over 30 days old go
        self.assertEqual(list(Path(folder.name).iterdir()), [])

    def test_scenery_never_repeats_until_every_film_is_used(self):
        # karaoke-server (user, 2026-11-22): scenery instead of lyric videos; "never replayed"; no intros
        import tempfile
        from pathlib import Path
        from ktvibes import scenery
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        s = scenery.Scenery(Path(folder.name))
        s.films = lambda state: [("aaaaaaaaaaa.mp4", 600.0), ("bbbbbbbbbbb.mp4", 500.0)]
        got = [s.take(200) for _ in range(5)]
        self.assertEqual([g["file"][0] for g in got], ["a", "a", "b", "b", "a"])
        self.assertEqual(got[0]["start"], scenery.EDGE)                    # the film's first seconds are skipped
        self.assertGreaterEqual(got[1]["start"], got[0]["start"] + 200)    # the next song gets the next stretch
        self.assertTrue(all(g["start"] + 200 <= (600 if g["file"][0] == "a" else 500) - scenery.EDGE for g in got))
        self.assertEqual(got[4]["start"], scenery.EDGE)                    # only once both films are used up: again
        self.assertEqual(scenery.Scenery(Path(folder.name)).take(10) is not None, False)  # a fresh object with no films
        again = scenery.Scenery(Path(folder.name)); again.films = s.films
        self.assertGreater(again.take(50)["start"], got[4]["start"])       # the place is kept across restarts
        (Path(folder.name) / "aaaaaaaaaaa.720.mp4").touch()                   # a lighter copy for TVs that drop frames
        self.assertEqual(again.take(50).get("lite"), "aaaaaaaaaaa.720.mp4")
        self.assertTrue(scenery.SERVE.fullmatch("aaaaaaaaaaa.720.mp4") and not scenery.NAME.fullmatch("aaaaaaaaaaa.720.mp4"))

    def test_sung_parts_without_lyrics_are_found(self):
        # karaoke-server (beta 2026-11-22, "Broken Vessels": 13 lines for a 7.5-minute song)
        import numpy as np
        from ktvibes.lyrics import uncovered
        rate = 1000
        t = np.arange(300 * rate) / rate
        voice = np.where((t < 100) | (t > 160), 0.5, 0.0) * np.sin(2 * np.pi * 220 * t)  # quiet 100-160 s: a break
        lines = [{"t": float(x), "text": "la"} for x in (10, 20, 30, 40, 50, 60, 70, 80, 90, 165, 175, 185)]
        gaps = uncovered(voice.astype("float32"), rate, lines, 300)
        self.assertEqual(len(gaps), 1)                 # the instrumental break (90-165) isn't flagged...
        self.assertGreater(gaps[0][0], 185)            # ...but singing after the last line (193-299 s) is
        self.assertGreater(gaps[0][1], 290)

    def test_spotify_suggestions(self):
        # karaoke-server (user, 2026-11-26): Spotify's catalogue for search suggestions; nothing without keys
        import io, json, os
        from ktvibes import spotify
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(spotify.enabled())
        track = lambda name, artist, album_art: {"id": name, "name": name, "duration_ms": 206000, "artists": [{"name": artist}],
                                                 "album": {"images": [{"url": album_art + "640", "width": 640}, {"url": album_art + "64", "width": 64}]}}
        answers = [{"access_token": "t", "expires_in": 3600},
                   {"tracks": {"items": [track("Good Girls", "5 Seconds of Summer", "a"), track("Good Girls - Live", "5 Seconds of Summer", "b"),
                                         track("Amnesia", "5 Seconds of Summer", "c")]}}]
        calls = []
        def fake(request, timeout=0):
            calls.append(getattr(request, "full_url", request)); return io.BytesIO(json.dumps(answers.pop(0)).encode())
        spotify._cache.clear(); spotify._token.update(value="", until=0)
        with unittest.mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}), \
             unittest.mock.patch.object(spotify.urllib.request, "urlopen", fake):
            songs = spotify.search_tracks("5sos good girls")
            again = spotify.search_tracks("5SOS good girls ")          # same words: from memory, no call
        self.assertEqual([s["title"] for s in songs], ["Good Girls", "Amnesia"])   # the live copy of the same song folds in
        self.assertEqual((songs[0]["artist"], songs[0]["duration"], songs[0]["art"]), ("5 Seconds of Summer", 206.0, "a64"))
        self.assertEqual(again, songs)
        self.assertEqual(len(calls), 2)                                  # one token, one search
        self.assertIn("limit=10", calls[1])                             # never more than the app may ask for

    def test_album_cover_only_for_the_same_song(self):
        # karaoke-server (user, 2026-11-26): Spotify's album cover on the TV, but never another song's
        from ktvibes import spotify
        songs = [{"title": "Palagi - TJxKZ Version", "artist": "TJ Monterde, KZ Tandingan", "cover": "duet"},
                 {"title": "Palagi", "artist": "TJ Monterde", "cover": "album"}]
        with unittest.mock.patch.object(spotify, "search_tracks", lambda q, limit=5: songs):
            self.assertEqual(spotify.album_cover("TJ Monterde", "Palagi"), "album")       # the exact title over the duet version
            self.assertEqual(spotify.album_cover("TJ Monterde", "Palagi (Live)"), "duet")  # no exact title: the same song still fits
            self.assertEqual(spotify.album_cover("Someone Else", "Palagi"), "")           # another artist's "Palagi": no
            self.assertEqual(spotify.album_cover("TJ Monterde", "Ikaw at Ako"), "")       # another song: no

    def test_spotify_length_settles_and_sorts(self):
        # karaoke-server (user, 2026-11-26): Spotify's exact length checks lyric labels and puts the plain recording first
        from ktvibes.lyrics import real_lengths
        from ktvibes.youtube import mark_lyrics, order_results, song_name
        rec = lambda d: {"duration": d, "syncedLyrics": "[00:00.57]a\n[03:20.35]b"}
        records = [rec(218), rec(238), rec(238)]              # one timing; two uploads labelled it with a video's 238 s
        real_lengths(records); self.assertEqual([r["length"] for r in records], [238.0, 238.0, 238.0])   # outvoted alone
        records = [rec(218), rec(238), rec(238)]
        real_lengths(records, known=218.1); self.assertEqual([r["length"] for r in records], [218.0, 218.0, 218.0])  # Spotify settles it
        r = lambda i, kind, d: {"id": i, "kind": kind, "title": "Palagi", "duration": d, "parsed": {"title": "Palagi"}}
        results = [r("mv", "official", 238), r("lyric", "lyric", 219)]
        mark_lyrics(results, [(song_name("Palagi"), 238.0), (song_name("Palagi"), 218.0)])
        results[1]["spotify_fit"] = True                      # as long as Spotify's recording
        self.assertEqual([x["id"] for x in order_results(results)], ["lyric", "mv"])
        results[1]["spotify_fit"] = False
        self.assertEqual([x["id"] for x in order_results(results)], ["mv", "lyric"])   # without it: official first, as before

    def test_spotify_rate_limit_is_respected(self):
        # karaoke-server (2026-11-26): after a 429, no calls to Spotify until its Retry-After has passed
        import email.message, urllib.error, urllib.request
        from ktvibes import spotify
        headers = email.message.Message(); headers["Retry-After"] = "120"
        calls = []
        def refuse(*a, **k):
            calls.append(1); raise urllib.error.HTTPError("u", 429, "Too Many Requests", headers, None)
        with unittest.mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "x", "SPOTIFY_CLIENT_SECRET": "y"}), \
             unittest.mock.patch.object(urllib.request, "urlopen", refuse), unittest.mock.patch.object(spotify, "_quiet_until", 0.0):
            self.assertTrue(spotify.enabled())
            with self.assertRaises(spotify.Resting):
                spotify._open(urllib.request.Request("https://api.spotify.com/v1/search"), 4)
            self.assertFalse(spotify.enabled())                    # callers skip Spotify meanwhile
            with self.assertRaises(spotify.Resting):
                spotify._open(urllib.request.Request("https://api.spotify.com/v1/search"), 4)
            self.assertEqual(len(calls), 1)                        # the second try never reached Spotify

    def test_accents_do_not_stop_a_match(self):
        # karaoke-server: 5,000-song study (2026-11-22): "GIVĒON - Heartbreak Anniversary (Official Music Video)" vs
        # LRCLIB's "Giveon", and "MÍA" vs "MIA": the official video was passed over
        from ktvibes.youtube import mark_lyrics, artist_keys, song_name, names_from_records
        results = [{"id": "mv", "kind": "official", "title": "GIVĒON - Heartbreak Anniversary (Official Music Video)", "channel": "GIVĒON",
                    "duration": 248, "parsed": {"artist": "GIVĒON", "title": "Heartbreak Anniversary"}},
                   {"id": "mia", "kind": "official", "title": "BAD BUNNY x DRAKE - MÍA (Official Video)", "channel": "Bad Bunny",
                    "duration": 237, "parsed": {"artist": "BAD BUNNY x DRAKE", "title": "MÍA"}}]
        mark_lyrics(results, [(song_name("Heartbreak Anniversary"), 247.0, artist_keys("Giveon")), (song_name("MIA"), 236.0, artist_keys("Bad Bunny"))])
        self.assertTrue(all(r["lyrics_match"] for r in results))
        # one vote for both spellings, written the way the video writes it
        names_from_records(results, [{"artistName": "Bad Bunny", "trackName": "MIA"}, {"artistName": "Bad Bunny", "trackName": "MIA"},
                                     {"artistName": "Bad Bunny", "trackName": "MÍA"}])
        self.assertEqual(results[1]["parsed"]["title"], "MÍA")

    def test_real_length_of_lyrics_comes_from_their_timing_not_the_label(self):
        # karaoke-server: real LRCLIB records for Lady Gaga "Telephone" (2026-11-21), cut down to first/last lines
        from ktvibes.lyrics import real_lengths, fits
        rec = lambda i, d, first, last: {"id": i, "duration": d, "syncedLyrics": f"[{int(first // 60):02d}:{first % 60:05.2f}]a\n[{int(last // 60):02d}:{last % 60:05.2f}]b"}
        records = [rec("album1", 220, 8.8, 219.3), rec("album2", 221, 8.8, 218.9), rec("album3", 221, 8.1, 218.5), rec("album4", 222, 7.9, 218.0),
                   rec("album_as_video", 571, 8.0, 218.0),   # album timing labelled with the 9:31 video's length
                   rec("video", 571, 174.7, 566.0),          # really timed to the story video
                   rec("impossible", 220, 173.7, 568.9)]     # lines run past its own end
        real_lengths(records)
        got = {r["id"]: r["length"] for r in records}
        self.assertEqual(got["album_as_video"], 221)
        self.assertEqual(got["video"], 571)
        self.assertIsNone(got["impossible"])
        self.assertEqual(got["album1"], 220)
        self.assertFalse(fits(records[4], 571))   # no longer a "perfect fit" for the 9:31 video
        self.assertTrue(fits(records[5], 571))

    def test_search_results_take_clean_names_from_lyrics_records(self):
        # karaoke-server: real cases from the 1,000-song study (2026-11-21)
        from ktvibes.youtube import names_from_records
        res = lambda title, channel, artist, song: {"title": title, "channel": channel, "parsed": {"artist": artist, "title": song}}
        results = [res("Shake It Off - Taylor Swift (Lyrics)", "Pillow", "Shake It Off", "Taylor Swift"),        # reversed
                   res("XXXTENTACION - Moonlight Audio", "Yung Dagger X", "XXXTENTACION", "Moonlight Audio"),   # junk kept
                   res("Hozier- Take Me To Church (Audio)", "Music Audios", "Music Audios", "Hozier- Take Me To Church"),
                   res("Eye of the Tiger", "", "", "Eye of the Tiger"),                                         # no artist
                   res("Adele - Hello (Live at the NRJ Awards)", "Adele", "Adele", "Hello")]
        records = [{"artistName": "Taylor Swift", "trackName": "Shake It Off"}, {"artistName": "Taylor Swift", "trackName": "Shake It Off"},
                   {"artistName": "Taylor Swift", "trackName": "Taylor Swift - Shake It Off (Official Video)"},   # player upload: ignored
                   {"artistName": "XXXTENTACION", "trackName": "Moonlight"}, {"artistName": "Hozier", "trackName": "Take Me to Church"},
                   {"artistName": "Survivor", "trackName": "Eye of the Tiger"}, {"artistName": "Lionel Richie", "trackName": "Hello"}]
        names_from_records(results, records)
        got = [(r["parsed"]["artist"], r["parsed"]["title"]) for r in results]
        self.assertEqual(got[:4], [("Taylor Swift", "Shake It Off"), ("XXXTENTACION", "Moonlight"), ("Hozier", "Take Me to Church"), ("Survivor", "Eye of the Tiger")])
        self.assertEqual(got[4], ("Adele", "Hello"))   # Lionel Richie's "Hello" isn't named in this title: unchanged
        # a remix spelling doesn't name a video that isn't the remix (all of Keala Settle's records are remixes)
        tim = [res("This Is Me - Keala Settle (Lyrics)", "7clouds", "This Is Me", "Keala Settle")]
        names_from_records(tim, [{"artistName": "Keala Settle & The Greatest Showman Ensemble", "trackName": 'This Is Me (Dave Audé Remix) [From "The Greatest Showman"]'}] * 2)
        self.assertEqual(tim[0]["parsed"]["title"], 'This Is Me [From "The Greatest Showman"]')
        # Filipino uploads often read "Song - Artist"; YouTube's majority must not overrule LRCLIB's records
        opm = [res("Kung Kailangan Mo Ako - Rey Valera (Official Lyric Video)", "Vicor Music", "Kung Kailangan Mo Ako", "Rey Valera") for _ in range(3)]
        names_from_records(opm, [{"artistName": "Rey Valera", "trackName": "Kung Kailangan Mo Ako"}])
        self.assertEqual((opm[0]["parsed"]["artist"], opm[0]["parsed"]["title"]), ("Rey Valera", "Kung Kailangan Mo Ako"))

    def test_singer_names_are_unique_per_phone_until_idle(self):
        state = State()
        self.assertEqual(state.claim_singer('  Anna ', 'phone-1'), 'Anna')
        self.assertEqual(state.claim_singer('anna', 'phone-1'), 'anna')  # same phone may keep or restyle it
        with self.assertRaisesRegex(ValueError, 'already singing here'):
            state.claim_singer('ANNA', 'phone-2')
        state.claim_singer('Bea', 'phone-1')  # switching names frees the old one
        self.assertEqual(state.claim_singer('Anna', 'phone-2'), 'Anna')
        state.singers['anna']['seen'] -= 6 * 3600 + 1  # idle for 6 hours: free again
        self.assertEqual(state.claim_singer('Anna', 'phone-3'), 'Anna')
        with self.assertRaises(ValueError):
            state.claim_singer('   ', 'phone-3')

    async def test_queue_and_history_survive_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            state = State()
            state.restore(path, seed=lambda: {'zzzzzzzzzzz': 1.0})
            self.assertEqual(state.played, {'zzzzzzzzzzz': 1.0})
            a = await state.add('abcdefghijk', '周杰倫', '晴天')
            await state.add('12345678901', '아이유', '좋은 날')
            a['status'] = 'ready'
            state.promote()
            state.position = 42.5
            await state.broadcast()
            again = State()
            again.restore(path, seed=lambda: self.fail('history should come from the file'))
            self.assertEqual([(i['id'], i['title'], i['status']) for i in again.upcoming],
                             [('abcdefghijk', '晴天', 'queued'), ('12345678901', '좋은 날', 'queued')])
            again.upcoming[0]['status'] = 'ready'
            again.promote()
            self.assertEqual(again.current['title'], '晴天')
            self.assertEqual(again.position, 42.5)
            self.assertIn('zzzzzzzzzzz', again.played)

    async def test_order_waits_for_first_and_skips_error(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        b = await state.add('12345678901', 'B', 'Second')
        b['status'] = 'ready'
        state.promote()
        self.assertIsNone(state.current)
        a['status'] = 'error'
        state.promote()
        self.assertIs(state.current, b)
        self.assertEqual(state.upcoming, [a])

    @unittest.skip("karaoke-server: songs can\'t be removed from the queue")

    async def test_removed_inflight_item_cannot_return(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        a['status'] = 'separating'
        await state.control({'action': 'remove', 'key': a['key']})
        a['status'] = 'ready'
        state.promote()
        self.assertIsNone(state.current)

    async def test_stale_ended_and_wrong_player_do_not_advance(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        a['status'] = 'ready'
        state.promote()
        state.player = object()
        await state.control({'action': 'ended', 'key': a['key']}, object())
        await state.control({'action': 'ended', 'key': 'stale'}, state.player)
        self.assertIs(state.current, a)
        await state.control({'action': 'ended', 'key': a['key']}, state.player)
        self.assertIsNone(state.current)

    @unittest.skip("karaoke-server: the queue plays in order; songs can\'t be moved")

    async def test_reorder_requires_current_members(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        b = await state.add('12345678901', 'B', 'Second')
        with self.assertRaises(ValueError):
            await state.control({'action': 'reorder', 'keys': [a['key'], a['key']]})
        await state.control({'action': 'reorder', 'keys': [b['key'], a['key']]})
        self.assertEqual(state.upcoming, [b, a])

    @unittest.skip("karaoke-server: songs can\'t be removed from the queue")

    async def test_removal_can_be_undone_in_place(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        b = await state.add('12345678901', 'B', 'Second')
        await state.control({'action': 'remove', 'key': a['key']})
        self.assertEqual(state.undo['item']['title'], 'First')
        await state.control({'action': 'undo'})
        self.assertEqual(state.upcoming, [a, b])
        with self.assertRaises(ValueError):
            await state.control({'action': 'undo'})
        await state.control({'action': 'remove', 'key': a['key']})
        await state.add('abcdefghijk', 'A', 'Again')
        with self.assertRaises(ValueError):  # the song was queued again meanwhile
            await state.control({'action': 'undo'})

    async def test_failed_song_can_be_retried(self):
        state = State()
        a = await state.add('abcdefghijk', 'A', 'First')
        a['status'], a['error'] = 'error', 'HTTP 403'
        await state.control({'action': 'retry', 'key': a['key']})
        self.assertEqual((a['status'], a['error']), ('queued', None))

    async def test_lyrics_go_to_each_tv_once_per_song(self):
        class Socket:
            def __init__(self):
                self.sent = []
            async def send_json(self, message):
                self.sent.append(message)
        state = State()
        tv, remote = Socket(), Socket()
        state.clients = {tv: 'tv', remote: 'remote'}
        state.player = tv
        a = await state.add('abcdefghijk', 'A', 'First')
        a['status'], a['lyrics'] = 'ready', [{'t': 0, 'text': 'la'}]
        state.promote()
        await state.broadcast()
        await state.broadcast()
        self.assertEqual([m['current'].get('lyrics') for m in tv.sent[-2:]], [a['lyrics'], None])
        self.assertTrue(all('lyrics' not in (m['current'] or {}) for m in remote.sent))
        self.assertTrue(all('lyrics' not in i for m in tv.sent for i in m['upcoming']))

    async def test_audio_flag_only_from_player_and_cleared_on_disconnect(self):
        state = State()
        tv = object()
        state.player = tv
        await state.control({'action': 'audio', 'value': True}, object())
        self.assertFalse(state.snapshot()['player_audio'])
        await state.control({'action': 'audio', 'value': True}, tv)
        self.assertTrue(state.snapshot()['player_audio'])
        state.disconnect(tv)
        self.assertFalse(state.audio)

    async def test_lyric_offset_is_remembered_per_song(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            state = State()
            state.restore(Path(folder) / 'queue.json')
            (Path(folder) / 'abcdefghijk').mkdir()
            meta = Path(folder) / 'abcdefghijk' / 'meta.json'
            meta.write_text(json.dumps({'title': '晴天'}), encoding='utf-8')
            a = await state.add('abcdefghijk', 'A', 'First')
            a['status'], a['offset'] = 'ready', 1.0
            state.promote()
            self.assertEqual(state.offset, 1.0)
            await state.control({'action': 'offset', 'delta': .5})
            self.assertEqual(json.loads(meta.read_text(encoding='utf-8')), {'title': '晴天', 'lyric_offset': 1.5})

    @unittest.skip("karaoke-server: Classic theme and pronunciation guides removed")

    async def test_classic_room_timer_holds_the_next_song_until_time_is_added(self):
        state = State()
        clock = unittest.mock.patch('ktvibes.queue.time.time')
        now = clock.start()
        self.addCleanup(clock.stop)
        now.return_value = 1000.0
        state.lyric_mode = 'scroll'
        await state.control({'action': 'theme', 'value': 'classic'})
        self.assertEqual(state.room_ends, 1000.0 + 30 * 60)
        self.assertEqual(state.lyric_mode, 'two')  # Classic is always two lines
        await state.control({'action': 'lyric_mode', 'value': 'scroll'})
        self.assertEqual(state.lyric_mode, 'two')
        now.return_value = 1100.0
        await state.control({'action': 'theme', 'value': 'classic'})  # choosing it again keeps the time
        self.assertEqual(state.room_ends, 1000.0 + 30 * 60)
        for video_id in ('aaaaaaaaaaa', 'bbbbbbbbbbb'):
            item = await state.add(video_id, 'IU', video_id)
            item['status'], item['duration'] = 'ready', 100
        state.promote()
        now.return_value = 1000.0 + 31 * 60
        self.assertTrue(state.snapshot()['time_up'])
        await state.control({'action': 'skip'})
        self.assertIsNone(state.current)  # the room's time is up
        await state.control({'action': 'add_time', 'minutes': 10})
        self.assertEqual(state.current['id'], 'bbbbbbbbbbb')
        self.assertEqual(state.room_ends, 1000.0 + 41 * 60)
        await state.control({'action': 'theme', 'value': 'default'})
        self.assertIsNone(state.room_ends)
        self.assertFalse(state.snapshot()['time_up'])
        with self.assertRaises(ValueError):
            await state.control({'action': 'add_time', 'minutes': 10})  # no timer in Default
        await state.control({'action': 'theme', 'value': 'classic'})
        self.assertEqual(state.room_ends, 1000.0 + 61 * 60)  # a fresh 30 minutes

    @unittest.skip("karaoke-server: Classic theme and pronunciation guides removed")

    async def test_theme_and_room_time_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'queue.json'
            state = State()
            state.path = path
            await state.control({'action': 'theme', 'value': 'classic'})
            ends = state.room_ends
            again = State()
            again.restore(path)
            self.assertEqual((again.theme, again.room_ends), ('classic', ends))
            with self.assertRaises(ValueError):
                await again.control({'action': 'add_time', 'minutes': 0})

    @unittest.skip("karaoke-server: the queue plays in order; songs can\'t be moved")

    async def test_reserve_next_puts_the_song_first_with_its_number(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            for video_id, number in [('aaaaaaaaaaa', 10001), ('bbbbbbbbbbb', 10002)]:
                (cache / video_id).mkdir()
                (cache / video_id / 'meta.json').write_text(json.dumps({'artist': 'IU', 'title': video_id, 'number': number}), encoding='utf-8')
            state = State()
            state.path = cache / 'queue.json'
            await state.control({'action': 'reserve', 'number': 10001})
            await state.control({'action': 'reserve', 'number': 10002, 'next': True})
            self.assertEqual([(i['id'], i['number']) for i in state.upcoming], [('bbbbbbbbbbb', 10002), ('aaaaaaaaaaa', 10001)])

    async def test_dial_reaches_the_tv_and_reserve_queues_by_number(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            (cache / 'aaaaaaaaaaa').mkdir()
            (cache / 'aaaaaaaaaaa' / 'meta.json').write_text(json.dumps({'artist': 'IU', 'title': '좋은 날', 'number': 10001}), encoding='utf-8')
            state = State()
            state.path = cache / 'queue.json'
            state.player = tv = object()
            sent = []
            async def send(ws, message):
                sent.append((ws, message))
                return True
            state.send = send
            revision = state.revision
            await state.control({'action': 'dial', 'digits': '100'})
            self.assertEqual(sent, [(tv, {'type': 'dial', 'digits': '100'})])
            self.assertEqual(state.revision, revision)  # nothing saved or broadcast
            with self.assertRaises(ValueError):
                await state.control({'action': 'dial', 'digits': '12a'})
            await state.control({'action': 'reserve', 'number': 10001})
            self.assertEqual((state.upcoming[0]['id'], state.upcoming[0]['title']), ('aaaaaaaaaaa', '좋은 날'))
            self.assertIn((tv, {'type': 'reserved', 'number': 10001}), sent)
            with self.assertRaises(ValueError):
                await state.control({'action': 'reserve', 'number': 10001})  # already queued
            with self.assertRaisesRegex(ValueError, 'No song 10099'):
                await state.control({'action': 'reserve', 'number': 10099})
            self.assertEqual(len(state.upcoming), 1)
            # Reserving while the room's time is up queues the song but doesn't start it.
            state.theme, state.room_ends = 'classic', 1.0
            state.upcoming[0]['status'] = 'ready'
            state.promote()
            self.assertIsNone(state.current)

class WorkerRunTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("karaoke-server: songs can\'t be removed from the queue")
    async def test_removed_song_stops_holding_up_the_queue(self):
        import tempfile, threading
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from ktvibes import worker
        release = threading.Event()
        def fetch_video(video_id, folder, progress):
            if video_id == 'aaaaaaaaaaa':
                release.wait(10)  # a slow download that outlives its song's place in the queue
        async def no_lyrics(*args):
            return {'raw': '', 'lines': []}
        state = State()
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(worker, 'fetch_video', fetch_video), \
             patch.object(worker.stems, 'ready', lambda folder: True), \
             patch.object(worker.sf, 'info', lambda path: SimpleNamespace(duration=100)), \
             patch.object(worker.lyrics, 'fetch', no_lyrics):
            task = asyncio.create_task(worker.run(state, Path(temp)))
            try:
                slow = await state.add('aaaaaaaaaaa', 'A', 'Slow')
                for _ in range(50):
                    if slow.get('step') == 'video':
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(slow.get('step'), 'video')
                await state.control({'action': 'remove', 'key': slow['key']})
                nxt = await state.add('bbbbbbbbbbb', 'B', 'Next')
                for _ in range(60):
                    if state.current is nxt:
                        break
                    await asyncio.sleep(0.05)
                self.assertIs(state.current, nxt)
                self.assertFalse(release.is_set())
                # Undoing the removal prepares it again rather than leaving it half done.
                self.assertEqual(slow['status'], 'queued')
            finally:
                release.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task


class WorkerTests(unittest.TestCase):
    def test_old_stems_migrate_to_opus_with_gain(self):
        import tempfile
        from pathlib import Path
        import numpy as np
        import soundfile as sf
        from ktvibes import stems
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            self.assertIsNone(stems.migrate(folder, None))
            # A steady tone above full scale, as Demucs stems can be; Opus is lossy, so compare levels.
            tone = 1.5 * np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000).astype('float32')[:, None].repeat(2, 1)
            level = lambda x: float(np.sqrt(np.mean(x[4000:-4000] ** 2)))
            sf.write(folder / 'vocals.wav', tone / 3, 48000, subtype='FLOAT')
            sf.write(folder / 'no_vocals.wav', tone, 48000, subtype='FLOAT')
            gain = stems.migrate(folder, None)
            self.assertTrue(stems.ready(folder))
            self.assertFalse((folder / 'vocals.wav').exists())
            restored, rate = sf.read(folder / 'no_vocals.opus', dtype='float32')
            self.assertEqual(rate, 48000)
            self.assertAlmostEqual(level(restored) * gain, level(tone), delta=0.02)
            # FLAC from the previous version was stored scaled by its gain; the level survives conversion.
            for name in stems.NAMES:
                (folder / f'{name}.opus').unlink()
            sf.write(folder / 'vocals.flac', tone / 3 / 2, 44100, subtype='PCM_24')
            sf.write(folder / 'no_vocals.flac', tone / 2, 44100, subtype='PCM_24')
            gain = stems.migrate(folder, 2.0)
            self.assertFalse((folder / 'no_vocals.flac').exists())
            vocals, _ = sf.read(stems.path(folder, 'vocals'), dtype='float32')
            self.assertAlmostEqual(level(vocals) * gain, level(tone / 3), delta=0.02)

    def test_timed_lyrics_are_reused_until_the_lrc_changes(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import numpy as np
        import soundfile as sf
        from ktvibes import worker
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            sf.write(folder / 'vocals.wav', np.zeros((8000, 2), dtype='float32'), 8000)
            with patch.object(worker.align, 'align') as align:
                first = worker.timed_lyrics(folder, '[00:00.10]hello', folder / 'vocals.wav', parse_lrc('[00:00.10]hello'))
                again = worker.timed_lyrics(folder, '[00:00.10]hello', folder / 'vocals.wav', parse_lrc('[00:00.10]hello'))
                self.assertEqual(align.call_count, 1)
                self.assertEqual(first, again)
                worker.timed_lyrics(folder, '[00:00.10]goodbye', folder / 'vocals.wav', parse_lrc('[00:00.10]goodbye'))
                self.assertEqual(align.call_count, 2)
            # Without the aligner, the energy estimate is kept and only retried in a later run.
            with patch.object(worker.align, 'align', side_effect=RuntimeError) as align:
                worker.timed_lyrics(folder, '[00:00.10]again', folder / 'vocals.wav', parse_lrc('[00:00.10]again'))
                worker.timed_lyrics(folder, '[00:00.10]again', folder / 'vocals.wav', parse_lrc('[00:00.10]again'))
                self.assertEqual(align.call_count, 1)
                with patch.object(worker, 'STARTED', time.time() + 60):  # as if KTVibes restarted
                    worker.timed_lyrics(folder, '[00:00.10]again', folder / 'vocals.wav', parse_lrc('[00:00.10]again'))
                self.assertEqual(align.call_count, 2)

if __name__ == '__main__':
    unittest.main()

class LyricsMatchTests(unittest.TestCase):
    def test_cantonese_gets_jyutping_and_is_detected(self):
        from ktvibes import lyrics
        lines = [{'t': 0, 'text': '我哋喺度', 'units': [[c, 0, 1] for c in '我哋喺度']}, {'t': 1, 'text': '冇嘢', 'units': [[c, 1, 2] for c in '冇嘢']}]
        self.assertTrue(lyrics.cantonese(lines))
        self.assertEqual([u[5] for u in lyrics.add_jyutping(lines)[0]['units']], ['ngo5', 'dei6', 'hai2', 'dou6'])
        self.assertFalse(lyrics.cantonese([{'t': 0, 'text': '我是你的'}]))

    def test_japanese_gets_romaji_not_pinyin(self):
        from ktvibes import lyrics
        units = lyrics.split_units('きっと世界 ちゃんと Love')
        self.assertEqual(lyrics.romaji(units), ['ki', '', 'tto', 'sekai', '', 'cha', '', 'n', 'to', ''])
        lines = [{'t': 0, 'text': '世界', 'units': [['世', 0, 1], ['界', 1, 2]]}, {'t': 2, 'text': 'ありがとう', 'units': [[c, 2, 3] for c in 'ありがとう']}]
        lines = lyrics.add_pinyin(lyrics.add_romaji(lines))
        self.assertEqual(lines[0]['units'][0][3], 'sekai')  # a kanji-only line in a Japanese song is still Japanese

    def test_original_script_beats_romanized_record(self):
        from ktvibes import lyrics
        romanized = {"trackName": "Spring Day", "duration": 274, "syncedLyrics": "[00:10.00] bogo sipda"}
        hangul = {"trackName": "Spring Day", "duration": 276, "syncedLyrics": "[00:10.00] 보고 싶다"}
        japanese = {"trackName": "Spring Day", "duration": 274, "syncedLyrics": "[00:10.00] 会いたい"}
        best = min([romanized, japanese, hangul], key=lambda c: lyrics.rank(c, ["Spring Day"], 274, "ko"))
        self.assertIs(best, hangul)

    def test_script_tells_korean_japanese_and_chinese_apart(self):
        from ktvibes import lyrics
        self.assertEqual([lyrics.script(t) for t in ("보고 싶다", "会いたい", "晴天", "bogo sipda")], ["ko", "ja", "zh", "latin"])
        self.assertEqual(lyrics.wanted_script("BTS (방탄소년단)", "Spring Day"), "ko")
        self.assertIsNone(lyrics.wanted_script("NewJeans", "Super Shy"))

    def test_untimed_synced_lyrics_are_rejected(self):
        from unittest.mock import patch
        from ktvibes import lyrics
        untimed = {'trackName': '一路向北', 'duration': 301.0, 'syncedLyrics': '後視鏡裏的世界\n越來越遠的道別'}
        timed = {'trackName': '一路向北', 'duration': 302.0, 'syncedLyrics': '[00:20.00]後視鏡裏的世界'}
        class Response:
            def __init__(self, status, data): self.status_code, self.data = status, data
            def json(self): return self.data
            def raise_for_status(self): pass
        class Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, path, params):
                return Response(404, {}) if path == '/api/get' else Response(200, [untimed, timed])
        with patch('httpx.AsyncClient', Client):
            result = asyncio.run(lyrics.fetch('周杰倫', '一路向北', 301.2))
        self.assertEqual(result['lines'], [{'t': 20.0, 'text': '後視鏡裏的世界'}])

    def test_variants_split_bilingual_names(self):
        from ktvibes.lyrics import variants
        self.assertEqual(variants("아이유(IU)"), ["아이유(IU)", "아이유", "IU"])
        self.assertEqual(variants("晴天"), ["晴天"])
        self.assertEqual(variants("周杰倫 Jay Chou"), ["周杰倫 Jay Chou", "周杰倫", "Jay Chou"])
        self.assertEqual(variants("安靜 Silence"), ["安靜 Silence", "安靜", "Silence"])

    def test_rank_prefers_title_match_over_duration(self):
        from ktvibes.lyrics import rank
        a = {"trackName": "Good day (좋은 날)", "duration": 236.0}
        b = {"trackName": "Rain Drop", "duration": 235.6}
        self.assertLess(rank(a, ["좋은 날"], 235.6), rank(b, ["좋은 날"], 235.6))

    def test_units_follow_synthetic_vocal_energy(self):
        from ktvibes.lyrics import time_units
        # Synthetic envelope, 0.05s frames: singing 1-2s, silence 2-3s, singing 3-4s.
        energy = [0.0] * 20 + [1.0] * 20 + [0.0] * 20 + [1.0] * 20 + [0.0] * 40
        lines = time_units([{'t': 1.0, 'text': '你好'}, {'t': 5.0, 'text': ''}], energy)
        (a, a_start, a_end), (b, b_start, b_end) = lines[0]['units']
        self.assertEqual((a, b), ('你', '好'))
        self.assertAlmostEqual(a_start, 1.0)
        self.assertLessEqual(a_end, 2.1)
        self.assertGreaterEqual(b_start, 2.9)
        self.assertLessEqual(b_end, 4.1)

    def test_whole_song_shift_found_from_vocal_onsets(self):
        import numpy as np
        from ktvibes.lyrics import find_shift, shift_lines
        stamps = [4.0, 11.5, 17.0, 26.5, 33.0, 41.5, 47.0, 55.5, 61.0, 68.5, 74.0, 82.5]
        lines = [{'t': t, 'text': 'la la'} for t in stamps]
        def sung(offset):
            energy = np.zeros(1900)  # 95 s of 50 ms frames
            for t in stamps:
                start = int((t + offset) / .05)
                energy[start:start + 40] = 1  # two seconds of singing per line
            return energy
        self.assertAlmostEqual(find_shift(lines, sung(3.3)), 3.3, delta=.1)
        self.assertEqual(find_shift(lines, sung(0)), 0)
        self.assertEqual(find_shift(lines, sung(.3)), 0)  # small offsets are left to per-line alignment
        self.assertEqual(find_shift(lines, np.random.default_rng(1).random(1900)), 0)  # no clear fit
        lines[0]['units'] = [['la ', 4.0, 4.5]]
        shift_lines(lines, 3.3)
        self.assertEqual((lines[0]['t'], lines[0]['units'][0][1:]), (7.3, [7.3, 7.8]))

    def test_sections_after_a_cut_break_move_again(self):
        import numpy as np
        from ktvibes.lyrics import find_shift, shift_lines, shift_sections
        # Fix You: its singing (start frame, length) and LRC stamps. The vocals start ~2 s before the stamps,
        # but with lines ~7 s apart, shifting every stamp back a whole line also lands on singing, and scored higher.
        runs = [(227, 6), (234, 21), (257, 8), (276, 11), (289, 35), (368, 7), (377, 5), (385, 4), (390, 20), (415, 5),
                (422, 4), (428, 41), (508, 9), (519, 17), (538, 13), (556, 4), (562, 47), (632, 10), (644, 84), (791, 4),
                (797, 14), (812, 41), (856, 42), (931, 30), (963, 16), (980, 11), (998, 29), (1071, 18), (1090, 29),
                (1122, 12), (1135, 39), (1199, 15), (1217, 101), (1401, 33), (1437, 50), (1489, 31), (1544, 32),
                (1577, 51), (1629, 8), (1640, 31), (1678, 4), (1683, 31), (1715, 49), (1776, 17), (1921, 19), (1948, 12),
                (1961, 4), (1966, 51), (2061, 7), (2069, 89), (2198, 6), (2205, 99), (2324, 15), (2340, 100), (2527, 15),
                (2543, 4), (2548, 14), (2564, 50), (2615, 34), (2669, 85), (2756, 7), (2766, 32), (2810, 86), (2904, 24),
                (4041, 33), (4099, 52), (4162, 38), (4201, 74), (4276, 5), (4315, 33), (4372, 144), (4588, 36),
                (4646, 23), (4671, 32), (4706, 114), (4863, 36), (4900, 11), (4919, 146), (5137, 34), (5172, 84),
                (5274, 121), (5406, 5), (5412, 375), (5854, 15)]
        energy = np.zeros(5876)
        for start, length in runs:
            energy[start:start + length] = 1
        stamps = [13.18, 20.29, 27.41, 33.32, 41.3, 48.53, 55.25, 61.66, 71.92, 79.02, 85.95, 105.02, 111.66, 118.55,
                  124.89, 135.35, 142.18, 149.3, 210.85, 217.04, 224.64, 238.28, 244.04, 251.98, 265.69, 272.31, 279.18]
        lines = [{'t': t, 'text': 'la'} for t in stamps]
        self.assertAlmostEqual(find_shift(lines, energy), -2, delta=.3)
        # The video also cuts ~7 s from the break after the first chorus, so later sections move again.
        gaps = [{'t': t, 'text': ''} for t in (39.56, 68.44, 92.14, 131.48, 155.64, 262.73, 285.32)]
        song = sorted(lines + gaps, key=lambda line: line['t'])
        shift_lines(song, -1.95)
        moved = shift_sections(song, energy)
        self.assertEqual(moved[:3], [0, 0, 0])  # sections already in place stay
        for later in moved[3:]:
            self.assertAlmostEqual(later, -7, delta=.5)

    def test_sections_stay_when_the_song_opens_on_its_first_word(self):
        import numpy as np
        from ktvibes.lyrics import shift_sections
        # Love The Way You Lie (Part II): sung from 0.23 s, with no quiet half second before it to compare.
        # That scored the opening as misplaced, moved it ~6 s, and the move carried into every later section.
        stamps = [0.23, 5.86, 11.56, 17.1, 23.22, 29.38, 35.28, 40.5]
        energy = np.zeros(1000)
        for t in stamps:
            energy[round(t / .05):round(t / .05) + 50] = 1
        song = sorted([{'t': t, 'text': 'la'} for t in stamps] + [{'t': t, 'text': ''} for t in (8.71, 14.0, 20.0, 26.0)],
                      key=lambda line: line['t'])
        self.assertEqual(set(shift_sections(song, energy)), {0})

    def test_a_long_video_intro_is_found(self):
        import numpy as np
        from ktvibes.lyrics import find_shift
        # Adele's Hello: the music video talks for ~75 s before the song, past the usual 30 s search.
        stamps = [5.82, 11.47, 17.81, 23.23, 30.1, 35.9, 41.2, 47.6, 53.3, 58.8, 64.5, 70.2]
        energy = np.zeros(3000)
        for t in stamps:
            energy[round((t + 75) / .05):round((t + 75) / .05) + 40] = 1
        self.assertAlmostEqual(find_shift([{'t': t, 'text': 'la'} for t in stamps], energy), 75, delta=.1)

    def test_a_song_that_fits_is_not_moved_to_a_far_repeat(self):
        import numpy as np
        from ktvibes.lyrics import find_shift
        # Stamps that fit as they are, if only a third of them clearly (soft singing), in audio where the
        # pattern repeats in full 60 s later: three times as good a fit, but the far search for long intros
        # only runs when the stamps fit badly as they are.
        stamps = [5.82, 11.47, 17.81, 23.23, 30.1, 35.9, 41.2, 47.6, 53.3, 58.8, 64.5, 70.2]
        energy = np.zeros(4000)
        for index, t in enumerate(stamps):
            for at in ((t, t + 60) if index % 3 == 0 else (t + 60,)):
                energy[round(at / .05):round(at / .05) + 40] = 1
        self.assertEqual(find_shift([{'t': t, 'text': 'la'} for t in stamps], energy), 0)

    def test_an_onset_of_minus_one_is_still_inside_the_audio(self):
        import numpy as np
        from ktvibes.lyrics import line_scores
        energy = np.zeros(400)
        energy[:20] = 1  # sung for the first second, then silent: a line at 1 s scores -1
        scores = line_scores([{'t': 1.0}, {'t': 30.0}], energy, .05, 0)
        self.assertEqual(scores[0, 0], -1.0)
        self.assertTrue(np.isnan(scores[1, 0]))  # past the end of the audio

    def test_a_short_section_after_a_cut_moves_with_the_rest(self):
        import numpy as np
        from ktvibes.lyrics import shift_sections
        # The video cuts 4 s from the break before a two-line bridge. Two lines alone are too little to move
        # a section, but the lines after the bridge are off by the same 4 s, so all of them move together.
        sung = [[5, 9.5, 15], [30, 34.5], [45, 49, 54.5, 58], [70, 75.5, 79]]
        gaps = [18, 37, 61, 82]
        energy = np.zeros(2000)
        for t in (t for section in sung for t in section):
            energy[round(t / .05):round(t / .05) + 30] = 1
        song = []
        for index, (section, gap) in enumerate(zip(sung, gaps)):
            late = 4 if index else 0
            song += [{'t': t + late, 'text': 'la'} for t in section] + [{'t': gap + late, 'text': ''}]
        self.assertEqual(shift_sections(song, energy), [0, -4, -4, -4])

    def test_line_switches_after_last_word_and_before_first(self):
        from ktvibes.lyrics import line_starts
        lines = [{'t': 27.08, 'text': 'Go ahead and bark after dark', 'units': [['Go ', 26.8, 26.9], ['dark', 29.23, 29.33]]},
                 {'t': 29.03, 'text': 'Fallen star', 'units': [['Fallen ', 30.04, 30.58], ['star', 30.68, 31.16]]},
                 {'t': 31.0, 'text': 'overlap', 'units': [['overlap', 31.1, 31.5]]},
                 {'t': 40.0, 'text': ''}]
        line_starts(lines)
        # Sanctuary: the stamp for "Fallen star" (29.03) cut off "after dark", sung until 29.33.
        self.assertEqual([line['start'] for line in lines], [26.5, 29.74, 31.1, 40.0])

    def test_backing_vocal_lines_overlap_the_lead(self):
        from ktvibes.lyrics import line_end, line_starts
        # Numb: "(Caught in the undertow…)" is stamped while the lead line is still being sung.
        lines = [{'t': 34.09, 'text': 'Put under the pressure of walking in your shoes', 'units': [['Put ', 34.1, 34.4], ['shoes', 40.2, 40.9]]},
                 {'t': 39.3, 'text': '(Caught in the undertow, just caught in the undertow)', 'units': [['(Caught ', 39.4, 39.8], ['undertow)', 42.0, 42.6]]},
                 {'t': 42.54, 'text': 'Every step that I take', 'units': [['Every ', 42.6, 42.9], ['take', 44.0, 44.5]]}]
        self.assertEqual(line_end(lines, 0), 42.54)  # aligned up to the next lead line, not cut at the echo
        self.assertEqual(line_end(lines, 1), 42.54)
        line_starts(lines)
        self.assertEqual([line['start'] for line in lines], [33.8, 39.1, 42.3])
        self.assertEqual([line.get('over') for line in lines], [None, 0, None])

    def test_lines_align_in_runs_split_at_gaps_and_backing_vocals(self):
        from ktvibes.align import runs
        lines = [{'t': 0, 'text': 'a'}, {'t': 3, 'text': 'b'}, {'t': 5, 'text': '(echo)'}, {'t': 6, 'text': 'c'},
                 {'t': 9, 'text': ''}, {'t': 20, 'text': 'd'}, {'t': 30, 'text': 'e'}, {'t': 70, 'text': 'f'}]
        words = {i: ['x'] for i, line in enumerate(lines) if line['text']}
        self.assertEqual(runs(lines, words), [[0, 1], [2], [3], [5, 6], [7]])

    def test_energy_timing_starts_after_the_previous_line(self):
        from ktvibes.lyrics import time_units
        lines = [{'t': 0, 'text': 'one', 'units': [['one', 0.5, 4.2]]}, {'t': 3.5, 'text': 'two'}, {'t': 9, 'text': ''}]
        time_units(lines, [1.0] * 200)
        self.assertGreaterEqual(lines[1]['units'][0][1], 4.2)  # the stamp (3.5) is before "one" is finished

    def test_enhanced_lrc_word_stamps(self):
        line = parse_lrc('[00:01.00]<00:01.00>Hel<00:01.50>lo')[0]
        self.assertEqual(line['text'], 'Hello')
        self.assertEqual(line['units'], [['Hel', 1.0, 1.5], ['lo', 1.5, 2.5]])

    def test_pinyin_per_unit_uses_line_context(self):
        from ktvibes.lyrics import add_pinyin
        lines = add_pinyin([{'t': 0, 'text': '还是 ok', 'units': [['还', 0, 1], ['是 ', 1, 2], ['ok', 2, 3]]},
                            {'t': 3, 'text': '좋은', 'units': [['좋', 3, 4], ['은', 4, 5]]}])
        self.assertEqual([u[3] for u in lines[0]['units']], ['hái', 'shì', ''])
        self.assertEqual(len(lines[1]['units'][0]), 3)

    def test_korean_romanization_follows_pronunciation(self):
        from ktvibes.lyrics import add_korean_romanization, romanize
        self.assertEqual(romanize('실라면'), ['sil', 'la', 'myeon'])
        units = [['눈', 0, 1], ['물', 1, 2], ['이 ', 2, 3], ['좋', 3, 4], ['은', 4, 5]]
        add_korean_romanization([{'t': 0, 'text': '눈물이 좋은', 'units': units}])
        self.assertEqual([u[3] for u in units], ['nun', 'mu', 'ri', 'jo', 'eun'])

    def test_aligner_letters_cover_all_scripts(self):
        from ktvibes.align import spoken_letters
        self.assertEqual(spoken_letters(['我', '好 ', '좋', '은 ', "Don't ", 'café', ', ']),
                         ['wo', 'hao', 'jot', 'eun', "don't", 'cafe', ''])

    def test_enhanced_lrc_voice_tag_and_end_stamp(self):
        line = parse_lrc('[00:06.00]v2: <00:06.00>a<00:06.40>pateu <00:07.00>')[0]
        self.assertEqual((line['text'], line['voice']), ('apateu', 'v2'))
        self.assertEqual(line['units'], [['a', 6.0, 6.4], ['pateu ', 6.4, 7.0]])

class GuideTests(unittest.TestCase):
    def test_english_to_hangul(self):
        from ktvibes.guides import english_hangul
        self.assertEqual([english_hangul(w) for w in ['someone', 'kiss', 'lips', 'crazy', 'queen', 'sweet', "Don't,", 'tryna']],
                         ['섬원', '키스', '립스', '크레이지', '퀸', '스위트', '돈트', ''])

    def test_chinese_to_hangul_standard_table(self):
        from ktvibes.guides import pinyin_hangul
        self.assertEqual([pinyin_hangul(s) for s in ['qing', 'tian', 'zhou', 'jie', 'lun', 'zhi', 'si', 'yi', 'xue', 'lv']],
                         ['칭', '톈', '저우', '제', '룬', '즈', '쓰', '이', '쉐', '뤼'])

    def test_add_hangul_keeps_latin_slot(self):
        from ktvibes.guides import add_hangul
        units = [['晴', 0, 1, 'qíng'], ['天 ', 1, 2, 'tiān'], ['kiss', 2, 3]]
        add_hangul([{'t': 0, 'text': '晴天 kiss', 'units': units}])
        self.assertEqual([u[3:] for u in units], [['qíng', '칭'], ['tiān', '톈'], ['', '키스']])


class SongbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def song(self, video_id, mtime, **meta):
        folder = self.cache / video_id
        folder.mkdir()
        path = folder / 'meta.json'
        path.write_text(json.dumps({'artist': 'IU', 'title': video_id, **meta}), encoding='utf-8')
        os.utime(path, (mtime, mtime))
        return path

    def test_numbers_start_at_10001_and_never_repeat(self):
        from ktvibes import songbook
        meta = {}
        self.assertEqual(songbook.assign(self.cache, meta), 10001)
        self.song('aaaaaaaaaaa', 1, number=10001)
        self.song('bbbbbbbbbbb', 2, number=10007)
        meta = {}
        self.assertEqual(songbook.assign(self.cache, meta), 10008)
        kept = {'number': 10003}
        self.assertEqual(songbook.assign(self.cache, kept), 10003)  # a number is for life
        self.assertEqual(songbook.numbers(self.cache), {10001: 'aaaaaaaaaaa', 10007: 'bbbbbbbbbbb'})
        self.assertEqual(songbook.find(self.cache, 10007)[0], 'bbbbbbbbbbb')
        self.assertIsNone(songbook.find(self.cache, 10002))

    def test_backfill_numbers_prepared_songs_oldest_first_and_keeps_timestamps(self):
        from ktvibes import songbook
        newer = self.song('bbbbbbbbbbb', 200)
        older = self.song('aaaaaaaaaaa', 100)
        self.song('ccccccccccc', 300)  # not prepared: no stems
        self.song('ddddddddddd', 50, number=10001)
        songbook.backfill(self.cache, lambda folder: folder.name != 'ccccccccccc')
        self.assertEqual(json.loads(older.read_text(encoding='utf-8'))['number'], 10002)
        self.assertEqual(json.loads(newer.read_text(encoding='utf-8'))['number'], 10003)
        self.assertNotIn('number', json.loads((self.cache / 'ccccccccccc' / 'meta.json').read_text(encoding='utf-8')))
        self.assertEqual(older.stat().st_mtime, 100)  # "prepared" times feed the Recent list

class MusicEndTest(unittest.TestCase):
    def stem(self, folder, seconds_of_tone, seconds_of_silence, name="no_vocals"):
        import numpy as np, soundfile as sf
        rate = 48000
        t = np.arange(int(rate * seconds_of_tone)) / rate
        tone = 0.3 * np.sin(2 * np.pi * 440 * t)
        data = np.concatenate([tone, np.zeros(int(rate * seconds_of_silence))])
        path = Path(folder) / f"{name}.opus"
        sf.write(path, np.stack([data, data], axis=1), rate, format="OGG", subtype="OPUS")
        return path

    def test_music_end_is_where_the_trailing_silence_starts(self):
        from ktvibes import stems
        with tempfile.TemporaryDirectory() as folder:
            end = stems.music_end(self.stem(folder, 5, 4))
        self.assertAlmostEqual(end, 5, delta=0.3)

    def test_music_end_is_the_length_when_the_track_plays_to_the_end(self):
        from ktvibes import stems
        with tempfile.TemporaryDirectory() as folder:
            end = stems.music_end(self.stem(folder, 6, 0))
        self.assertAlmostEqual(end, 6, delta=0.3)

    def test_song_end_waits_for_a_last_line_sung_after_the_music_stops(self):
        from ktvibes import stems
        with tempfile.TemporaryDirectory() as folder:
            self.stem(folder, 5, 4)
            self.stem(folder, 7, 2, name="vocals")
            end = stems.song_end(Path(folder))
        self.assertAlmostEqual(end, 7, delta=0.3)

    def test_song_end_is_none_when_a_stem_cannot_be_read(self):
        from ktvibes import stems
        with tempfile.TemporaryDirectory() as folder:
            self.stem(folder, 5, 4)
            (Path(folder) / "vocals.opus").write_bytes(b"not audio")
            self.assertIsNone(stems.song_end(Path(folder)))

class SeparationTests(unittest.TestCase):
    # karaoke-server: Mel-Band RoFormer separates; HTDemucs only when it can't be loaded.
    def setUp(self):
        from ktvibes import separate
        self.separate = separate
        patcher = unittest.mock.patch.multiple(separate, _roformer=None, _roformer_failed=False, _htdemucs=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chunks_cover_the_song_and_the_last_ends_at_its_end(self):
        starts = self.separate._chunk_starts
        self.assertEqual(starts(25, 10, 10), [0, 10, 15])
        self.assertEqual(starts(20, 10, 10), [0, 10])
        self.assertEqual(starts(7, 10, 10), [0])
        self.assertEqual(starts(25, 10, 5), [0, 5, 10, 15])

    def test_roformer_is_used_when_it_loads(self):
        model = object()
        with unittest.mock.patch.object(self.separate, "_load_roformer", return_value=model):
            self.assertEqual(self.separate._model(), (self.separate.ROFORMER, model))

    def test_htdemucs_is_the_fallback_and_roformer_is_not_retried_every_song(self):
        fallback = object()
        with unittest.mock.patch.object(self.separate, "_load_roformer", side_effect=RuntimeError("no checkpoint")) as load, \
             unittest.mock.patch("demucs.pretrained.get_model") as get_model, self.assertLogs("ktvibes.separate", "ERROR"):
            get_model.return_value.to.return_value.eval.return_value = fallback
            self.assertEqual(self.separate._model(), ("htdemucs", fallback))
            self.assertEqual(self.separate._model(), ("htdemucs", fallback))
        self.assertEqual(load.call_count, 1)

    def test_a_download_with_the_wrong_checksum_is_thrown_away(self):
        import io
        with tempfile.TemporaryDirectory() as folder, \
             unittest.mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"not the model")):
            target = Path(folder) / "model.ckpt"
            with self.assertRaises(RuntimeError):
                self.separate.download(target)
            self.assertEqual(list(Path(folder).iterdir()), [])
