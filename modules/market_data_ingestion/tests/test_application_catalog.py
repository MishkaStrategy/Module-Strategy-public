import json
from datetime import UTC, datetime

import pytest
from auth_helpers import LOCAL_ADMIN_HEADERS, local_admin_authenticator
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from market_data_application import (
    ApiRole,
    ApiTokenAuthenticator,
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.cli import create_cli
from market_data_application.contracts import StorageFormat
from market_data_application.errors import ApplicationError
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState


def request_model() -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
        storage_format=StorageFormat.CSV,
        compression="zstd",
        concurrency=1,
    )


def application(tmp_path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="catalog-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def registered_dataset(app: MarketDataApplication) -> str:
    created = app.create_download(
        request_model(),
        idempotency_key="catalog-dataset",
    )
    completed = app.run_job(created.job.job_id)
    assert completed.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}
    assert completed.result is not None
    return str(completed.result["dataset"]["dataset_id"])


def test_application_lists_registered_dataset_and_capabilities(tmp_path):
    app = application(tmp_path)
    try:
        dataset_id = registered_dataset(app)
        listed = app.list_datasets()
        shown = app.get_dataset(dataset_id)
        exchanges = app.list_exchanges()

        assert listed.pagination.total == 1
        assert listed.items[0].dataset_id == dataset_id
        assert shown.dataset_id == dataset_id
        assert shown.dataset_hash == listed.items[0].dataset_hash
        assert exchanges.items[0].exchange_id == "fake"
        assert DataType.KLINES in {item.data_type for item in exchanges.items[0].data}
    finally:
        app.shutdown()


def test_application_reads_exchange_markets_and_public_instrument_metadata(tmp_path):
    app = application(tmp_path)
    try:
        exchange = app.get_exchange(" FAKE ")
        markets = app.list_markets("fake")
        instrument = app.resolve_instrument(
            "Fake",
            MarketType.USDT_M_PERPETUAL,
            " btc/usdt ",
        )
        payload = instrument.model_dump(mode="json")

        assert exchange.exchange_id == "fake"
        assert exchange.adapter_version == "1.2.0"
        assert markets.exchange_id == "fake"
        assert markets.markets == (MarketType.SPOT, MarketType.USDT_M_PERPETUAL)
        assert instrument.exchange_id == "fake"
        assert instrument.market_type is MarketType.USDT_M_PERPETUAL
        assert instrument.symbol == "BTCUSDT"
        assert instrument.canonical_id == "fake:usdt_m_perpetual:btcusdt"
        assert instrument.base_asset == "BTC"
        assert instrument.quote_asset == "USDT"
        assert instrument.settlement_asset == "USDT"
        assert "evidence" not in payload
        assert "source" not in payload
        assert "expected_uri" not in payload
    finally:
        app.shutdown()


def test_exchange_catalog_maps_missing_and_unsupported_inputs(tmp_path):
    app = application(tmp_path)
    try:
        with pytest.raises(ApplicationError) as missing_exchange:
            app.get_exchange("missing")
        assert missing_exchange.value.code == "EXCHANGE_NOT_FOUND"

        with pytest.raises(ApplicationError) as unsupported_market:
            app.resolve_instrument("fake", MarketType.COIN_M_PERPETUAL, "BTCUSDT")
        assert unsupported_market.value.code == "MARKET_UNSUPPORTED"

        with pytest.raises(ApplicationError) as missing_instrument:
            app.resolve_instrument("fake", MarketType.SPOT, "MISSINGUSDT")
        assert missing_instrument.value.code == "INSTRUMENT_NOT_FOUND"
    finally:
        app.shutdown()


def test_binance_capability_contract_accepts_mixed_timestamp_policy(tmp_path):
    app = MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="binance-catalog-test",
                worker_count=1,
            )
        )
    )
    try:
        exchanges = app.list_exchanges()
        assert len(exchanges.items) == 1
        assert exchanges.items[0].exchange_id == "binance"
        assert "milliseconds" in exchanges.items[0].timestamp_unit
        assert len(exchanges.items[0].timestamp_unit) > 32
    finally:
        app.shutdown()


def test_cli_and_api_share_dataset_catalog(tmp_path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        dataset_id = registered_dataset(app)
        cli_list = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "datasets", "list"],
        )
        cli_show = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "datasets", "show", dataset_id],
        )
        cli_exchanges = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "exchanges"],
        )
        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        api_list = client.get("/api/v1/datasets")
        api_show = client.get(f"/api/v1/datasets/{dataset_id}")
        api_exchanges = client.get("/api/v1/exchanges")

        assert cli_list.exit_code == 0, cli_list.output
        assert cli_show.exit_code == 0, cli_show.output
        assert cli_exchanges.exit_code == 0, cli_exchanges.output
        assert api_list.status_code == 200
        assert api_show.status_code == 200
        assert api_exchanges.status_code == 200

        assert json.loads(cli_list.output)["items"] == api_list.json()["items"]
        assert json.loads(cli_show.output) == api_show.json()
        assert json.loads(cli_exchanges.output) == api_exchanges.json()
    finally:
        app.shutdown()


def test_cli_and_api_share_exchange_read_contracts(tmp_path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        cli_capabilities = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "capabilities", "--exchange", "fake"],
        )
        cli_markets = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "markets", "--exchange", "fake"],
        )
        cli_instrument = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "instruments",
                "--exchange",
                "fake",
                "--market",
                "usdt_m_perpetual",
                "--symbol",
                "BTCUSDT",
            ],
        )
        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        api_capabilities = client.get("/api/v1/exchanges/fake")
        api_markets = client.get("/api/v1/exchanges/fake/markets")
        api_instrument = client.get(
            "/api/v1/exchanges/fake/instruments/BTCUSDT",
            params={"market_type": "usdt_m_perpetual"},
        )

        assert cli_capabilities.exit_code == 0, cli_capabilities.output
        assert cli_markets.exit_code == 0, cli_markets.output
        assert cli_instrument.exit_code == 0, cli_instrument.output
        assert api_capabilities.status_code == 200
        assert api_markets.status_code == 200
        assert api_instrument.status_code == 200
        assert json.loads(cli_capabilities.output) == api_capabilities.json()
        assert json.loads(cli_markets.output) == api_markets.json()
        assert json.loads(cli_instrument.output) == api_instrument.json()
    finally:
        app.shutdown()


def test_exchange_read_api_requires_reader_token(tmp_path):
    app = application(tmp_path)
    token = "r" * 32
    client = TestClient(
        create_api(
            app,
            authenticator=ApiTokenAuthenticator({token: ApiRole.READER}),
        )
    )
    try:
        unauthorized = client.get("/api/v1/exchanges/fake")
        authorized = client.get(
            "/api/v1/exchanges/fake/markets",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "API_AUTHENTICATION_REQUIRED"
        assert authorized.status_code == 200
        assert authorized.json()["exchange_id"] == "fake"
    finally:
        app.shutdown()


def test_exchange_read_errors_match_cli_and_http(tmp_path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        missing_exchange = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "capabilities", "--exchange", "missing"],
        )
        missing_instrument = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "instruments",
                "--exchange",
                "fake",
                "--market",
                "spot",
                "--symbol",
                "MISSINGUSDT",
            ],
        )
        unsupported_market = runner.invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "instruments",
                "--exchange",
                "fake",
                "--market",
                "coin_m_perpetual",
                "--symbol",
                "BTCUSDT",
            ],
        )
        client = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        )
        api_missing_exchange = client.get("/api/v1/exchanges/missing")
        api_missing_instrument = client.get(
            "/api/v1/exchanges/fake/instruments/MISSINGUSDT",
            params={"market_type": "spot"},
        )
        api_unsupported_market = client.get(
            "/api/v1/exchanges/fake/instruments/BTCUSDT",
            params={"market_type": "coin_m_perpetual"},
        )

        assert missing_exchange.exit_code == 4
        assert json.loads(missing_exchange.output)["error"]["code"] == "EXCHANGE_NOT_FOUND"
        assert api_missing_exchange.status_code == 404
        assert api_missing_exchange.json()["error"]["code"] == "EXCHANGE_NOT_FOUND"

        assert missing_instrument.exit_code == 4
        assert json.loads(missing_instrument.output)["error"]["code"] == "INSTRUMENT_NOT_FOUND"
        assert api_missing_instrument.status_code == 404
        assert api_missing_instrument.json()["error"]["code"] == "INSTRUMENT_NOT_FOUND"

        assert unsupported_market.exit_code == 7
        assert json.loads(unsupported_market.output)["error"]["code"] == "MARKET_UNSUPPORTED"
        assert api_unsupported_market.status_code == 422
        assert api_unsupported_market.json()["error"]["code"] == "MARKET_UNSUPPORTED"
    finally:
        app.shutdown()


def test_invalid_cli_market_type_uses_structured_request_error(tmp_path):
    app = application(tmp_path)
    try:
        result = CliRunner().invoke(
            create_cli(app),
            [
                "--storage-root",
                str(tmp_path),
                "instruments",
                "--exchange",
                "fake",
                "--market",
                "invalid",
                "--symbol",
                "BTCUSDT",
            ],
        )
        payload = json.loads(result.output)

        assert result.exit_code == 2
        assert payload["error"]["code"] == "INVALID_MARKET_TYPE"
        assert payload["error"]["details"] == {"market_type": "invalid"}
    finally:
        app.shutdown()


def test_missing_dataset_maps_to_cli_and_http_not_found(tmp_path):
    app = application(tmp_path)
    runner = CliRunner()
    try:
        cli_result = runner.invoke(
            create_cli(app),
            ["--storage-root", str(tmp_path), "datasets", "show", "missing-dataset"],
        )
        response = TestClient(
            create_api(app, authenticator=local_admin_authenticator()), headers=LOCAL_ADMIN_HEADERS
        ).get("/api/v1/datasets/missing-dataset")

        assert cli_result.exit_code == 4
        assert json.loads(cli_result.output)["error"]["code"] == "DATASET_NOT_FOUND"
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DATASET_NOT_FOUND"
    finally:
        app.shutdown()
