"""Standalone approximate graphic EQ DSP helper."""
import numpy as np
from scipy.signal import butter, sosfilt

BAND_CENTERS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

def apply_equalizer(samples, sample_rate, gains_db):
    source = np.asarray(samples, dtype=np.float64)
    result = source.copy()
    nyquist = sample_rate / 2.0
    for center, gain in zip(BAND_CENTERS, gains_db):
        if center >= nyquist * 0.95 or abs(gain) < 0.01:
            continue
        low, high = max(10.0, center / (2 ** 0.5)), min(nyquist * 0.95, center * (2 ** 0.5))
        if low >= high:
            continue
        sos = butter(2, [low, high], btype="bandpass", fs=sample_rate, output="sos")
        band = sosfilt(sos, source, axis=0)
        result += band * (10 ** (gain / 20.0) - 1.0)
    peak = np.max(np.abs(result)) if result.size else 0
    if peak > 0.999:
        result *= 0.999 / peak
    return result.astype(np.float32)
