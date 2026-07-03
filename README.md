# iplayerDL

A yt-dlp and ffmpeg wrapper designed to make downloading from BBC iPlayer easy.

By default, downloads best feed available and then transcodes with ffmpeg to given quality level

Set `pipeline.max_non_transcoded` in `config.toml` to limit how many full-quality
downloads can be waiting for transcode/move at once. This is useful when
`delete_downloads = true` and disk space is tight.

## Gettings Started

1. Create .env file with TMD_API_KEY=
2. Adjust [config.toml] with desired urls and values
3. Run:

```python
uv sync
uv run -m iplayerdl.main
```
