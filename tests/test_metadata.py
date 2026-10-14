import tempfile
import unittest
import wave
import base64
from pathlib import Path

from mutagen.wave import WAVE

from nova_music.audio.metadata import read_metadata, write_metadata


class AudioMetadataTest(unittest.TestCase):
    def test_edits_wav_id3_metadata_and_clears_empty_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\0\0" * 800)

            values = {"title": "New title", "artist": "Singer", "album": "Record",
                      "genre": "Jazz", "year": 2024, "track_number": 3}
            cover = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jvuoAAAAASUVORK5CYII=")
            write_metadata(str(path), values, cover)
            self.assertEqual(read_metadata(str(path)), {"title": "New title", "artist": "Singer",
                "album": "Record", "genre": "Jazz", "year": "2024", "track_number": "3"})
            self.assertEqual(WAVE(str(path)).tags.getall("APIC")[0].data, cover)

            write_metadata(str(path), {key: None for key in values}, remove_cover=True)
            self.assertEqual(read_metadata(str(path)), {"title": "track", "artist": "", "album": "",
                "genre": "", "year": "", "track_number": ""})
            self.assertIsNotNone(WAVE(str(path)).info)
            self.assertEqual(WAVE(str(path)).tags.getall("APIC"), [])


if __name__ == "__main__":
    unittest.main()
