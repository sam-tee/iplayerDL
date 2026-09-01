import threading


def _new_job(url: str) -> dict:
    return {
        "url": url,
        "status": "pending",
        "percent": None,
        "detail": "",
        "done": 0,
        "eps_total": 0,
        "eps_done": 0,
        "ep": 0,
        "has_failed": False,
    }


class Tracker:
    """Thread-safe per-URL job status for the web UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancelled: set[str] = set()

    def reset(self, urls: list[str]) -> None:
        with self._lock:
            self._cancelled.clear()
            self._jobs = {url: _new_job(url) for url in urls}

    def _update(self, url: str, **fields) -> None:
        with self._lock:
            job = self._jobs.setdefault(url, _new_job(url))
            job.update(fields)

    def cancel(self, url: str) -> bool:
        """Mark a URL as cancelled; returns False if already finished."""
        with self._lock:
            if url in self._cancelled:
                return False
            job = self._jobs.get(url)
            if job is not None and job["status"] in (
                "completed",
                "failed",
                "unresolved",
            ):
                return False
            self._cancelled.add(url)
            if job is not None and job["status"] != "cancelled":
                job["status"] = "cancelled"
                job["percent"] = None
                job["detail"] = ""
            return True

    def cancelled(self, url: str) -> bool:
        with self._lock:
            return url in self._cancelled

    def resolving(self, url: str) -> None:
        with self._lock:
            if url in self._cancelled:
                return
            job = self._jobs.setdefault(url, _new_job(url))
            job["status"] = "resolving"
            job["percent"] = None

    def set_episodes(self, url: str, total: int) -> None:
        if total > 1:
            self._update(url, eps_total=total)

    def episode_start(self, url: str, index: int) -> None:
        self._update(url, ep=index, percent=None, detail="")

    def downloading(
        self, url: str, downloaded: int | None = None, total: int | None = None
    ) -> None:
        frac = downloaded / total if downloaded is not None and total else None
        mb = ""
        if total:
            mb = f"{downloaded / 1e6:.0f}/{total / 1e6:.0f} MB"
        elif downloaded:
            mb = f"{downloaded / 1e6:.0f} MB"
        percent = round(frac * 100, 1) if frac is not None else None
        detail = mb
        with self._lock:
            job = self._jobs.setdefault(url, _new_job(url))
            if url in self._cancelled:
                return
            if job["eps_total"] > 1 and job["ep"]:
                base = (job["ep"] - 1) + (frac or 0.0)
                percent = round(base / job["eps_total"] * 100, 1)
                detail = f"Ep {min(job['ep'], job['eps_total'])}/{job['eps_total']}"
                if mb:
                    detail += f" · {mb}"
            job["status"] = "downloading"
            job["percent"] = percent
            job["detail"] = detail

    def unresolved(self, url: str, title: str = "") -> None:
        self._update(
            url,
            status="unresolved",
            percent=None,
            detail=f"No match found for {title}" if title else "",
        )

    def transcoding(self, url: str) -> None:
        with self._lock:
            job = self._jobs.get(url)
            if job is None or url in self._cancelled:
                return
            if job["status"] in ("downloading", "pending", "resolving"):
                job["status"] = "transcoding"

    def completed_task(self, url: str, ok: bool) -> None:
        with self._lock:
            job = self._jobs.get(url)
            if job is None or job["status"] in ("cancelled", "completed"):
                return
            if ok:
                remaining = job["eps_total"] - job["eps_done"]
                if job["eps_total"] > 1 and remaining > 1:
                    # More episodes of this series are still in flight.
                    job["eps_done"] += 1
                    if job.get("has_failed"):
                        job["detail"] = f"{job['eps_done']}/{job['eps_total']} done (1+ failed)"
                    else:
                        job["detail"] = f"{job['eps_done']}/{job['eps_total']} done"
                    return
                # Last (or only) episode for this URL.
                if job.get("has_failed"):
                    job["eps_done"] += 1
                    job["status"] = "failed"
                else:
                    job["done"] += 1
                    job["status"] = "completed"
                    job["percent"] = 100
            else:
                job["has_failed"] = True
                remaining = job["eps_total"] - job["eps_done"]
                if job["eps_total"] > 1 and remaining > 1:
                    # One episode failed but more are still in flight — don't
                    # fail the whole URL yet; let remaining episodes continue.
                    job["eps_done"] += 1
                    job["detail"] = f"{job['eps_done']}/{job['eps_total']} done (1+ failed)"
                    return
                job["eps_done"] += 1
                job["status"] = "failed"

    def fail_pending(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                if job["status"] not in ("completed", "failed", "unresolved", "cancelled"):
                    job["status"] = "failed"

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(job) for job in self._jobs.values()]


tracker = Tracker()
