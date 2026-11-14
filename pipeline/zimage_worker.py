"""
zimage_worker.py — Z-Image Turbo image generator, run by the pipeline as a separate process
inside .venv-mflux (Python 3.12 + mflux / MLX). The model is loaded ONCE; requests are JSON
lines on stdin, replies are JSON lines on stdout:

    → {"prompt": "...", "width": 768, "height": 1344, "seed": 42, "steps": 8, "out": "/path.jpg"}
    ← {"ok": true, "out": "/path.jpg", "seconds": 21.4}      or   {"ok": false, "error": "..."}

Not imported by the main app (different Python) — see pipeline/ai_image.py::_zimage.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_REPO = os.getenv("ZIMAGE_MODEL", os.path.join(ROOT, "models", "z-image-turbo-q4"))


def _reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    t0 = time.time()
    try:
        from mflux.models.z_image.variants.z_image import ZImage
        if os.path.isdir(MODEL_REPO):                     # downloaded with fetch_model.py
            path = MODEL_REPO
        else:
            from huggingface_hub import snapshot_download
            path = snapshot_download(MODEL_REPO)
        model = ZImage(model_path=path)                    # weights are pre-quantized
    except Exception as e:
        _reply({"ok": False, "error": f"model load failed: {e}"})
        return
    _reply({"ready": True, "model": MODEL_REPO, "load_seconds": round(time.time() - t0, 1)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            t = time.time()
            img = model.generate_image(
                seed=int(req.get("seed", 0)),
                prompt=req["prompt"],
                num_inference_steps=int(req.get("steps", 8)),
                width=int(req.get("width", 768)),
                height=int(req.get("height", 1344)),
            )
            out = req["out"]
            pil = img.image if hasattr(img, "image") else img
            pil.convert("RGB").save(out, quality=95)
            _reply({"ok": True, "out": out, "seconds": round(time.time() - t, 1)})
        except Exception as e:
            _reply({"ok": False, "error": str(e)[:500]})


if __name__ == "__main__":
    main()
