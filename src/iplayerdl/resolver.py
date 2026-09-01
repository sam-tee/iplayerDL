import logging
import re
import urllib.error
from difflib import SequenceMatcher, get_close_matches
from functools import cache

from iplayerdl import arr
from iplayerdl.info import get_media_name as tmdb_media_name

logger = logging.getLogger(__name__)


@cache
def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_filename(s: str) -> str:
    return s.replace("/", "-").replace(":", " -").replace("?", "").strip().rstrip(".")


def split_title(title: str) -> tuple[str, int | None, str]:
    parts = title.split(", ", maxsplit=2)
    if len(parts) == 3:
        m = re.search(r"(?:Series|Season)\s+(\d+)", parts[1])
        season = int(m.group(1)) if m else None
        return parts[0], season, parts[2]
    return parts[0], None, parts[-1]


def part_number(ep_name: str) -> int | None:
    m = re.search(r"\((\d+)\)\s*$", ep_name)
    if m:
        return int(m.group(1))
    words = {"one": 1, "two": 2, "three": 3}
    m = re.search(r"[Pp]art\s+(\w+)\s*$", ep_name)
    if m and m.group(1).lower() in words:
        return words[m.group(1).lower()]
    return None


def year_range(text: str) -> tuple[int, int] | None:
    m = re.search(r"\((\d{4})[–—-](\d{4})\)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def has_year_hint(text: str) -> bool:
    return re.search(r"\(\d{4}([–—-]\d{4})?\)", text) is not None


def pick_series(candidates: list[dict], show_text: str) -> dict | None:
    """Choose best library series for a show name, using year hints when present."""
    if not candidates:
        return None
    target = norm(re.sub(r"\s*\(.*?\)\s*", " ", show_text))
    scored = [
        (
            SequenceMatcher(
                None, target, norm(re.sub(r"\s*\(\d{4}\)\s*$", "", c["title"]))
            ).ratio(),
            c,
        )
        for c in candidates
    ]
    top = max(r for r, _ in scored)
    finalists = [c for r, c in scored if r >= top - 0.05]
    rng = year_range(show_text)
    if len(finalists) > 1 and rng is not None:
        in_range = [c for c in finalists if rng[0] <= (c.get("year") or 0) <= rng[1]]
        if in_range:
            finalists = in_range
    ratio_by_id = {c["id"]: r for r, c in scored}
    # When year hints are absent and scores tie, don't arbitrarily prefer
    # the newer remake — keep deterministic (first-by-ratio) ordering.
    if rng is None and len(finalists) > 1:
        return max(finalists, key=lambda c: ratio_by_id[c["id"]])
    return max(finalists, key=lambda c: (ratio_by_id[c["id"]], c.get("year") or 0))


def match_episode(
    episodes: list[dict], season: int | None, ep_name: str
) -> tuple[dict, float] | None:
    face = re.fullmatch(r"[Ee]pisode\s+(\d+)", ep_name.strip())
    if face:
        num = int(face.group(1))
        want_season = season or 0
        ep = next(
            (
                e
                for e in episodes
                if e["seasonNumber"] == want_season and e["episodeNumber"] == num
            ),
            None,
        )
        if ep is not None:
            return ep, 1.0
    want_part = part_number(ep_name)
    pool = [
        e
        for e in episodes
        if season is None or season == 0 or e["seasonNumber"] == season
    ]
    best, best_ratio = None, 0.0
    for ep in pool:
        r = SequenceMatcher(None, norm(ep["title"]), norm(ep_name)).ratio()
        if want_part is not None and part_number(ep["title"]) == want_part:
            r = min(1.0, r + 0.5)
        if r > best_ratio:
            best, best_ratio = ep, r
    if best is None or best_ratio < 0.6:
        return None
    return best, best_ratio


def resolve_series(
    show: str,
    season: int | None,
    ep_name: str,
    trace: list | None = None,
    allow_adds: bool = False,
) -> str | None:
    try:
        sonarr = arr.load_instance("SONARR")
        library = arr.get_series(sonarr)
    except (urllib.error.URLError, ValueError, OSError) as e:
        if trace is not None:
            trace.append(f"sonarr:load failed: {type(e).__name__}: {e}")
        logger.warning("resolve_series load failed for %r: %s: %s", show, type(e).__name__, e)
        return None
    except Exception as e:  # noqa: BLE001 — catch JSON/timeout etc
        if trace is not None:
            trace.append(f"sonarr:load failed: {type(e).__name__}: {e}")
        logger.warning("resolve_series load failed for %r: %s: %s", show, type(e).__name__, e)
        return None
    keys = list(library)
    matches = get_close_matches(
        norm(show), [norm(k["title"]) for k in keys], n=5, cutoff=0.6
    )
    candidates = [k for k in keys if norm(k["title"]) in matches]
    series = pick_series(candidates, show)
    added = False
    if series is None and not allow_adds:
        if trace is not None:
            trace.append(f"sonarr:not-in-library, speculative add disabled for {show!r}")
        return None
    if series is None:
        if trace is not None:
            trace.append(f"sonarr:not-in-library, looking up {show!r}")
        try:
            lookup = arr.series_lookup(sonarr, show)
            if lookup is None:
                if trace is not None:
                    trace.append(f"sonarr:no-lookup-result for {show!r}")
                return None
            series = arr.add_series(sonarr, lookup)
            arr.refresh_series(sonarr, series["id"])
        except (urllib.error.URLError, OSError, ValueError) as e:
            if trace is not None:
                trace.append(f"sonarr:lookup/add failed for {show!r}: {type(e).__name__}: {e}")
            logger.warning("sonarr lookup/add failed for %r: %s: %s", show, type(e).__name__, e)
            return None
        except Exception as e:  # noqa: BLE001 — JSON/timeout etc
            if trace is not None:
                trace.append(f"sonarr:lookup/add failed for {show!r}: {type(e).__name__}: {e}")
            logger.warning("sonarr lookup/add failed for %r: %s: %s", show, type(e).__name__, e)
            return None
        added = True
        if trace is not None:
            trace.append(f"sonarr:added {series['title']} (id={series['id']})")
    elif trace is not None:
        trace.append(f"sonarr:library-hit {series['title']} ({series.get('year')})")
    try:
        episodes = (
            arr.wait_for_episodes(sonarr, series["id"])
            if added
            else arr.series_episodes(sonarr, series["id"])
        )
    except Exception as e:  # noqa: BLE001 — transient API failure must not crash pipeline
        logger.warning(
            "resolve_series episodes fetch failed for %r (id=%s): %s: %s",
            show,
            series["id"],
            type(e).__name__,
            e,
        )
        if trace is not None:
            trace.append(f"sonarr:episodes fetch failed: {type(e).__name__}: {e}")
        if added:
            try:
                arr.delete_series(sonarr, series["id"])
                if trace is not None:
                    trace.append(f"sonarr:rolled-back speculative add {series['title']} (episodes fetch failed)")
            except Exception:  # noqa: BLE001, S110 — rollback best-effort
                pass
        return None
    matched = match_episode(episodes, season, ep_name)
    if matched is None:
        if trace is not None:
            trace.append(f"sonarr:no-episode-match for {ep_name!r}")
        if added:
            arr.delete_series(sonarr, series["id"])
            if trace is not None:
                trace.append(f"sonarr:rolled-back speculative add {series['title']}")
        return None
    ep, ratio = matched
    if trace is not None:
        trace.append(
            f"sonarr:episode S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"
            f" '{ep['title']}' (ratio {ratio:.2f})"
        )
    year = series.get("year")
    title_year = f"{series['title']} ({year})" if year else series["title"]
    title_year = clean_filename(title_year)
    s_num = ep["seasonNumber"]
    e_num = ep["episodeNumber"]
    season_dir = "Specials" if s_num == 0 else f"Season {s_num:02d}"
    stem = f"{title_year} - S{s_num:02d}E{e_num:02d} - {clean_filename(ep['title'])}"
    return f"tv/{title_year}/{season_dir}/{stem}"


def resolve_movie(
    title: str, trace: list | None = None, allow_adds: bool = False
) -> str | None:
    try:
        radarr = arr.load_instance("RADARR")
        movies = arr.get_movies(radarr)
    except (urllib.error.URLError, ValueError, OSError) as e:
        if trace is not None:
            trace.append(f"radarr:load failed: {type(e).__name__}: {e}")
        logger.warning("resolve_movie load failed for %r: %s: %s", title, type(e).__name__, e)
        return None
    except Exception as e:  # noqa: BLE001 — catch JSON/timeout etc
        if trace is not None:
            trace.append(f"radarr:load failed: {type(e).__name__}: {e}")
        logger.warning("resolve_movie load failed for %r: %s: %s", title, type(e).__name__, e)
        return None
    matches = get_close_matches(
        norm(title), [norm(m["title"]) for m in movies], n=1, cutoff=0.85
    )
    movie = None
    if matches:
        movie = next(m for m in movies if norm(m["title"]) == matches[0])
        if trace is not None:
            trace.append(f"radarr:library-hit {movie['title']} ({movie.get('year')})")
    elif allow_adds:
        if trace is not None:
            trace.append(f"radarr:not-in-library, looking up {title!r}")
        if not has_year_hint(title):
            if trace is not None:
                trace.append(f"radarr:skipped speculative add, no year hint in {title!r}")
            return None
        try:
            lookup = arr.movie_lookup(radarr, title)
            if lookup is None:
                return None
            movie = arr.add_movie(radarr, lookup)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if trace is not None:
                trace.append(f"radarr:lookup/add failed for {title!r}: {type(e).__name__}: {e}")
            logger.warning("radarr lookup/add failed for %r: %s: %s", title, type(e).__name__, e)
            return None
        except Exception as e:  # noqa: BLE001 — JSON/timeout etc
            if trace is not None:
                trace.append(f"radarr:lookup/add failed for {title!r}: {type(e).__name__}: {e}")
            logger.warning("radarr lookup/add failed for %r: %s: %s", title, type(e).__name__, e)
            return None
        if trace is not None:
            trace.append(f"radarr:added {movie['title']} (id={movie['id']})")
    else:
        if trace is not None:
            trace.append(f"radarr:not-in-library, speculative add disabled for {title!r}")
        return None
    year = movie.get("year")
    name = f"{movie['title']} ({year})" if year else movie["title"]
    name = clean_filename(name)
    return f"film/{name}/{name}"


def resolve_media_name(
    title: str,
    overrides: dict | None = None,
    trace: list | None = None,
    allow_adds: bool = False,
) -> str | None:
    overrides = overrides or {}
    title = overrides.get(title, title)
    show, season, ep_name = split_title(title)
    result = resolve_series(show, season, ep_name, trace, allow_adds)
    if result is not None:
        return result
    result = resolve_movie(show, trace, allow_adds)
    if result is not None:
        return result
    if trace is not None:
        trace.append("tmdb:fallback")
    return tmdb_media_name(title, overrides)
