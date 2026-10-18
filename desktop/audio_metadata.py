from pathlib import Path

SUPPORTED = {'.mp3','.flac','.m4a','.m4b','.wav'}


def embed_metadata(path, title='', text='', cover=None):
    """Embed lyrics/transcript and optional JPEG/PNG artwork; never a sidecar."""
    path=Path(path);suffix=path.suffix.lower()
    if suffix not in SUPPORTED:return
    picture=Path(cover).read_bytes() if cover else None
    mime='image/png' if picture and picture.startswith(b'\x89PNG') else 'image/jpeg'
    if suffix in ('.mp3','.wav'):
        from mutagen.id3 import ID3,ID3NoHeaderError,TIT2,USLT,APIC
        if suffix=='.wav':
            # Mutagen's WAVE parser does not support RF64; keep RF64 audio intact.
            with path.open('rb') as f:
                if f.read(4)==b'RF64':return
            from mutagen.wave import WAVE
            audio=WAVE(path)
            if audio.tags is None:audio.add_tags()
            tags=audio.tags
        else:
            try:tags=ID3(path)
            except ID3NoHeaderError:tags=ID3()
        if title:tags.add(TIT2(encoding=3,text=title))
        if text:tags.add(USLT(encoding=3,lang='und',desc='Transcript',text=text))
        if picture:tags.add(APIC(encoding=3,mime=mime,type=3,desc='Cover',data=picture))
        if suffix=='.wav':audio.save()
        else:tags.save(path,v2_version=3)
    elif suffix=='.flac':
        from mutagen.flac import FLAC,Picture
        audio=FLAC(path)
        if title:audio['title']=title
        if text:audio['lyrics']=text;audio['description']=text
        if picture:
            image=Picture();image.type=3;image.mime=mime;image.data=picture
            audio.clear_pictures();audio.add_picture(image)
        audio.save()
    else:
        from mutagen.mp4 import MP4,MP4Cover
        audio=MP4(path)
        if title:audio['\xa9nam']=[title]
        if text:audio['\xa9lyr']=[text]
        if picture:audio['covr']=[MP4Cover(picture,imageformat=MP4Cover.FORMAT_PNG if mime=='image/png' else MP4Cover.FORMAT_JPEG)]
        audio.save()
