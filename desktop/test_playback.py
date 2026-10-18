import wave
from PySide6.QtCore import Qt,QUrl,QPoint
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from test_ui import app,window
from test_background_import import wait_until


def prepare(window):
    p=window.project
    for i,(_,s) in enumerate(p.rows(p.data['root'])):
        path=p.path(f'audio/seek-{i}.wav');path.parent.mkdir(exist_ok=True)
        with wave.open(str(path),'wb') as f:
            f.setparams((1,2,24000,0,'NONE','not compressed'));f.writeframes(b'\0\0'*72000)
        s.update(audio=f'audio/seek-{i}.wav',duration=3,status='review')
    p.data['settings']['pause_ms']=200;p.save();window.refresh()
    return [s for _,s in window.rows]


class Player:
    def __init__(self):
        self.url=QUrl();self.pos=0;self.status=QMediaPlayer.LoadedMedia;self.state=QMediaPlayer.StoppedState
    def source(self):return self.url
    def setSource(self,url):self.url=url;self.pos=0
    def setPosition(self,p):self.pos=p
    def position(self):return self.pos
    def play(self):self.state=QMediaPlayer.PlayingState
    def pause(self):self.state=QMediaPlayer.PausedState
    def stop(self):self.state=QMediaPlayer.StoppedState
    def mediaStatus(self):return self.status
    def playbackState(self):return self.state


def test_whole_section_seek_forward_backward_and_gap(window):
    rows=prepare(window);window.player=Player();window.play_all()
    assert window.play_total==9400 and window.play_offsets==[0,3200,6400]
    window.seek_audio(round(window.seek.maximum()*500/1000))
    assert window.play_index==1 and window.player.pos==1500
    window.seek_audio(round(window.seek.maximum()*100/1000))
    assert window.play_index==0 and window.player.pos==940
    window.seek_audio(round(window.seek.maximum()*330/1000))
    assert window.play_index==1 and window.player.pos==0
    window.seek_audio(round(window.seek.maximum()*1000/1000))
    assert window.play_index==2 and window.player.pos==2999
    assert window.time_label.text().endswith('/ 00:09')


def test_timeline_click_moves_between_files_preserving_pause_and_modifiers(window):
    rows=prepare(window);window.player=Player();window.play_all();window.pause_audio()
    window.timeline_click(rows[2]['id'],Qt.NoModifier,.5)
    assert window.play_index==2 and window.player.pos==1500
    assert window.player.state==QMediaPlayer.PausedState
    window.timeline_click(rows[0]['id'],Qt.ControlModifier,.8)
    assert window.play_index==2
    window.timeline_click(rows[0]['id'],Qt.NoModifier,.25)
    assert window.play_index==0 and window.player.pos==750
    window.pause_audio();assert window.player.state==QMediaPlayer.PlayingState


def test_seek_cancels_old_end_timer_and_keeps_latest_pending_position(window,app):
    prepare(window);window.player=Player();window.play_all()
    window.audio_status(QMediaPlayer.EndOfMedia)
    assert window.next_audio_timer.isActive()
    window.player.status=QMediaPlayer.LoadingMedia
    window.seek_audio(round(window.seek.maximum()*800/1000));window.seek_audio(round(window.seek.maximum()*900/1000))
    assert not window.next_audio_timer.isActive()
    assert window.pending_seek==2060
    window.pause_audio();window.pause_audio()
    assert window.pending_seek==2060
    window.player.status=QMediaPlayer.LoadedMedia
    window.audio_status(QMediaPlayer.LoadedMedia)
    assert window.player.pos==2060 and window.pending_seek is None
    QTest.qWait(250);assert window.play_index==2


def test_actual_qt_player_seeks_to_another_file_while_paused(window,app):
    rows=prepare(window);window.play_all()
    wait_until(app,lambda:window.player.playbackState()==QMediaPlayer.PlayingState,10)
    window.pause_audio();window.seek_audio(round(window.seek.maximum()*500/1000))
    wait_until(app,lambda:window.pending_seek is None and window.player.position()>=1450,10)
    assert window.play_index==1 and window.player.playbackState()==QMediaPlayer.PausedState
    assert window.player.source().toLocalFile().endswith('seek-1.wav')
    window.timeline_click(rows[0]['id'],Qt.NoModifier,.5)
    wait_until(app,lambda:window.pending_seek is None and window.player.position()>=1450,10)
    assert window.play_index==0 and window.player.playbackState()==QMediaPlayer.PausedState
    assert window.player.error()==QMediaPlayer.NoError


def test_drag_playhead_across_fragments(window,app):
    prepare(window);window.player=Player();window.play_all();app.processEvents()
    rects=window.timeline.rects
    first=QPoint(int(rects[0][0]+rects[0][1]*.3),60)
    last=QPoint(int(rects[2][0]+rects[2][1]*.5),60)
    QTest.mousePress(window.timeline,Qt.LeftButton,Qt.NoModifier,first)
    QTest.mouseMove(window.timeline,last)
    QTest.mouseRelease(window.timeline,Qt.LeftButton,Qt.NoModifier,last)
    assert window.play_index==2 and abs(window.player.pos-1500)<50


def test_seek_reveals_fragment_on_another_page(window):
    rows=prepare(window)
    from project import segment
    window.project.node(window.current_node)['segments']=[rows[0]]+[segment('Ожидает') for _ in range(300)]+[rows[1]]
    window.refresh();window.player=Player();window.play_all()
    window.seek_audio(round(window.seek.maximum()*.8))
    assert window.page==1 and window.timeline.playing==rows[1]['id']
    assert window.timeline.time_offset==3.2
