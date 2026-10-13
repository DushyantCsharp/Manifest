"""Turn a rendered PDF into a "scanned" one: rasterised, skewed, noisy, image-only.

The result has no text layer, so it exercises the OCR path. All randomness comes from the
caller's seeded RNG, so a pack's scans are reproducible.
"""

import io
import random
from decimal import Decimal
from typing import Any

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter

MAX_SKEW_HUNDREDTHS = 300  # ±3.00 degrees
BLUR_RADIUS = Decimal("0.6")


def scan(pdf: bytes, rng: random.Random, *, dpi: int = 150) -> tuple[bytes, dict[str, Any]]:
    rotation = Decimal(rng.randint(-MAX_SKEW_HUNDREDTHS, MAX_SKEW_HUNDREDTHS)) / 100
    noise_sigma = rng.randint(6, 14)  # grey levels
    salt_pepper = Decimal(rng.randint(5, 30)) / 10_000  # share of pixels flipped
    noise = np.random.default_rng(rng.getrandbits(32))

    pages = []
    document = pdfium.PdfDocument(pdf)
    try:
        for page in document:
            image = page.render(scale=dpi / 72).to_pil().convert("L")
            image = image.rotate(float(rotation), resample=Image.Resampling.BICUBIC, fillcolor=255)
            pixels = np.asarray(image, dtype=np.float64)
            pixels = pixels + noise.normal(0, noise_sigma, pixels.shape)
            flip = noise.random(pixels.shape)
            pixels[flip < float(salt_pepper) / 2] = 0
            pixels[flip > 1 - float(salt_pepper) / 2] = 255
            noisy = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
            pages.append(noisy.filter(ImageFilter.GaussianBlur(float(BLUR_RADIUS))))
    finally:
        document.close()

    out = io.BytesIO()
    pages[0].save(out, "PDF", resolution=dpi, save_all=True, append_images=pages[1:], quality=80)
    params = {
        "dpi": dpi,
        "rotation_degrees": str(rotation),
        "noise_sigma": noise_sigma,
        "salt_pepper": str(salt_pepper),
        "blur_radius": str(BLUR_RADIUS),
    }
    return out.getvalue(), params


def page_count(pdf: bytes) -> int:
    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()
