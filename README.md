# BanGlish

BanGlish is an Anki desktop add-on for Korean study cards. It searches a BanGlish subtitle corpus first, falls back to YouGlish when needed, lets you preview real Korean subtitle lines, and can extract a sentence-level audio clip locally.

The project now has two parts:
- the Anki add-on UI and local playback workflow
- the BanGlish corpus builder and local API used as the primary context source

## Features

- Adds an editor button labeled `BanGlish Context`
- Adds `BanGlish Context` to the reviewer `More` menu during card review
- Adds a browser bulk action labeled `Fetch BanGlish Context`
- Reads the search term from a configurable source field
- Tries the BanGlish local corpus first and falls back to YouGlish only when BanGlish returns no results or is unavailable
- Shows a read-only viewer with:
  - Korean transcript text
  - match highlighting when possible
  - source title
  - timestamp
  - source/provider badge
  - duplicate warnings
- Plays a sentence-level audio clip locally using `yt-dlp` and `ffmpeg`
- Lets you copy the transcript text to the clipboard
- Lets you append a liked clip to the note’s `Sound` field without overwriting existing audio
- Can show an English translation through DeepL Free when a local API key is configured
- Includes a settings dialog under `Tools > BanGlish Context Settings...`
- Includes a BanGlish corpus builder and local HTTP API for Kimchi-backed subtitle search

## Current Corpus Strategy

The current BanGlish corpus builder:
- uses Kimchi Reader as the discovery source
- crawls learner-focused YouTube channels
- ingests episode metadata
- fetches Korean manual subtitle tracks only
- rejects auto captions
- stores subtitle cues in a local SQLite corpus

The current learner crawl uses:
- `POST /v2/media/browse/unified`
- `sources=["youtube_channel"]`
- `made_for="learner"`

## Runtime Requirements

### Add-on / playback

For transcript lookup and local sentence audio extraction, the host machine needs:
- Anki desktop
- `yt-dlp`
- `ffmpeg`
- at least one supported browser with usable YouTube cookies when YouTube requires them

### Corpus builder

For the BanGlish corpus path, the host machine also needs:
- outbound access to `api.kimchi-reader.app`
- outbound access to YouTube subtitle/media endpoints
- enough disk space for the SQLite DB and cached subtitle files

## Data Location

BanGlish stores runtime data outside the add-on folder:

- data dir: `~/Library/Application Support/BanGlish`
- corpus DB: `~/Library/Application Support/BanGlish/kimchi_corpus.sqlite3`
- subtitle cache: `~/Library/Application Support/BanGlish/kimchi_subtitles`
- audio cache: `~/Library/Application Support/BanGlish/audio_cache`
- log file: `~/Library/Application Support/BanGlish/banglish.log`

This keeps the large runtime corpus separate from the add-on source code.

## Install

Copy the add-on folder into Anki’s `addons21` directory, then restart Anki.

Example:

```text
addons21/
  9834512704/
    __init__.py
    manifest.json
    config.json
    README.md
    config/
    corpus/
    provider/
    services/
    ui/
```

## Default Config

```json
{
  "context_provider": "local_api",
  "source_field_name": "Korean",
  "sound_field_name": "Sound",
  "translation_enabled": true,
  "translation_provider": "deepl_free",
  "translation_target_language": "EN-US",
  "translation_timeout_seconds": 15,
  "local_api_base_url": "http://127.0.0.1:8765",
  "local_api_timeout_seconds": 5,
  "max_candidates": 5,
  "exact_match_bias": true,
  "exact_match_only": false,
  "max_sentence_length": 120,
  "duplicate_detection_enabled": true,
  "provider_order": [
    "scrape_fallback",
    "youglish_widget"
  ],
  "request_timeout_seconds": 12,
  "user_agent": "Anki YouGlish Korean Context Grabber/0.1"
}
```

## Config Options

### Active options in `config.json`

- `context_provider`
  - Default: `"local_api"`
  - BanGlish-local is the intended primary mode.
  - The add-on still favors the local BanGlish corpus first even if the legacy YouGlish setting is used.

- `source_field_name`
  - Default: `"Korean"`
  - The note field used as the search term.

- `sound_field_name`
  - Default: `"Sound"`
  - The field used by `Append to Sound`.
  - New clips are appended on a new line instead of overwriting existing audio.

- `translation_enabled`
  - Default: `true`
  - Enables translation display in the viewer.

- `translation_provider`
  - Default: `"deepl_free"`
  - Current implementation: DeepL Free only.

- `translation_target_language`
  - Default: `"EN-US"`
  - DeepL target language for the viewer translation.

- `translation_timeout_seconds`
  - Default: `15`
  - HTTP timeout for translation requests.

- `local_api_base_url`
  - Default: `"http://127.0.0.1:8765"`
  - The BanGlish local API base URL.

- `local_api_timeout_seconds`
  - Default: `5`
  - Timeout used for local API requests and health checks.

- `max_candidates`
  - Default: `5`
  - Number of candidates shown by default.
  - Effective clamp: `3` to `20`
  - The viewer can refetch with any value in that range from its dropdown.

- `exact_match_bias`
  - Default: `true`
  - Gives exact query matches a ranking bonus.

- `exact_match_only`
  - Default: `false`
  - Filters out candidates that do not contain the exact query string.

- `max_sentence_length`
  - Default: `120`
  - Filters out overly long subtitle lines before ranking.

- `duplicate_detection_enabled`
  - Default: `true`
  - Warns when a transcript sentence already appears on another note.

- `provider_order`
  - Default: `["scrape_fallback", "youglish_widget"]`
  - Controls YouGlish fallback provider order only.
  - BanGlish local search is still preferred first.

- `request_timeout_seconds`
  - Default: `12`
  - HTTP timeout for YouGlish provider requests.

- `user_agent`
  - Default: `"Anki YouGlish Korean Context Grabber/0.1"`
  - User agent for the legacy YouGlish fallback HTTP path.

### Legacy compatibility fields

These are still parsed for backward compatibility, but are not part of the current recommended workflow:
- `destination_fields`
- `overwrite_existing`
- `protected_fields`

They belong to the older note-writing flow and are not used by the current read-only viewer / append-to-sound workflow.

## Notes on Search Behavior

- BanGlish search prefers lower `complexity_score` items first.
- The viewer also favors shorter sentences.
- Korean dictionary-form searches now expand into a basic set of common conjugated surface forms.
- BanGlish results are labeled `BanGlish`.
- YouGlish fallback results are labeled `YouGlish`.

## Corpus CLI

The corpus builder can run outside Anki:

```bash
env PYTHONPATH=/path/to/anki python3 -m youglish_korean_context_grabber.corpus.cli --addon-dir "/path/to/addons21/9834512704" stats
```

Common commands:
- `backfill`
- `recheck-subtitles`
- `stats`
- `serve`

## Known Limitations

- BanGlish only indexes videos with usable Korean manual subtitle tracks.
- Videos with only auto captions or only hard-burned subtitles are intentionally excluded.
- Audio extraction still depends on YouTube access behavior, browser cookies, and local `yt-dlp`/`ffmpeg`.
- Some internal Python module/package names still reference the older `youglish_korean_context_grabber` package name for compatibility.

## Repository Naming Note

The repo and user-facing branding now use `BanGlish`, but the internal Python package name remains `youglish_korean_context_grabber` to avoid breaking the existing Anki add-on structure.
