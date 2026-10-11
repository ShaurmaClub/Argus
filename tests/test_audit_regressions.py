# Reuse the existing integration fixture; test parameters deliberately share its name.
# ruff: noqa: F811
import json
import time
import urllib.error
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.reviews.client_yandex import YandexReviewsClient
from app.reviews.models import DEFAULT_SOURCES, ReviewItem, ReviewSource, ReviewSyncStatus
from tests.test_reviews import test_env  # noqa: F401


def response(payload):
    result = MagicMock()
    result.read.return_value = json.dumps(payload).encode()
    result.__enter__.return_value = result
    return result


def client_and_source():
    client = YandexReviewsClient()
    client._csrf_token = "test"
    client._session_id = "test"
    client._opener = MagicMock()
    source = ReviewSource(id=1, **DEFAULT_SOURCES[0])
    client._ratings_updated_at[source.external_id] = time.time()
    return client, source


def test_stale_unsorted_html_must_not_report_success():
    client, source = client_and_source()
    # An old unordered HTML snapshot cannot substitute a fresh by_time API page.
    client._cached_html_reviews = {
        source.external_id: [
            {"reviewId": "old", "rating": 5, "updatedTime": "2026-01-01T00:00:00Z"}
        ]
    }
    client._opener.open.return_value = response({"error": {"code": 500}})
    with patch("app.reviews.client_yandex.time.sleep"):
        status, items, _, code, *_ = client._sync_fetch(source)
    assert status == ReviewSyncStatus.HTTP_ERROR
    assert items == []
    assert code == 500
    assert client._opener.open.call_count == 4


@pytest.mark.parametrize(
    "code,expected",
    [
        (429, ReviewSyncStatus.RATE_LIMITED),
        (403, ReviewSyncStatus.SOURCE_BLOCKED),
        (400, ReviewSyncStatus.HTTP_ERROR),
    ],
)
def test_json_nontransient_error_is_not_retried(code, expected):
    client, source = client_and_source()
    client._opener.open.return_value = response({"error": {"code": str(code)}})
    with (
        patch("app.reviews.client_yandex.time.sleep") as sleep,
        patch.object(
            client,
            "_initialize_session",
            return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, None, 200),
        ),
    ):
        status, _, _, result_code, *_ = client._sync_fetch(source)
    assert status == expected
    assert result_code == code
    assert client._opener.open.call_count == (2 if code == 403 else 1)
    sleep.assert_not_called()


@pytest.mark.parametrize("transport", [False, True])
def test_auth_refresh_after_transient_error(transport):
    client, source = client_and_source()
    expired = (
        urllib.error.HTTPError(source.url, 403, "expired", {}, None)
        if transport
        else response({"csrfToken": "expired"})
    )
    client._opener.open.side_effect = [
        response({"error": {"code": 503}}),
        expired,
        response({"data": {"reviews": []}}),
    ]
    with (
        patch("app.reviews.client_yandex.time.sleep"),
        patch.object(
            client,
            "_initialize_session",
            return_value=(ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS, None, 200),
        ) as refresh,
    ):
        assert client._sync_fetch(source)[0] == ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS
    refresh.assert_called_once()


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_http_retries_are_bounded(code):
    client, source = client_and_source()
    client._opener.open.side_effect = urllib.error.HTTPError(source.url, code, "upstream", {}, None)
    with patch("app.reviews.client_yandex.time.sleep") as sleep:
        status, items, _, result_code, *_ = client._sync_fetch(source)
    assert (status, items, result_code) == (ReviewSyncStatus.HTTP_ERROR, [], code)
    assert client._opener.open.call_count == 4
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,external_id,client_key",
    [
        ("yandex", "1093602317", "yandex_client"),
        ("2gis", "4504127908536611", "dgis_client"),
    ],
)
async def test_catchup_limit_does_not_make_older_unseen_reviews_unreachable(
    test_env, platform, external_id, client_key
):
    service, repo = test_env["service"], test_env["bundle"].reviews
    await repo.ensure_default_sources(DEFAULT_SOURCES)
    source = await repo.get_source_by_external_id(platform, external_id)
    await repo.mark_source_initialized(source.id)
    source = await repo.get_source_by_id(source.id)
    baseline = ReviewItem(
        "baseline", source.platform, source.branch_name, "A", 5, "Old", "2026-01-01T00:00:00Z"
    )
    await repo.save_reviews([baseline], source.id, mark_sent=True)
    stream = [
        replace(baseline, external_review_id=f"new-{i}", published_at="2026-10-11T00:00:00Z")
        for i in range(60)
    ] + [baseline]

    async def fetch(src, page=1, page_size=10):
        return (
            ReviewSyncStatus.SUCCESS_NO_NEW_REVIEWS,
            stream[(page - 1) * page_size : page * page_size],
            None,
            200,
            5.0,
            len(stream),
        )

    test_env[client_key].fetch_reviews = AsyncMock(side_effect=fetch)
    result = await service.sync_source(source)
    assert result.is_error
    assert "REVIEWS_MAX_CATCHUP_REVIEWS" in result.error_message
    assert not await repo.is_review_known(source.platform, "new-0")
    # Explicitly raising the budget and retrying recovers the entire gap.
    service.settings.reviews_max_catchup_reviews = 100
    source = await repo.get_source_by_id(source.id)
    source.backoff_until = None
    result = await service.sync_source(source)
    assert len(result.new_reviews) == 60


@pytest.mark.asyncio
async def test_no_recovery_alert_until_catchup_succeeds(test_env):
    service, repo = test_env["service"], test_env["bundle"].reviews
    await repo.ensure_default_sources(DEFAULT_SOURCES)
    source = await repo.get_source_by_external_id("yandex", "1093602317")
    source.is_initialized = True
    source.health_alert_active = 1
    service.alerts.send_review_recovery_alert = AsyncMock(return_value=True)
    items = [
        ReviewItem(
            str(i), source.platform, source.branch_name, "A", 5, "new", "2026-10-11T00:00:00Z"
        )
        for i in range(10)
    ]
    test_env["yandex_client"].fetch_reviews = AsyncMock(
        side_effect=[
            (ReviewSyncStatus.SUCCESS_NEW_REVIEWS, items, None, 200),
            (ReviewSyncStatus.HTTP_ERROR, [], "upstream", 500),
        ]
    )
    assert (await service.sync_source(source)).is_error
    service.alerts.send_review_recovery_alert.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rating,verdict,silent", [(5, "GREEN", True), (4, "GREEN", False), (5, "YELLOW", False)]
)
async def test_real_delivery_preserves_silent_policy(test_env, rating, verdict, silent):
    service, repo = test_env["service"], test_env["bundle"].reviews
    await repo.ensure_default_sources(DEFAULT_SOURCES)
    source = await repo.get_source_by_external_id("yandex", "1093602317")
    review = ReviewItem(
        "silent-test",
        source.platform,
        source.branch_name,
        "A",
        rating,
        "Review",
        "2026-10-11T00:00:00Z",
    )
    await repo.save_reviews([review], source.id, mark_sent=False)
    from app.storage.models import ReviewAiAnalysis

    await repo.save_ai_analysis(
        ReviewAiAnalysis(id=None, review_id=review.id, status="SUCCESS", verdict=verdict)
    )
    assert await service.deliver_pending_reviews() == 1
    assert test_env["bot"].sent_messages[-1]["disable_notification"] is silent
