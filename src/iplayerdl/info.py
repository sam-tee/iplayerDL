from tmdbv3api import TV, Movie, Season, TMDb
from tmdbv3api.exceptions import TMDbException

tmdb = TMDb()


def title2show_data(title: str) -> dict:
    """
    Converts BBC title of form <Show Name>, Series <>, <Episode Name>
    to show_data dict with keys show_name, series_num, episode_name
    """
    parts = title.split(", ", maxsplit=3)
    if len(parts) == 3:
        return {
            "show_name": parts[0],
            "series_num": parts[1][-1] if parts[1][-1].isdigit() else "0",
            "episode_name": parts[2],
        }
    else:
        return {"show_name": parts[0], "series_num": "0", "episode_name": parts[1]}


def find_series(show_data: dict):
    """
    Maps dict with keys: {show_name, series_num, episode_name} ->
        dict with keys: {show_name, series_num, year, episode_num, episode_name}
    """
    tv = TV()
    season = Season()
    search_results = tv.search(show_data["show_name"])
    for i, show in enumerate(search_results):
        if i >= 3:
            return
        try:
            details = season.details(show.id, show_data["series_num"])
            episodes = details.episodes
        except TMDbException:
            episodes = []
        except KeyError:
            episodes = []
        for ep in episodes:
            if ep.name.lower() == show_data["episode_name"].lower():
                name_year = f"{show.name} ({show.first_air_date.split('-')[0]})"
                season_num = int(show_data["series_num"])
                season = f"Season {season_num:02d}" if season_num != 0 else "Specials"
                return f"tv/{name_year}/{season}/{name_year} - S{season_num:02d}E{ep.episode_number:02d} - {ep.name}"
        if show_data.get("series_num") != 0:
            try:
                specials = season.details(show.id, 0)
                eps = specials.episodes
            except TMDbException:
                eps = []
            except KeyError:
                eps = []
            for special in eps:
                if special.name.lower() == show_data["episode_name"].lower():
                    name_year = f"{show.name} ({show.first_air_date.split('-')[0]})"
                    season_num = int(show_data["series_num"])
                    season = (
                        f"Season {season_num:02d}" if season_num != 0 else "Specials"
                    )
                    return f"tv/{name_year}/{season}/{name_year} - S{season_num:02d}E{ep.episode_number:02d} - {ep.name}"


def find_movie(title: str) -> str | None:
    movie = Movie()
    search_results = movie.search(title)
    if len(search_results) == 0:
        return None

    title = search_results[0].title
    if len(search_results) == 2 and title == search_results[1].title:
        m1 = movie.details(search_results[0].id)
        m2 = movie.details(search_results[1].id)
        if m1.popularity < m2.popularity * 5:
            return f"{title}"
    year = search_results[0].release_date.split("-")[0]
    movie_name = f"{title} ({year})"
    return f"film/{movie_name}/{movie_name}"


def get_media_name(title: str) -> str | None:
    tv_name = find_series(title2show_data(title))
    if tv_name is not None:
        return tv_name
    movie_name = find_movie(title)
    return movie_name
