import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import app.db as db
from app.services.screen_images import nearest_size, screen_file


class ScreenImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = (db.DB_PATH, db.LIBRARY_PATH)
        db.DB_PATH, db.LIBRARY_PATH = self.root / 'index.db', self.root
        db.init_db()

    def tearDown(self):
        db.DB_PATH, db.LIBRARY_PATH = self.old
        self.temp.cleanup()

    def add(self, name, image, fmt):
        path = self.root / 'images' / 'folder' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format=fmt)
        conn = db.get_connection()
        image_id = conn.execute("""INSERT INTO image (sha256, width, height, format, file_size, path, thumb_path, added_at)
                                   VALUES (?, ?, ?, ?, ?, ?, NULL, 'x')""",
                                (name, image.width, image.height, fmt.lower(), path.stat().st_size, f'folder/{name}')).lastrowid
        conn.commit()
        conn.close()
        return image_id, path

    def test_large_images_become_cached_screen_jpegs_and_follow_edits(self):
        image_id, original = self.add('big.png', Image.new('RGBA', (3000, 2000), (0, 0, 0, 0)), 'PNG')
        path, media = screen_file(image_id, 1080)
        self.assertEqual(media, 'image/jpeg')
        with Image.open(path) as screen:
            self.assertEqual(screen.size, (1080, 720))
            self.assertEqual(screen.getpixel((0, 0)), (255, 255, 255))  # transparency on white
        self.assertEqual(screen_file(image_id, 1080)[0], path)  # cached
        Image.new('RGB', (3000, 2000), 'red').save(original, format='PNG')
        os.utime(original, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns + 10_000_000))
        newer, _ = screen_file(image_id, 1080)
        self.assertNotEqual(newer, path)
        self.assertFalse(path.exists())  # the stale copy is replaced

    def test_gifs_are_sent_as_they_are_and_unknown_images_are_none(self):
        image_id, original = self.add('anim.gif', Image.new('P', (40, 40)), 'GIF')
        self.assertEqual(screen_file(image_id), (original.resolve(), 'image/gif'))
        self.assertIsNone(screen_file(999))
        self.assertEqual((nearest_size(1000), nearest_size(5000), nearest_size(100)), (1080, 2048, 720))


if __name__ == '__main__':
    unittest.main()
