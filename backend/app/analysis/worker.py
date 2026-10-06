"""Analysis worker: `python -m app.analysis.worker --job ID` (started by the app) or `--self-test`.

Downloads each queued post's medium-size sample (per-site pacing, in
background threads), runs every loaded model on batches, and stores scores
and style vectors. GPU use is capped: the app passes CUDA_VISIBLE_DEVICES and
the worker sleeps after each batch so the GPUs are busy at most `duty` of the
time. Stops cleanly when the app sets `stop_requested`.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.analysis.store import AnalysisConfig, connect, get_config, save_results
from app.services.planner_store import now

log = logging.getLogger('analysis.worker')
MAX_SAMPLE_BYTES = 40 * 1024 * 1024
USER_AGENT = 'IllustrationScrappingStudio/1.1 analysis (+https://github.com/RicemanT/Illustration-Scrapping-Studio)'


class SitePacer:
    """At most one request start per `interval` seconds per site; backs off on 429/503."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at: dict[str, float] = {}
        self.penalty: dict[str, float] = {}

    def wait(self, site: str) -> None:
        with self.lock:
            moment = time.monotonic()
            start = max(moment, self.next_at.get(site, 0.0))
            self.next_at[site] = start + self.interval * self.penalty.get(site, 1.0)
        time.sleep(max(0.0, start - moment))

    def throttled(self, site: str) -> None:
        with self.lock:
            self.penalty[site] = min(8.0, self.penalty.get(site, 1.0) * 2)

    def succeeded(self, site: str) -> None:
        with self.lock:
            self.penalty[site] = max(1.0, self.penalty.get(site, 1.0) * 0.9)


def fetch_image(client, pacer: SitePacer, site: str, urls: list[str]):
    """First decodable image among `urls`, downscaled to at most 1024 px; (image, url) or (None, error)."""
    from PIL import Image
    error = 'no URL'
    for url in urls:
        for attempt in range(3):
            pacer.wait(site)
            try:
                response = client.get(url, headers={'Referer': 'https://gelbooru.com/'} if site == 'gelbooru' else {})
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                time.sleep(1 + attempt)
                continue
            if response.status_code in (429, 503):
                pacer.throttled(site)
                time.sleep(2 ** attempt)
                error = f'HTTP {response.status_code}'
                continue
            if response.status_code != 200:
                error = f'HTTP {response.status_code}'
                break
            pacer.succeeded(site)
            data = response.content
            if len(data) > MAX_SAMPLE_BYTES:
                error = 'sample too large'
                break
            try:
                with Image.open(io.BytesIO(data)) as raw:
                    raw.seek(0)
                    image = raw.convert('RGB')
                image.thumbnail((1024, 1024))
                return image, url
            except Exception as exc:
                error = f'not an image: {exc}'
                break
    return None, error


class Progress:
    def __init__(self, job_id: int, total: int):
        self.job_id, self.total = job_id, total
        self.done = self.failed = 0
        self.started = time.monotonic()
        self.gpu_seconds = 0.0
        self.models: dict[str, str] = {}
        self.phase = 'starting'

    def snapshot(self) -> dict:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        finished = self.done + self.failed
        rate = finished / elapsed
        return {'phase': self.phase, 'total': self.total, 'done': self.done, 'failed': self.failed, 'rate': round(rate, 2),
                'eta_seconds': int((self.total - finished) / rate) if rate > 0 else None,
                'gpu_busy': round(self.gpu_seconds / elapsed, 3), 'models': self.models, 'updated_at': now()}

    def save(self, conn) -> None:
        conn.execute('UPDATE analysis_job SET progress=? WHERE id=?', (json.dumps(self.snapshot()), self.job_id))
        conn.commit()


def devices(config: AnalysisConfig) -> list[str]:
    try:
        import torch
        if torch.cuda.is_available() and config.gpu_list():
            return [f'cuda:{index}' for index in range(torch.cuda.device_count())] or ['cpu']
    except ImportError:
        pass
    return ['cpu']


def run_job(job_id: int) -> int:
    from app.analysis.models import load_models, run_models
    import httpx
    conn = connect()
    job = conn.execute('SELECT * FROM analysis_job WHERE id=?', (job_id,)).fetchone()
    if not job:
        print(f'No analysis job {job_id}', file=sys.stderr)
        return 2
    config = AnalysisConfig.model_validate({**get_config(conn).model_dump(), **json.loads(job['params']).get('config', {})})
    total = conn.execute("SELECT count(*) FROM analysis_queue WHERE job_id=? AND state IN ('queued', 'working')", (job_id,)).fetchone()[0]
    conn.execute("UPDATE analysis_queue SET state='queued' WHERE job_id=? AND state='working'", (job_id,))
    conn.execute("UPDATE analysis_job SET status='running', started_at=COALESCE(started_at, ?), error=NULL WHERE id=?", (now(), job_id))
    conn.commit()
    progress = Progress(job_id, total)
    progress.phase = 'loading models'
    progress.save(conn)

    def report(name, status):
        progress.models[name] = status
        log.info('model %s: %s', name, status)
        progress.save(conn)

    models = load_models(config, devices(config), report)
    if not models:
        conn.execute("UPDATE analysis_job SET status='failed', error=?, finished_at=? WHERE id=?",
                     ('No model could be loaded; see the model list', now(), job_id))
        conn.commit()
        return 1
    progress.phase = 'analysing'
    progress.save(conn)

    pacer = SitePacer(config.download_interval)
    ready: queue.Queue = queue.Queue(maxsize=config.batch_size * 4)
    stop = threading.Event()

    def producer():
        reader = connect()
        client = httpx.Client(timeout=30, follow_redirects=True, headers={'User-Agent': USER_AGENT})
        try:
            with ThreadPoolExecutor(max_workers=config.download_workers * 3) as pool:
                while not stop.is_set():
                    rows = reader.execute("SELECT site, remote_id, artist_id, urls FROM analysis_queue WHERE job_id=? AND state='queued' LIMIT ?",
                                          (job_id, config.batch_size * 4)).fetchall()
                    if not rows:
                        break
                    reader.executemany("UPDATE analysis_queue SET state='working' WHERE job_id=? AND site=? AND remote_id=?",
                                       [(job_id, r['site'], r['remote_id']) for r in rows])
                    reader.commit()
                    futures = [(r, pool.submit(fetch_image, client, pacer, r['site'], json.loads(r['urls']))) for r in rows]
                    for row, future in futures:
                        image, detail = future.result()
                        while not stop.is_set():
                            try:
                                ready.put((dict(row), image, detail), timeout=1)
                                break
                            except queue.Full:
                                continue
        finally:
            ready.put(None)
            client.close()
            reader.close()

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    finished = False
    batch: list = []
    last_save = 0.0

    def flush(items):
        good = [(row, image, url) for row, image, url in items if image is not None]
        results = []
        for row, image, error in items:
            if image is None:
                results.append({'site': row['site'], 'remote_id': row['remote_id'], 'artist_id': row['artist_id'], 'status': 'failed', 'error': error})
        if good:
            started = time.monotonic()
            try:
                outputs = run_models(models, [image for _, image, _ in good])
            except Exception as exc:
                log.exception('batch failed')
                outputs = None
                error = f'{type(exc).__name__}: {exc}'[:400]
            busy = time.monotonic() - started
            progress.gpu_seconds += busy
            for (row, _, url), output in zip(good, outputs or [None] * len(good)):
                base = {'site': row['site'], 'remote_id': row['remote_id'], 'artist_id': row['artist_id'], 'source': url}
                results.append({**base, 'status': 'done', **output} if output else {**base, 'status': 'failed', 'error': error})
            # Keep the GPUs busy at most `duty` of the time.
            if config.duty < 1.0:
                time.sleep(busy * (1.0 - config.duty) / config.duty)
        save_results(conn, results)
        conn.executemany('UPDATE analysis_queue SET state=? WHERE job_id=? AND site=? AND remote_id=?',
                         [('done' if r['status'] == 'done' else 'failed', job_id, r['site'], r['remote_id']) for r in results])
        conn.commit()
        progress.done += sum(r['status'] == 'done' for r in results)
        progress.failed += sum(r['status'] != 'done' for r in results)

    try:
        while True:
            if conn.execute('SELECT stop_requested FROM analysis_job WHERE id=?', (job_id,)).fetchone()[0]:
                stop.set()
                break
            try:
                item = ready.get(timeout=2)
            except queue.Empty:
                item = 'idle'
            if item is None:
                finished = True
            elif item != 'idle':
                batch.append(item)
            if batch and (len(batch) >= config.batch_size or finished or item == 'idle'):
                flush(batch)
                batch = []
            if time.monotonic() - last_save > 3:
                progress.save(conn)
                last_save = time.monotonic()
            if finished:
                break
        if batch:
            flush(batch)
        stop.set()
        status = 'completed' if finished else 'stopped'
        conn.execute("UPDATE analysis_queue SET state='queued' WHERE job_id=? AND state='working'", (job_id,))
        progress.phase = status
        progress.save(conn)
        conn.execute('UPDATE analysis_job SET status=?, finished_at=? WHERE id=?', (status, now(), job_id))
        conn.commit()
        return 0
    except Exception as exc:
        log.exception('analysis job failed')
        stop.set()
        conn.execute("UPDATE analysis_job SET status='failed', error=?, finished_at=? WHERE id=?", (f'{type(exc).__name__}: {exc}'[:1000], now(), job_id))
        conn.commit()
        return 1
    finally:
        conn.close()


def self_test() -> int:
    """Load every enabled model and run it on one test image; prints one JSON line per model."""
    from PIL import Image, ImageDraw
    from app.analysis.models import load_models, run_models
    config = get_config()
    image = Image.new('RGB', (768, 1024), (240, 236, 230))
    draw = ImageDraw.Draw(image)
    draw.ellipse((200, 150, 560, 520), fill=(250, 210, 190), outline=(40, 30, 30), width=6)
    draw.rectangle((230, 520, 540, 950), fill=(70, 90, 160), outline=(30, 30, 50), width=6)
    statuses = {}
    loaded = load_models(config, devices(config), lambda name, status: statuses.__setitem__(name, status))
    print(json.dumps({'devices': devices(config), 'load': statuses}), flush=True)
    failures = sum(not str(s).startswith('ok') for s in statuses.values())
    for name, model in loaded:
        started = time.monotonic()
        try:
            output = run_models([(name, model)], [image, image])[0]
            print(json.dumps({'model': name, 'ok': True, 'seconds_for_2': round(time.monotonic() - started, 3),
                              'scores': output['scores'], 'era': output.get('era'),
                              'vectors': {k: len(v) for k, v in output['vectors'].items()}}), flush=True)
        except Exception as exc:
            failures += 1
            print(json.dumps({'model': name, 'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:500]}), flush=True)
    try:
        import torch
        if torch.cuda.is_available():
            print(json.dumps({'cuda_memory_mb': {i: round(torch.cuda.max_memory_allocated(i) / 2 ** 20)
                                                for i in range(torch.cuda.device_count())}}), flush=True)
    except ImportError:
        pass
    print(json.dumps({'result': 'ok' if failures == 0 else f'{failures} problem(s)'}), flush=True)
    return 0 if failures == 0 else 1


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', type=int)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.job is None:
        parser.error('--job or --self-test is required')
    return run_job(args.job)


if __name__ == '__main__':
    sys.exit(main())
