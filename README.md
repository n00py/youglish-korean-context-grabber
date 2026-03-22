# YouGlish Korean Context Grabber

An Anki desktop add-on for Korean vocabulary and sentence cards that reads a Korean term from a note field, fetches real YouGlish examples, and opens a read-only viewer for transcript preview and raw sentence-audio playback.

## Features

- Adds an editor button labeled `YouGlish Context`
- Adds `YouGlish Context` to the reviewer `More` menu during card review
- Adds a browser bulk action labeled `Fetch YouGlish Context`
- Reads the Korean search term from a configurable source field
- Fetches multiple candidate YouGlish examples and ranks them for study-friendly use
- Shows a read-only viewer for each note with:
  - Korean sentence/context
  - match highlighting when possible
  - source title when available
  - timestamp
  - URL
  - duplicate warnings
- Uses YouGlish for transcript text and timestamps, then extracts a local sentence audio clip with `yt-dlp` and `ffmpeg`
- Automatically loops through browser cookies it can find and stops at the first browser that yields a usable YouTube audio stream
- Plays the extracted sentence clip inline when Qt multimedia is available
- Lets you explicitly append a liked clip into the note's `Sound` field when that field exists
- Handles selected browser notes one-by-one with a viewer dialog for each note
- Does not edit note fields unless you explicitly click `Append to Sound`
- Logs provider and playback errors to `user_files/youglish_context.log`

## Intended Anki Use

Designed for current Anki desktop add-on environments using Python and Qt/PyQt APIs exposed by Anki.

For the raw-audio path, the host machine also needs:

- `yt-dlp`
- `ffmpeg`
- at least one supported browser with usable YouTube cookies, typically Firefox, Chrome, or Safari

## Install

Copy the add-on folder into Anki's `addons21` directory, then restart Anki.

Example:

```text
addons21/
  youglish_korean_context_grabber/
    __init__.py
    manifest.json
    config.json
    README.md
    IMPLEMENTATION_NOTE.md
    config/
    provider/
    services/
    ui/
    user_files/
```

## Default Config

```json
{
  "source_field_name": "Korean",
  "sound_field_name": "Sound",
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

- `source_field_name`
  - field used to read the Korean search term
- `max_candidates`
  - capped to the add-on UI range of `3` to `10`
- `sound_field_name`
  - field that receives appended `[sound:...]` tags when you click `Append to Sound`
- `exact_match_bias`
  - favors candidates containing the exact query text
- `exact_match_only`
  - rejects candidates that do not contain the exact query text
- `max_sentence_length`
  - filters out long subtitle lines
- `duplicate_detection_enabled`
  - marks candidates whose transcript already appears in another note
- `provider_order`
  - provider preference order
- `request_timeout_seconds`
  - HTTP timeout for the fallback adapter
- `user_agent`
  - request header for the fallback adapter

## Provider Integration

The add-on depends on `BaseContextProvider` rather than talking directly to YouGlish from the UI.

- `OptionalScrapeFallbackProvider`
  - preferred adapter for current builds
  - parses the server-rendered bootstrap payload from the YouGlish Korean search page
- `YouGlishProvider`
  - secondary adapter
  - uses YouGlish's documented JavaScript widget when Qt web engine support is available in Anki

This split is intentional so the fetch layer can be adjusted later without rewriting the note update or UI flows.
This split is intentional so the fetch layer can be adjusted later without rewriting the viewer flow.

## Known Limitations

- Human-readable source titles are not always available from current public YouGlish payloads.
- The HTML fallback is limited by whatever candidates YouGlish includes in the bootstrap payload for a search page.
- Raw audio extraction depends on `yt-dlp`, `ffmpeg`, and usable browser cookies for YouTube on the local machine.
- Inline playback of the extracted clip depends on the Qt multimedia components available in your Anki build.
- Browser bulk mode is still one-note-at-a-time and depends on network availability and YouGlish access limits.
- Heavy usage may hit YouGlish daily limits, especially on free plans.

## Runtime Files

The add-on writes logs under `user_files/`:

- `youglish_context.log`

## Development Notes

Focused test coverage is included for:

- ranking logic
