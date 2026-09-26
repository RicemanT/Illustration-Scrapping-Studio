import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
import app.db as db
from app.services.filters import LocalFilter, LocalQuery, list_images, explore_tags
from app.services.tags import TagService
from app.services import filter_review


class LocalFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = db.DB_PATH
        db.DB_PATH = self.root / 'index.db'
        db.init_db()
        conn = db.get_connection()
        for folder_id in (1,2):
            conn.execute("INSERT INTO collection(id,name,slug,type,query,artist_tag_template,caption_template,filters,created_at,updated_at) VALUES (?,?,?,'artist','name','Drawn by {artist}','{}','{}','now','now')", (folder_id,f'Artist {folder_id}',f'artist-{folder_id}'))
        for image_id, folder_id, width in ((1,1,512),(2,1,1024),(3,1,2000),(4,2,512)):
            conn.execute("INSERT INTO image(id,sha256,width,height,format,file_size,path,added_at,folder_id) VALUES (?,?,?,512,'jpg',1,?,'same',?)", (image_id,str(image_id),width,f'artist-{folder_id}/{image_id}.jpg',folder_id))
            conn.execute("INSERT INTO collection_image(collection_id,image_id,added_at) VALUES (?,?,'same')",(folder_id,image_id))
        conn.execute("UPDATE image SET favorite=1,review_status='accepted' WHERE id=2")
        sources = [(1,'e621','a',1,'safe',[('species','red_fox'),('general','blue_hair')]),
                   (1,'e621','a',2,'explicit',[('species','red_fox'),('general','solo')]),
                   (1,'pixiv','b',1,None,[('general','untrusted_tag')]),
                   (2,'danbooru','c',1,'safe',[('general','solo'),('general','blue_hair')]),
                   (4,'danbooru','d',1,'safe',[('general','solo')])]
        for image_id,provider,remote,version,rating,tags in sources:
            sid = conn.execute("INSERT INTO image_source(image_id,provider,remote_id,version,fetched_at,metadata) VALUES (?,?,?,?,'now',?)",(image_id,provider,remote,version,json.dumps({'rating':rating}))).lastrowid
            conn.executemany('INSERT INTO image_tag(image_id,source_id,category,tag) VALUES (?,?,?,?)',[(image_id,sid,c,t) for c,t in tags])
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.previous
        self.temp.cleanup()

    def ids(self, **filters):
        return [i['id'] for i in list_images(1,LocalQuery(filters=LocalFilter(**filters)))['items']]

    def test_and_not_normalization_basis_and_folder_isolation(self):
        self.assertEqual(self.ids(required_tags=['blue_hair','solo'],excluded_tags=['red fox']),[2])
        self.assertEqual(self.ids(required_tags=['Drawn by artist 1']),[3,2,1])
        self.assertEqual(self.ids(required_tags=['untrusted tag']),[])
        self.assertEqual(self.ids(required_tags=['untrusted tag'],tag_basis='provenance'),[1])
        self.assertEqual(self.ids(required_tags=['blue hair'],tag_basis='provenance'),[2])
        self.assertEqual(self.ids(required_tags=['blue hair'],excluded_tags=['blue hair']),[])

    def test_latest_rating_provider_unknown_and_stored_dimensions(self):
        self.assertEqual(self.ids(rating='safe'),[2])
        self.assertEqual(self.ids(rating='explicit',provider='e621'),[1])
        self.assertEqual(self.ids(rating='safe',provider='pixiv'),[])
        self.assertEqual(self.ids(rating='unknown'),[3,1])
        self.assertEqual(self.ids(rating='unknown',provider='pixiv'),[1])
        self.assertEqual(self.ids(min_width=1000,max_width=1500,favorite=True,review_status='accepted'),[2])

    def test_distinct_counts_policies_edits_undo_and_rename(self):
        tags = explore_tags(1,LocalQuery())['items']
        self.assertEqual(next(t for t in tags if t['tag']=='red fox')['count'],1)
        self.assertEqual(next(t for t in tags if t['tag']=='red fox')['category'],'species')
        self.assertEqual(next(t for t in tags if t['tag']=='Drawn by artist 1')['count'],3)
        service = TagService(self.root)
        edit = service.bulk_edit(1,[1],['solo'],'remove')
        self.assertEqual(self.ids(required_tags=['solo']),[2])
        service.undo(edit['undo_token'])
        self.assertEqual(self.ids(required_tags=['solo']),[2,1])
        service.set_folder_category_policy(1,['artist','general'])
        self.assertEqual(self.ids(required_tags=['red fox']),[])
        self.assertEqual(self.ids(required_tags=['red fox'],tag_basis='provenance'),[1])
        conn = db.get_connection()
        conn.execute("UPDATE collection SET name='New Name' WHERE id=1")
        conn.commit(); conn.close()
        self.assertEqual(self.ids(required_tags=['Drawn by new name']),[3,2,1])

    def test_pagination_ties_validation_empty_ids_and_read_only(self):
        conn = db.get_connection(); before = conn.total_changes
        pages = [list_images(1,LocalQuery(limit=1,offset=i)) for i in range(3)]
        self.assertEqual([p['items'][0]['id'] for p in pages],[3,2,1])
        self.assertEqual([p['total'] for p in pages],[3,3,3])
        self.assertIsNone(pages[-1]['next_cursor'])
        self.assertEqual(self.ids(image_ids=[]),[])
        self.assertEqual(conn.total_changes,before); conn.close()
        for payload in ({'limit':201},{'offset':-1},{'filters':{'min_width':900,'max_width':512}},{'filters':{'rating':'invented'}}):
            with self.assertRaises(ValidationError): LocalQuery.model_validate(payload)
        db.init_db()  # indexes/migrations are idempotent

    def test_preview_no_mutations_and_confirm_only_frozen_valid_ids_with_undo(self):
        conn = db.get_connection(); before = '\n'.join(conn.iterdump()); conn.close()
        result = filter_review.preview(1,LocalQuery(limit=1))
        self.assertEqual((result['matched'],result['unmatched']),(3,0))
        conn = db.get_connection(); after = '\n'.join(conn.iterdump()); conn.close()
        self.assertEqual(before,after)
        with self.assertRaises(ValueError): filter_review.apply(1,result['preview_token'],[2],'rejected')
        with self.assertRaises(ValueError): filter_review.apply(2,result['preview_token'],[3],'rejected')
        changed = filter_review.apply(1,result['preview_token'],[3],'archived')
        self.assertEqual(self.ids(review_status='archived'),[3])
        filter_review.undo(1,changed['undo_token'])
        self.assertEqual(self.ids(review_status='archived'),[])
        result = filter_review.preview(1,LocalQuery(filters=LocalFilter(favorite=True)))
        conn = db.get_connection(); conn.execute('UPDATE image SET favorite=0 WHERE id=2'); conn.commit(); conn.close()
        with self.assertRaises(ValueError): filter_review.apply(1,result['preview_token'],[2],'rejected')

    def test_effective_sql_membership_matches_tag_service(self):
        service = TagService(self.root)
        service.bulk_replace(1,[1,2],'blue hair','custom replacement')
        for policy in (['artist','general'],[],['artist','character','copyright','species','general','meta']):
            service.set_folder_category_policy(1,policy)
            conn = db.get_connection()
            for image_id in (1,2,3):
                expected = {t.casefold() for t in service._effective_tags(conn,image_id)}
                actual = {t['tag'].casefold() for t in explore_tags(1,LocalQuery(filters=LocalFilter(image_ids=[image_id])))['items']}
                self.assertEqual(expected,actual)
            conn.close()


if __name__ == '__main__':
    unittest.main()
