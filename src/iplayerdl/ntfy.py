import logging
import threading

import requests

logger = logging.getLogger(__name__)

_session: requests.Session | None = None
_session_lock = threading.Lock()


def send_ntfy(
    message: str,
    title: str = "Pipeline Update",
    priority: str = "default",
    tags: str = "bell",
    topic: str | None = None,
    url_base: str = "https://ntfy.sh/",
) -> None:
    """Publish a notification message to an ntfy topic.

    Args:
        message: Notification message body.
        title: Title shown on the notification.
        priority: Optional priority value.
        tags: Optional tags value.
        topic: ntfy topic to publish to (required).
        url_base: Base URL of the ntfy server.

    Returns:
        None
    """
    if not topic:
        logger.warning("ntfy topic not configured; skipping notification")
        return
    session = _get_session()
    try:
        session.post(
            f"{url_base}{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        logger.error("Failed to notify: %s", e)


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
        return _session
