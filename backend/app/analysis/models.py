"""Model adapters for the analysis worker (torch, transformers and onnxruntime are imported here only).

Every adapter takes a batch of RGB PIL images and returns one dict per image:
style models return vectors keyed `<model>:<a>-<b>`, scorers and classifiers
return column values from `app.analysis.store.COLUMNS`.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np

from app.analysis.store import AnalysisConfig, parse_range, vector_key

log = logging.getLogger('analysis.models')


def _torch():
    import torch
    return torch


def _dtype(device: str):
    torch = _torch()
    return torch.float16 if device.startswith('cuda') else torch.float32


def _pooled(output):
    """`get_image_features` returns a tensor in transformers 4 and an output object in 5 (whose `pooler_output` is the embedding)."""
    if isinstance(output, _torch().Tensor):
        return output
    embeds = getattr(output, 'image_embeds', None)
    return embeds if embeds is not None else output.pooler_output


class StyleModel:
    """DINOv2 / DINOv3: mean-pooled patch tokens of chosen transformer blocks, averaged over each block range."""

    def __init__(self, name: str, repo: str, ranges: list[str], device: str):
        from transformers import AutoImageProcessor, AutoModel
        self.name, self.device, self.dtype = name, device, _dtype(device)
        self.processor = AutoImageProcessor.from_pretrained(repo)
        self.model = AutoModel.from_pretrained(repo, torch_dtype=self.dtype).to(device).eval()
        config = self.model.config
        self.skip = 1 + int(getattr(config, 'num_register_tokens', 0) or 0)
        layers = int(config.num_hidden_layers)
        self.ranges = [(a, min(b, layers)) for a, b in (parse_range(r) for r in ranges) if a <= layers]
        if not self.ranges:
            raise ValueError(f'{repo} has {layers} blocks; no stored range fits')

    def __call__(self, images) -> list[dict]:
        torch = _torch()
        inputs = self.processor(images=images, return_tensors='pt')
        pixels = inputs['pixel_values'].to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            hidden = self.model(pixel_values=pixels, output_hidden_states=True).hidden_states
        results = [dict() for _ in images]
        for a, b in self.ranges:
            # hidden[0] is the embedding output; hidden[i] is block i.
            pooled = torch.stack([hidden[i][:, self.skip:, :].float().mean(dim=1) for i in range(a, b + 1)]).mean(dim=0)
            for index, vector in enumerate(pooled.cpu().numpy()):
                results[index][vector_key(self.name, f'{a}-{b}')] = vector
        return results


class ClipFeatures:
    """OpenAI CLIP ViT-L/14 image embeddings (L2-normalised), shared by both waifu-scorers."""

    def __init__(self, device: str):
        from transformers import CLIPModel, CLIPProcessor
        self.device, self.dtype = device, _dtype(device)
        self.processor = CLIPProcessor.from_pretrained('openai/clip-vit-large-patch14')
        self.model = CLIPModel.from_pretrained('openai/clip-vit-large-patch14', torch_dtype=self.dtype).to(device).eval()

    def __call__(self, images):
        torch = _torch()
        pixels = self.processor(images=images, return_tensors='pt')['pixel_values'].to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            features = _pooled(self.model.get_image_features(pixel_values=pixels)).float()
        return features / features.norm(dim=-1, keepdim=True).clamp_min(1e-6)


def _sequential_from_state(layers, state: dict):
    """Load a state dict whose keys may carry a module prefix (e.g. `layers.0.weight`) into an nn.Sequential."""
    cleaned = {}
    for key, value in state.items():
        parts = key.split('.')
        while parts and not parts[0].isdigit():
            parts = parts[1:]
        cleaned['.'.join(parts)] = value
    layers.load_state_dict(cleaned)
    return layers


class WaifuScorer:
    """Eugeoter waifu-scorer (v3 and v4-beta): an MLP over normalised CLIP ViT-L/14 features, 0-10."""

    def __init__(self, repo: str, column: str, features: ClipFeatures):
        from huggingface_hub import hf_hub_download
        from safetensors.torch import load_file
        nn = _torch().nn
        self.column, self.features = column, features
        layers = nn.Sequential(
            nn.Linear(768, 2048), nn.ReLU(), nn.BatchNorm1d(2048), nn.Dropout(0.3),
            nn.Linear(2048, 512), nn.ReLU(), nn.BatchNorm1d(512), nn.Dropout(0.3),
            nn.Linear(512, 256), nn.ReLU(), nn.BatchNorm1d(256), nn.Dropout(0.2),
            nn.Linear(256, 128), nn.ReLU(), nn.BatchNorm1d(128), nn.Dropout(0.1),
            nn.Linear(128, 32), nn.ReLU(), nn.Linear(32, 1))
        state = load_file(hf_hub_download(repo, 'model.safetensors'))
        self.head = _sequential_from_state(layers, state).to(features.device).eval()

    def __call__(self, images, features=None) -> list[dict]:
        torch = _torch()
        features = self.features(images) if features is None else features
        with torch.inference_mode():
            # Raw output, not clamped to 0-10: v4-beta works on another (mostly negative) scale, and the planner
            # only uses each scorer's percentiles, so the order is what matters.
            scores = self.head(features).squeeze(-1).cpu().numpy()
        return [{self.column: float(score)} for score in scores]


class NaflexScorer:
    """Silvelter's Naflex anime scorer: SigLIP2 so400m NaFlex features into a 10-bin distribution head, 1-10."""

    SPACE = 'Silvelter/Naflex-anime-scorer'

    def __init__(self, device: str):
        from huggingface_hub import hf_hub_download
        from transformers import AutoModel, AutoProcessor
        torch = _torch()
        nn = torch.nn
        self.device = device
        meta = json.loads(open(hf_hub_download(self.SPACE, 'meta.json', repo_type='space'), encoding='utf-8').read())
        self.max_patches = int(meta.get('max_num_patches', 256))
        self.processor = AutoProcessor.from_pretrained(meta['backbone'])
        self.backbone = AutoModel.from_pretrained(meta['backbone'], torch_dtype=torch.float32).to(device).eval()
        out_dim = 10 if meta.get('head_type') == 'distribution' else 1
        self.distribution = out_dim == 10
        self.bounded = bool(meta.get('bounded_scalar'))
        dim = int(meta['input_dim'])

        class Head(nn.Module):
            def __init__(self):
                super().__init__()
                self.net = nn.Sequential(nn.LayerNorm(dim), nn.Linear(dim, 512), nn.GELU(), nn.Dropout(0.0), nn.Linear(512, 128), nn.GELU(),
                                         nn.Dropout(0.0), nn.Linear(128, 96), nn.GELU(), nn.Linear(96, out_dim))
                self.register_buffer('bins', torch.arange(1, 11, dtype=torch.float32))

        self.head = Head()
        self.head.load_state_dict(torch.load(hf_hub_download(self.SPACE, 'mlp.pt', repo_type='space'), map_location='cpu', weights_only=True))
        self.head = self.head.to(device).eval()

    def __call__(self, images) -> list[dict]:
        torch = _torch()
        processor = getattr(self.processor, 'image_processor', self.processor)
        inputs = processor(images=images, max_num_patches=self.max_patches, return_tensors='pt').to(self.device)
        with torch.inference_mode():
            features = _pooled(self.backbone.get_image_features(**inputs))
            logits = self.head.net(features.float())
            if self.distribution:
                scores = (torch.softmax(logits, dim=1) * self.head.bins).sum(dim=1)
            else:
                scores = logits.squeeze(-1)
                scores = 1.0 + 9.0 * torch.sigmoid(scores) if self.bounded else scores
        return [{'naflex': float(score)} for score in scores.clamp(1, 10).cpu().numpy()]


class AestheticPredictor25:
    """discus0434 aesthetic-predictor v2.5 (SigLIP so400m), 1-10."""

    def __init__(self, device: str):
        from aesthetic_predictor_v2_5 import convert_v2_5_from_siglip
        self.device, self.dtype = device, _dtype(device)
        model, self.preprocessor = convert_v2_5_from_siglip(low_cpu_mem_usage=True, trust_remote_code=True)
        self.model = model.to(self.dtype).to(device).eval()

    def __call__(self, images) -> list[dict]:
        torch = _torch()
        pixels = self.preprocessor(images=images, return_tensors='pt').pixel_values.to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            scores = self.model(pixels).logits.squeeze(-1).float().cpu().numpy()
        return [{'aps25': float(score)} for score in np.atleast_1d(scores)]


class DeepGHSClassifiers:
    """DeepGHS ONNX models (the ones imgutils wraps), run directly with onnxruntime.

    imgutils itself pins numpy<2, which conflicts with the app; the models are
    plain ONNX classifiers: resize to the model's input size, scale to [-1, 1],
    channels first, and read the `output` probabilities in meta.json label order.
    """

    MODELS = (
        ('dbaesthetic', 'deepghs/anime_aesthetic', 'swinv2pv3_v0_448_ls0.2_x'),
        ('completeness', 'deepghs/anime_completeness', 'mobilenetv3_v2.2_dist'),
        ('classify', 'deepghs/anime_classification', 'mobilenetv3_v1.5_dist'),
        ('real', 'deepghs/anime_real_cls', 'mobilenetv3_v1.4_dist'),
        ('aicheck', 'deepghs/anime_ai_check', 'mobilenetv3_sce_dist'),
        ('monochrome', 'deepghs/monochrome_detect', 'mobilenetv3_large_100_dist_safe2'),
        ('style_age', 'deepghs/anime_style_ages', 'mobilenetv3_v0_dist'),
    )
    AESTHETIC_LABELS = ('worst', 'low', 'normal', 'good', 'great', 'best', 'masterpiece')

    def __init__(self, device: str):
        import onnxruntime
        from huggingface_hub import hf_hub_download
        self.device = device
        if device.startswith('cuda') and hasattr(onnxruntime, 'preload_dlls'):
            try:  # CUDA/cuDNN libraries from the nvidia-* pip packages that came with PyTorch
                onnxruntime.preload_dlls()
            except Exception as exc:
                log.warning('onnxruntime could not preload CUDA libraries: %s', exc)
        providers = ['CPUExecutionProvider']
        if device.startswith('cuda') and 'CUDAExecutionProvider' in onnxruntime.get_available_providers():
            # Heuristic cuDNN algorithms: no multi-second search for every new batch size, and a bounded memory arena,
            # so one GPU can hold every model.
            providers = [('CUDAExecutionProvider', {'device_id': int(device.split(':')[1] or 0), 'cudnn_conv_algo_search': 'HEURISTIC',
                                                    'arena_extend_strategy': 'kSameAsRequested', 'gpu_mem_limit': 3 * 1024 ** 3}),
                         'CPUExecutionProvider']
        self.models, self.missing = [], []
        for name, repo, model in self.MODELS:
            try:
                session = onnxruntime.InferenceSession(hf_hub_download(repo, f'{model}/model.onnx'), providers=providers)
                labels = json.loads(open(hf_hub_download(repo, f'{model}/meta.json'), encoding='utf-8').read())['labels']
                shape = session.get_inputs()[0].shape
                size = (shape[3], shape[2]) if isinstance(shape[2], int) and isinstance(shape[3], int) else (384, 384)
                batched = not isinstance(shape[0], int) or shape[0] != 1
                self.models.append((name, session, list(labels), size, batched))
            except Exception as exc:
                self.missing.append(f'{name}: {exc}')
                log.warning('DeepGHS %s unavailable: %s', name, exc)
        if not self.models:
            raise RuntimeError('No DeepGHS model could be loaded: ' + '; '.join(self.missing))
        used = self.models[0][1].get_providers()
        self.runs_on = (device if 'CUDAExecutionProvider' in used else 'cpu (onnxruntime has no CUDA here)' if device.startswith('cuda') else 'cpu')

    @staticmethod
    def encode(image, size) -> np.ndarray:
        from PIL import Image
        data = np.asarray(image.convert('RGB').resize(size, Image.BILINEAR), dtype=np.float32) / 255.0
        return ((data - 0.5) / 0.5).transpose(2, 0, 1)

    def probabilities(self, session, size, batched, images) -> np.ndarray:
        name = session.get_inputs()[0].name
        output = 'output' if any(o.name == 'output' for o in session.get_outputs()) else session.get_outputs()[0].name
        encoded = np.stack([self.encode(image, size) for image in images])
        if batched:
            return session.run([output], {name: encoded})[0]
        return np.concatenate([session.run([output], {name: row[None]})[0] for row in encoded])

    def __call__(self, images) -> list[dict]:
        results = [dict() for _ in images]
        for name, session, labels, size, batched in self.models:
            try:
                probs = self.probabilities(session, size, batched, images)
            except Exception as exc:
                log.warning('DeepGHS %s failed on a batch: %s', name, exc)
                continue
            for row, values in zip(results, probs):
                scores = dict(zip(labels, (float(v) for v in values)))
                if name == 'dbaesthetic':
                    row['dbaes'] = sum(scores.get(label, 0.0) * index for index, label in enumerate(self.AESTHETIC_LABELS))
                elif name == 'completeness':
                    row.update({k: scores[k] for k in ('polished', 'rough', 'monochrome') if k in scores})
                elif name == 'classify':
                    row.update({f'cls_{k}': scores[k] for k in ('3d', 'comic', 'illustration', 'bangumi') if k in scores})
                elif name == 'real':
                    row['real'] = scores.get('real', 0.0)
                elif name == 'aicheck':
                    row['ai'] = scores.get('ai', 0.0)
                elif name == 'monochrome':
                    row['mono'] = scores.get('monochrome', 0.0)
                elif name == 'style_age':
                    era, confidence = max(scores.items(), key=lambda item: item[1])
                    row['era'], row['era_conf'] = era, confidence
        return results


def load_models(config: AnalysisConfig, devices: list[str], report: Callable[[str, str], None]) -> list[tuple[str, object]]:
    """Load every enabled model; a model that fails is reported and skipped."""
    style_device = devices[0]
    score_device = devices[1] if len(devices) > 1 else devices[0]
    loaded = []

    def attempt(name, factory):
        try:
            model = factory()
            loaded.append((name, model))
            where = getattr(model, 'runs_on', None) or getattr(model, 'device', None) or getattr(getattr(model, 'features', None), 'device', None)
            report(name, f'ok on {where}' if where else 'ok')
        except Exception as exc:
            report(name, f'error: {type(exc).__name__}: {exc}'[:400])

    if config.dinov2:
        attempt('dinov2', lambda: StyleModel('dinov2', config.dinov2_repo, config.dino_ranges, style_device))
    if config.dinov3:
        attempt('dinov3', lambda: StyleModel('dinov3', config.dinov3_repo, config.dino_ranges, style_device))
    clip = None
    if config.ws3 or config.ws4:
        try:
            clip = ClipFeatures(score_device)
        except Exception as exc:
            report('clip', f'error: {type(exc).__name__}: {exc}'[:400])
    if clip is not None and config.ws3:
        attempt('ws3', lambda: WaifuScorer('Eugeoter/waifu-scorer-v3', 'ws3', clip))
    if clip is not None and config.ws4:
        attempt('ws4', lambda: WaifuScorer('Eugeoter/waifu-scorer-v4-beta', 'ws4', clip))
    if config.naflex:
        attempt('naflex', lambda: NaflexScorer(score_device))
    if config.aps25:
        attempt('aps25', lambda: AestheticPredictor25(score_device))
    if config.deepghs:
        attempt('deepghs', lambda: DeepGHSClassifiers(style_device))
    if config.anzhc:
        attempt('anzhc', lambda: AnzhcScorer(score_device))
    return loaded


class AnzhcScorer:
    """Anzhc's Anime Score CLS v1 (YOLO classifier, 224 px): which 10% band of Danbooru scores an image falls in.

    Score = expected band, 10 for top 10% ... 1 for the bottom 10%; `anzhc_class` = most likely band (10 = top 10%).
    The ultralytics package is only needed to unpickle the network; preprocessing is ultralytics' classify
    transform (shortest edge 224, centre crop, 0-1 range) done here so ultralytics never touches device selection.
    """

    REPO, FILE = 'Anzhc/Anzhcs_YOLOs', 'Anzhcs Anime Score CLS v1.pt'

    def __init__(self, device: str):
        torch = _torch()
        from huggingface_hub import hf_hub_download
        from torchvision import transforms
        checkpoint = torch.load(hf_hub_download(self.REPO, self.FILE), map_location='cpu', weights_only=False)
        net = checkpoint.get('ema') or checkpoint['model']
        names = net.names if isinstance(net.names, dict) else dict(enumerate(net.names))
        bands = [int(re.sub(r'\D', '', names[i])) for i in range(len(names))]  # 10 = top 10% ... 100
        self.device, self.dtype = device, _dtype(device)
        self.bands = torch.tensor(bands, dtype=torch.float32, device=device)
        self.values = 11 - self.bands / 10  # top 10% -> 10, top 100% -> 1
        self.net = net.float().to(device).to(self.dtype).eval()
        self.transform = transforms.Compose([transforms.Resize(224, interpolation=transforms.InterpolationMode.BILINEAR),
                                             transforms.CenterCrop(224), transforms.ToTensor()])

    def __call__(self, images) -> list[dict]:
        torch = _torch()
        pixels = torch.stack([self.transform(image) for image in images]).to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            output = self.net(pixels)
            probs = (output[0] if isinstance(output, (tuple, list)) else output).float()
            scores = (probs * self.values).sum(dim=1)
            top = self.bands[probs.argmax(dim=1)]
        return [{'anzhc': float(score), 'anzhc_class': float(band)} for score, band in zip(scores.cpu().numpy(), top.cpu().numpy())]


def _device_of(model) -> str:
    return str(getattr(model, 'device', None) or getattr(getattr(model, 'features', None), 'device', None) or 'cpu')


_pool: ThreadPoolExecutor | None = None


def _run_group(group: list[tuple[str, object]], images) -> list[tuple[str, list[dict]]]:
    """Run the models of one device in turn; waifu-scorers share one CLIP pass."""
    outputs, clip_features = [], None
    for name, model in group:
        if isinstance(model, WaifuScorer):
            if clip_features is None:
                clip_features = model.features(images)
            outputs.append((name, model(images, clip_features)))
        else:
            outputs.append((name, model(images)))
    return outputs


def run_models(models: list[tuple[str, object]], images) -> list[dict]:
    """Run every loaded model on a batch. Models on different devices run at the same time (style models on
    one GPU, scorers on the other), so a batch takes as long as the slower GPU rather than the sum of both."""
    global _pool
    groups: dict[str, list] = {}
    for name, model in models:
        groups.setdefault(_device_of(model), []).append((name, model))
    if len(groups) > 1:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='models')
        produced = dict(output for future in [_pool.submit(_run_group, group, images) for group in groups.values()]
                        for output in future.result())
    else:
        produced = dict(output for group in groups.values() for output in _run_group(group, images))
    results = [{'scores': {}, 'vectors': {}, 'models': []} for _ in images]
    for name, _ in models:
        for result, output in zip(results, produced[name]):
            for key, value in output.items():
                if isinstance(value, np.ndarray):
                    result['vectors'][key] = value
                elif key == 'era':
                    result['era'] = value
                else:
                    result['scores'][key] = value
            result['models'].append(name)
    return results
