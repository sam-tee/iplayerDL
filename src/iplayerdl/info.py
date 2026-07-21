import dotenv
from tmdbv3api import TV, Movie, Season, TMDb
from tmdbv3api.exceptions import TMDbException

tmdb = TMDb()


def sanitise(input: str) -> str:
    input = input.replace("’", "'")
    input = input.replace("？", "?")
    input = input.replace("：", ":")
    input = input.replace(", Part One", "(1)")
    input = input.replace(", Part Two", "(2)")
    input = input.replace("- Part One", "(1)")
    input = input.replace("- Part Two", "(2)")
    return input


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
            if str(ep.name.lower()).startswith(show_data["episode_name"].lower()):
                name_year = f"{show.name} ({show.first_air_date.split('-')[0]})"
                s_num = ep.season_number
                season = f"Season {s_num:02d}" if s_num != 0 else "Specials"
                return f"tv/{name_year}/{season}/{name_year} - S{s_num:02d}E{ep.episode_number:02d} - {ep.name}"


def find_movie(title: str) -> str | None:
    """
    Takes in title of film and return Jellyfin style partial mapping with film and year
    If multiple films have same title and first result is not 5x more popular then returns
        without year
    """
    movie = Movie()
    search_results = movie.search(title)

    if search_results.total_results == 0:
        return None
    elif search_results.total_results == 1:
        title = search_results[0].title
    elif search_results[0] == search_results[1].title:
        # if top two have same title then multiple of same film exist
        # checks if second one is five times less popular and if so returns
        # the first else returns just title and choice can be made later
        m1 = movie.details(search_results[0].id)
        m2 = movie.details(search_results[1].id)
        if m1.popularity < m2.popularity * 5:
            return f"film/{title}/{title}"
    year = search_results[0].release_date.split("-")[0]
    movie_name = f"{title} ({year})"
    return f"film/{movie_name}/{movie_name}"


def get_media_name(title: str, overrides: dict) -> str | None:
    tv_name = find_series(title2show_data(title, overrides))
    if tv_name is not None:
        return tv_name
    movie_name = find_movie(title)
    return movie_name


if __name__ == "__main__":
    dotenv.load_dotenv()
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
