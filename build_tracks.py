#!/usr/bin/env python3
"""
build_tracks.py  —  Satsang Player track scanner  (v3)
=======================================================

Walks your music folder, reads every audio file, and writes a clean
tracks.json that the Satsang Player web app loads directly.

YOUR FOLDER LAYOUT (v3)
-----------------------
    Guruji Ka Satsang/
    |-- Bhajan/                  <- misc, IGNORED by the Satsang Player
    |-- index.html
    |-- tracks.json
    `-- Shabad/
        |-- 01 Ek Onkar.mp3            <- opening track
        |-- 02 Shabads/                <- regular shabads
        |-- 03 Shabads with Vyakhya/   <- long-form Vyakhya shabads
        |-- 04 Guruji Mantra Jaap.mp3  <- closing track
        `-- 05 Aarti/                  <- aartis (the "extras")

FOLDER MATCHING
---------------
Matching ignores case, spaces AND leading numbers, so "02 Shabads",
"Shabads", "shabads" all match the same rule. That means you can
re-number your folders any time without breaking the scan.

WHAT IT NEEDS
-------------
- Python 3 + mutagen:   pip3 install mutagen
- ffmpeg (for loudness): check with  ffmpeg -version
  Without ffmpeg the scan still works; it just skips loudness leveling.

HOW TO RUN
----------
    cd "/Users/musician/Downloads/Guruji Ka Satsang"
    python3 build_tracks.py
Writes tracks.json next to this script.
"""

import sys
import os
import json
import re
import subprocess
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: the 'mutagen' library is not installed.")
    print("Fix it by running:  pip3 install mutagen")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Settings you can adjust
# ---------------------------------------------------------------------------

DEFAULT_MUSIC_DIR = "/Users/musician/Downloads/Guruji Ka Satsang"

# Folder names. Matching ignores case, spaces AND leading numbers,
# so "02 Shabads" matches "Shabads", "05 Aarti" matches "Aarti", etc.
SHABAD_FOLDER  = "Shabads"
VYAKHYA_FOLDER = "Shabads with Vyakhya"
AARTI_FOLDER   = "Aarti"
# The Bhajan folder is excluded from the Satsang Player entirely.
EXCLUDE_FOLDER = "Bhajan"

OPENING_TRACK_NAMES = ["ek onkar", "ekonkar"]
CLOSING_TRACK_NAMES = ["guruji mantra jaap", "gurujimantrajaap", "mantra jaap", "om dhun"]

# Target loudness in LUFS for the volume-leveling feature.
TARGET_LUFS = -16.0
MEASURE_LOUDNESS = True

# Folder where embedded album art from ID3 tags is saved as .jpg files.
# The app reads these to show per-track artwork in the player.
ARTWORK_FOLDER = "Guruji Swaroop_Cover Art"

# Folder of photos for the slideshow banner at the top of the app.
# Any image in here is picked up -- filenames do not matter.
SLIDESHOW_FOLDER = "Guruji Swaroop Main"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize(name: str) -> str:
    """Lowercase, drop a leading number, strip non-alphanumerics."""
    name = re.sub(r"^\s*\d+\s*", "", name)      # drop leading "02 "
    return re.sub(r"[^a-z0-9]", "", name.lower())


def clean_title_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^\s*\d+\s*[-_.]?\s*", "", stem)
    stem = stem.replace("_", " ").strip()
    return stem or Path(filename).stem


def get_tag(audio, keys):
    if audio is None or not hasattr(audio, "tags") or audio.tags is None:
        return None
    for key in keys:
        try:
            value = audio.tags.get(key)
        except Exception:
            value = None
        if value:
            text = value[0] if isinstance(value, (list, tuple)) else value
            text = str(text).strip()
            if text:
                return text
    return None


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def extract_artwork(audio, dest_folder, basename):
    """
    Pull embedded album art out of an audio file and save it as a .jpg.
    Uses mutagen only (no ffmpeg needed). Returns the saved filename,
    or None if the track has no embedded image.
    """
    if audio is None:
        return None

    data = None
    mime = "image/jpeg"

    try:
        # MP3 / ID3: artwork lives in APIC frames.
        if hasattr(audio, "tags") and audio.tags is not None:
            for key in audio.tags.keys():
                if key.startswith("APIC"):
                    apic = audio.tags[key]
                    data = apic.data
                    mime = getattr(apic, "mime", "image/jpeg")
                    break
        # MP4 / m4a: artwork lives in the 'covr' atom.
        if data is None and hasattr(audio, "get"):
            covr = audio.get("covr")
            if covr:
                data = bytes(covr[0])
        # FLAC and others expose .pictures.
        if data is None and hasattr(audio, "pictures") and audio.pictures:
            pic = audio.pictures[0]
            data = pic.data
            mime = pic.mime or "image/jpeg"
    except Exception:
        return None

    if not data:
        return None

    ext = "png" if "png" in mime.lower() else "jpg"
    safe = re.sub(r"[^a-zA-Z0-9 _-]", "", basename).strip() or "art"
    filename = f"{safe}.{ext}"
    try:
        dest_folder.mkdir(parents=True, exist_ok=True)
        with open(dest_folder / filename, "wb") as f:
            f.write(data)
        return filename
    except Exception:
        return None


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def measure_loudness(filepath):
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(filepath),
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
        m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', result.stderr)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def gain_for(loudness):
    if loudness is None:
        return 0.0
    diff = TARGET_LUFS - loudness
    return round(max(-12.0, min(12.0, diff)), 2)


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def scan(music_dir: str):
    base = Path(music_dir).expanduser()
    if not base.is_dir():
        print(f"ERROR: folder not found:\n  {base}")
        sys.exit(1)

    use_loudness = MEASURE_LOUDNESS and ffmpeg_available()
    if MEASURE_LOUDNESS and not use_loudness:
        print("NOTE: ffmpeg not found -- skipping loudness measurement.\n")

    audio_paths = sorted(
        p for p in base.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )

    n_shabad   = normalize(SHABAD_FOLDER)
    n_vyakhya  = normalize(VYAKHYA_FOLDER)
    n_aarti    = normalize(AARTI_FOLDER)
    n_exclude  = normalize(EXCLUDE_FOLDER)

    tracks = []
    skipped = []
    excluded = 0
    track_id = 0

    for idx, path in enumerate(audio_paths, 1):
        try:
            rel_parts = path.relative_to(base).parts
        except ValueError:
            rel_parts = (path.name,)
        folders = rel_parts[:-1]
        norm_folders = [normalize(f) for f in folders]

        # Skip anything in the Bhajan folder entirely.
        if n_exclude in norm_folders:
            excluded += 1
            continue

        try:
            audio = MutagenFile(path)
        except Exception as e:
            skipped.append((path.name, f"could not read: {e}"))
            continue
        if audio is None or not getattr(audio, "info", None):
            skipped.append((path.name, "unsupported or corrupt audio"))
            continue

        duration_sec = getattr(audio.info, "length", 0) or 0
        if duration_sec <= 0:
            skipped.append((path.name, "no readable duration"))
            continue

        title = get_tag(audio, ["TIT2", "title", "\xa9nam"])
        if not title:
            title = clean_title_from_filename(path.name)
        artist = get_tag(audio, ["TPE1", "artist", "\xa9ART"]) or "Unknown"

        norm = normalize(path.stem)
        is_opening = any(n in norm for n in (normalize(x) for x in OPENING_TRACK_NAMES))
        is_closing = any(n in norm for n in (normalize(x) for x in CLOSING_TRACK_NAMES))

        is_aarti    = n_aarti in norm_folders
        has_vyakhya = n_vyakhya in norm_folders

        if is_aarti:
            category = "Aarti"
        else:
            # Shabad folder, Vyakhya folder, or the two loose bookend tracks.
            category = "Shabad"

        loudness = None
        if use_loudness:
            print(f"  [{idx}/{len(audio_paths)}] measuring {path.name}")
            loudness = measure_loudness(path)
        gain_db = gain_for(loudness)

        # Pull embedded album art into the artwork folder.
        artwork = extract_artwork(audio, base / ARTWORK_FOLDER, path.stem)

        track_id += 1
        tracks.append({
            "id": track_id,
            "title": title,
            "artist": artist,
            "category": category,
            "filename": path.name,
            "url": str(path.relative_to(base)).replace(os.sep, "/"),
            "durationSeconds": int(round(duration_sec)),
            "duration": format_duration(duration_sec),
            "hasVyakhya": has_vyakhya,
            "isAarti": is_aarti,
            "isOpening": is_opening,
            "isClosing": is_closing,
            "loudnessLufs": round(loudness, 2) if loudness is not None else None,
            "gainDb": gain_db,
            # Path to extracted cover art (relative to the music folder), or null.
            "artwork": (ARTWORK_FOLDER + "/" + artwork) if artwork else None,
        })

    return tracks, skipped, excluded


def main():
    music_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MUSIC_DIR
    print(f"Scanning: {music_dir}\n")

    tracks, skipped, excluded = scan(music_dir)
    if not tracks:
        print("No audio files found (after excluding Bhajan). Check the path.")
        sys.exit(1)

    tracks.sort(key=lambda t: (not t["isOpening"], t["isClosing"],
                               t["category"], t["title"]))
    for i, t in enumerate(tracks, start=1):
        t["id"] = i

    # Gather slideshow photos from the slideshow folder (any image, any name).
    base = Path(music_dir).expanduser()
    slideshow = []
    slide_dir = base / SLIDESHOW_FOLDER
    if slide_dir.is_dir():
        for p in sorted(slide_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                # Path relative to the music folder, as the app expects.
                slideshow.append(
                    str(p.relative_to(base)).replace(os.sep, "/"))

    output = {
        "generatedBy": "build_tracks.py v4",
        "trackCount": len(tracks),
        "targetLufs": TARGET_LUFS,
        "slideshow": slideshow,
        "tracks": tracks,
    }
    out_path = Path(__file__).parent / "tracks.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    vyakhya = sum(1 for t in tracks if t["hasVyakhya"])
    aartis  = sum(1 for t in tracks if t["isAarti"])
    shabads = sum(1 for t in tracks if t["category"] == "Shabad")
    opening = [t["title"] for t in tracks if t["isOpening"]]
    closing = [t["title"] for t in tracks if t["isClosing"]]
    measured = sum(1 for t in tracks if t["loudnessLufs"] is not None)
    with_art = sum(1 for t in tracks if t["artwork"])

    print(f"\nDone. Wrote {len(tracks)} tracks to:\n  {out_path}\n")
    print(f"  Shabad tracks (incl. Vyakhya) : {shabads}")
    print(f"  Vyakhya (long) tracks         : {vyakhya}")
    print(f"  Aarti tracks                  : {aartis}")
    print(f"  Bhajan files excluded         : {excluded}")
    print(f"  Opening track detected        : {opening or 'NONE - check filename'}")
    print(f"  Closing track detected        : {closing or 'NONE - check filename'}")
    print(f"  Loudness measured for         : {measured}/{len(tracks)} tracks")
    print(f"  Cover art extracted for       : {with_art}/{len(tracks)} tracks")
    if with_art:
        print(f"  Artwork saved into            : {ARTWORK_FOLDER}/")
    print(f"  Slideshow photos found        : {len(slideshow)}  (in {SLIDESHOW_FOLDER}/)")
    if not slideshow:
        print(f"  NOTE: no photos in {SLIDESHOW_FOLDER}/ -- slideshow shows a plain background.")

    if skipped:
        print(f"\n  Skipped {len(skipped)} file(s):")
        for name, reason in skipped:
            print(f"    - {name}: {reason}")


if __name__ == "__main__":
    main()
