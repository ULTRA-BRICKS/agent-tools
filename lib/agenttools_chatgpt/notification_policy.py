"""Local reporters collect evidence; the repair coordinator owns user escalation.

Monitoring age, elapsed Thinking time, retry caps and service failure do not prove
that a user must act. The local reporters have no complete repair-method evidence,
so they must not send even when an old configuration or queued alert is loaded.
"""

COORDINATOR_ONLY = "coordinator-only"


def notification_mode(value=COORDINATOR_ONLY):
    """Reject an unsupported sending mode rather than silently enabling it."""
    if value != COORDINATOR_ONLY:
        raise ValueError("notification_mode must be coordinator-only")
    return COORDINATOR_ONLY
