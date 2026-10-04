import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from app.assembly import inspect_broker, _build_platform
from platforms.brokers.tiger_readonly import (CONFIG_FILE, TigerReadOnly, TigerReadOnlyError,
    account_snapshot, configuration_check, _readonly_client_class, _connect)

HAS_SDK = importlib.util.find_spec("tigeropen") is not None


def client_fixture():
    account = "TEST12345678"
    contract = NS(symbol="TEST", sec_type="STK", currency="USD")
    client = Mock(spec=["get_managed_accounts", "get_assets", "get_positions", "get_open_orders"])
    client.get_managed_accounts.return_value = [NS(account=account, capability="CASH", status="Open")]
    client.get_assets.return_value = [NS(account=account, summary=NS(currency="USD", cash=1000, net_liquidation=float("inf")))]
    client.get_positions.return_value = [NS(account=account, contract=contract, quantity=10, market_value=100,
                                            secret_key="SHOULD-NOT-LEAK")]
    client.get_open_orders.return_value = [NS(account=account, contract=contract, id=1, quantity=10, filled=2,
                                              status="PARTIALLY_FILLED", secret_key="SHOULD-NOT-LEAK")]
    return account, client


class ReadOnlyTests(unittest.TestCase):
    def test_diagnostic_offline_by_default_and_never_constructs_sdk_client(self):
        with tempfile.TemporaryDirectory() as folder, patch(
                "platforms.brokers.tiger_readonly._connect") as connect, patch.object(
                    socket, "create_connection", side_effect=AssertionError("Network forbidden")):
            result = inspect_broker("tiger_readonly", folder)
            self.assertEqual(result["status"], "not_ready")
            self.assertFalse(result["network_requested"])
            connect.assert_not_called()

    def test_reader_cannot_be_used_as_an_execution_connector(self):
        with self.assertRaisesRegex(ValueError, "account_reader"):
            _build_platform({"type": "tiger_readonly"}, "broker")
        reader = TigerReadOnly(".")
        for name in ("submit", "cancel", "place_order", "transfer", "connect"):
            self.assertFalse(hasattr(reader, name))

    def test_requests_are_explicitly_scoped_and_output_is_allowlisted(self):
        account, client = client_fixture()
        result = account_snapshot(client, account, True)
        self.assertEqual(result["account_environment"], "tiger_paper")
        self.assertIsNone(result["assets"][0]["net_liquidation"])
        self.assertEqual(result["open_orders"][0]["filled"], 2)
        text = json.dumps(result, allow_nan=False)
        self.assertNotIn(account, text)
        self.assertNotIn("SHOULD-NOT-LEAK", text)
        self.assertEqual([c[0] for c in client.mock_calls],
                         ["get_managed_accounts", "get_assets", "get_positions", "get_open_orders"])
        for call in client.mock_calls:
            self.assertEqual(call.kwargs["account"], account)
        self.assertIsNone(client.get_positions.call_args.kwargs["sec_type"])

    def test_missing_or_foreign_responses_are_not_presented_as_an_empty_account(self):
        for field in ("get_managed_accounts", "get_assets", "get_positions", "get_open_orders"):
            account, client = client_fixture()
            getattr(client, field).return_value = None
            with self.assertRaises(TigerReadOnlyError):
                account_snapshot(client, account, False)
        account, client = client_fixture()
        client.get_positions.return_value[0].account = "ANOTHER-ACCOUNT"
        with self.assertRaisesRegex(TigerReadOnlyError, "unexpected account"):
            account_snapshot(client, account, False)

    def test_sdk_errors_do_not_leak_signed_urls_or_credentials(self):
        with patch("platforms.brokers.tiger_readonly.configuration_check", return_value={"issues": []}), patch(
                "platforms.brokers.tiger_readonly._connect", side_effect=RuntimeError("SECRET signed_url token")):
            with self.assertRaises(TigerReadOnlyError) as error:
                TigerReadOnly(".").inspect(connect=True)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)

    def test_environment_overrides_are_reported_without_the_values(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"TIGEROPEN_ACCOUNT": "SECRET"}):
            result = configuration_check(folder)
            self.assertTrue(any("environment overrides" in text for text in result["issues"]))
            self.assertNotIn("SECRET", json.dumps(result))


@unittest.skipUnless(HAS_SDK, "Install requirements/tiger.txt for real SDK contract tests")
class TigerSdkContractTests(unittest.TestCase):
    def test_sdk_read_boundary_allows_only_documented_account_queries(self):
        from tigeropen.trade.trade_client import TradeClient
        cls = _readonly_client_class()
        client = cls.__new__(cls)
        with patch.object(TradeClient, "execute", return_value="{}") as execute:
            for method in ("accounts", "assets", "positions", "active_orders"):
                self.assertEqual(client.execute(NS(_method=method)), "{}")
            self.assertEqual(execute.call_count, 4)
            for method in ("place_order", "modify_order", "cancel_order", "transfer", "user_token_refresh"):
                with self.assertRaises(TigerReadOnlyError):
                    client.execute(NS(_method=method))
            self.assertEqual(execute.call_count, 4)

    def test_actual_sdk_domain_objects_are_supported(self):
        from tigeropen.common.util.contract_utils import stock_contract
        from tigeropen.trade.domain.account import PortfolioAccount
        from tigeropen.trade.domain.position import Position
        from tigeropen.trade.domain.profile import AccountProfile
        from tigeropen.trade.domain.order import Order
        account, client = client_fixture()
        contract = stock_contract("TEST", "USD")
        assets = PortfolioAccount(account)
        assets.summary.cash, assets.summary.currency = 1000, "USD"
        client.get_assets.return_value = [assets]
        client.get_positions.return_value = [Position(account, contract, quantity=5)]
        client.get_managed_accounts.return_value = [AccountProfile(account, "CASH", "Open")]
        client.get_open_orders.return_value = [Order(account, contract, "BUY", quantity=5, id=10)]
        result = account_snapshot(client, account, False)
        self.assertEqual(result["positions"][0]["quantity"], 5)
        self.assertEqual(result["positions"][0]["contract"]["symbol"], "TEST")
        self.assertEqual(result["open_orders"][0]["id"], 10)

    def test_sdk_setup_and_props_check_do_not_query_a_server(self):
        from tigeropen.tiger_open_client import TigerOpenClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / CONFIG_FILE
            path.write_text("tiger_id=TEST\naccount=12345678\nlicense=TBAU\nprivate_key_pk8=TEST-ONLY-NOT-A-KEY\nenv=PROD\n")
            with patch.object(TigerOpenClient, "execute", side_effect=AssertionError("Network forbidden")), patch.object(
                    TigerOpenClientConfig, "query_domains", side_effect=AssertionError("Network forbidden")):
                check = configuration_check(folder)
                self.assertEqual(check["status"], "configuration_present_not_authenticated")
                client, account, is_paper = _connect(folder)
                self.assertEqual(account, "12345678")
                self.assertEqual(client.__class__.__name__, "ReadOnlyClient")
                self.assertFalse(is_paper)
            path.write_text(path.read_text() + "server_url=https://invalid.example/gateway\n")
            self.assertTrue(any("server_url" in message for message in configuration_check(folder)["issues"]))


if __name__ == "__main__":
    unittest.main()
