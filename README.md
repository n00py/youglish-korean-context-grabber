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
- Can show an English translation for the selected sentence through DeepL Free when a local API key file is present
- Adds a `YouGlish Context Settings...` entry under `Tools` so users can save or clear a DeepL key inside Anki
- Automatically loops through browser cookies it can find and stops at the first browser that yields a usable YouTube audio stream
- Plays the extracted sentence clip inline when Qt multimedia is available
- Lets you refetch the current query with a larger result count from a dropdown in the viewer
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
  "translation_enabled": true,
  "translation_provider": "deepl_free",
  "translation_target_language": "EN-US",
  "translation_timeout_seconds": 15,
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

### Active options in the shipped `config.json`

- `source_field_name`
  - Default: `"Korean"`
  - What it does: tells the add-on which note field to read when it needs the search term.
  - When it matters: every fetch starts here in editor mode, browser mode, and review mode.
  - Good reason to change it: your Korean text lives in a field called something like `Expression`, `Target`, or `Sentence`.
  - Failure mode if wrong: the add-on will tell you the source field is missing or empty.

- `sound_field_name`
  - Default: `"Sound"`
  - What it does: tells the `Append to Sound` action where to append the imported `[sound:...]` tag.
  - Current behavior: it appends without overwriting existing audio, and new clips go on a new line.
  - Good reason to change it: your audio field is named `Audio`, `Pronunciation`, `Native Audio`, or something else.
  - Failure mode if wrong: the `Append to Sound` button stays disabled or reports that the field is missing.

- `translation_enabled`
  - Default: `true`
  - What it does: allows the viewer to fetch and show an English translation for the selected Korean sentence.
  - Important detail: this still requires a local DeepL API key file; enabling it does not store credentials in the repo config.
  - Good reason to disable it: you do not want translation requests at all.

- `translation_provider`
  - Default: `"deepl_free"`
  - What it does: selects the translation backend.
  - Current status: only `deepl_free` is implemented right now.

- `translation_target_language`
  - Default: `"EN-US"`
  - What it does: tells DeepL which English target to produce.
  - Good alternatives: `EN-GB` if you prefer British spelling and phrasing.

- `translation_timeout_seconds`
  - Default: `15`
  - What it does: HTTP timeout for the translation request.
  - Good reason to increase it: your network is slow.
  - Good reason to decrease it: you want translation failures to surface faster.

- `max_candidates`
  - Default: `5`
  - What it does: controls how many ranked YouGlish candidates the viewer shows for a search.
  - Effective range: the add-on clamps this to `3` through `20`, even if you enter something outside that range.
  - Viewer behavior: the picker also has a dropdown that lets you refetch the current query with any value from `3` to `20` without editing the config first.
  - Lower values: faster to scan, less clutter.
  - Higher values: more chances to find a good sentence, but more noise.

- `exact_match_bias`
  - Default: `true`
  - What it does: adds a ranking bonus to candidates that contain the exact query text.
  - Important detail: this does not filter anything by itself; it just pushes exact matches higher.
  - Good reason to disable it: you want broader contextual examples, including conjugated or nearby subtitle variants.

- `exact_match_only`
  - Default: `false`
  - What it does: filters out candidates that do not contain the exact query string.
  - Important detail: this is stricter than `exact_match_bias`.
  - Good reason to enable it: you only want literal hits for the exact Korean form on your card.
  - Tradeoff: you may get fewer results or no results for inflected words, spacing variants, or noisier subtitles.

- `max_sentence_length`
  - Default: `120`
  - What it does: filters out very long subtitle lines before ranking.
  - Unit: approximate character count of the sentence text returned by the provider.
  - Lower values: cleaner, shorter, more study-friendly lines.
  - Higher values: allows longer subtitle chunks, but increases clutter and subtitle noise.

- `duplicate_detection_enabled`
  - Default: `true`
  - What it does: checks whether a transcript sentence already appears in another note and flags duplicates in the viewer.
  - Important detail: it warns and annotates; it does not block playback or selection.
  - Good reason to disable it: you want slightly faster lookups or do not care about transcript reuse across notes.

- `provider_order`
  - Default: `["scrape_fallback", "youglish_widget"]`
  - What it does: controls which YouGlish provider adapter is tried first.
  - `scrape_fallback`: currently the most reliable default in this add-on; it parses the YouGlish page/bootstrap payload.
  - `youglish_widget`: the alternate adapter based on the YouGlish widget path.
  - Good reason to change it: only if you are troubleshooting provider behavior or experimenting with a different fetch path.

- `request_timeout_seconds`
  - Default: `12`
  - What it does: sets the HTTP timeout for the YouGlish fetch layer.
  - Important detail: this is for provider requests, not for `yt-dlp` audio extraction and not for `ffmpeg`.
  - Lower values: fail faster on weak network conditions.
  - Higher values: can help on slow connections, but also makes bad requests hang longer before erroring.

- `user_agent`
  - Default: `"Anki YouGlish Korean Context Grabber/0.1"`
  - What it does: sets the `User-Agent` header used by the fallback provider HTTP requests.
  - Good reason to change it: almost none for normal use.
  - Best practice: leave this alone unless you are debugging provider-specific request behavior.

### Parsed by the code, but not part of the current recommended workflow

These come from the earlier note-writing version of the add-on. They are still parsed for compatibility, but the current UI does not use them in normal operation.

- `destination_fields`
  - Default:
    ```json
    {
      "sentence": "Context Sentence",
      "source": "Context Source",
      "url": "Context URL",
      "timestamp": "Context Timestamp",
      "translation": "Context Translation"
    }
    ```
  - Original purpose: map fetched data into note fields when writing context directly back to cards.
  - Current status: not used by the current read-only viewer / `Append to Sound` flow.

- `overwrite_existing`
  - Default: `false`
  - Original purpose: allow overwriting protected destination fields when writing note content.
  - Current status: not used by the current workflow.

- `protected_fields`
  - Default:
    ```json
    [
      "Context Sentence",
      "Context Source",
      "Context URL",
      "Context Timestamp",
      "Context Translation"
    ]
    ```
  - Original purpose: list of fields that should not be overwritten unless explicitly allowed.
  - Current status: not used by the current workflow.

### Practical examples

- If your Korean text is stored in `Expression`, change:
  ```json
  {
    "source_field_name": "Expression"
  }
  ```

- If your note’s audio field is called `Audio`, change:
  ```json
  {
    "sound_field_name": "Audio"
  }
  ```

- If you only want literal matches and fewer lines:
  ```json
  {
    "exact_match_only": true,
    "max_candidates": 4,
    "max_sentence_length": 80
  }
  ```

## DeepL Key Setup

The DeepL API key is intentionally not stored in the tracked repo config.

The easiest setup is now inside Anki:

1. Open `Tools`
2. Click `YouGlish Context Settings...`
3. Paste your DeepL key
4. Click `Save Key`

To enable translation on a local install, put your key in:

```text
addons21/9834512704/user_files/deepl_api_key.txt
```

The file should contain only the key text on one line.

Example:

```text
xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx
```

Because `user_files/` is ignored by git in this repo, that key file will stay local-only unless you manually force-add it.

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
