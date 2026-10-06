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
from collections import defaultdict, deque
from pathlib import PurePath
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from app.analysis.store import AnalysisConfig, connect, get_config, save_results
from app.services.planner_store import now

log = logging.getLogger('analysis.worker')
MAX_SAMPLE_BYTES = 40 * 1024 * 1024
COOLING_POLL = 10.0  # seconds between temperature checks
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


def cuda_report(config: AnalysisConfig) -> dict:
    """Whether PyTorch can use the configured GPUs, and why not (the warning PyTorch gives, e.g. a driver too old for its CUDA)."""
    import os
    import warnings
    info = {'wanted': config.gpu_list(), 'visible': os.environ.get('CUDA_VISIBLE_DEVICES')}
    try:
        import torch
    except ImportError:
        return {**info, 'available': False, 'reason': 'PyTorch is not installed'}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        available = torch.cuda.is_available()
    info.update(torch=torch.__version__, torch_cuda=torch.version.cuda, available=available)
    if available:
        info['devices'] = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
    else:
        reason = ' '.join(str(w.message) for w in caught).strip()
        info['reason'] = reason or ('this PyTorch build has no CUDA' if not torch.version.cuda else 'no GPU visible to PyTorch')
    return info


def gpu_problem(config: AnalysisConfig) -> str | None:
    """A message when GPUs are configured but PyTorch cannot use them (None when fine or the CPU was chosen)."""
    if not config.gpu_list():
        return None
    report = cuda_report(config)
    if report['available']:
        return None
    return (f"GPUs {','.join(map(str, report['wanted']))} are set but PyTorch {report.get('torch', '')} "
            f"(CUDA {report.get('torch_cuda') or 'none'}) cannot use them: {report['reason']}"[:600])


def hot_gpus(config: AnalysisConfig, limit: float) -> list[dict]:
    """The GPUs this job uses whose temperature is at or above `limit` (nvidia-smi numbers are physical, like `gpus`)."""
    from app.analysis.service import gpu_status
    used = set(config.gpu_list())
    return [gpu for gpu in gpu_status() if gpu['index'] in used and (gpu.get('temperature') or 0) >= limit]


def library_images(count: int = 4) -> list:
    """A few random images from the library (real art makes the self-test scores meaningful)."""
    import sqlite3
    import app.db as db
    from PIL import Image
    images = []
    try:
        conn = sqlite3.connect(f'file:{db.DB_PATH.as_posix()}?mode=ro', uri=True)
        paths = [r[0] for r in conn.execute('SELECT path FROM image ORDER BY random() LIMIT ?', (count * 5,))]
        conn.close()
    except Exception as exc:
        log.warning('library images unavailable: %s', exc)
        return []
    for path in paths:
        try:
            with Image.open(db.LIBRARY_PATH / 'images' / path) as raw:
                image = raw.convert('RGB')
            image.thumbnail((1024, 1024))
            images.append((path, image))
        except Exception:
            continue
        if len(images) >= count:
            break
    return images


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

    problem = gpu_problem(config)
    if problem:
        # Running every model on the CPU would take days and load the shared server; fail with the reason instead.
        conn.execute("UPDATE analysis_job SET status='failed', error=?, finished_at=? WHERE id=?",
                     (problem + '. Click Reinstall / update packages, or clear "GPUs to use" to run on the CPU.', now(), job_id))
        conn.commit()
        conn.close()
        return 1
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
    per_site = config.download_workers * 2  # downloads in flight per site (each site has its own threads and pacing)

    def producer():
        """Download continuously, each site with its own threads, so Danbooru, e621 and Gelbooru run side by side
        and a slow site never holds up the others. Posts are taken artist by artist within each site."""
        reader = connect()
        client = httpx.Client(timeout=30, follow_redirects=True, headers={'User-Agent': USER_AGENT},
                              limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        pools: dict[str, ThreadPoolExecutor] = {}
        backlog: dict[str, deque] = defaultdict(deque)
        pending: dict = {}
        open_sites = {r[0] for r in reader.execute("SELECT DISTINCT site FROM analysis_queue WHERE job_id=? AND state='queued'", (job_id,))}

        def claim(site: str) -> list[dict]:
            rows = reader.execute("SELECT site, remote_id, artist_id, urls FROM analysis_queue WHERE job_id=? AND site=? AND state='queued' "
                                  'ORDER BY artist_id, remote_id LIMIT ?', (job_id, site, per_site * 4)).fetchall()
            reader.executemany("UPDATE analysis_queue SET state='working' WHERE job_id=? AND site=? AND remote_id=?",
                               [(job_id, r['site'], r['remote_id']) for r in rows])
            reader.commit()
            return [dict(r) for r in rows]

        try:
            while not stop.is_set():
                for site in list(open_sites):
                    if len(backlog[site]) < per_site:
                        rows = claim(site)
                        if not rows:
                            open_sites.discard(site)
                        backlog[site].extend(rows)
                in_flight = defaultdict(int)
                for row in pending.values():
                    in_flight[row['site']] += 1
                for site, rows in backlog.items():
                    while rows and in_flight[site] < per_site:
                        row = rows.popleft()
                        pool = pools.get(site) or pools.setdefault(site, ThreadPoolExecutor(config.download_workers, thread_name_prefix=f'download-{site}'))
                        pending[pool.submit(fetch_image, client, pacer, site, json.loads(row['urls']))] = row
                        in_flight[site] += 1
                if not pending:
                    break
                done, _ = wait(list(pending), timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    row = pending.pop(future)
                    image, detail = future.result()
                    while not stop.is_set():
                        try:
                            ready.put((row, image, detail), timeout=1)
                            break
                        except queue.Full:
                            continue
        finally:
            for pool in pools.values():
                pool.shutdown(wait=True, cancel_futures=True)
            try:
                ready.put(None, timeout=30)
            except queue.Full:
                pass
            client.close()
            reader.close()

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    finished = False
    batch: list = []
    last_save = 0.0
    next_temperature_check = 0.0

    def stop_requested() -> bool:
        return bool(conn.execute('SELECT stop_requested FROM analysis_job WHERE id=?', (job_id,)).fetchone()[0])

    def cool_down() -> None:
        """Pause while a used GPU is at or above `max_temp`, until it is 5 °C below (checked every 10 s)."""
        nonlocal next_temperature_check
        if not config.max_temp or not config.gpu_list() or time.monotonic() < next_temperature_check:
            return
        next_temperature_check = time.monotonic() + COOLING_POLL
        try:
            hot = hot_gpus(config, config.max_temp)
            while hot and not stop_requested():
                progress.phase = 'cooling: ' + ', '.join(f"GPU {gpu['index']} at {gpu['temperature']:.0f} °C" for gpu in hot)
                progress.save(conn)
                time.sleep(COOLING_POLL)
                hot = hot_gpus(config, config.max_temp - 5)
        except Exception as exc:
            log.warning('temperature check failed: %s', exc)
        if progress.phase.startswith('cooling'):
            progress.phase = 'analysing'
            progress.save(conn)

    def flush(items):
        good = [(row, image, url) for row, image, url in items if image is not None]
        results = []
        for row, image, error in items:
            if image is None:
                results.append({'site': row['site'], 'remote_id': row['remote_id'], 'artist_id': row['artist_id'], 'status': 'failed', 'error': error})
        if good:
            cool_down()
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
            if stop_requested():
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
    """Load every enabled model and score a few real images from the library; prints one JSON line per model."""
    from PIL import Image, ImageDraw
    from app.analysis.models import load_models, run_models
    config = get_config()
    statuses = {}
    gpu = cuda_report(config)
    problem = gpu_problem(config)
    print(json.dumps({'model': 'gpu', 'ok': problem is None, 'error': problem,
                      'detail': f"PyTorch {gpu.get('torch')} · CUDA {gpu.get('torch_cuda') or 'none'} · " +
                                (', '.join(gpu.get('devices', [])) or 'CPU only')}), flush=True)
    samples = library_images(4)
    if samples:
        images = [image for _, image in samples]
        folders = sorted({PurePath(path.replace('\\', '/')).parent.name or path for path, _ in samples})
        print(json.dumps({'model': 'images', 'ok': True, 'detail': f"{len(images)} random library images (from {', '.join(folders)})"}), flush=True)
    else:
        image = Image.new('RGB', (768, 1024), (240, 236, 230))
        draw = ImageDraw.Draw(image)
        draw.ellipse((200, 150, 560, 520), fill=(250, 210, 190), outline=(40, 30, 30), width=6)
        draw.rectangle((230, 520, 540, 950), fill=(70, 90, 160), outline=(30, 30, 50), width=6)
        images = [image, image]
        print(json.dumps({'model': 'images', 'ok': True, 'detail': 'no library images found; a drawn test image (scores mean little)'}), flush=True)
    loaded = load_models(config, devices(config), lambda name, status: statuses.__setitem__(name, status))
    print(json.dumps({'devices': devices(config), 'load': statuses}), flush=True)
    failures = sum(not str(s).startswith('ok') for s in statuses.values()) + (problem is not None)
    for name, model in loaded:
        try:
            run_models([(name, model)], images[:1])  # warm-up, so the timing below is the steady speed
            started = time.monotonic()
            outputs = run_models([(name, model)], images)
            seconds = round(time.monotonic() - started, 3)
            keys = sorted({key for output in outputs for key in output['scores']})
            values = {key: [output['scores'][key] for output in outputs if key in output['scores']] for key in keys}
            print(json.dumps({'model': name, 'ok': True, 'seconds': seconds, 'images': len(images),
                              'scores': {key: sum(v) / len(v) for key, v in values.items()},
                              'spread': {key: [min(v), max(v)] for key, v in values.items() if len(v) > 1},
                              'era': outputs[0].get('era'), 'vectors': {k: len(v) for k, v in outputs[0]['vectors'].items()}}), flush=True)
        except Exception as exc:
            failures += 1
            print(json.dumps({'model': name, 'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:500]}), flush=True)
    if len(loaded) > 1:
        try:
            started = time.monotonic()
            run_models(loaded, images)
            seconds = time.monotonic() - started
            print(json.dumps({'model': 'all models', 'ok': True, 'seconds': round(seconds, 3), 'images': len(images),
                              'detail': f'GPUs in parallel · about {len(images) / seconds:.1f} images/s while busy, '
                                        f'{len(images) / seconds * config.duty:.1f} images/s at {round(config.duty * 100)}% busy'}), flush=True)
        except Exception as exc:
            failures += 1
            print(json.dumps({'model': 'all models', 'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:500]}), flush=True)
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
