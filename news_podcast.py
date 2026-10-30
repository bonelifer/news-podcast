#!/usr/bin/env python3
"""Turn the day's news into a spoken MP3 briefing.

Each run reads the RSS/Atom feeds listed in the config, keeps the stories
published within the lookback window, asks the Antigravity CLI (``agy``) to
write a spoken script for every section, reads each part aloud with Piper
using the section's voice, and saves one dated MP3 plus its text script.

If ``agy`` fails for a section, that section falls back to reading the
headlines as published, and says so in the audio.

Commands:
    run      Make today's episode (``--dry-run`` prints the script only).
    install  Seed the live config and enable the daily systemd user timer.

Exit codes: 0 success, 1 failure, 2 bad arguments, 75 another run active.
"""

import argparse
import configparser
import datetime as dt
import fcntl
import hashlib
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
VENV_DIR = REPO_DIR / ".venv"
REQUIREMENTS = REPO_DIR / "requirements.txt"
CONFIG_TEMPLATE = REPO_DIR / "config.ini.example"
CONFIG_PATH = Path.home() / ".config" / "news-podcast" / "config.ini"
CACHE_DIR = Path.home() / ".cache" / "news-podcast"
SYSTEMD_TEMPLATE_DIR = REPO_DIR / "systemd"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAMES = ("news-podcast.service", "news-podcast.timer")

WORDS_PER_MINUTE = 150
INTRO_OUTRO_WORDS = 120
SEGMENT_GAP_SECONDS = 0.8
FEED_TIMEOUT_SECONDS = 20
FEED_ATTEMPTS = 3
SUMMARY_MAX_CHARS = 600
USER_AGENT = "news-podcast/1.0 (+https://github.com/bonelifer/news-podcast)"
EPISODE_NAME = re.compile(r"^news-(\d{4}-\d{2}-\d{2})\.(mp3|txt)$")
DAY_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EXIT_BUSY = 75

log = logging.getLogger("news-podcast")


def ensure_venv() -> None:
    """Re-exec inside the project's own venv, creating it when needed.

    The venv lives in ``.venv`` next to this script. Requirements are
    reinstalled whenever ``requirements.txt`` changes.
    """
    venv_python = VENV_DIR / "bin" / "python"
    if Path(sys.prefix).resolve() != VENV_DIR.resolve():
        if not venv_python.exists():
            create_venv()
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])

    marker = VENV_DIR / ".requirements.sha256"
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not marker.exists() or marker.read_text().strip() != wanted:
        print("Installing requirements ...", file=sys.stderr)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
            check=True,
        )
        marker.write_text(wanted + "\n")


def create_venv() -> None:
    """Create .venv with the running Python, or the system Python if it lacks venv.

    Some Pythons (for example a deadsnakes 3.11 without python3.11-venv)
    cannot create virtual environments; any Python 3.10+ works the same here.
    """
    candidates = [sys.executable]
    if Path("/usr/bin/python3").exists() and Path(sys.executable).resolve() != Path(
            "/usr/bin/python3").resolve():
        candidates.append("/usr/bin/python3")
    print("Creating virtual environment in .venv ...", file=sys.stderr)
    for python in candidates:
        result = subprocess.run([python, "-m", "venv", str(VENV_DIR)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        print(f"{python} could not create a venv: "
              f"{(result.stderr.strip().splitlines() or ['unknown error'])[-1]}", file=sys.stderr)
    sys.exit("No usable Python found to create .venv; install your distribution's "
             "python3-venv package and try again.")


@dataclass
class Voice:
    """A Piper voice model, optional speaker number and optional own speed."""

    name: str
    model: Path
    speaker: str
    length_scale: float | None  # None uses [piper] length_scale


@dataclass
class Section:
    """One segment of the episode and the feeds it draws from."""

    name: str
    voice: str
    feeds: list[tuple[str, str]]  # (URL, display name or "" to use the feed's title)


@dataclass
class Config:
    """Validated settings from config.ini."""

    output_dir: Path
    keep_days: int
    target_minutes: int
    lookback_hours: int
    max_items_per_feed: int
    piper_command: str
    default_voice: str
    length_scale: float
    sentence_silence: float
    voices: dict[str, Voice]
    agy_command: str
    agy_timeout: int
    sections: list[Section] = field(default_factory=list)


@dataclass
class Story:
    """A single news item taken from a feed."""

    source: str
    title: str
    summary: str
    published: dt.datetime | None


class ConfigError(Exception):
    """Raised when config.ini is missing or has an invalid value."""


def read_int(parser: configparser.ConfigParser, section: str, key: str,
             fallback: int, minimum: int) -> int:
    """Read an integer setting, naming the setting when it is invalid."""
    raw = parser.get(section, key, fallback=str(fallback)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"[{section}] {key} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"[{section}] {key} must be at least {minimum}, got {value}")
    return value


def read_float(parser: configparser.ConfigParser, section: str, key: str,
               fallback: float) -> float:
    """Read a positive decimal setting, naming the setting when it is invalid."""
    raw = parser.get(section, key, fallback=str(fallback)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"[{section}] {key} must be a number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"[{section}] {key} must not be negative, got {value}")
    return value


def load_config(path: Path) -> Config:
    """Parse and validate the config file at ``path``."""
    if not path.exists():
        raise ConfigError(f"{path} not found; run `news_podcast.py install` to create it")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")

    voices: dict[str, Voice] = {}
    sections: list[Section] = []
    for name in parser.sections():
        kind, _, label = name.partition(":")
        label = label.strip()
        if kind.strip() == "voice" and label:
            model = Path(parser.get(name, "model", fallback="").strip()).expanduser()
            if not model.is_file():
                raise ConfigError(f"[{name}] model file not found: {model}")
            if not Path(f"{model}.json").is_file():
                raise ConfigError(f"[{name}] model config not found: {model}.json")
            speaker = parser.get(name, "speaker", fallback="").strip()
            if speaker and not speaker.isdigit():
                raise ConfigError(f"[{name}] speaker must be a number or empty, got {speaker!r}")
            speed = None
            if parser.get(name, "length_scale", fallback="").strip():
                speed = read_float(parser, name, "length_scale", 1.0) or None
            voices[label] = Voice(label, model, speaker, speed)
        elif kind.strip() == "section" and label:
            feeds = []
            for line in parser.get(name, "feeds", fallback="").splitlines():
                url, _, feed_name = (part.strip() for part in line.partition("|"))
                if not url:
                    continue
                if not url.startswith(("http://", "https://")):
                    raise ConfigError(f"[{name}] feed is not an http(s) URL: {url}")
                feeds.append((url, feed_name))
            if not feeds:
                raise ConfigError(f"[{name}] feeds must list at least one URL")
            voice = parser.get(name, "voice", fallback="").strip()
            sections.append(Section(label, voice, feeds))

    if not voices:
        raise ConfigError("no [voice: Name] blocks found")
    if not sections:
        raise ConfigError("no [section: Name] blocks found")
    default_voice = parser.get("piper", "default_voice", fallback="").strip()
    if default_voice not in voices:
        raise ConfigError(f"[piper] default_voice must be one of {', '.join(voices)}, "
                          f"got {default_voice!r}")
    for section in sections:
        section.voice = section.voice or default_voice
        if section.voice not in voices:
            raise ConfigError(f"[section: {section.name}] voice must be one of "
                              f"{', '.join(voices)}, got {section.voice!r}")

    return Config(
        output_dir=Path(parser.get("podcast", "output_dir", fallback="~/Podcasts/news")
                        .strip()).expanduser(),
        keep_days=read_int(parser, "podcast", "keep_days", 30, 0),
        target_minutes=read_int(parser, "podcast", "target_minutes", 20, 1),
        lookback_hours=read_int(parser, "podcast", "lookback_hours", 24, 1),
        max_items_per_feed=read_int(parser, "podcast", "max_items_per_feed", 15, 1),
        piper_command=parser.get("piper", "command", fallback="piper").strip() or "piper",
        default_voice=default_voice,
        length_scale=read_float(parser, "piper", "length_scale", 1.0) or 1.0,
        sentence_silence=read_float(parser, "piper", "sentence_silence", 0.3),
        voices=voices,
        agy_command=parser.get("agy", "command", fallback="agy").strip() or "agy",
        agy_timeout=read_int(parser, "agy", "timeout_seconds", 900, 30),
        sections=sections,
    )


def clean_text(raw: str) -> str:
    """Strip HTML tags and entities and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\[(…|\.\.\.)\]", " ", html.unescape(text))
    return re.sub(r"\s+", " ", text).strip()


def ordinal(day: int) -> str:
    """Return a day of the month as it is said aloud: 1st, 2nd, 3rd, 30th."""
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def spoken_list(items: list[str]) -> str:
    """Join items the way they are said aloud: "a, b, and c"."""
    if len(items) < 3:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def normalize_title(title: str) -> str:
    """Reduce a headline to lowercase letters and digits for de-duplication."""
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def download(url: str) -> bytes:
    """Fetch ``url``, retrying timeouts, connection errors and 5xx/429 replies."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, FEED_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=FEED_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500 or attempt == FEED_ATTEMPTS:
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 5 * attempt
        except (urllib.error.URLError, TimeoutError):
            if attempt == FEED_ATTEMPTS:
                raise
            delay = 5 * attempt
        time.sleep(min(delay, 60))
    raise RuntimeError("unreachable")


def entry_time(entry) -> dt.datetime | None:
    """Return the entry's published or updated time in UTC, if it has one."""
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return None
    return dt.datetime(*stamp[:6], tzinfo=dt.timezone.utc)


def fetch_section(section: Section, config: Config, now: dt.datetime) -> tuple[list[Story], int]:
    """Collect recent, de-duplicated stories for one section.

    Returns the stories (newest first) and how many feeds failed.
    """
    import feedparser  # Installed into the venv by ensure_venv().

    cutoff = now - dt.timedelta(hours=config.lookback_hours)
    stories: list[Story] = []
    seen: set[str] = set()
    failures = 0
    for url, feed_name in section.feeds:
        try:
            parsed = feedparser.parse(download(url))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            failures += 1
            log.warning("feed failed: %s (%s)", url, error)
            continue
        if parsed.bozo and not parsed.entries:
            failures += 1
            log.warning("feed unreadable: %s (%s)", url, parsed.get("bozo_exception"))
            continue

        feed_title = feed_name or clean_text(parsed.feed.get("title", "")) or url
        taken = 0
        for entry in parsed.entries:
            published = entry_time(entry)
            if published and published < cutoff:
                continue
            title = clean_text(entry.get("title", ""))
            source = clean_text(entry.get("source", {}).get("title", "")) or feed_title
            # Google News appends " - Outlet" to every headline.
            if source and title.endswith(f" - {source}"):
                title = title[: -len(source) - 3].strip()
            key = normalize_title(title)
            if not key or key in seen:
                continue
            summary = clean_text(entry.get("summary", ""))
            if normalize_title(summary).startswith(key):
                summary = ""
            seen.add(key)
            stories.append(Story(source, title, summary[:SUMMARY_MAX_CHARS], published))
            taken += 1
            if taken >= config.max_items_per_feed:
                break
        log.info("%s: %d stories from %s", section.name, taken, feed_title)

    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    stories.sort(key=lambda story: story.published or oldest, reverse=True)
    return stories, failures


def story_list(stories: list[Story]) -> str:
    """Format stories as a numbered list for the agy prompt."""
    lines = []
    for number, story in enumerate(stories, 1):
        when = story.published.strftime("%Y-%m-%d %H:%M UTC") if story.published else "time unknown"
        lines.append(f"[{number}] {story.source}: {story.title} ({when})")
        if story.summary:
            lines.append(f"    {story.summary}")
    return "\n".join(lines)


def write_with_agy(section: Section, stories: list[Story], words: int,
                   date_text: str, config: Config) -> str:
    """Ask agy for a spoken script of the section; raise RuntimeError on failure."""
    prompt = f"""You are writing the "{section.name}" segment of a spoken daily news \
podcast for {date_text}. A text-to-speech voice will read your words aloud exactly as written.

Write about {words} words covering the stories below, in the style of a modern public \
radio newsreader, somewhere between NPR and BBC Radio 4: calm, measured and understated, \
current rather than old-fashioned.

Rules:
- Use only the facts in the stories below. Do not add facts from memory, do not \
browse the web, and do not use any tools.
- Open with a brief, plain lead-in such as "Here in Hot Springs..." or "In technology \
news...". Do not welcome the listener, name the segment formally, or repeat the date.
- Lead with the most important stories and group related ones together. Skip \
trivial or duplicate items if you run short of space.
- For each story, say what happened first, then add context or why it matters only \
when the story itself gives it. Never guess at consequences, causes or details.
- Reach the length by covering more of the stories, in the order of importance, \
not by stretching any one story. Only if every relevant story is covered may you \
finish short; never pad or restate.
- Skip stories that are not about this segment's subject, such as national news in a \
local feed.
- Keep sentences mostly short and direct, one idea each, so they are easy to follow \
by ear. Contractions and everyday words are fine.
- No hype or opinion: avoid words like shocking, slams, huge or stunning, and never \
use exclamation marks.
- Use quiet transitions such as "Elsewhere..." or "Also today...", never "Stay tuned" \
or "Turning now to".
- Name the outlet for most stories, naturally and with variety, for example \
"NPR reports" or "according to the BBC".
- Plain text only: no markdown, headings, bullet points, URLs, emoji or stage directions.
- Write numbers, dates and abbreviations the way a newsreader would say them.
- Output only the script itself, with no preface or closing remarks to me.

Stories:
{story_list(stories)}
"""
    workdir = CACHE_DIR / "agy-workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [config.agy_command, "-p", prompt, "--print-timeout", f"{config.agy_timeout}s"],
            cwd=workdir, capture_output=True, text=True, timeout=config.agy_timeout + 60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"agy did not finish: {error}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"agy exited with {result.returncode}: {detail[0]}")

    script = re.sub(r"[*#_`]+", "", result.stdout).strip()
    if len(script.split()) < 50:
        raise RuntimeError(f"agy returned only {len(script.split())} words")
    return script


def headline_script(section: Section, stories: list[Story], words: int) -> str:
    """Build a plain headline reading for a section, within the word budget."""
    parts = [f"{section.name}. The summary writer was not available today, "
             f"so here are the headlines as published."]
    used = len(parts[0].split())
    for story in stories:
        line = f"From {story.source}: {story.title}."
        if story.summary:
            line += f" {story.summary}"
        used += len(line.split())
        if used > words and len(parts) > 1:
            break
        parts.append(line)
    return "\n\n".join(parts)


def build_script(config: Config, now: dt.datetime, use_agy: bool) -> list[tuple[str, str]]:
    """Return the episode as (voice name, text) segments in reading order."""
    local_now = now.astimezone()
    date_text = f"{local_now:%A, %B} {local_now.day}, {local_now.year}"
    section_words = max(
        150, (config.target_minutes * WORDS_PER_MINUTE - INTRO_OUTRO_WORDS) // len(config.sections))

    segments: list[tuple[str, str]] = []
    source_counts: dict[str, int] = {}
    total_stories = 0
    for section in config.sections:
        stories, failures = fetch_section(section, config, now)
        total_stories += len(stories)
        for story in stories:
            source_counts[story.source] = source_counts.get(story.source, 0) + 1
        if not stories:
            reason = ("its news sources could not be reached" if failures == len(section.feeds)
                      else "there were no new stories in the last "
                           f"{config.lookback_hours} hours")
            segments.append(
                (section.voice, f"{section.name}. Nothing to report today, because {reason}."))
            continue
        text = None
        if use_agy:
            try:
                text = write_with_agy(section, stories, section_words, date_text, config)
            except RuntimeError as error:
                log.error("%s: falling back to headlines: %s", section.name, error)
        segments.append((section.voice, text or headline_script(section, stories, section_words)))

    if total_stories == 0:
        raise RuntimeError("no stories were found in any section; no episode was made")

    local_day = now.astimezone()
    intro = (f"This is the morning briefing for {local_day:%A, %B} {ordinal(local_day.day)}. "
             f"Coming up: {spoken_list([s.name for s in config.sections])}.")
    top_sources = sorted(source_counts, key=source_counts.get, reverse=True)[:5]
    outro = (f"That's the briefing. Today's stories came from {len(source_counts)} news "
             f"outlets, including {spoken_list(top_sources)}. More tomorrow.")
    return [(config.default_voice, intro), *segments, (config.default_voice, outro)]


def synthesize(segments: list[tuple[str, str]], config: Config, workdir: Path) -> Path:
    """Read every segment with Piper and join them into one WAV file."""
    parts: list[Path] = []
    for index, (voice_name, text) in enumerate(segments):
        voice = config.voices[voice_name]
        part = workdir / f"part-{index:02d}.wav"
        command = [config.piper_command, "-m", str(voice.model), "-f", str(part),
                   "--length-scale", str(voice.length_scale or config.length_scale),
                   "--sentence-silence", str(config.sentence_silence)]
        if voice.speaker:
            command += ["-s", voice.speaker]
        log.info("reading segment %d of %d with %s", index + 1, len(segments), voice_name)
        result = subprocess.run(command, input=text, text=True, capture_output=True)
        if result.returncode != 0 or not part.exists():
            detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(f"piper failed with voice {voice_name}: {detail[0]}")
        parts.append(part)

    combined = workdir / "episode.wav"
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
    gap = b"\0" * int(params.framerate * SEGMENT_GAP_SECONDS) * params.sampwidth * params.nchannels
    with wave.open(str(combined), "wb") as out:
        out.setparams(params)
        for index, part in enumerate(parts):
            with wave.open(str(part), "rb") as piece:
                if (piece.getframerate(), piece.getsampwidth(), piece.getnchannels()) != (
                        params.framerate, params.sampwidth, params.nchannels):
                    raise RuntimeError(f"voice {segments[index][0]} uses a different sample "
                                       f"rate or format; all voices must match")
                out.writeframes(piece.readframes(piece.getnframes()))
            out.writeframes(gap)
    return combined


def save_episode(wav: Path, script: str, config: Config, day: dt.date) -> Path:
    """Encode the MP3 and write it and the script atomically.

    Files go in a folder per day: output_dir/YYYY-MM-DD/news-YYYY-MM-DD.mp3.
    """
    day_dir = config.output_dir / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    mp3 = day_dir / f"news-{day.isoformat()}.mp3"
    txt = mp3.with_suffix(".txt")
    tmp_mp3 = day_dir / f".{mp3.name}.partial"
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-q:a", "5", "-f", "mp3",
         "-metadata", f"title=News briefing {day:%m/%d/%Y}",
         "-metadata", "artist=news-podcast", "-metadata", "album=Daily news briefing",
         "-metadata", f"date={day.isoformat()}", str(tmp_mp3)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        tmp_mp3.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=day_dir,
                                     prefix=f".{txt.name}.", delete=False) as handle:
        handle.write(script + "\n")
    # NamedTemporaryFile creates files as 0600; give the script the same
    # permissions as any other new file (normally 0644).
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(handle.name, 0o666 & ~umask)
    os.replace(handle.name, txt)
    os.replace(tmp_mp3, mp3)
    return mp3


def prune_old(config: Config, today: dt.date) -> None:
    """Delete episodes and scripts older than keep_days.

    Only news-YYYY-MM-DD.mp3/.txt files inside YYYY-MM-DD day folders are
    removed; a day folder is then removed only if nothing else is left in it.
    """
    if config.keep_days == 0 or not config.output_dir.is_dir():
        return
    cutoff = today - dt.timedelta(days=config.keep_days)
    for day_dir in config.output_dir.iterdir():
        if not DAY_FOLDER.match(day_dir.name) or day_dir.is_symlink() or not day_dir.is_dir():
            continue
        try:
            if dt.date.fromisoformat(day_dir.name) >= cutoff:
                continue
        except ValueError:
            continue
        for path in day_dir.iterdir():
            match = EPISODE_NAME.match(path.name)
            if match and match.group(1) == day_dir.name and path.is_file() \
                    and not path.is_symlink():
                path.unlink()
                log.info("deleted old episode file %s", path)
        if not any(day_dir.iterdir()):
            day_dir.rmdir()


def command_run(args: argparse.Namespace) -> int:
    """Make today's episode, or print its script with --dry-run."""
    config = load_config(args.config)
    for tool in (config.piper_command, "ffmpeg") if not args.dry_run else ():
        if not shutil.which(tool):
            raise ConfigError(f"required program not found on PATH: {tool}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(CACHE_DIR / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("another news-podcast run is already in progress")
        return EXIT_BUSY

    now = dt.datetime.now(dt.timezone.utc)
    segments = build_script(config, now, use_agy=not args.headlines_only)
    script = "\n\n".join(text for _, text in segments)
    if args.dry_run:
        for voice, text in segments:
            print(f"--- voice: {voice} ---\n{text}\n")
        return 0

    today = now.astimezone().date()
    with tempfile.TemporaryDirectory(prefix="news-podcast-") as workdir:
        wav = synthesize(segments, config, Path(workdir))
        mp3 = save_episode(wav, script, config, today)
    prune_old(config, today)
    log.info("saved %s (%d words)", mp3, len(script.split()))
    return 0


def command_install(_args: argparse.Namespace) -> int:
    """Seed the live config and install and enable the systemd user timer."""
    if CONFIG_PATH.exists():
        log.info("keeping existing config %s", CONFIG_PATH)
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONFIG_TEMPLATE, CONFIG_PATH)
        log.info("created config %s", CONFIG_PATH)

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    for name in UNIT_NAMES:
        text = (SYSTEMD_TEMPLATE_DIR / name).read_text(encoding="utf-8")
        text = text.replace("@SCRIPT@", str(Path(__file__).resolve()))
        (SYSTEMD_USER_DIR / name).write_text(text, encoding="utf-8")
        log.info("installed %s", SYSTEMD_USER_DIR / name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "news-podcast.timer"], check=True)
    log.info("daily timer enabled; check it with: systemctl --user list-timers news-podcast.timer")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="news_podcast.py",
        description="Turn the day's news into a spoken MP3 briefing with agy and Piper.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = commands.add_parser("run", help="make today's episode now",
                              description="Fetch the news, write the script and save the MP3.")
    run.add_argument("-c", "--config", type=Path, default=CONFIG_PATH,
                     help=f"config file to use (default: {CONFIG_PATH})")
    run.add_argument("-n", "--dry-run", action="store_true",
                     help="print the script with its voices instead of recording audio; "
                          "saves and deletes nothing")
    run.add_argument("-H", "--headlines-only", action="store_true",
                     help="skip agy and read the headlines as published")
    run.set_defaults(handler=command_run)

    install = commands.add_parser(
        "install", help="create the config and enable the daily 6 AM timer",
        description=f"Copy config.ini.example to {CONFIG_PATH} if it does not exist yet, "
                    f"then install and enable the systemd user timer.")
    install.set_defaults(handler=command_install)
    return parser


def main() -> int:
    """Parse arguments and run the chosen command."""
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return 0
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 2
    ensure_venv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m/%d/%Y %I:%M:%S %p")
    try:
        return args.handler(args)
    except (ConfigError, RuntimeError, subprocess.CalledProcessError) as error:
        log.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
