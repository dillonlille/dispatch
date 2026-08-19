"""Paycom Core collector registrations."""
from __future__ import annotations

from collection_manager import CollectorRegistration

from .roster.collector import collect_roster, verify_roster_publication
from .timecards.collector import collect_timecards, verify_timecard_publication

PLUGIN_ID = "paycom"
PLUGIN_RELEASE = "0.1.0"


def roster_registration() -> CollectorRegistration:
    return CollectorRegistration(
        collector_id="paycom-roster",
        plugin_id=PLUGIN_ID,
        plugin_release=PLUGIN_RELEASE,
        runner=collect_roster,
        browser_realm="paycom-client",
        authentication_required=True,
        publication_verifier=verify_roster_publication,
        execution_timeout_seconds=450,
    )


def timecards_registration() -> CollectorRegistration:
    return CollectorRegistration(
        collector_id="paycom-timecards",
        plugin_id=PLUGIN_ID,
        plugin_release=PLUGIN_RELEASE,
        runner=collect_timecards,
        browser_realm="paycom-client",
        authentication_required=True,
        publication_verifier=verify_timecard_publication,
        execution_timeout_seconds=3_600,
    )


def registrations() -> tuple[CollectorRegistration, CollectorRegistration]:
    return roster_registration(), timecards_registration()


def register_collectors(manager: object) -> list[dict[str, object]]:
    register = getattr(manager, "register", None)
    if not callable(register):
        raise TypeError("collection manager is invalid")
    return [register(registration) for registration in registrations()]


collector_registrations = registrations
get_collector_registrations = registrations

__all__ = ["PLUGIN_ID", "PLUGIN_RELEASE", "collector_registrations", "get_collector_registrations", "register_collectors", "registrations", "roster_registration", "timecards_registration"]
