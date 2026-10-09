import time


class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; half-opens after `reset_seconds`.

    State is per-process, which is sufficient to shed load from a failing dependency per replica.
    """

    def __init__(self, name: str, threshold: int = 5, reset_seconds: float = 30.0):
        self.name, self.threshold, self.reset_seconds = name, threshold, reset_seconds
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self.opened_at >= self.reset_seconds else "open"

    def before(self) -> None:
        if self.state == "open":
            raise CircuitOpen(f"circuit '{self.name}' is open")

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
