"""Domain-specific MiniQueue errors."""


class MiniQueueError(Exception):
    """Base class for errors callers may handle without inspecting strings."""


class InvalidJobError(MiniQueueError, ValueError):
    """Raised when a job or queue option is invalid."""


class JobNotFoundError(MiniQueueError, LookupError):
    """Raised when an operation names an unknown job identifier."""


class LeaseError(MiniQueueError):
    """Raised when a worker does not own the active lease for a job."""


class ConflictError(MiniQueueError):
    """Raised when optimistic store revision validation fails."""
