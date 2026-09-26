import json
import threading
from unittest.mock import patch
from pathlib import Path

import unittest
import test_qa_export
from app.services import dataset_jobs as jobs
from app.services.filters import LocalFilter
from app.services.tags import TagService
import app.db as db
from app.services.collections import CollectionService
from concurrent.futures import ThreadPoolExecutor
from PIL import Image


class DatasetJobTests(unittest.TestCase):
    setUp = test_qa_export.DatasetQAExportTests.setUp
    tearDown = test_qa_export.DatasetQAExportTests.tearDown
    def test_scan_repair_preserves_original_tags_and_provenance(self):
        before = self.image_path.read_bytes()
        tags = TagService(self.root)
        tags.replace_ground_truth(1,['custom curated tag'])
        caption = self.image_path.with_suffix('.txt').read_bytes()
        conn = db.get_connection(); sources = [tuple(r) for r in conn.execute('SELECT * FROM image_source')]; conn.close()
        scan = jobs.create(1,LocalFilter())
        jobs._run(scan['job_id'],self.root,threading.Event())
        result = jobs.get_job(scan['job_id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['progress']['completed'],1)
        findings = jobs.issues(scan['job_id'])['items']
        self.assertIn('missing_thumbnail',[i['code'] for i in findings])
        repaired = jobs.repair_one(self.root,1,1,'thumbnail',threading.Event())
        self.assertEqual(repaired['status'],'repaired')
        self.assertEqual(before,self.image_path.read_bytes())
        self.assertEqual(caption,self.image_path.with_suffix('.txt').read_bytes())
        conn = db.get_connection(); self.assertEqual(sources,[tuple(r) for r in conn.execute('SELECT * FROM image_source')]); conn.close()
        self.assertNotIn('missing_thumbnail',self.service.validate_collection(1)['summary']['by_code'])

    def test_sidecar_repair_uses_curated_tags_and_failed_write_cleans_temp(self):
        TagService(self.root).replace_ground_truth(1,['custom tag'])
        sidecar = self.image_path.with_suffix('.txt')
        sidecar.unlink()
        with patch.object(Path,'replace',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError): jobs.repair_one(self.root,1,1,'sidecar',threading.Event())
        self.assertFalse(list(self.image_path.parent.glob('*.tmp')))
        self.assertFalse(sidecar.exists())
        jobs.repair_one(self.root,1,1,'sidecar',threading.Event())
        self.assertEqual(sidecar.read_text(),'custom tag')

    def test_cancellation_and_restart_resume_snapshot(self):
        scan = jobs.create(1,LocalFilter())
        stop = threading.Event(); stop.set()
        jobs._run(scan['job_id'],self.root,stop)
        self.assertEqual(jobs.get_job(scan['job_id'])['status'],'canceled')
        self.assertEqual(jobs.get_job(scan['job_id'])['progress']['completed'],0)
        job = jobs.get_job(scan['job_id']); job['status']='running'; jobs.save(job)
        jobs.recover()
        self.assertEqual(jobs.get_job(scan['job_id'])['status'],'interrupted')
        jobs._run(scan['job_id'],self.root,threading.Event())
        self.assertEqual(jobs.get_job(scan['job_id'])['progress']['completed'],1)

    def test_deleted_image_scan_and_repair_skip(self):
        scan = jobs.create(1,LocalFilter())
        conn = db.get_connection(); conn.execute('DELETE FROM image WHERE id=1'); conn.commit(); conn.close()
        jobs._run(scan['job_id'],self.root,threading.Event())
        self.assertEqual(jobs.get_job(scan['job_id'])['status'],'completed')
        self.assertEqual(jobs.issues(scan['job_id'])['total'],0)
        self.assertEqual(jobs.repair_one(self.root,1,1,'thumbnail',threading.Event())['status'],'skipped')

    def test_unsafe_paths_corrupt_bytes_and_hash_mismatch_refuse_repair(self):
        original = self.image_path.read_bytes()
        self.image_path.write_bytes(b'broken')
        with self.assertRaises(ValueError): jobs.repair_one(self.root,1,1,'thumbnail',threading.Event())
        self.image_path.write_bytes(original)
        conn = db.get_connection(); conn.execute("UPDATE image SET sha256='changed' WHERE id=1"); conn.commit(); conn.close()
        with self.assertRaises(ValueError): jobs.repair_one(self.root,1,1,'thumbnail',threading.Event())
        conn = db.get_connection(); conn.execute("UPDATE image SET path='../escaped.jpg' WHERE id=1"); conn.commit(); conn.close()
        result = self.service.validate_collection(1)
        self.assertIn('unsafe_path',result['summary']['by_code'])
        self.assertTrue(all(not issue['allowed_actions'] for issue in result['issues']))

    def test_disabled_categories_and_untrusted_empty_tags_are_normal(self):
        TagService(self.root).set_folder_category_policy(1,[])
        self.assertNotIn('empty_ground_truth_tags',self.service.validate_collection(1)['summary']['by_code'])
        conn = db.get_connection(); conn.execute("UPDATE image_source SET provider='pixiv'"); conn.execute('UPDATE collection SET ground_truth_categories=NULL'); conn.commit(); conn.close()
        self.assertNotIn('empty_ground_truth_tags',self.service.validate_collection(1)['summary']['by_code'])

    def test_cancel_repair_before_replace_leaves_files_unchanged(self):
        stop = threading.Event(); stop.set()
        sidecar = self.image_path.with_suffix('.txt'); sidecar.write_text('stale')
        with self.assertRaises(InterruptedError): jobs.repair_one(self.root,1,1,'sidecar',stop)
        self.assertEqual(sidecar.read_text(),'stale')
        self.assertFalse(list(sidecar.parent.glob('*.tmp')))

    def test_concurrent_delete_waits_for_repair_and_leaves_no_recreated_files(self):
        entered, release = threading.Event(), threading.Event()
        original = Image.Image.thumbnail
        def pause_thumbnail(image, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Fixture release timeout')
            return original(image,*args,**kwargs)
        with patch.object(Image.Image,'thumbnail',pause_thumbnail), ThreadPoolExecutor(max_workers=2) as pool:
            repair = pool.submit(jobs.repair_one,self.root,1,1,'thumbnail',threading.Event())
            try:
                self.assertTrue(entered.wait(5))
                removal = pool.submit(CollectionService().remove_images,1,[1],self.root,'abcd')
                self.assertFalse(removal.done())
            finally:
                release.set()
            self.assertEqual(repair.result(timeout=5)['status'],'repaired')
            self.assertEqual(len(removal.result(timeout=5)),1)
        self.assertFalse(self.image_path.exists())
        self.assertFalse(list((self.root/'thumbnails').rglob('*.jpg')))
        self.assertEqual(CollectionService().restore_images(1,'abcd',self.root),1)
        self.assertTrue(self.image_path.exists())

    def test_broken_thumbnail_and_duplicate_candidate_are_actionable_warnings(self):
        thumb = self.root/'thumbnails'/'training-set'/'fixture.jpg'
        thumb.parent.mkdir(parents=True); thumb.write_bytes(b'broken')
        conn = db.get_connection()
        conn.execute("UPDATE image SET thumb_path='training-set/fixture.jpg' WHERE id=1")
        conn.execute("INSERT INTO image(id,folder_id,sha256,width,height,format,file_size,path,added_at) VALUES (2,1,'other',512,512,'jpg',1,'training-set/other.jpg','now')")
        conn.execute("INSERT INTO duplicate_candidate(image_id_a,image_id_b,visual_similarity,methods,created_at) VALUES (1,2,90,'[]','now')")
        conn.commit(); conn.close()
        result = self.service.validate_collection(1,[1])
        codes = {i['code']:i for i in result['issues']}
        self.assertEqual(codes['broken_thumbnail']['allowed_actions'],['thumbnail'])
        self.assertEqual(codes['unresolved_duplicate']['allowed_actions'],[])
        jobs.repair_one(self.root,1,1,'thumbnail',threading.Event())
        self.assertNotIn('broken_thumbnail',self.service.validate_collection(1,[1])['summary']['by_code'])

    def test_missing_image_provenance_and_deletion_during_scan(self):
        original = self.image_path.read_bytes()
        self.image_path.unlink()
        conn = db.get_connection(); conn.execute('DELETE FROM image_source WHERE image_id=1'); conn.commit(); conn.close()
        codes = self.service.validate_collection(1,[1])['summary']['by_code']
        self.assertIn('missing_file',codes)
        self.assertIn('missing_provenance',codes)
        self.image_path.write_bytes(original)
        from app.services import qa
        original_hash = qa._sha256
        def delete_during_hash(path):
            result = original_hash(path)
            conn = db.get_connection(); conn.execute('DELETE FROM image WHERE id=1'); conn.commit(); conn.close()
            return result
        with patch.object(qa,'_sha256',delete_during_hash):
            self.assertEqual(self.service.validate_collection(1,[1])['issues'],[])
