"""MOSS annotation files (<image>_moss.json, _layout.json, _ocr.json) travel with their image."""
import json

import app.db as db
from app.services.captions import ANNOTATION_SUFFIXES, read_annotations
from app.services.collections import CollectionService
from app.services.images import ImageService
from tests.test_folder_guard import PlannerFolderCase


class AnnotationTests(PlannerFolderCase):
    def image_path(self, image_id):
        main = db.get_connection()
        try:
            return main.execute('SELECT path FROM image WHERE id=?', (image_id,)).fetchone()[0]
        finally:
            main.close()

    def write_annotations(self, image_id):
        relative = self.image_path(image_id)
        base = self.root / 'images' / relative
        files = {}
        for name, suffix in ANNOTATION_SUFFIXES.items():
            path = base.with_name(base.stem + suffix)
            path.write_text(json.dumps({'kind': name}), encoding='utf-8')
            files[name] = path
        return files

    async def test_read_remove_restore_delete(self):
        await self.deliver()
        folder_id, ids = self.folder_images()
        files = self.write_annotations(ids[0])
        self.assertEqual({k: v['kind'] for k, v in read_annotations(ids[0]).items()},
                         {'moss': 'moss', 'layout': 'layout', 'ocr': 'ocr'})
        self.assertEqual(read_annotations(ids[1]), {'moss': None, 'layout': None, 'ocr': None})

        CollectionService().remove_images(folder_id, [ids[0]], self.root, 'cc' * 16)
        self.assertFalse(any(path.exists() for path in files.values()), 'annotations go to recovery with the image')
        CollectionService().restore_images(folder_id, 'cc' * 16, self.root)
        self.assertEqual(read_annotations(ids[0])['layout'], {'kind': 'layout'})

        files = self.write_annotations(ids[1])
        ImageService(self.root).delete_image(ids[1])
        self.assertFalse(any(path.exists() for path in files.values()), 'deleting an image deletes its annotations')

    async def test_broken_file_is_reported_not_raised(self):
        await self.deliver()
        _, ids = self.folder_images()
        files = self.write_annotations(ids[0])
        files['layout'].write_text('{not json', encoding='utf-8')
        self.assertIn('error', read_annotations(ids[0])['layout'])
