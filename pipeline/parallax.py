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

DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Base-hf"     # Base: cleaner edges than Small
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


def parallax_clip(image_path, w, h, duration, variant=0, haze=0.18, focus_pull=True):
    """MoviePy VideoClip: camera move with depth parallax over a still image, plus
    depth-aware atmosphere (drifting haze that thickens with distance) and a slow
    lens focus pull (shallow depth of field moving from background to foreground)."""
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
    # subject depth = typical depth in the centre of the frame (where the shot's subject sits)
    hh, ww = dep.shape
    subject = float(np.median(dep[hh // 4: hh * 3 // 4, ww // 4: ww * 3 // 4]))
    # shallow depth of field: a blurred copy, blended in by distance from the focal depth
    soft = torch.from_numpy(np.asarray(img.filter(ImageFilter.GaussianBlur(max(3, img.width // 160))),
                                       np.float32) / 255.0).permute(2, 0, 1)[None].to(dev)
    # drifting haze: low-frequency noise, scrolled slowly; tinted from the image's own shadows
    rng = np.random.default_rng(int(abs(hash(str(image_path))) % 2**32))
    nz = Image.fromarray((rng.random((max(8, sh // 48), max(8, sw // 48))) * 255).astype(np.uint8))
    nz = nz.resize((sw, sh), Image.BICUBIC).filter(ImageFilter.GaussianBlur(max(6, sw // 40)))
    noise = torch.from_numpy(np.asarray(nz, np.float32) / 255.0)[None, None].to(dev)
    tint = torch.from_numpy(np.percentile(np.asarray(img, np.float32).reshape(-1, 3) / 255.0, 35, axis=0)
                            ).view(1, 3, 1, 1).to(dev) * 0.6 + 0.25

    ys = torch.linspace(-1, 1, h, device=dev)
    xs = torch.linspace(-1, 1, w, device=dev)
    gy, gx = torch.meshgrid(ys, xs, indexing="ij")
    win = 1.0 / over                               # half-extent of the visible window
    mode = variant % 7     # 0 dolly-in · 1 orbit R · 2 orbit L · 3 crane up · 4 dolly-out
                           # 5 push-in + orbit · 6 slow pull-back reveal

    def frame(t):
        p = min(max(t / max(duration, 0.01), 0.0), 1.0)
        p = p * p * (3 - 2 * p)                    # ease in/out
        bx, by = gx * win, gy * win
        # depth under each output pixel (sampled at the undisplaced position)
        base = torch.stack([bx, by], dim=-1)[None]
        d = F.grid_sample(dsrc, base, mode="bilinear", padding_mode="zeros", align_corners=True)[0, 0]
        near = d - 0.5
        if mode in (0, 4, 6):
            amp = 0.16 if mode != 6 else 0.22
            z = amp * (p if mode == 0 else 1 - p)
            k = 1.0 / (1.0 + z * (0.4 + 1.2 * d))  # near things grow faster than far things
            sx, sy = bx * k, by * k
        elif mode == 5:
            z = 0.12 * p
            k = 1.0 / (1.0 + z * (0.4 + 1.2 * d))
            cam = (p - 0.5) * 2
            sx = bx * k * 0.97 + cam * (0.01 + 0.05 * near)
            sy = by * k * 0.97
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
        dd = F.grid_sample(dsrc, grid, mode="bilinear", padding_mode="zeros", align_corners=True)
        if focus_pull:                             # rack focus settles ON the subject
            fz = subject - 0.12 * (1 - p)
            blur_amt = (((dd - fz).abs() - 0.18) * 1.6).clamp(0, 0.5)   # subject zone stays sharp
            sb = F.grid_sample(soft, grid, mode="bilinear", padding_mode="zeros", align_corners=True)
            out = out * (1 - blur_amt) + sb * blur_amt
        if haze > 0:                               # thicker with distance, drifting sideways
            drift = torch.stack([(sx + 0.10 * p - 0.05).clamp(-1, 1), sy.clamp(-1, 1)], dim=-1)[None]
            n = F.grid_sample(noise, drift, mode="bilinear", padding_mode="zeros", align_corners=True)
            amt = (haze * (1 - dd) ** 1.5 * (0.55 + 0.9 * n)).clamp(0, 0.6)
            out = out * (1 - amt) + tint * amt
        return (out[0].permute(1, 2, 0).clamp(0, 1) * 255).to(torch.uint8).cpu().numpy()

    return VideoClip(frame_function=frame, duration=duration)
