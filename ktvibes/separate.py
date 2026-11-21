"""One lazily loaded model on CUDA, Apple MPS, or CPU. Run: uv run python -m ktvibes.separate AUDIO OUTPUT_DIR

CPU tuning (environment):
  KTVIBES_TORCH_THREADS=N   PyTorch compute threads. The default is one per physical core, usually the fastest:
                            hyperthreads share the cores' math units, so more threads tend to be slower.
  KTVIBES_CPU_BF16=1        On CPU, run separation in bfloat16. About 1.7x faster on CPUs with AVX512-BF16 or AMX
                            (Zen 4, Sapphire Rapids and later); the vocal stem stays within about 23 dB of float32.
"""
import os
from pathlib import Path
import sys
import threading
import time
import torch

if os.environ.get("KTVIBES_TORCH_THREADS"):
    torch.set_num_threads(int(os.environ["KTVIBES_TORCH_THREADS"]))
CPU_BF16 = os.environ.get("KTVIBES_CPU_BF16", "").lower() in ("1", "true", "yes")
from demucs.apply import apply_model
from demucs.audio import AudioFile
from demucs.pretrained import get_model
from . import stems as stem_files

_model = None
_lock = threading.Lock()

def _device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"

def separate(source: Path, destination: Path) -> tuple[float, float]:
    """Write vocals.opus and no_vocals.opus; returns (seconds taken, playback gain)."""
    global _model
    started = time.monotonic()
    with _lock:
        if _model is None:
            _model = get_model("htdemucs").to(_device()).eval()
        audio = AudioFile(str(source)).read(streams=0, samplerate=_model.samplerate, channels=_model.audio_channels)
        reference = audio.mean(0)
        mean, std = reference.mean(), reference.std().clamp_min(1e-8)
        bf16 = CPU_BF16 and _device() == "cpu"
        with torch.inference_mode(), torch.autocast("cpu", dtype=torch.bfloat16, enabled=bf16):
            stems = apply_model(_model, ((audio - mean) / std)[None], device=_device(), shifts=1, split=True, overlap=0.25, progress=False)[0].cpu().float()
        stems = stems * std + mean
        index = _model.sources.index("vocals")
        vocals = stems[index]
        instrumental = sum(stems[i] for i in range(len(stems)) if i != index)
        destination.mkdir(parents=True, exist_ok=True)
        gain = stem_files.write(destination, vocals.T.numpy(), instrumental.T.numpy(), _model.samplerate)
    return time.monotonic() - started, gain

if __name__ == "__main__":
    seconds, gain = separate(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Separation: {seconds:.1f}s, playback gain {gain:.3f}")
