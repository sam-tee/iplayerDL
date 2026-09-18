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
   replaces the old `.env` file.
3. Adjust the rest of `config.toml` by hand or via the web interface.
   (The URL queue is ephemeral: pass URLs as `run` arguments or paste
   them into the web UI — they are never stored.)
4. Run:

```python
uv sync
uv run -m iplayerdl.main run <url> ...  # download and process the given URLs
uv run -m iplayerdl.main web             # web interface (bind via [web] or --host/--port)
```

## Web interface

```bash
uv run -m iplayerdl.main web
```

- Edit the whole `config.toml` behind the gear icon (validated as TOML before saving)
- Paste one or more URLs on the front page — they are queued in memory
  only (gone on restart), optionally starting a download immediately
- Watch per-URL progress bars showing resolving/downloading (with % and MB)/transcoding/
  done/failed states, plus live pipeline output

The server binds to `127.0.0.1` by default; it has no authentication, so do not expose it
to untrusted networks without putting auth in front of it.
