"""Disposable offline app for the Phase 7 browser smoke test. Never use a real library."""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if os.environ.get('ARTIST_TEST_FIXTURE') != 'phase7':
    raise RuntimeError('This fixture requires ARTIST_TEST_FIXTURE=phase7')

from PIL import Image
import app.db as db

if db.DB_PATH.exists():
    raise RuntimeError('Fixture refuses to open an existing database')
db.init_db()
root = db.LIBRARY_PATH
folder = root / 'images' / 'fixture'
folder.mkdir(parents=True)
conn = db.get_connection()
conn.execute("INSERT INTO collection(id,name,slug,type,query,artist_tag_template,caption_template,filters,created_at,updated_at) VALUES (1,'Fixture','fixture','artist','fixture','Drawn by {artist}','{}','{}','now','now')")
for index,color in ((1,'navy'),(2,'green')):
    path = folder / f'{index}.jpg'
    dimensions = ((512,768) if index == 1 else (1024,512)) if os.environ.get('ARTIST_TEST_UI') == '1' else (512,512)
    Image.new('RGB',dimensions,color).save(path,quality=100)
    content = path.read_bytes()
    conn.execute("INSERT INTO image(id,folder_id,sha256,width,height,format,file_size,path,added_at) VALUES (?,1,?,512,512,'jpg',?,?,'same')",(index,hashlib.sha256(content).hexdigest(),len(content),f'fixture/{index}.jpg'))
    conn.execute("INSERT INTO collection_image(collection_id,image_id,added_at) VALUES (1,?,'same')",(index,))
    conn.execute("INSERT INTO image_source(id,image_id,provider,remote_id,version,fetched_at,metadata) VALUES (?,?,'danbooru',?,1,'now',?)",(index,index,str(index),json.dumps({'rating':'safe'})))
    conn.execute("INSERT INTO image_tag(image_id,source_id,category,tag) VALUES (?,?,'general',?)",(index,index,'blue_hair' if index==1 else 'solo'))
    path.with_suffix('.txt').write_text('Drawn by fixture, '+('blue hair' if index==1 else 'solo'),encoding='utf-8')
if os.environ.get('ARTIST_TEST_UI') == '1':
    conn.execute('UPDATE image SET width=512,height=768 WHERE id=1')
    conn.execute('UPDATE image SET width=1024,height=512 WHERE id=2')
conn.commit(); conn.close()

from app.main import app
if os.environ.get('ARTIST_TEST_COUNTS') == '1':
    # Exercise real job routes/polling and real delete/recovery with an offline
    # sync producer. Only media/provider work is substituted in this fixture.
    from app.routes import sync
    from app.models import SyncResult
    conn = db.get_connection()
    conn.execute("INSERT INTO collection_source(collection_id,provider,enabled) VALUES (1,'danbooru',1)")
    conn.commit(); conn.close()
    async def fixture_sync(collection_id, provider, limit, progress=None, **options):
        import asyncio
        await asyncio.sleep(0.3)
        conn = db.get_connection()
        try:
            slug = conn.execute('SELECT slug FROM collection WHERE id=?', (collection_id,)).fetchone()[0]
            target_folder = root / 'images' / slug
            target_folder.mkdir(parents=True, exist_ok=True)
            start = conn.execute('SELECT coalesce(max(id),0)+1 FROM image').fetchone()[0]
            for image_id in range(start,start+limit):
                path = target_folder / f'{image_id}.jpg'
                Image.new('RGB',(512,512),((image_id*17) % 256,(image_id*31) % 256,(image_id*47) % 256)).save(path,quality=100,subsampling=0)
                content = path.read_bytes()
                conn.execute("INSERT INTO image(id,folder_id,sha256,width,height,format,file_size,path,added_at) VALUES (?,?,?,512,512,'jpg',?,?,'sync')",(image_id,collection_id,hashlib.sha256(content).hexdigest(),len(content),f'{slug}/{image_id}.jpg'))
                conn.execute("INSERT INTO collection_image(collection_id,image_id,added_at) VALUES (?,?,'sync')",(collection_id,image_id))
                path.with_suffix('.txt').write_text('Drawn by fixture',encoding='utf-8')
            conn.commit()
        finally:
            conn.close()
        if progress:
            progress({'completed':limit,'total':limit,'new_images':limit})
        return SyncResult(collection_id=collection_id,provider=provider,new_images=limit,skipped=0,errors=0,duration_seconds=0.3)
    sync.sync_service.sync_collection = fixture_sync
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse
@app.middleware('http')
async def fixture_slash_redirect(request, call_next):
    # The catch-all test UI mount would otherwise intercept Starlette's
    # automatic slash redirects for these existing API endpoints.
    if os.environ.get('ARTIST_TEST_UI') == '1' and request.url.path == '/api/imports/preview':
        from starlette.responses import JSONResponse
        return JSONResponse({'items': [
            {'provider': 'danbooru', 'remote_id': 'fixture-one', 'preview_url': '/static/images/fixture/1.jpg', 'width': 512, 'height': 768, 'tags': {'general': ['blue_hair']}, 'already_imported': True},
            {'provider': 'danbooru', 'remote_id': 'fixture-two', 'preview_url': '/static/images/fixture/2.jpg', 'width': 1024, 'height': 512, 'tags': {'general': ['solo']}, 'already_imported': False},
        ], 'next_cursor': None})
    if request.url.path in {'/api/folders','/api/collections','/api/exports'}:
        return RedirectResponse(str(request.url.replace(path=request.url.path+'/')),status_code=307)
    return await call_next(request)
app.router.routes[:] = [route for route in app.router.routes if getattr(route,'path',None) != '/']
app.mount('/',StaticFiles(directory=str(Path(__file__).resolve().parents[2]/'frontend'/'dist'),html=True),name='test-ui')

if __name__ == '__main__':
    import uvicorn
    # No normal startup reconciliation or scheduler in this isolated fixture.
    uvicorn.run(app,host='127.0.0.1',port=int(os.environ.get('ARTIST_TEST_PORT','8766')),lifespan='off')
