"""SEC HTTP client helpers.

SEC automated access requires an identifying User-Agent. Production requests
must never fall back to a placeholder contact address.
"""

PLACEHOLDER_SEC_USER_AGENT = "HiddenConviction/0.14.0 replace-with-your-email@example.com"
SEC_MIN_REQUEST_CYCLE_SECONDS = 2.0


def validate_sec_user_agent(user_agent: str | None) -> str:
    value = (user_agent or "").strip()
    if not value or "replace-with-your-email" in value.lower() or "example.com" in value.lower():
        raise RuntimeError(
            "SEC_USER_AGENT is required and must contain a real monitored contact address"
        )
    return value


def sec_headers(user_agent: str | None = None) -> dict:
    return {
        "User-Agent": validate_sec_user_agent(user_agent),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json, application/xml, application/atom+xml, text/xml, text/plain, */*",
    }


async def sec_fetch_text(url: str, user_agent: str | None = None) -> str:
    import asyncio
    import time
    from workers import fetch

    started = time.perf_counter()
    response = await fetch(url, headers=sec_headers(user_agent))
    # Keep each active worker below ~1.33 requests/second. With the public
    # ingestion controller capped at five queue consumers plus isolated cron/13F
    # work, this leaves headroom below the SEC fair-access ceiling. Slow SEC
    # responses naturally exceed the floor and incur no added delay.
    remaining = SEC_MIN_REQUEST_CYCLE_SECONDS - (time.perf_counter() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)
    if not response.ok:
        raise RuntimeError(f"SEC request failed: {response.status} {url}")
    return await response.text()
