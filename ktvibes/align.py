"""Per-character/word lyric timing by forced alignment on the separated vocals.

LRCLIB gives line start times. Inside each line, torchaudio's multilingual MMS
aligner finds where each romanized character or word is sung. Lines it cannot
align confidently keep the vocal-energy estimate from lyrics.time_units.
"""
import re
import threading
import unicodedata
from .lyrics import CJK, HANGUL, echo, japanese, line_end, romaji, romanize, split_units

_bundle = _model = None
_lock = threading.Lock()
HAN = re.compile(r"[㐀-鿿豈-﫿]")
MIN_SCORE = 0.2  # Mean token probability below this means the aligner is guessing.
LEAD = 0.1  # The aligner marks words ~0.1s after they begin; karaoke should never lag.

def spoken_letters(units: list[str]) -> list[str]:
    """Romanize each unit into the aligner's a-z alphabet ('' when nothing is sung)."""
    from pypinyin import Style, lazy_pinyin
    text = "".join(units)
    han = iter(lazy_pinyin("".join(HAN.findall(text)), style=Style.NORMAL))
    out = []
    for unit in units:
        parts = []
        for char in unit:
            if HAN.match(char):
                parts.append(next(han))
            elif HANGUL.match(char):
                parts.extend(romanize(char))
            else:
                parts.append(unicodedata.normalize("NFKD", char))
        out.append(re.sub(r"[^a-z']", "", "".join(parts).lower().replace("’", "'")))
    return out

def _load():
    global _bundle, _model
    if _model is None:
        import torch
        from torchaudio.pipelines import MMS_FA
        _bundle = MMS_FA
        _model = MMS_FA.get_model().to("cuda" if torch.cuda.is_available() else "cpu").eval()
    return _bundle, _model

def runs(lines: list[dict], words: dict[int, list[str]], longest: float = 30) -> list[list[int]]:
    """Consecutive lines to align together, so the sung audio (not the LRC stamps) decides where one
    line ends and the next begins. A run breaks at an instrumental gap (an empty line), at lines that
    can't be aligned, and before it grows past `longest` seconds. A backing-vocal line, sung over the
    lead, is aligned on its own."""
    groups, current = [], []
    for index, line in enumerate(lines):
        if index not in words or echo(line):
            if current:
                groups.append(current)
            current = []
            if index in words:
                groups.append([index])
            continue
        if current and line_end(lines, index) - lines[current[0]]["t"] > longest:
            groups.append(current)
            current = []
        current.append(index)
    return groups + ([current] if current else [])

def align(lines: list[dict], samples, rate: int, pad: float = 0.4) -> set[int]:
    """Time units for every line the aligner is confident about. Returns aligned line indexes."""
    import torch
    import torchaudio.functional as F
    with _lock:
        bundle, model = _load()
        device = next(model.parameters()).device
        mono = torch.as_tensor(samples, dtype=torch.float32)
        mono = mono.mean(dim=1) if mono.ndim == 2 else mono
        audio = F.resample(mono, rate, bundle.sample_rate)
        tokenizer, aligner = bundle.get_tokenizer(), bundle.get_aligner()
        is_japanese = japanese(lines)
        units, letters = {}, {}
        for index, line in enumerate(lines):
            if not line["text"] or line.get("exact"):
                continue  # Enhanced LRC already has real word timing.
            units[index] = split_units(line["text"])
            # Japanese is aligned on its romaji; kana have no a-z letters of their own.
            letters[index] = [re.sub(r"[^a-z']", "", r.lower()) for r in romaji(units[index])] if is_japanese else spoken_letters(units[index])
        words = {index: [w for w in found if w] for index, found in letters.items() if any(found)}
        aligned = set()

        def place(group: list[int], start: float, end: float, context: tuple[int, ...] = ()):
            """Align `group`'s lines together in [start, end]; lines in `context` only steer the others."""
            clip = audio[int(start * bundle.sample_rate): int(end * bundle.sample_rate)]
            if len(clip) < bundle.sample_rate // 2:
                return
            with torch.inference_mode():
                emission, _ = model(clip[None].to(device))
            try:
                spans = iter(aligner(emission[0], tokenizer([w for index in group for w in words[index]])))
            except Exception:
                return  # Too many letters for the clip length.
            seconds = clip.shape[0] / bundle.sample_rate / emission.shape[1]
            for index in group:
                mine = [next(spans) for _ in words[index]]
                # Mean token probability below MIN_SCORE means the aligner is guessing on this line.
                if index in context or sum(s.score for word in mine for s in word) / sum(len(word) for word in mine) < MIN_SCORE:
                    continue
                timed = iter(mine)
                result, last = [], start
                for unit, letter in zip(units[index], letters[index]):
                    if letter:
                        word = next(timed)
                        a, b = start + word[0].start * seconds - LEAD, start + word[-1].end * seconds - LEAD
                        result.append([unit, round(a, 2), round(b, 2)])
                        last = b
                    else:
                        result.append([unit, round(last, 2), round(last, 2)])  # punctuation/space
                lines[index]["units"] = result
                aligned.add(index)

        previous = []
        for group in runs(lines, words):
            # A run split only for length re-aligns the line before it as context (its timing is kept from
            # the earlier run), so the first line of this run can't be pulled back over the end of that one.
            context = previous[-1:] if previous and previous[-1] + 1 == group[0] and not echo(lines[group[0]]) else []
            previous = group
            place(context + group, max(0.0, lines[(context or group)[0]]["t"] - pad), line_end(lines, group[-1]) + pad, tuple(context))
        # A line the run couldn't place (a misheard or unsung line drags its score down) gets one more try
        # on its own, inside the gap its aligned neighbours leave, so it can't overlap them.
        for index in sorted(set(words) - aligned):
            before = lines[index - 1].get("units") if index - 1 in aligned and not echo(lines[index]) else None
            after = lines[index + 1].get("units") if index + 1 in aligned and not echo(lines[index + 1]) else None
            start = max(lines[index]["t"] - pad, before[-1][2] + LEAD if before else 0.0)
            end = min(line_end(lines, index) + pad, after[0][1] + LEAD if after else float("inf"))
            place([index], start, end)
        return aligned

# karaoke-server: a whole-song shift found by the aligner itself -------------------------------------------------
# The room's votes (2026-11-27: 9 of 88 voted songs "not in sync") were mostly whole songs 10-25 s off: the YouTube
# version has another intro than the recording LRCLIB timed, and the vocal-energy shift (lyrics.find_shift) missed it.
# Word alignment only looks +-0.4 s around each line, so it can't recover that. Here the letters of the lyric lines
# are scored against the vocals at every shift of -SHIFT_REACH..SHIFT_REACH seconds (CTC likelihood, one batched call
# per line), once with SHIFT_SAMPLE lines spread over the song and once with every line (a few lines can latch onto
# a repeated chorus). A shift is used only when the letters fit SHIFT_RATIO times better than as they are AND clearly
# more lines can then be word-aligned (SHIFT_GAIN): that second test rejected every wrong shift in the study.
# Measured against Whisper word times on the voted songs: 5 of 9 flagged songs fixed (median error 10-25 s -> under
# 0.7 s), 1 of 31 confirmed songs improved, 30 unchanged; the rest of the flagged ones have other lyrics or versions.
SHIFT_REACH = 120.0
SHIFT_STEP = 0.5
SHIFT_SAMPLE = 16
SHIFT_RATIO = 2.5
SHIFT_MIN = 1.5  # smaller offsets are left to the word alignment
SHIFT_GAIN = 0.08  # more of the lines must align: at least this share of them

def free_stamps(lines: list[dict], samples, rate: int) -> dict[int, float]:
    """karaoke-server: where each line starts when its stamp is NOT trusted: every word of the lyrics is aligned over
    the whole song in one pass, in order. For lyrics whose words are right and whose stamps are broken ("I Won't Last
    A Day Without You": LRCLIB lines bunched under a second apart; 21 s off -> 0.1 s). On the 30 songs the room voted
    in sync it was as accurate as the stamps (study 2026-11-28), but it has nothing to hold on to when the lyrics
    miss parts of the song, so the caller keeps it only if the words are then heard where they are shown (heard.py).
    {line index: start in seconds}; raises when the lyrics can't be aligned."""
    import torch
    import torchaudio.functional as F
    with _lock:
        bundle, model = _load()
        device = next(model.parameters()).device
        mono = torch.as_tensor(samples, dtype=torch.float32)
        mono = mono.mean(dim=1) if mono.ndim == 2 else mono
        audio = F.resample(mono, rate, bundle.sample_rate)
        chunk, parts = 30 * bundle.sample_rate, []
        with torch.inference_mode():
            for start in range(0, len(audio), chunk):
                clip = audio[start:start + chunk]
                if len(clip) >= bundle.sample_rate // 10:
                    parts.append(model(clip[None].to(device))[0][0])
        emission = torch.cat(parts)
        fps = emission.shape[0] / (len(audio) / bundle.sample_rate)
        words = {}
        for index, line in enumerate(lines):
            if line["text"] and not line.get("exact"):
                found = [w for w in spoken_letters(split_units(line["text"])) if w]
                if found:
                    words[index] = found
        spans = iter(bundle.get_aligner()(emission, bundle.get_tokenizer()([w for index in words for w in words[index]])))
        return {index: [next(spans) for _ in words[index]][0][0].start / fps for index in words}

def _shift_candidates(lines: list[dict], samples, rate: int) -> list[float]:
    import numpy as np
    import torch
    import torch.nn.functional as Fn
    import torchaudio.functional as F
    with _lock:
        bundle, model = _load()
        device = next(model.parameters()).device
        mono = torch.as_tensor(samples, dtype=torch.float32)
        mono = mono.mean(dim=1) if mono.ndim == 2 else mono
        audio = F.resample(mono, rate, bundle.sample_rate)
        chunk, parts = 30 * bundle.sample_rate, []
        with torch.inference_mode():
            for start in range(0, len(audio), chunk):
                clip = audio[start:start + chunk]
                if len(clip) >= bundle.sample_rate // 10:
                    parts.append(model(clip[None].to(device))[0][0])
            if not parts:
                return []
            logp = torch.log_softmax(torch.cat(parts).float(), dim=-1)
        fps = logp.shape[0] / (len(audio) / bundle.sample_rate)
        tokenizer = bundle.get_tokenizer()
        usable = []
        for index, line in enumerate(lines):
            if line["text"] and not line.get("exact") and not echo(line):
                letters = [w for w in spoken_letters(split_units(line["text"])) if w]
                if len(letters) >= 3:
                    usable.append((index, torch.tensor([t for word in tokenizer(letters) for t in word], device=device)))
        if len(usable) < 6:
            return []
        shifts = np.round(np.arange(-SHIFT_REACH, SHIFT_REACH + SHIFT_STEP / 2, SHIFT_STEP), 2)
        table = np.full((len(usable), len(shifts)), np.nan)
        with torch.inference_mode():
            for row, (index, target) in enumerate(usable):
                width = int((line_end(lines, index) - lines[index]["t"] + 1.0) * fps)
                starts = int((lines[index]["t"] - 0.5) * fps) + np.round(shifts * fps).astype(int)
                inside = (starts >= 0) & (starts + width <= logp.shape[0])
                if width < 2 * len(target) or not inside.any():
                    continue
                at = torch.as_tensor(starts[inside], device=device)[:, None] + torch.arange(width, device=device)
                batch = logp[at].transpose(0, 1)
                n = batch.shape[1]
                loss = Fn.ctc_loss(batch, target.repeat(n, 1), torch.full((n,), width, dtype=torch.long),
                                   torch.full((n,), len(target), dtype=torch.long), blank=0, reduction="none", zero_infinity=True)
                table[row, inside] = torch.exp(-loss / len(target)).cpu().numpy()
    zero = int(np.argmin(np.abs(shifts)))
    found = []
    spread = sorted({round(k * (len(usable) - 1) / (SHIFT_SAMPLE - 1)) for k in range(min(SHIFT_SAMPLE, len(usable)))})
    for rows in (spread, list(range(len(usable)))):
        part = table[rows]
        known = ~np.isnan(part)
        # a shift that pushes lines out of the audio fits less (weighted by the share still inside), and not at all
        # once many are out: otherwise a repeat of the chorus further along can win with fewer lines
        inside = known.sum(axis=0) / len(rows)
        scores = np.where(inside >= 0.6, np.where(known, part, 0).sum(axis=0) / len(rows) * inside, 0)
        best = int(np.argmax(scores))
        if scores[zero] > 0 and scores[best] >= SHIFT_RATIO * scores[zero] and abs(shifts[best]) >= SHIFT_MIN:
            found.append(float(shifts[best]))
    return list(dict.fromkeys(found))

def song_shift(lines: list[dict], samples, rate: int) -> float:
    """Seconds to add to every stamp so the words sit where they are sung; 0.0 unless clearly better (see above)."""
    import copy
    candidates = _shift_candidates(lines, samples, rate)
    if not candidates:
        return 0.0
    def share(shift):
        trial = copy.deepcopy(lines)
        for line in trial:
            line["t"] = max(0.0, line["t"] + shift)
        return len(align(trial, samples, rate)) / max(1, sum(1 for line in trial if line["text"]))
    as_is = share(0.0)
    best = max(((share(shift), shift) for shift in candidates), key=lambda pair: pair[0])
    return round(best[1], 2) if best[0] >= as_is + SHIFT_GAIN else 0.0
