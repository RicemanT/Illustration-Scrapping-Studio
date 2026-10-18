"""The viewer's imported source tags: each tag once, from the latest metadata version of each source post."""
import app.db as db
from app.services.images import ImageService
from tests.test_folder_guard import PlannerFolderCase


class SourceTagVersionTests(PlannerFolderCase):
    async def test_refetched_versions_do_not_duplicate_tags(self):
        await self.deliver()
        _, ids = self.folder_images()
        image_id = ids[0]
        conn = db.get_connection()
        try:
            conn.execute('DELETE FROM image_tag WHERE image_id=?', (image_id,))
            conn.execute('DELETE FROM image_source WHERE image_id=?', (image_id,))

            def source(provider, remote_id, version, primary, tags):
                cursor = conn.execute(
                    """INSERT INTO image_source (image_id, provider, remote_id, remote_url, source_url, fetched_at,
                       metadata, version, metadata_version, is_primary) VALUES (?, ?, ?, '', '', '2026-01-01', ?, ?, ?, ?)""",
                    (image_id, provider, remote_id, '{"v": %d}' % version, version, version, primary))
                conn.executemany('INSERT INTO image_tag (image_id, source_id, category, tag, score) VALUES (?, ?, ?, ?, NULL)',
                                 [(image_id, cursor.lastrowid, category, tag) for category, tag in tags])

            # e621 post fetched twice (its score changed): version 2 dropped "old_tag" and added "felid".
            source('e621', '1', 1, 1, [('artist', 'notmypornaccount'), ('general', 'old_tag'), ('species', 'domestic_cat')])
            source('e621', '1', 2, 1, [('artist', 'notmypornaccount'), ('species', 'domestic_cat'), ('species', 'felid')])
            # The same artwork on another site, with the same artist under a spacing variant.
            source('gelbooru', '9', 1, 0, [('artist', 'notmypornaccount'), ('character', 'mavis (notmypornaccount)'),
                                           ('general', 'smile')])
            conn.commit()
        finally:
            conn.close()

        tags = ImageService(self.root).get_image_by_id(image_id)['tags']
        self.assertEqual(tags['artist'], ['notmypornaccount'])
        self.assertEqual(tags['species'], ['domestic_cat', 'felid'])
        self.assertEqual(tags['character'], ['mavis (notmypornaccount)'])
        self.assertEqual(tags['general'], ['smile'], 'tags of a superseded version are not shown')
