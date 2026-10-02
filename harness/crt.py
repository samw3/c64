"""CRT view: how a PAL (or NTSC) monitor shows the exact C64 pixels of a capture.

The frames in frames/ are pixel-exact. On a real screen, dithers merge and alternating
lines of equal-brightness colors mix ("PAL mixing"). This module models a Commodore
1084S-style monitor fed the C64's separate luma and chroma signals:

  1. palette sRGB -> video signal R'G'B' -> Y'UV. Colodore's palette is the signal
     shown on a gamma-2.8 PAL CRT and re-encoded for a 2.2 display (sRGB = signal^(2.8/2.2)),
     so blending happens where the TV does it: on the signal.
  2. horizontal bandwidth, 4 samples per C64 pixel: luma sigma 0.25 px, chroma sigma 1.1 px
     (about 1 MHz; the same width as VICE's 4-pixel chroma average)
  3. PAL delay line: every line's chroma is averaged with the line above (NTSC: none)
  4. CRT gamma to linear light, a round beam spot (also blurs horizontally)
  5. scanlines: each raster line is a Gaussian beam that widens with brightness (bloom)
  6. pixel aspect ratio 0.936 (PAL) / 0.75 (NTSC); output encoded with gamma 2.2

Large flat areas keep their exact palette color (on average across a line's rows).
Not modeled: composite/RF cross-color and dot crawl, the VIC-II's odd-line phase error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image as PILImage

# PAL Y'UV (BT.601 weights); NTSC monitors decode Y/C the same way for our purposes.
YUV_FROM_RGB = np.array([
    [0.299, 0.587, 0.114],
    [-0.14713, -0.28886, 0.436],
    [0.615, -0.51499, -0.10001],
])
RGB_FROM_YUV = np.linalg.inv(YUV_FROM_RGB)

PIXEL_ASPECT = {"pal": 0.9357, "ntsc": 0.75}   # C64 pixel width / line height on a 4:3 screen
SOURCE_GAMMA = {"pal": 2.8, "ntsc": 2.2}       # CRT gamma the palette was designed for
DISPLAY_GAMMA = 2.2


@dataclass(frozen=True)
class CrtModel:
    luma_blur: float = 0.25          # signal-path sigma, in C64 pixels
    chroma_blur: float = 1.1         # signal-path sigma, in C64 pixels
    delay_line: bool | None = None   # PAL delay line; None = on for PAL, off for NTSC
    spot: float = 0.30               # beam sigma in line heights (dark); also blurs horizontally
    bloom: float = 0.20              # extra beam sigma at full brightness
    scanlines: bool = True
    scale: int = 3                   # output rows per raster line
    oversample: int = 4              # signal samples per C64 pixel


def signal(indices: np.ndarray, palette, video: str = "pal", model: CrtModel = CrtModel()) -> np.ndarray:
    """Steps 1-3: (H, W) color indices -> filtered Y'UV signal, shape (H, W * oversample, 3)."""
    pal = np.asarray(palette, dtype=np.float64)[:16] / 255.0
    yuv_pal = (pal ** (DISPLAY_GAMMA / SOURCE_GAMMA[video])) @ YUV_FROM_RGB.T
    s = model.oversample
    yuv = np.repeat(yuv_pal[np.asarray(indices, dtype=np.intp)], s, axis=1)
    yuv[..., :1] = _blur_rows(yuv[..., :1], model.luma_blur * s)
    yuv[..., 1:] = _blur_rows(yuv[..., 1:], model.chroma_blur * s)
    if model.delay_line if model.delay_line is not None else video == "pal":
        above = np.concatenate([yuv[:1, :, 1:], yuv[:-1, :, 1:]], axis=0)
        yuv[..., 1:] = (yuv[..., 1:] + above) / 2
    return yuv


def render(indices: np.ndarray, palette, video: str = "pal", model: CrtModel = CrtModel()) -> PILImage.Image:
    """CRT view of one frame of C64 color indices, as an RGB image."""
    yuv = signal(indices, palette, video, model)
    light = np.clip(yuv @ RGB_FROM_YUV.T, 0.0, 1.0) ** SOURCE_GAMMA[video]
    aspect = PIXEL_ASPECT[video]
    light = _blur_rows(light, model.spot / aspect * model.oversample)
    width = round(indices.shape[1] * aspect * model.scale)
    light = _resample_rows(light, width)
    out = _beam(light, model) if model.scanlines else np.repeat(light, model.scale, axis=0)
    out = np.clip(out, 0.0, 1.0) ** (1.0 / DISPLAY_GAMMA)
    return PILImage.fromarray((out * 255.0 + 0.5).astype(np.uint8), "RGB")


def _blur_rows(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur along axis 1 (edges extended)."""
    if sigma <= 0:
        return a
    r = max(1, math.ceil(3.5 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = [(0, 0)] * a.ndim
    pad[1] = (r, r)
    p = np.pad(a, pad, mode="edge")
    out = np.zeros_like(a)
    n = a.shape[1]
    for j, w in enumerate(k):
        out += w * p[:, j : j + n]
    return out


def _resample_rows(a: np.ndarray, n_out: int) -> np.ndarray:
    """Linear interpolation along axis 1 to n_out samples (input is already band-limited)."""
    n_in = a.shape[1]
    x = np.clip((np.arange(n_out) + 0.5) * n_in / n_out - 0.5, 0, n_in - 1)
    i0 = np.floor(x).astype(np.intp)
    i1 = np.minimum(i0 + 1, n_in - 1)
    t = (x - i0)[None, :, None]
    return a[:, i0] * (1 - t) + a[:, i1] * t


def _beam(light: np.ndarray, model: CrtModel) -> np.ndarray:
    """Draw each raster line as a Gaussian beam (width grows with brightness) onto `scale`
    rows per line. Each line deposits exactly its own light, so flat areas keep their color."""
    h = light.shape[0]
    v = model.scale
    sigma = model.spot + model.bloom * light
    density = light / (sigma * math.sqrt(2 * math.pi))
    reach = 2
    dp = np.pad(density, ((reach, reach), (0, 0), (0, 0)), mode="edge")
    sp = np.pad(sigma, ((reach, reach), (0, 0), (0, 0)), mode="edge")
    out = np.empty((h * v,) + light.shape[1:])
    for k in range(v):
        y = (k + 0.5) / v - 0.5                   # sub-row offset from its line's center
        acc = np.zeros_like(light)
        for j in range(-reach, reach + 1):        # light from line n+j
            s = sp[reach + j : reach + j + h]
            acc += dp[reach + j : reach + j + h] * np.exp(-0.5 * ((y - j) / s) ** 2)
        out[k::v] = acc
    return out
