"""One lazily loaded model on CUDA, Apple MPS, or CPU. Run: uv run python -m ktvibes.separate AUDIO OUTPUT_DIR

karaoke-server: vocals come from Mel-Band RoFormer (Kim's vocals model, MVSEP vocal SDR 12.6 dB against HTDemucs's
9.9); the user picked it by ear in a blind A/B test (2026-11-27) because HTDemucs leaves ghost vocals. It runs as
the A/B test ran it: float32, one prediction per 11-second chunk (overlap 1). The instrumental is the mix minus the
vocals. If its checkpoint is missing and can't be downloaded, or fails to load, HTDemucs separates instead
(MODEL says which one ran; meta.json and the metrics keep it per song). Only one model is ever loaded.
Fetch the checkpoint ahead of time with: uv run python -m ktvibes.separate --download

CPU tuning (environment):
  KTVIBES_TORCH_THREADS=N   PyTorch compute threads. The default is one per physical core, usually the fastest:
                            hyperthreads share the cores' math units, so more threads tend to be slower.
  KTVIBES_CPU_BF16=1        On CPU, run HTDemucs in bfloat16. About 1.7x faster on CPUs with AVX512-BF16 or AMX
                            (Zen 4, Sapphire Rapids and later); the vocal stem stays within about 23 dB of float32.
  KTVIBES_MODELS=DIR        Where the Mel-Band RoFormer checkpoint lives (default ~/.cache/ktvibes/models).
  KTVIBES_SEPARATION=htdemucs  Use HTDemucs only.
"""
import hashlib
import logging
import os
from pathlib import Path
import sys
import threading
import time
import urllib.request
import torch

if os.environ.get("KTVIBES_TORCH_THREADS"):
    torch.set_num_threads(int(os.environ["KTVIBES_TORCH_THREADS"]))
CPU_BF16 = os.environ.get("KTVIBES_CPU_BF16", "").lower() in ("1", "true", "yes")
from demucs.audio import AudioFile
from . import stems as stem_files

log = logging.getLogger("ktvibes.separate")

MODELS_DIR = Path(os.environ.get("KTVIBES_MODELS") or Path.home() / ".cache" / "ktvibes" / "models")
ROFORMER = "mel_band_roformer_kim"
ROFORMER_FILE = "vocals_mel_band_roformer.ckpt"
ROFORMER_URL = "https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/" + ROFORMER_FILE
ROFORMER_SHA256 = "87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e"
# vocals_mel_band_roformer.yaml, "model" section (its multi_stft_* entries only matter for training)
ROFORMER_CONFIG = dict(
    dim=384, depth=6, stereo=True, num_stems=1, time_transformer_depth=1, freq_transformer_depth=1, num_bands=60,
    dim_head=64, heads=8, attn_dropout=0, ff_dropout=0, flash_attn=True, dim_freqs_in=1025, sample_rate=44100,
    stft_n_fft=2048, stft_hop_length=441, stft_win_length=2048, stft_normalized=False, mask_estimator_depth=2,
    multi_stft_resolution_loss_weight=1.0, multi_stft_resolutions_window_sizes=(4096, 2048, 1024, 512, 256),
    multi_stft_hop_size=147, multi_stft_normalized=False,
)
SAMPLE_RATE = 44100
# As audio-separator computes it: stft_hop_length * (inference.dim_t - 1) = 441 * 1100 samples, about 11 s
CHUNK = 441 * (1101 - 1)
OVERLAP = 1  # inference.num_overlap
PEAK = 0.9  # audio-separator scales a mix that peaks above this down before separating

MODEL = None  # the model that separated the latest song: ROFORMER or "htdemucs"
_roformer = None
_roformer_failed = False
_htdemucs = None
_lock = threading.Lock()

def _device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"

def download(path: Path = MODELS_DIR / ROFORMER_FILE) -> Path:
    """Fetch the checkpoint (913 MB) unless it is already there; the SHA-256 must match."""
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    digest = hashlib.sha256()
    log.info("Downloading %s", ROFORMER_URL)
    with urllib.request.urlopen(ROFORMER_URL, timeout=60) as response, open(partial, "wb") as out:
        while block := response.read(1 << 20):
            digest.update(block)
            out.write(block)
    if digest.hexdigest() != ROFORMER_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{ROFORMER_FILE}: checksum mismatch")
    partial.replace(path)
    return path

def _load_roformer():
    from .roformer.mel_band_roformer import MelBandRoformer
    model = MelBandRoformer(**ROFORMER_CONFIG)
    model.load_state_dict(torch.load(download(), map_location="cpu", weights_only=True))
    return model.to(_device()).eval()

def _model():
    """Mel-Band RoFormer, or HTDemucs when it can't be had; returns (name, model)."""
    global _roformer, _roformer_failed, _htdemucs
    if _roformer is None and not _roformer_failed and os.environ.get("KTVIBES_SEPARATION", "").lower() != "htdemucs":
        try:
            _roformer = _load_roformer()
        except Exception:
            # Only once per process: a broken download shouldn't be retried for every song.
            _roformer_failed = True
            log.exception("Mel-Band RoFormer unavailable; separating with HTDemucs")
    if _roformer is not None:
        return ROFORMER, _roformer
    if _htdemucs is None:
        from demucs.pretrained import get_model
        _htdemucs = get_model("htdemucs").to(_device()).eval()
    return "htdemucs", _htdemucs

def _chunk_starts(length: int, chunk: int, step: int) -> list[int]:
    """Chunks that cover the song; the last one ends at the song's end (audio-separator's schedule)."""
    starts = []
    for offset in range(0, length, step):
        if offset + chunk >= length:
            tail = max(length - chunk, 0)
            if not starts or starts[-1] != tail:
                starts.append(tail)
            break
        starts.append(offset)
    return starts

def _roformer_vocals(model, mix: torch.Tensor) -> torch.Tensor:
    """(2, samples) float mix at 44.1 kHz -> vocals of the same shape, mirroring audio-separator's MDXC demix."""
    device = next(model.parameters()).device
    peak = float(mix.abs().max())
    scale = PEAK / peak if peak > PEAK else 1.0
    mix = mix * scale
    window = torch.hamming_window(CHUNK, periodic=False, dtype=torch.float32, device=device)
    result = torch.zeros(mix.shape, dtype=torch.float32, device=device)
    counter = torch.zeros(mix.shape, dtype=torch.float32, device=device)
    with torch.inference_mode():
        for start in _chunk_starts(mix.shape[1], CHUNK, CHUNK // OVERLAP):
            part = mix[:, start:start + CHUNK].to(device)
            out = model(part[None])[0]
            n = min(part.shape[-1], out.shape[-1], CHUNK)
            result[..., start:start + n] += out[..., :n] * window[:n]
            counter[..., start:start + n] += window[:n]
    return (result / counter.clamp(min=1e-10)).cpu() / scale

def separate(source: Path, destination: Path) -> tuple[float, float]:
    """Write vocals.opus and no_vocals.opus; returns (seconds taken, playback gain)."""
    global MODEL
    started = time.monotonic()
    with _lock:
        name, model = _model()
        if name == ROFORMER:
            audio = AudioFile(str(source)).read(streams=0, samplerate=SAMPLE_RATE, channels=2).float()
            vocals = _roformer_vocals(model, audio)
            instrumental = audio - vocals
            rate = SAMPLE_RATE
        else:
            vocals, instrumental = _htdemucs_stems(model, source)
            rate = model.samplerate
        destination.mkdir(parents=True, exist_ok=True)
        gain = stem_files.write(destination, vocals.T.numpy(), instrumental.T.numpy(), rate)
        MODEL = name
    return time.monotonic() - started, gain

def _htdemucs_stems(model, source: Path):
    from demucs.apply import apply_model
    audio = AudioFile(str(source)).read(streams=0, samplerate=model.samplerate, channels=model.audio_channels)
    reference = audio.mean(0)
    mean, std = reference.mean(), reference.std().clamp_min(1e-8)
    bf16 = CPU_BF16 and _device() == "cpu"
    with torch.inference_mode(), torch.autocast("cpu", dtype=torch.bfloat16, enabled=bf16):
        stems = apply_model(model, ((audio - mean) / std)[None], device=_device(), shifts=1, split=True, overlap=0.25, progress=False)[0].cpu().float()
    stems = stems * std + mean
    index = model.sources.index("vocals")
    return stems[index], sum(stems[i] for i in range(len(stems)) if i != index)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if sys.argv[1:] == ["--download"]:
        print(download())
        sys.exit()
    seconds, gain = separate(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Separation ({MODEL}): {seconds:.1f}s, playback gain {gain:.3f}")
