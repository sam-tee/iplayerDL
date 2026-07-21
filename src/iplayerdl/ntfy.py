import requests


def send_ntfy(
    message: str,
    title: str = "Pipeline Update",
    priority: str = "default",
    tags: str = "bell",
    topic: str = "iplayerdl-ntfy-3e58978",
    url_base: str = "https://ntfy.sh/",
) -> None:
    """Send a notification message to an ntfy topic.

    Args:
        message (str): Notification message body.
        title (str): Title to display on the generated plot.
        priority (str): Optional priority value. Defaults to `'default'`.
        tags (str): Optional tags value. Defaults to `'bell'`.
        topic (str): ntfy topic to publish to.
        url_base (str): Optional url base value. Defaults to `'https://ntfy.sh/'`.

    Returns:
        None: This function returns `None`; outputs are written to disk, plotted, logged, or applied through side effects.
    """
    url = f"{url_base}{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    try:
        requests.post(url, data=message.encode("utf-8"), headers=headers)
    except requests.exceptions.RequestException as e:
        print(f"Failed to notify: {e}")
