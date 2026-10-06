import logging
import re

from tmdbv3api import TV, Movie, Search, Season, TMDb
from tmdbv3api.exceptions import TMDbException

from iplayerdl.classes import TmdbPin

tmdb = TMDb()

logger = logging.getLogger(__name__)

# Keep in sync with resolver.part_number — both normalise "Part One/Two/Three" → "(n)"
_PART_NUMBERS = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}


def sanitise(text: str) -> str:
    text = text.replace("’", "'")
    text = text.replace("？", "?")
    text = text.replace("：", ":")
    return re.sub(
        r"[,:\-]?\s*Part\s+(One|Two|Three|1|2|3)\b",
        lambda m: f"({_PART_NUMBERS[m.group(1).lower()]})",
        text,
        flags=re.IGNORECASE,
    )


def _year(date_str: str | None) -> str:
    return (date_str or "").split("-")[0]


def title2show_data(title: str, overrides: dict) -> dict[str, str]:
    """
    Converts BBC title of form <Show Name>, Series <>, <Episode Name>
    to show_data dict with keys show_name, series_num, episode_name
    """
    title = overrides.get(title, title)
    parts = title.split(", ", maxsplit=2)
    if len(parts) == 3:
        series = parts[1].split(" ")
        return {
            "show_name": parts[0],
            "series_num": series[-1] if series[-1].isnumeric() else "0",
            "episode_name": sanitise(parts[2]),
        }
    else:
        return {
            "show_name": parts[0],
            "series_num": "0",
            "episode_name": sanitise(parts[-1]),
        }


def find_series(show_data: dict) -> str | None:
    """
    Maps dict with keys: {show_name, series_num, episode_name} -> str of Jellyfin style mapping
    """
    tv = TV()
    season = Season()
    search_results = tv.search(show_data["show_name"])
    if search_results.total_results == 0:
        # no matches found for tv show
        return None
    results = list(search_results.results)[:3]
    for show in results:
        try:
            episodes = season.details(show.id, show_data["series_num"]).episodes
        except TMDbException:
            episodes = []
        try:
            specials = season.details(show.id, 0).episodes
        except TMDbException:
            specials = []
        for ep in list(episodes) + list(specials):
            ep_name_comp, ep_name = (
                str(ep.name).lower(),
                show_data["episode_name"].lower(),
            )
            if "episode" in ep_name and ep_name != ep_name_comp:
                continue
            if (ep_name in ep_name_comp) or (ep_name_comp in ep_name):
                year = _year(show.first_air_date)
                name_year = f"{show.name} ({year})" if year else show.name
                s_num = ep.season_number
                season_dir = f"Season {s_num:02d}" if s_num != 0 else "Specials"
                return (
                    f"tv/{name_year}/{season_dir}/"
                    f"{name_year} - S{s_num:02d}E{ep.episode_number:02d} - {ep.name}"
                )


def find_movie(title: str) -> str | None:
    """
    Takes in title of film and returns Jellyfin style partial mapping with film and year
    If multiple films have same title and first result is not 5x more popular then returns
        without year
    """
    movie = Movie()
    search_results = movie.search(title)

    if search_results.total_results == 0:
        return None
    if search_results.total_results == 1:
        title = search_results[0].title
    elif search_results[0].title == search_results[1].title:
        # if top two have same title then multiple of the same film exist;
        # only trust the first result's identity if it is far more popular,
        # otherwise return without a year so the choice can be made later
        m1 = movie.details(search_results[0].id)
        m2 = movie.details(search_results[1].id)
        if m1.popularity < m2.popularity * 5:
            return f"film/{title}/{title}"
    year = _year(search_results[0].release_date)
    if not year:
        return f"film/{search_results[0].title}/{search_results[0].title}"
    movie_name = f"{title} ({year})"
    return f"film/{movie_name}/{movie_name}"


def get_media_name(title: str, overrides: dict) -> str | None:
    tv_name = find_series(title2show_data(title, overrides))
    if tv_name is not None:
        return tv_name
    movie_name = find_movie(title)
    return movie_name


def search_tmdb(query: str, limit: int = 8) -> list[dict]:
    """Search TMDb for TV shows and films matching a free-text query.

    Returns a list of candidate dicts, each with keys ``kind``, ``id``,
    ``title``, ``year``, ``overview`` and ``poster`` — everything the
    interactive picker needs to present a choice. TV results come first
    since most iPlayer/CBC items are episodes.
    """
    query = (query or "").strip()
    if not query:
        return []
    search = Search()
    results: list[dict] = []
    try:
        tv_results = list(search.tv_shows(query).results)
    except (TMDbException, AttributeError) as e:
        logger.warning(
            "TMDb tv search failed for %r: %s: %s", query, type(e).__name__, e
        )
        tv_results = []
    try:
        movie_results = list(search.movies(query).results)
    except (TMDbException, AttributeError) as e:
        logger.warning(
            "TMDb movie search failed for %r: %s: %s", query, type(e).__name__, e
        )
        movie_results = []

    for show in tv_results[:limit]:
        name = getattr(show, "name", None)
        show_id = getattr(show, "id", None)
        if not name or not show_id:
            # No usable id means the result cannot be pinned, so drop it
            # rather than offer a choice that could not be stored.
            continue
        year = _year(getattr(show, "first_air_date", None))
        results.append(
            {
                "kind": "tv",
                "id": int(show_id),
                "title": f"{name} ({year})" if year else name,
                "name": name,
                "year": year,
                "overview": (getattr(show, "overview", "") or "").strip(),
                "poster": getattr(show, "poster_path", None),
            }
        )
    for film in movie_results[:limit]:
        name = getattr(film, "title", None)
        film_id = getattr(film, "id", None)
        if not name or not film_id:
            continue
        year = _year(getattr(film, "release_date", None))
        results.append(
            {
                "kind": "movie",
                "id": int(film_id),
                "title": f"{name} ({year})" if year else name,
                "name": name,
                "year": year,
                "overview": (getattr(film, "overview", "") or "").strip(),
                "poster": getattr(film, "poster_path", None),
            }
        )
    return results


def _series_path(show_name: str, year: str, ep, season_number: int) -> str:
    name_year = f"{show_name} ({year})" if year else show_name
    season_dir = f"Season {season_number:02d}" if season_number != 0 else "Specials"
    return (
        f"tv/{name_year}/{season_dir}/"
        f"{name_year} - S{season_number:02d}E{ep.episode_number:02d} - {ep.name}"
    )


def resolve_tv_pin(pin: TmdbPin, show_data: dict) -> str | None:
    """Resolve an episode against a TMDb show id the user picked.

    Same episode-name matching as :func:`find_series`, but against a single
    known show instead of the top three search hits.
    """
    if pin.kind != "tv":
        return None
    try:
        show = TV().details(pin.id)
    except TMDbException as e:
        logger.warning(
            "TMDb details failed for tv id %s: %s: %s", pin.id, type(e).__name__, e
        )
        return None
    show_name = getattr(show, "name", None)
    if not show_name:
        logger.warning("TMDb tv id %s has no title; cannot pin it", pin.id)
        return None
    season = Season()
    try:
        episodes = season.details(pin.id, show_data["series_num"]).episodes
    except TMDbException:
        episodes = []
    try:
        specials = season.details(pin.id, 0).episodes
    except TMDbException:
        specials = []
    ep_name = show_data["episode_name"].lower()
    for ep in list(episodes) + list(specials):
        ep_name_comp = str(ep.name).lower()
        if "episode" in ep_name and ep_name != ep_name_comp:
            continue
        if (ep_name in ep_name_comp) or (ep_name_comp in ep_name):
            year = _year(getattr(show, "first_air_date", None))
            return _series_path(show_name, year, ep, ep.season_number)
    return None


def resolve_movie_pin(pin: TmdbPin) -> str | None:
    """Resolve a film against a TMDb movie id the user picked."""
    if pin.kind != "movie":
        return None
    try:
        details = Movie().details(pin.id)
    except TMDbException as e:
        logger.warning(
            "TMDb details failed for movie id %s: %s: %s", pin.id, type(e).__name__, e
        )
        return None
    name = getattr(details, "title", None)
    if not name:
        logger.warning("TMDb movie id %s has no title; cannot pin it", pin.id)
        return None
    year = _year(getattr(details, "release_date", None))
    movie_name = f"{name} ({year})" if year else name
    return f"film/{movie_name}/{movie_name}"


def resolve_pinned(pin: TmdbPin, title: str, overrides: dict) -> str | None:
    """Build a Jellyfin-style path for a user-picked TMDb result."""
    if pin.kind == "tv":
        return resolve_tv_pin(pin, title2show_data(title, overrides))
    return resolve_movie_pin(pin)


if __name__ == "__main__":
    from iplayerdl.config_loader import apply_environment, load_config

    apply_environment(load_config())
    titles = [
        "Doctor Who (2005–2022), Series 2, Love and Monsters",
        "Doctor Who (2005–2022), The End of Time - Part Two",
    ]
    overrides = {
        "Doctor Who (2005–2022), Series 12, Spyfall, Part 2": "Doctor Who (2005–2022), Series 12, Spyfall (2)",
        "Doctor Who (2005–2022), Series 12, Spyfall, Part 1": "Doctor Who (2005–2022), Series 12, Spyfall (1)",
        "Doctor Who (2005–2022), Series 9, New Series Prologue": "Doctor Who (2005–2022), Season 9 Prologue",
        "Doctor Who (2005–2022), Mini Episode - The Night of the Doctor": "Doctor Who (2005–2022), The Night of the Doctor",
        "Doctor Who (2005–2022), The Doctor, the Widow and the Wardrobe": "Doctor Who (2005–2022), , The Doctor, the Widow and the Wardrobe",
        "Doctor Who (2005–2022), Series 2, Love and Monsters": "Doctor Who (2005–2022), Series 2, Love & Monsters",
        # "Doctor Who (2005–2022), The End of Time - Part Two": "Doctor Who (2005–2022), The End of Time (2)",
    }
    for title in titles:
        print(title.split(", ", maxsplit=2))
        data = title2show_data(title, overrides)
        print(data)
        print(get_media_name(title, overrides))
