# iplayerDL

A yt-dlp and ffmpeg wrapper designed to make downloading from BBC iPlayer easy.

By default, downloads best feed available and then transcodes with ffmpeg to given quality level

Set `pipeline.max_non_transcoded` in `config.toml` to limit how many full-quality
downloads can be waiting for transcode/move at once. This is useful when
`delete_downloads = true` and disk space is tight.

By default the resolver only matches against your existing Sonarr/Radarr libraries
(and TMDb as a fallback). Set `pipeline.allow_speculative_adds = true` in
`config.toml` to let it add missing shows/movies to Sonarr/Radarr (rolling back if
no matching episode is found).

## Gettings Started

1. Run any command once to create the settings file at
   `$XDG_CONFIG_HOME/iplayerdl/config.toml` (normally `~/.config/iplayerdl/config.toml`).
   An existing repo `config.toml` is copied there automatically, and values from a legacy
   `.env` are migrated into the new `[environment]` section. Set `IPLAYERDL_CONFIG` to
   override the location.
2. Fill in `[environment]` in `config.toml` (e.g. `TMDB_API_KEY`, `RADARR_API_KEY`) — this
   replaces the old `.env` file. See `example/config.toml` and
   `example/iplayerdl.env` for annotated starting points (secrets belong
   in the env file, never in git or the Nix store).
3. Adjust the rest of `config.toml` by hand or via the web interface.
4. Run:

```python
uv sync
uv run -m iplayerdl.main run <url> ...  # queue the URLs, then process the queue
uv run -m iplayerdl.main web             # web interface (bind via [web] or --host/--port)
```

## Queue

The queue lives in a sqlite database next to `config.toml` (`queue.db`; override with
`$IPLAYERDL_DB`). Items can be added at any time — from the CLI or the web UI — and
survive restarts. Draining a run does not delete anything, so past items stay visible
with their final status.

```bash
iplayerdl queue add <url> ...            # queue without running
iplayerdl queue add <url> --run          # queue and process now
iplayerdl queue list                     # queued items plus recent history
iplayerdl queue list --status failed     # filter by status
iplayerdl queue cancel <id> ...          # cancel queued/running items
iplayerdl queue retry <id> ...           # put finished items back on the queue
iplayerdl queue remove <id> ... | --all  # delete rows
iplayerdl queue clear [--keep N]         # drop finished rows, keeping N newest
iplayerdl run                            # process everything queued
```

`iplayerdl run <url> ...` adds the given URLs first, then processes the whole queue, so
it can be used exactly like the old one-shot form.

## Metadata overrides

Ambiguous titles (several remakes, similarly named shows) can be pinned to a specific
TMDb entry when queueing. The picker searches TMDb with your query and lists the results
to choose from; a pinned item bypasses the Sonarr/Radarr fuzzy matching entirely and
resolves against that TMDb id. If the pin cannot produce a match, resolution falls back
to the normal chain rather than failing.

```bash
iplayerdl run <url> --search "Doctor Who 2005"    # search, pick, queue, then run
iplayerdl queue add <url> --search "Chicken Run"  # queue only
iplayerdl search "Doctor Who 2005"                # just browse TMDb results
```

The same picker is available per item in the web UI (the magnifier button on a queue row).

## Web interface

```bash
uv run -m iplayerdl.main web
```

- Edit the whole `config.toml` behind the gear icon (validated as TOML before saving)
- Queue one URL with the **Queue** button. Downloading starts automatically as soon as a
  download slot is free; if one is already downloading, the URL waits and is picked up
  automatically when that run finishes. The row under the button shows which it is.
- Optionally pick a metadata override first: search TMDb and choose the exact show or film.
  The choice is saved with the URL and shown as a chip until you queue it.
- Watch per-item progress bars (resolving/downloading with % and MB/transcoding/done/
  failed), the queued items and finished history with their status, and live pipeline output.
  A row shows just the title when you configured the metadata, and the link plus the matched
  title when it is being detected automatically.
- Pin, retry, cancel or remove any item; the magnifier on a queued row reopens the TMDb
  search to change its override

Downloads are pipelined: the next item starts downloading while the previous one is still
transcoding. `pipeline.max_non_transcoded` caps how many full-quality files may be waiting
for transcode at once, so set it low on a tight disk — with `1` each download waits for the
previous transcode to finish.

The server binds to `127.0.0.1` by default; it has no authentication, so do not expose it
to untrusted networks without putting auth in front of it.
