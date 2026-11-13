"""
pipeline/parallax.py
2.5D "3D photo" animation for still images: estimates depth (Depth Anything V2 Small,
Apache-2.0) and moves a virtual camera through the scene so foreground and background
move at different speeds — the motion style of viral AI-animated Shorts.

Rendering is a per-frame torch.grid_sample on the GPU (Apple MPS / CUDA), so it costs
a few ms per frame. Falls back to plain Ken Burns (caller) if torch is unavailable.
"""

import threading

import numpy as np
from PIL import Image, ImageFilter

DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"
_depth = [None]
_lock = threading.Lock()


def available():
    try:
        import torch
        return torch.backends.mps.is_available() or torch.cuda.is_available()
    except Exception:
        return False


def _device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cuda"


def depth_map(img: Image.Image) -> np.ndarray:
    """Relative depth in [0,1], 1 = nearest. Smoothed so edges don't tear when warped."""
    from transformers import pipeline
    with _lock:
        if _depth[0] is None:
            _depth[0] = pipeline("depth-estimation", model=DEPTH_MODEL, device=_device())
        d = _depth[0](img)["depth"]
    d = d.resize(img.size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(max(2, img.width // 120)))
    a = np.asarray(d, np.float32)
    a = (a - a.min()) / max(a.max() - a.min(), 1e-6)
    return a


def parallax_clip(image_path, w, h, duration, variant=0):
    """MoviePy VideoClip: camera move with depth parallax over a still image."""
    import torch
    import torch.nn.functional as F
    from moviepy import VideoClip

    dev = _device()
    over = 1.14                                    # source is 14% larger than the frame
    img = Image.open(image_path).convert("RGB")
    scale = max(w * over / img.width, h * over / img.height)
    img = img.resize((int(img.width * scale + 0.5), int(img.height * scale + 0.5)), Image.LANCZOS)
    # centre-crop to exactly the oversized frame aspect
    sw, sh = int(w * over), int(h * over)
    left, top = (img.width - sw) // 2, (img.height - sh) // 2
    img = img.crop((left, top, left + sw, top + sh))
    dep = depth_map(img)

    src = torch.from_numpy(np.asarray(img, np.float32) / 255.0).permute(2, 0, 1)[None].to(dev)
    dsrc = torch.from_numpy(dep)[None, None].to(dev)

    ys = torch.linspace(-1, 1, h, device=dev)
    xs = torch.linspace(-1, 1, w, device=dev)
    gy, gx = torch.meshgrid(ys, xs, indexing="ij")
    win = 1.0 / over                               # half-extent of the visible window
    mode = variant % 5     # 0 dolly-in · 1 orbit right · 2 orbit left · 3 crane up · 4 dolly-out

    def frame(t):
        p = min(max(t / max(duration, 0.01), 0.0), 1.0)
        p = p * p * (3 - 2 * p)                    # ease in/out
        bx, by = gx * win, gy * win
        # depth under each output pixel (sampled at the undisplaced position)
        base = torch.stack([bx, by], dim=-1)[None]
        d = F.grid_sample(dsrc, base, mode="bilinear", padding_mode="zeros", align_corners=True)[0, 0]
        near = d - 0.5
        if mode in (0, 4):
            z = 0.16 * (p if mode == 0 else 1 - p)
            k = 1.0 / (1.0 + z * (0.4 + 1.2 * d))  # near things grow faster than far things
            sx, sy = bx * k, by * k
        elif mode in (1, 2):
            cam = (p - 0.5) * 2 * (1 if mode == 1 else -1)
            sx = bx * 0.96 + cam * (0.015 + 0.075 * near)
            sy = by * 0.96
        else:
            cam = (p - 0.5) * 2
            sx = bx * 0.96
            sy = by * 0.96 - cam * (0.012 + 0.065 * near)
        # clamp = "border" padding (MPS doesn't support padding_mode="border")
        grid = torch.stack([sx.clamp(-1, 1), sy.clamp(-1, 1)], dim=-1)[None]
        out = F.grid_sample(src, grid, mode="bilinear", padding_mode="zeros", align_corners=True)
        return (out[0].permute(1, 2, 0).clamp(0, 1) * 255).to(torch.uint8).cpu().numpy()

    return VideoClip(frame_function=frame, duration=duration)
