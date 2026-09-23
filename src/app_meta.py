"""Application-level metadata shared by API, diagnostics, and deployment tools."""

APP_NAME = "Hidden Conviction"
APP_VERSION = "0.14.0"
API_SCHEMA_VERSION = "2026-09-22.v5"
PUBLIC_HCS_THRESHOLD = 80
MAX_PUBLIC_PAGE_SIZE = 100


def version_payload(*, hcs_version: str, institutional_version: str, universe_version: str, security_master_version: str) -> dict:
    return {
        "application": APP_NAME,
        "version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "public_hcs_threshold": PUBLIC_HCS_THRESHOLD,
        "scoring_engine": hcs_version,
        "institutional_engine": institutional_version,
        "universe_engine": universe_version,
        "security_master": security_master_version,
        "cache_generation": f"{APP_VERSION}:{API_SCHEMA_VERSION}",
    }
