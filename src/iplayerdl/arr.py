import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ArrInstance:
    name: str
    url: str
    api_key: str


@dataclass
class ParsedEpisode:
    season_number: int
    episode_number: int
    title: str


@dataclass
class ParseResult:
    instance: str
    kind: str  # "series" or "movie"
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: int | None
    episodes: list[ParsedEpisode]


def load_instance(name: str) -> ArrInstance:
    url = os.getenv(f"{name}_URL")
    api_key = os.getenv(f"{name}_API_KEY")
    if not url or not api_key:
        raise ValueError(f"Missing {name}_URL / {name}_API_KEY in environment")
    return ArrInstance(name, url.rstrip("/"), api_key)


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca_file = os.getenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    if Path(ca_file).exists():
        ctx.load_verify_locations(cafile=ca_file)
    return ctx


def _get(instance: ArrInstance, path: str, params: dict | None = None) -> dict:
    url = f"{instance.url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Api-Key": instance.api_key})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
        return json.load(resp)


def _post(instance: ArrInstance, path: str, body: dict) -> dict:
    url = f"{instance.url}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-Api-Key": instance.api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
        return json.load(resp)


def get_series(instance: ArrInstance) -> list[dict]:
    return _get(instance, "/api/v3/series")


def get_movies(instance: ArrInstance) -> list[dict]:
    return _get(instance, "/api/v3/movie")


def parse(instance: ArrInstance, title: str) -> ParseResult | None:
    try:
        data = _get(instance, "/api/v3/parse", {"title": title})
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return None
        raise
    if not data or ("movie" not in data and "series" not in data):
        return None
    if "movie" in data:
        movie = data["movie"]
        return ParseResult(
            instance=instance.name,
            kind="movie",
            title=movie["title"],
            year=movie.get("year"),
            imdb_id=movie.get("imdbId"),
            tmdb_id=movie.get("tmdbId"),
            episodes=[],
        )
    series = data["series"]
    episodes = [
        ParsedEpisode(
            season_number=ep["seasonNumber"],
            episode_number=ep["episodeNumber"],
            title=ep["title"],
        )
        for ep in data.get("episodes", [])
    ]
    return ParseResult(
        instance=instance.name,
        kind="series",
        title=series["title"],
        year=series.get("year"),
        imdb_id=None,
        tmdb_id=None,
        episodes=episodes,
    )


def series_lookup(instance: ArrInstance, term: str) -> dict | None:
    results = _get(instance, "/api/v3/series/lookup", {"term": term})
    return results[0] if results else None


def _require_first(instance: ArrInstance, path: str, label: str) -> dict:
    items = _get(instance, path)
    if not items:
        raise ValueError(f"No {label} configured in {instance.name} at {instance.url}{path} — configure one first")
    return items[0]


def add_series(
    instance: ArrInstance, lookup_result: dict, monitored: bool = False
) -> dict:
    quality = _require_first(instance, "/api/v3/qualityprofile", "quality profiles")
    root = _require_first(instance, "/api/v3/rootfolder", "root folders")
    body = {
        "title": lookup_result["title"],
        "tvdbId": lookup_result["tvdbId"],
        "qualityProfileId": quality["id"],
        "rootFolderPath": root["path"],
        "monitored": monitored,
        "seasons": [],
        "addOptions": {"monitor": "none", "searchForMissingEpisodes": False},
    }
    return _post(instance, "/api/v3/series", body)


def refresh_series(instance: ArrInstance, series_id: int) -> None:
    try:
        _post(
            instance,
            "/api/v3/command",
            {"name": "RefreshSeries", "seriesId": series_id},
        )
    except urllib.error.HTTPError:
        pass


def _delete(instance: ArrInstance, path: str) -> None:
    req = urllib.request.Request(
        f"{instance.url}{path}",
        headers={"X-Api-Key": instance.api_key},
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()):
        pass


def delete_series(instance: ArrInstance, series_id: int, delete_files: bool = False) -> None:
    suffix = "?deleteFiles=true" if delete_files else ""
    _delete(instance, f"/api/v3/series/{series_id}{suffix}")


def delete_movie(instance: ArrInstance, movie_id: int, delete_files: bool = False) -> None:
    suffix = "?deleteFiles=true" if delete_files else ""
    _delete(instance, f"/api/v3/movie/{movie_id}{suffix}")


def wait_for_episodes(
    instance: ArrInstance, series_id: int, attempts: int = 15, delay: float = 2.0
) -> list[dict]:
    episodes: list[dict] = []
    for _ in range(attempts):
        try:
            episodes = series_episodes(instance, series_id)
        except Exception as e:  # noqa: BLE001 — transient API failure must not crash pipeline
            logger.warning(
                "wait_for_episodes failed for series %s (attempt %d/%d): %s: %s",
                series_id,
                _ + 1,
                attempts,
                type(e).__name__,
                e,
            )
            time.sleep(delay)
            continue
        if any(e.get("episodeNumber") is not None for e in episodes):
            return episodes
        time.sleep(delay)
    return episodes


def series_episodes(instance: ArrInstance, series_id: int) -> list[dict]:
    return _get(instance, "/api/v3/episode", {"seriesId": series_id})


def movie_lookup(instance: ArrInstance, term: str) -> dict | None:
    results = _get(instance, "/api/v3/movie/lookup", {"term": term})
    return results[0] if results else None


def add_movie(
    instance: ArrInstance, lookup_result: dict, monitored: bool = False
) -> dict:
    quality = _require_first(instance, "/api/v3/qualityprofile", "quality profiles")
    root = _require_first(instance, "/api/v3/rootfolder", "root folders")
    body = {
        "title": lookup_result["title"],
        "tmdbId": lookup_result["tmdbId"],
        "qualityProfileId": quality["id"],
        "rootFolderPath": root["path"],
        "monitored": monitored,
        "addOptions": {"searchForMovie": False},
    }
    return _post(instance, "/api/v3/movie", body)


def get_external_ids(
    instance: ArrInstance, kind: str, arr_id: int
) -> tuple[str | None, int | None]:
    """Returns (imdb_id, tmdb_id) via the item's metadata."""
    if kind == "series":
        data = _get(instance, f"/api/v3/series/{arr_id}")
    else:
        data = _get(instance, f"/api/v3/movie/{arr_id}")
    return data.get("imdbId"), data.get("tmdbId")


def parse_any(services: list[ArrInstance], title: str) -> ParseResult | None:
    for svc in services:
        result = parse(svc, title)
        if result is not None:
            return result
    return None
