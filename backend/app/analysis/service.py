"""App-side control of the analysis worker: environment, installation, self-test, jobs and GPU status."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import app.db as db
from app.analysis import queue as analysis_queue
from app.analysis.store import AnalysisConfig, connect, get_config
from app.services.planner_store import connect as planner_connect, now, planner_dir

BACKEND = Path(__file__).resolve().parents[2]
REQUIREMENTS = BACKEND / 'requirements-analysis.txt'
_processes: dict[str, subprocess.Popen] = {}

ENVIRONMENT_PROBE = r"""
import json, importlib
info = {}
for name in ('torch', 'transformers', 'onnxruntime', 'aesthetic_predictor_v2_5', 'safetensors', 'huggingface_hub'):
    try:
        module = importlib.import_module(name)
        info[name] = getattr(module, '__version__', 'installed')
    except Exception as exc:
        info[name] = None
try:
    import torch, warnings
    info['torch_cuda'] = torch.version.cuda
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        info['cuda'] = torch.cuda.is_available()
    info['cuda_warning'] = ' '.join(str(w.message) for w in caught).strip()[:600]
    info['devices'] = [{'index': i, 'name': torch.cuda.get_device_name(i),
                        'memory_gb': round(torch.cuda.get_device_properties(i).total_memory / 2**30, 1)} for i in range(torch.cuda.device_count())]
except Exception:
    info['cuda'] = False
    info['devices'] = []
try:
    import onnxruntime
    info['onnx_providers'] = onnxruntime.get_available_providers()
except Exception:
    info['onnx_providers'] = []
print(json.dumps(info))
"""


# PyTorch publishes builds per CUDA version; a driver runs builds up to the CUDA version it reports.
# The PyPI default build follows the newest CUDA and silently falls back to the CPU on older drivers.
TORCH_INDEX = 'https://download.pytorch.org/whl/{}'

INSTALL_SCRIPT = r"""
import subprocess, sys
index, requirements = sys.argv[1], sys.argv[2]
def pip(*args):
    print('$ pip ' + ' '.join(args), flush=True)
    return subprocess.call([sys.executable, '-m', 'pip', *args])
if index:
    probe = subprocess.run([sys.executable, '-c', 'import torch, torchvision; print(torch.cuda.is_available())'], capture_output=True, text=True)
    if probe.stdout.strip().endswith('True'):
        print('PyTorch already uses the GPU; keeping it', flush=True)
    else:
        print('Installing the PyTorch build for this GPU driver from ' + index, flush=True)
        pip('uninstall', '-y', 'torch', 'torchvision', 'torchaudio')
        if pip('install', 'torch', 'torchvision', '--index-url', index) != 0:
            print('That failed; falling back to the PyPI build', flush=True)
sys.exit(pip('install', '-r', requirements))
"""


def driver() -> dict:
    """NVIDIA driver version and the newest CUDA it supports, from the nvidia-smi header ({} without NVIDIA drivers)."""
    smi = shutil.which('nvidia-smi')
    if not smi:
        return {}
    try:
        text = subprocess.run([smi], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    version, cuda = re.search(r'Driver Version:\s*([\d.]+)', text), re.search(r'CUDA Version:\s*([\d.]+)', text)
    return {'driver': version.group(1) if version else None, 'driver_cuda': cuda.group(1) if cuda else None}


def torch_index(driver_cuda: Optional[str]) -> Optional[str]:
    """The PyTorch wheel index whose CUDA runs on a driver supporting `driver_cuda` (None: use PyPI)."""
    try:
        major, minor = (int(part) for part in (driver_cuda or '').split('.')[:2])
    except ValueError:
        return None
    if major >= 13:
        tag = 'cu130'
    elif major == 12:
        tag = 'cu126'  # runs on any 12.x driver (minor version compatibility) and gets the newest torch releases
    elif (major, minor) >= (11, 8):
        tag = 'cu118'
    else:
        return None
    return TORCH_INDEX.format(tag)


def _log_path(name: str) -> Path:
    path = planner_dir() / 'analysis-logs' / f'{name}.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def tail(path: Path, lines: int = 40) -> str:
    try:
        return '\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:])
    except FileNotFoundError:
        return ''


def environment() -> dict:
    """Which analysis packages and GPUs this backend's Python sees (probed in a subprocess)."""
    try:
        result = subprocess.run([sys.executable, '-c', ENVIRONMENT_PROBE], capture_output=True, text=True, timeout=180)
        info = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 and result.stdout.strip() else {'error': result.stderr[-2000:]}
    except Exception as exc:
        info = {'error': str(exc)}
    required = ('torch', 'transformers', 'onnxruntime', 'aesthetic_predictor_v2_5')
    info['ready'] = 'error' not in info and all(info.get(name) for name in required)
    info['python'] = sys.executable
    info.update(driver())
    info['torch_index'] = torch_index(info.get('driver_cuda'))
    if info.get('torch') and info.get('driver') and not info.get('cuda'):
        info['cuda_problem'] = (f"The GPUs work (driver {info['driver']}, CUDA up to {info.get('driver_cuda') or '?'}) but PyTorch "
                                f"{info['torch']} (built for CUDA {info.get('torch_cuda') or 'none'}) cannot use them"
                                + (f": {info['cuda_warning']}" if info.get('cuda_warning') else '') + '.')
    return info


def gpu_status() -> list[dict]:
    """Live utilisation, temperature and memory from nvidia-smi (empty without NVIDIA drivers)."""
    smi = shutil.which('nvidia-smi')
    if not smi:
        return []
    try:
        output = subprocess.run([smi, '--query-gpu=index,name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    gpus = []
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(',')]
        if len(parts) >= 6:
            number = lambda text: float(text) if text.replace('.', '', 1).isdigit() else None
            gpus.append({'index': int(parts[0]), 'name': parts[1], 'utilization': number(parts[2]), 'temperature': number(parts[3]),
                         'memory_used_mb': number(parts[4]), 'memory_total_mb': number(parts[5]),
                         'power_w': number(parts[6]) if len(parts) > 6 else None})
    return gpus


def _running(name: str) -> bool:
    process = _processes.get(name)
    return process is not None and process.poll() is None


def install() -> dict:
    """Install the analysis packages into this backend's Python (background; output in the install log)."""
    if _running('install'):
        raise RuntimeError('Installation is already running')
    log = _log_path('install')
    handle = log.open('w', encoding='utf-8')
    index = torch_index(driver().get('driver_cuda')) or ''
    handle.write(f'{now()} installing {REQUIREMENTS.name}' + (f' with PyTorch from {index}' if index else '') + '\n')
    handle.flush()
    _processes['install'] = subprocess.Popen([sys.executable, '-c', INSTALL_SCRIPT, index, str(REQUIREMENTS)],
                                             stdout=handle, stderr=subprocess.STDOUT, cwd=BACKEND)
    return install_status()


def install_status() -> dict:
    process = _processes.get('install')
    state = 'idle' if process is None else 'running' if process.poll() is None else ('completed' if process.returncode == 0 else 'failed')
    return {'status': state, 'log': tail(_log_path('install'))}


def hf_token() -> str:
    from app.services.provider_settings import read_provider_settings
    return os.getenv('HF_TOKEN', '').strip() or str(read_provider_settings().get('huggingface', {}).get('token', '')).strip()


def save_hf_token(token: str) -> None:
    from app.services.provider_settings import update_provider_settings
    update_provider_settings('huggingface', {'token': token.strip()})


def _worker_env(config: AnalysisConfig) -> dict:
    env = dict(os.environ)
    env.update(ARTIST_LIBRARY_PATH=str(db.LIBRARY_PATH), ARTIST_DB_PATH=str(db.DB_PATH), ARTIST_PLANNER_PATH=str(planner_dir()),
               CUDA_VISIBLE_DEVICES=','.join(str(i) for i in config.gpu_list()), PYTHONUNBUFFERED='1',
               PYTHONPATH=str(BACKEND) + os.pathsep + env.get('PYTHONPATH', ''))
    token = hf_token()
    if token:
        env['HF_TOKEN'] = token
    return env


def self_test() -> dict:
    if _running('selftest'):
        raise RuntimeError('The model self-test is already running')
    log = _log_path('selftest')
    handle = log.open('w', encoding='utf-8')
    _processes['selftest'] = subprocess.Popen([sys.executable, '-m', 'app.analysis.worker', '--self-test'], stdout=handle,
                                              stderr=subprocess.STDOUT, cwd=BACKEND, env=_worker_env(get_config()))
    return self_test_status()


def self_test_status() -> dict:
    process = _processes.get('selftest')
    state = 'idle' if process is None else 'running' if process.poll() is None else ('completed' if process.returncode == 0 else 'failed')
    lines = tail(_log_path('selftest'), 200).splitlines()
    results = []
    for line in lines:
        try:
            results.append(json.loads(line))
        except ValueError:
            continue
    return {'status': state, 'results': results, 'log': '\n'.join(lines[-40:])}


def active_job(conn) -> Optional[dict]:
    row = conn.execute("SELECT * FROM analysis_job WHERE status IN ('queued', 'running', 'stopping') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def start_job(scope: str, artist_ids: Optional[list[int]] = None, reanalyze: bool = False) -> dict:
    config = get_config()
    conn = connect()
    try:
        if active_job(conn) or _running('job'):
            raise RuntimeError('An analysis job is already running')
        job_id = conn.execute("INSERT INTO analysis_job (status, params, created_at) VALUES ('queued', ?, ?)",
                              (json.dumps({'scope': scope, 'artist_ids': artist_ids, 'reanalyze': reanalyze, 'config': config.model_dump()}), now())).lastrowid
        conn.commit()
    finally:
        conn.close()
    planner = planner_connect()
    try:
        artists = analysis_queue.scope_artists(planner, scope, artist_ids)
    finally:
        planner.close()
    counts = analysis_queue.build_queue(job_id, artists, config, reanalyze)
    conn = connect()
    try:
        conn.execute('UPDATE analysis_job SET progress=? WHERE id=?', (json.dumps({'phase': 'queued', 'queue': counts, 'total': counts['queued'],
                                                                                    'done': 0, 'failed': 0}), job_id))
        if not counts['queued']:
            conn.execute("UPDATE analysis_job SET status='completed', finished_at=? WHERE id=?", (now(), job_id))
            conn.commit()
            return job(job_id)
        conn.commit()
    finally:
        conn.close()
    _launch(job_id, config)
    return job(job_id)


def _launch(job_id: int, config: AnalysisConfig) -> None:
    handle = _log_path(f'job-{job_id}').open('a', encoding='utf-8')
    process = subprocess.Popen([sys.executable, '-m', 'app.analysis.worker', '--job', str(job_id)], stdout=handle, stderr=subprocess.STDOUT,
                               cwd=BACKEND, env=_worker_env(config))
    _processes['job'] = process
    conn = connect()
    try:
        conn.execute("UPDATE analysis_job SET pid=?, status='running', stop_requested=0 WHERE id=?", (process.pid, job_id))
        conn.commit()
    finally:
        conn.close()


def resume_job(job_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM analysis_job WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise LookupError('Analysis job not found')
        if active_job(conn) or _running('job'):
            raise RuntimeError('An analysis job is already running')
        conn.execute("UPDATE analysis_queue SET state='queued' WHERE job_id=? AND state IN ('working', 'failed')", (job_id,))
        conn.execute("UPDATE analysis_job SET status='queued', finished_at=NULL, error=NULL WHERE id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    _launch(job_id, get_config())
    return job(job_id)


def stop_job() -> Optional[dict]:
    conn = connect()
    try:
        row = active_job(conn)
        if not row:
            return None
        conn.execute("UPDATE analysis_job SET stop_requested=1, status='stopping' WHERE id=?", (row['id'],))
        conn.commit()
        return job(row['id'])
    finally:
        conn.close()


def job(job_id: Optional[int] = None) -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM analysis_job WHERE id=?', (job_id,)).fetchone() if job_id else \
            conn.execute('SELECT * FROM analysis_job ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            return None
        result = dict(row)
        result['params'] = json.loads(result['params'])
        result['progress'] = json.loads(result['progress'] or '{}')
        result['queue'] = {r['state']: r['n'] for r in conn.execute('SELECT state, count(*) AS n FROM analysis_queue WHERE job_id=? GROUP BY state', (row['id'],))}
        process = _processes.get('job')
        if result['status'] in ('running', 'stopping') and (process is None or process.poll() is not None) and not _pid_alive(result['pid']):
            # The worker died without finishing (crash or backend restart).
            conn.execute("UPDATE analysis_job SET status='interrupted', finished_at=? WHERE id=?", (now(), row['id']))
            conn.commit()
            result['status'] = 'interrupted'
        result['log'] = tail(_log_path(f"job-{row['id']}"), 30)
        return result
    finally:
        conn.close()


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        return False
    except Exception:
        return False
    return os.name != 'nt'  # Windows cannot probe safely this way; rely on the process handle there


def shutdown() -> None:
    """On app shutdown ask a running worker to stop; it finishes its batch and exits."""
    conn = connect()
    try:
        conn.execute("UPDATE analysis_job SET stop_requested=1 WHERE status IN ('queued', 'running')")
        conn.commit()
    finally:
        conn.close()
    process = _processes.get('job')
    if process is not None and process.poll() is None:
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.terminate()
