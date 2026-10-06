"""Image analysis for the Dataset Planner.

A separate worker process (`python -m app.analysis.worker`) downloads a
medium-size sample of each candidate post, runs style models (DINOv2, DINOv3),
aesthetic scorers (waifu-scorer v3/v4, Naflex, aesthetic-predictor v2.5,
DeepGHS dbaesthetic) and content classifiers (DeepGHS completeness, classify,
real, AI check, monochrome, style age), and stores only the scores and style
vectors in `<planner>/analysis.db`. The planner then picks each artist's
images by style consistency, content rules, aesthetics and coverage.

Heavy dependencies (torch, transformers, onnxruntime) are imported only by the
worker; the app reads results with numpy.
"""
