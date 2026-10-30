# news-podcast

Every morning, news-podcast turns the day's news into a spoken MP3 briefing.
It reads RSS feeds, has the [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli)
(`agy`) write a newsreader-style script for each section, and records it
with [Piper](https://github.com/rhasspy/piper) text-to-speech. Each section
can use its own voice.

The default setup covers Hot Springs and Arkansas, US and world news, and
technology. It makes a briefing of about 20 minutes at 6:00 AM each day and
saves it as `~/Podcasts/news/YYYY-MM-DD/news-YYYY-MM-DD.mp3`, with the script beside it
as a `.txt` file.

## How it works

1. **Fetch:** reads every feed in the config and keeps stories from the last
   24 hours, removing duplicates.
2. **Write:** sends each section's headlines and feed summaries to
   `agy -p` with instructions to use only those facts. If `agy` fails, that
   section reads the headlines as published instead and says so aloud.
3. **Record:** Piper reads the opening and closing and each section with its
   chosen voice, and the parts are joined and encoded to MP3 with `ffmpeg`.
4. **Tidy:** deletes episodes older than 30 days.

`agy` only sees what the feeds provide (headline plus a short summary). It
does not read full articles.

## Requirements

- Linux with systemd (for the daily timer)
- Python 3.10 or newer, with venv support (`python3-venv` on Debian/Ubuntu)
- [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli),
  signed in (run `agy` once interactively first)
- [Piper](https://github.com/rhasspy/piper) on `PATH`, plus the voice models
  named in the config
- `ffmpeg`

## Install

```bash
git clone https://github.com/bonelifer/news-podcast.git
cd news-podcast
./news_podcast.py install
```

`install` copies `config.ini.example` to `~/.config/news-podcast/config.ini`
(only if that file does not exist yet), installs the systemd user service
and timer, and enables the timer. The first run also creates the
project's own `.venv` and installs its one Python dependency.

## Usage

```bash
./news_podcast.py run
```

Makes today's episode now. Useful options:

| Option | What it does |
|--------|--------------|
| `-n`, `--dry-run` | Print the script with its voices; record, save and delete nothing |
| `-H`, `--headlines-only` | Skip `agy` and read the headlines as published |
| `-c`, `--config FILE` | Use a different config file |

Running with no arguments shows help. Check the timer and the last run with:

```bash
systemctl --user list-timers news-podcast.timer
journalctl --user -u news-podcast.service -n 50
```

If the computer is off at 6:00 AM, the timer runs once at the next boot.
The timer only runs while you are logged in, unless you enable lingering
with `loginctl enable-linger`.

## Configuration

Everything lives in `~/.config/news-podcast/config.ini`; the comments in
[`config.ini.example`](config.ini.example) explain each setting. In short:

- `[podcast]`: output folder, days to keep, target length, lookback window
- `[voice: Name]`: Piper voice models (all must share one sample rate)
- `[section: Name]`: the episode's segments, in order, each with a `voice`
  and a list of feed URLs (`URL | Spoken name` sets the outlet name read
  aloud)
- `[agy]`: the `agy` command, its time limit, and the models to try in
  order (the next one is used if one fails, for example when its weekly
  allowance runs out)
- `[pronunciations]`: local names Piper says wrong and a spelling that fixes
  them, such as `Saline = Say-lean` (only the audio changes); a
  `[pronunciations: Voice]` block overrides it for one voice

Every episode uses some of your Antigravity usage allowance: one `agy`
request per section. Check what is left with `agy -p /usage`.

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/bonelifer/news-podcast/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/bonelifer/news-podcast/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- Code and documentation written with [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
