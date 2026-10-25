"""Flattening transparent images onto a palette colour: pixels, hash, thumbnail, backup and tags, once."""
import io

from PIL import Image

import app.db as db
from app.services import transparency
from app.services.qa import _sha256
from app.services.tags import TagService
from tests.test_folder_guard import PlannerFolderCase


class TransparencyTests(PlannerFolderCase):
    def test_detection_and_palette(self):
        rgba = Image.new('RGBA', (8, 8), (10, 20, 30, 255))
        self.assertFalse(transparency.has_transparency(rgba))
        rgba.putpixel((0, 0), (255, 0, 255, 0))
        self.assertTrue(transparency.has_transparency(rgba))
        self.assertFalse(transparency.has_transparency(Image.new('RGB', (8, 8))))
        palette_image = Image.new('P', (4, 4), 0)
        palette_image.info['transparency'] = 0
        self.assertTrue(transparency.has_transparency(palette_image))
        self.assertEqual(transparency.background_for('0' * 64), transparency.PALETTE[0])
        self.assertEqual(len({name for name, _ in transparency.PALETTE}), 12)
        flat = transparency.flatten(rgba, (1, 2, 3))
        self.assertEqual((flat.mode, flat.getpixel((0, 0)), flat.getpixel((5, 5))), ('RGB', (1, 2, 3), (10, 20, 30)))

    async def test_flatten_folder(self):
        main = db.get_connection()   # a small reserve, so a nearly full test disk still admits the few test images
        main.execute("INSERT OR REPLACE INTO app_setting (key, value, updated_at) VALUES ('storage_reserve_gib', '0.5', '2026-01-01')")
        main.commit()
        main.close()
        await self.deliver()
        folder_id, ids = self.folder_images()
        target, other = ids[0], ids[1]
        main = db.get_connection()
        try:
            path, width, height, thumb = main.execute('SELECT path, width, height, thumb_path FROM image WHERE id=?', (target,)).fetchone()
            other_sha = main.execute('SELECT sha256 FROM image WHERE id=?', (other,)).fetchone()[0]
        finally:
            main.close()
        # A sprite: opaque square in the middle, transparent elsewhere with magenta garbage under the alpha.
        sprite = Image.new('RGBA', (width, height), (255, 0, 255, 0))
        sprite.paste((20, 120, 40, 255), (width // 4, height // 4, width * 3 // 4, height * 3 // 4))
        file = self.root / 'images' / path
        buffer = io.BytesIO()
        sprite.save(buffer, 'PNG')
        file.write_bytes(buffer.getvalue())
        original_sha = _sha256(file)
        main = db.get_connection()
        main.execute("UPDATE image SET sha256=?, format='png' WHERE id=?", (original_sha, target))
        main.commit()
        main.close()
        TagService(self.root).bulk_edit(folder_id, [target], ['transparent background'], 'add')

        stats = transparency.apply(folder_id=folder_id)
        name, colour = transparency.background_for(original_sha)
        self.assertEqual((stats['flattened'], stats['failed'], stats['colours']), (1, 0, {name: 1}))
        with Image.open(file) as result:
            self.assertFalse(transparency.has_transparency(result))
            rgb = result.convert('RGB')
            self.assertEqual(rgb.getpixel((0, 0)), colour)                       # garbage replaced by the palette colour
            self.assertEqual(rgb.getpixel((width // 2, height // 2)), (20, 120, 40))   # the art untouched
        main = db.get_connection()
        try:
            sha, new_thumb = main.execute('SELECT sha256, thumb_path FROM image WHERE id=?', (target,)).fetchone()
            self.assertEqual(main.execute('SELECT sha256 FROM image WHERE id=?', (other,)).fetchone()[0], other_sha)
        finally:
            main.close()
        self.assertEqual(sha, _sha256(file))
        self.assertNotEqual(new_thumb, thumb)
        self.assertTrue((self.root / 'thumbnails' / new_thumb).is_file())
        self.assertEqual(_sha256(self.root / '.trash' / 'transparent-originals' / path), original_sha)
        tags = TagService(self.root).get_ground_truth(target)['tags']
        self.assertNotIn('transparent background', tags)
        self.assertIn('simple background', tags)
        self.assertIn(f'{name} background', tags)
        self.assertIn(f'{name} background', file.with_suffix('.txt').read_text(encoding='utf-8'))

        again = transparency.apply(folder_id=folder_id)   # nothing transparent is left
        self.assertEqual((again['flattened'], again['checked']), (0, stats['checked']))
