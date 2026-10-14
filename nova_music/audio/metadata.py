"""Read and write common audio tags and embedded artwork."""
from __future__ import annotations

import base64
import mimetypes
import struct
from pathlib import Path

from mutagen import File as MutagenFile


def _set_id3(audio, values, cover, remove_cover=False, path=None):
    from mutagen.id3 import (APIC, TALB, TCON, TIT2, TPE1, TRCK, TDRC, TYER)
    from mutagen.id3 import ID3

    tags = audio if isinstance(audio, ID3) else audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    mapping = {"title": TIT2, "artist": TPE1, "album": TALB, "genre": TCON,
               "year": TDRC, "track_number": TRCK}
    tags.delall("TYER")
    for key, frame in mapping.items():
        tags.delall(frame.__name__)
        value = values.get(key)
        if value not in (None, ""):
            if key == "year":
                tags.add(TDRC(encoding=3, text=[str(value)]))
                tags.add(TYER(encoding=3, text=[str(value)]))
            else:
                tags.add(frame(encoding=3, text=[str(value)]))
    if cover is not None or remove_cover:
        tags.delall("APIC")
    if cover is not None:
        tags.add(APIC(encoding=3, mime=mimetypes.guess_type("cover" + _image_ext(cover))[0] or "image/jpeg",
                      type=3, desc="Cover", data=cover))
    audio.save(path) if path else audio.save()


def _image_ext(data):
    return ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"


def write_metadata(path: str, values: dict, cover: bytes | None = None, remove_cover=False) -> None:
    """Write editable fields to an audio file. Raises on unsupported/broken files."""
    from mutagen.id3 import ID3
    audio = MutagenFile(path)
    if audio is None and Path(path).suffix.lower() == ".aac":
        from mutagen.id3 import ID3
        audio = ID3()
        _set_id3(audio, values, cover, remove_cover, path)
        return
    if audio is None:
        # WAV can be valid audio without an ID3 chunk; Mutagen recognizes it separately.
        raise ValueError("قالب فایل یا برچسب‌های آن برای ویرایش پشتیبانی نمی‌شود.")

    if isinstance(audio, ID3) or audio.__class__.__name__ in {"MP3", "WAVE"}:
        _set_id3(audio, values, cover, remove_cover)
        return

    name = audio.__class__.__name__
    if name in {"FLAC", "OggVorbis", "OggOpus", "OggFLAC"}:
        if audio.tags is None:
            audio.add_tags()
        tagmap = {"title": "title", "artist": "artist", "album": "album", "genre": "genre",
                  "year": "date", "track_number": "tracknumber"}
        for key, tag in tagmap.items():
            if values.get(key) not in (None, ""):
                audio[tag] = [str(values[key])]
            else:
                audio.pop(tag, None)
        if name == "FLAC":
            from mutagen.flac import Picture
            if cover is not None or remove_cover: audio.clear_pictures()
            if cover is not None:
                pic = Picture(); pic.type = 3; pic.mime = mimetypes.guess_type("cover" + _image_ext(cover))[0] or "image/jpeg"; pic.data = cover
                audio.add_picture(pic)
        else:
            if cover is not None or remove_cover:
                audio.pop("metadata_block_picture", None); audio.pop("coverart", None)
            if cover is not None:
                from mutagen.flac import Picture
                pic = Picture(); pic.type = 3; pic.mime = mimetypes.guess_type("cover" + _image_ext(cover))[0] or "image/jpeg"; pic.data = cover
                audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save(); return

    if name == "ASF":
        from mutagen.asf import ASFUnicodeAttribute, ASFDWordAttribute, ASFByteArrayAttribute
        if audio.tags is None: audio.add_tags()
        tags = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "genre": "WM/Genre", "year": "WM/Year", "track_number": "WM/TrackNumber"}
        for key, tag in tags.items():
            value = values.get(key)
            if value in (None, ""):
                audio.pop(tag, None)
            elif key == "track_number":
                audio[tag] = [ASFDWordAttribute(int(value))]
            else:
                audio[tag] = [ASFUnicodeAttribute(str(value))]
        if cover is not None or remove_cover: audio.pop("WM/Picture", None)
        if cover is not None:
            mime = mimetypes.guess_type("cover" + _image_ext(cover))[0] or "image/jpeg"
            picture = struct.pack("<BI", 3, len(cover)) + (mime + "\0").encode("utf-16-le") + "Cover\0".encode("utf-16-le") + cover
            audio["WM/Picture"] = [ASFByteArrayAttribute(picture)]
        audio.save(); return

    if name == "MP4":
        from mutagen.mp4 import MP4Cover
        if audio.tags is None: audio.add_tags()
        keys = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "genre": "\xa9gen", "year": "\xa9day", "track_number": "trkn"}
        for key, tag in keys.items():
            value = values.get(key)
            if key == "track_number" and value not in (None, ""):
                audio[tag] = [(int(value), 0)]
            elif value not in (None, ""):
                audio[tag] = [str(value)]
            else:
                audio.pop(tag, None)
        if cover is not None:
            fmt = MP4Cover.FORMAT_PNG if _image_ext(cover) == ".png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
        elif remove_cover:
            audio.pop("covr", None)
        audio.save(); return

    # For formats with a mutable native tag mapping (ASF and other Mutagen formats).
    if audio.tags is None: audio.add_tags()
    tags = {"title": "title", "artist": "artist", "album": "album", "genre": "genre", "year": "date", "track_number": "tracknumber"}
    for key, tag in tags.items():
        value = values.get(key)
        if value not in (None, ""): audio[tag] = [str(value)]
        else: audio.pop(tag, None)
    audio.save()


def read_metadata(path: str) -> dict:
    """Return normalized editable values from a file."""
    defaults = {"title": Path(path).stem, "artist": "", "album": "", "genre": "", "year": "", "track_number": ""}
    # Easy tags do not expose the ID3 chunk in WAV consistently, so inspect raw frames too.
    raw = MutagenFile(path)
    id3 = getattr(raw, "tags", None)
    if id3 is not None:
        for key, frame in (("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB"),
                           ("genre", "TCON"), ("year", "TDRC"), ("track_number", "TRCK")):
            found = id3.get(frame)
            if found and getattr(found, "text", None):
                value = str(found.text[0])
                defaults[key] = value.split("/", 1)[0] if key == "track_number" else value[:4] if key == "year" else value
    audio = MutagenFile(path, easy=True)
    if audio:
        for key, candidates in {"title": ("title",), "artist": ("artist", "albumartist"), "album": ("album",), "genre": ("genre",), "year": ("date", "year"), "track_number": ("tracknumber",)}.items():
            for tag in candidates:
                if audio.get(tag):
                    defaults[key] = str(audio[tag][0]).split("/", 1)[0] if key == "track_number" else str(audio[tag][0])[:4] if key == "year" else str(audio[tag][0])
                    break
    return defaults
