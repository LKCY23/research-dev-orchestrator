"""Domain-specific MiniQueue errors."""


class MiniQueueError(Exception):
    """Base class for errors callers may handle without inspecting strings."""


class InvalidJobError(MiniQueueError, ValueError):
    """Raised when a job or queue option is invalid."""


class JobNotFoundError(MiniQueueError, LookupError):
    """Raised when an operation names an unknown job identifier."""


class InvalidStateTransitionError(MiniQueueError):
    """Raised when an operation is invalid from the job's lifecycle state."""


class LeaseError(MiniQueueError):
    """Raised when lease ownership, validity, or expiry checks fail."""


class ConflictError(MiniQueueError):
    """Raised when optimistic store revision validation fails."""
