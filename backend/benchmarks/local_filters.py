"""Offline metadata benchmark. Uses only a disposable database; downloads nothing."""
import argparse
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.db as db
from app.services.filters import LocalFilter, LocalQuery, list_images, explore_tags, query_sql


def process_peak_bytes():
    if os.name != 'nt':
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == 'darwin' else value * 1024
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)] + [(name,ctypes.c_size_t) for name in (
            'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
            'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    counters = Counters(); counters.cb=ctypes.sizeof(counters)
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi = ctypes.WinDLL('psapi',use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        return None
    return counters.PeakWorkingSetSize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--images', type=int, default=10000)
    args = parser.parse_args()
    original = db.DB_PATH
    with tempfile.TemporaryDirectory() as temp:
        db.DB_PATH = Path(temp) / 'benchmark.db'
        try:
            db.init_db()
            conn = db.get_connection()
            conn.execute("INSERT INTO collection(id,name,slug,type,query,artist_tag_template,caption_template,filters,created_at,updated_at) VALUES (1,'Benchmark','benchmark','artist','benchmark','Drawn by {artist}','{}','{}','now','now')")
            for start in range(1,args.images+1,1000):
                ids = range(start,min(start+1000,args.images+1))
                conn.executemany("INSERT INTO image(id,folder_id,sha256,width,height,format,file_size,path,added_at) VALUES (?,1,?,1024,768,'jpg',1,?,'same')",((i,str(i),f'benchmark/{i}.jpg') for i in ids))
                conn.executemany("INSERT INTO collection_image(collection_id,image_id,added_at) VALUES (1,?,'same')",((i,) for i in ids))
                conn.executemany("INSERT INTO image_source(id,image_id,provider,remote_id,version,fetched_at,metadata) VALUES (?,?,'e621',?,1,'now',?)",((i,i,str(i),json.dumps({'rating':'safe','fixture':True})) for i in ids))
                conn.executemany("INSERT INTO image_tag(image_id,source_id,category,tag) VALUES (?,?,'general',?)",((i,i,tag) for i in ids for tag in ('solo',f'group_{i%100}')))
                conn.commit()
            filters = LocalFilter(required_tags=['solo','group 5'],excluded_tags=['absent'],rating='safe',provider='e621')
            cte, where, params = query_sql(1,filters)
            plan = [r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + cte + f'SELECT i.id FROM scope i WHERE {where} ORDER BY i.added_at DESC,i.id DESC LIMIT 100',params)]
            conn.close()
            measurements = {}
            for name, call in [('gallery',lambda:list_images(1,LocalQuery(filters=filters))),('tags',lambda:explore_tags(1,LocalQuery(filters=filters)))]:
                samples = []
                for _ in range(3):
                    started = time.perf_counter(); result = call(); samples.append((time.perf_counter()-started)*1000)
                # Measure allocation separately: tracing Python SQLite UDFs
                # materially distorts query latency.
                tracemalloc.start()
                call()
                _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                measurements[name] = {'median_ms':round(statistics.median(samples),2),'python_peak_bytes':peak,'returned_rows':len(result['items']),'total':result['total']}
            print(json.dumps({'images':args.images,'page_size':100,'measurements':measurements,'process_peak_bytes':process_peak_bytes(),'query_plan':plan,'memory_note':'Python allocations measured separately from timings. Process peak includes fixture creation and native SQLite caches across the whole benchmark.'},indent=2))
        finally:
            db.DB_PATH = original


if __name__ == '__main__':
    main()
