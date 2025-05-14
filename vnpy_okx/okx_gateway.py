import base64
import hashlib
import hmac
import json
import time
from copy import copy
from datetime import datetime
from urllib.parse import urlencode
from types import TracebackType
from collections.abc import Callable

from vnpy.event import EventEngine
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Interval,
    Offset,
    OrderType,
    Product,
    Status
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.utility import round_to, ZoneInfo
from vnpy.trader.object import (
    AccountData,
    BarData,
    CancelRequest,
    ContractData,
    HistoryRequest,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TickData,
    TradeData
)
from vnpy_rest import Request, Response, RestClient
from vnpy_websocket import WebsocketClient


# UTC timezone
UTC_TZ: ZoneInfo = ZoneInfo("UTC")

# Real server hosts
REAL_REST_HOST: str = "https://www.okx.com"
REAL_PUBLIC_HOST: str = "wss://ws.okx.com:8443/ws/v5/public"
REAL_PRIVATE_HOST: str = "wss://ws.okx.com:8443/ws/v5/private"
REAL_BUSINESS_HOST: str = "wss://ws.okx.com:8443/ws/v5/business"

# AWS server hosts
AWS_REST_HOST: str = "https://aws.okx.com"
AWS_PUBLIC_HOST: str = "wss://wsaws.okx.com:8443/ws/v5/public"
AWS_PRIVATE_HOST: str = "wss://wsaws.okx.com:8443/ws/v5/private"
AWS_BUSINESS_HOST: str = "wss://wsaws.okx.com:8443/ws/v5/business"

# Demo server hosts
DEMO_REST_HOST: str = "https://www.okx.com"
DEMO_PUBLIC_HOST: str = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
DEMO_PRIVATE_HOST: str = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
DEMO_BUSINESS_HOST: str = "wss://wspap.okx.com:8443/ws/v5/business?brokerId=9999"

# Order status map
STATUS_OKX2VT: dict[str, Status] = {
    "live": Status.NOTTRADED,
    "partially_filled": Status.PARTTRADED,
    "filled": Status.ALLTRADED,
    "canceled": Status.CANCELLED,
    "mmp_canceled": Status.CANCELLED
}

# Order type map
ORDERTYPE_OKX2VT: dict[str, OrderType] = {
    "limit": OrderType.LIMIT,
    "fok": OrderType.FOK,
    "ioc": OrderType.FAK
}
ORDERTYPE_VT2OKX: dict[OrderType, str] = {v: k for k, v in ORDERTYPE_OKX2VT.items()}

# Direction map
DIRECTION_OKX2VT: dict[str, Direction] = {
    "buy": Direction.LONG,
    "sell": Direction.SHORT
}
DIRECTION_VT2OKX: dict[Direction, str] = {v: k for k, v in DIRECTION_OKX2VT.items()}

# Kline interval map
INTERVAL_VT2OKX: dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1H",
    Interval.DAILY: "1D",
}

# Product type map
PRODUCT_OKX2VT: dict[str, Product] = {
    "SWAP": Product.SWAP,
    "SPOT": Product.SPOT,
    "FUTURES": Product.FUTURES
}
PRODUCT_VT2OKX: dict[Product, str] = {v: k for k, v in PRODUCT_OKX2VT.items()}


class OkxGateway(BaseGateway):
    """
    The OKX trading gateway for VeighNa.

    Only support net mode
    """

    default_name = "OKX"

    default_setting: dict = {
        "API Key": "",
        "Secret Key": "",
        "Passphrase": "",
        "Server": ["REAL", "AWS", "DEMO"],
        "Proxy Host": "",
        "Proxy Port": 0,
    }

    exchanges: Exchange = [Exchange.GLOBAL]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        """
        The init method of the gateway.

        event_engine: the global event engine object of VeighNa
        gateway_name: the unique name for identifying the gateway
        """
        super().__init__(event_engine, gateway_name)

        self.key: str = ""
        self.secret: str = ""
        self.passphrase: str = ""
        self.server: str = ""
        self.proxy_host: str = ""
        self.proxy_port: int = 0

        self.orders: dict[str, OrderData] = {}
        self.local_orderids: set[str] = set()

        self.symbol_contract_map: dict[str, ContractData] = {}
        self.name_contract_map: dict[str, ContractData] = {}

        self.rest_api: RestApi = RestApi(self)
        self.public_api: PublicApi = PublicApi(self)
        self.private_api: PrivateApi = PrivateApi(self)

    def connect(self, setting: dict) -> None:
        """
        Start server connections.

        This method establishes connections to OKX servers
        using the provided settings.

        Parameters:
            setting: A dictionary containing connection parameters including
                    API credentials, server selection, and proxy configuration
        """
        self.key = setting["API Key"]
        self.secret = setting["Secret Key"]
        self.passphrase = setting["Passphrase"]
        self.server = setting["Server"]
        self.proxy_host = setting["Proxy Host"]
        self.proxy_port = setting["Proxy Port"]

        self.rest_api.connect(
            self.key,
            self.secret,
            self.passphrase,
            self.server,
            self.proxy_host,
            self.proxy_port
        )

    def connect_ws_api(self) -> None:
        """
        Connect to OKX websocket API.
        """
        self.public_api.connect(
            self.server,
            self.proxy_host,
            self.proxy_port,
        )
        self.private_api.connect(
            self.key,
            self.secret,
            self.passphrase,
            self.server,
            self.proxy_host,
            self.proxy_port,
        )

    def subscribe(self, req: SubscribeRequest) -> None:
        """
        Subscribe to market data.

        Parameters:
            req: Subscription request object containing symbol information
        """
        self.public_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """
        Send new order to OKX.

        This function delegates order placement to the private websocket API,
        which handles validation, order generation, and submission to the exchange.

        Parameters:
            req: Order request object containing order details

        Returns:
            str: The VeighNa order ID if successful, empty string otherwise
        """
        return self.private_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """
        Cancel existing order on OKX.

        This function delegates order cancellation to the private websocket API,
        which determines the appropriate ID type to use and submits the request.

        Parameters:
            req: Cancel request object containing order details
        """
        self.private_api.cancel_order(req)

    def query_account(self) -> None:
        """
        Not required since OKX provides websocket update for account balances.
        """
        pass

    def query_position(self) -> None:
        """
        Not required since OKX provides websocket update for positions.
        """
        pass

    def query_history(self, req: HistoryRequest) -> list[BarData]:
        """
        Query historical kline data.

        Parameters:
            req: History request object containing query parameters

        Returns:
            list[BarData]: List of historical kline data bars
        """
        return self.rest_api.query_history(req)

    def close(self) -> None:
        """
        Close server connections.

        This method stops all API connections and releases resources.
        """
        self.rest_api.stop()
        self.public_api.stop()
        self.private_api.stop()

    def on_order(self, order: OrderData) -> None:
        """
        Save a copy of order and then push to event engine.

        Parameters:
            order: Order data object
        """
        self.orders[order.orderid] = order
        super().on_order(order)

    def get_order(self, orderid: str) -> OrderData:
        """
        Get previously saved order by order id.

        Parameters:
            orderid: The ID of the order to retrieve

        Returns:
            OrderData: Order data object if found, None otherwise
        """
        return self.orders.get(orderid, None)

    def on_contract(self, contract: ContractData) -> None:
        """
        Save a copy of contract and then push to event engine.

        Parameters:
            contract: Contract data object
        """
        self.symbol_contract_map[contract.symbol] = contract
        self.name_contract_map[contract.name] = contract

        super().on_contract(contract)

    def get_contract_by_symbol(self, symbol: str) -> ContractData | None:
        """
        Get contract by VeighNa symbol.

        Parameters:
            symbol: The symbol of the contract

        Returns:
            ContractData: Contract data object if found, None otherwise
        """
        return self.symbol_contract_map.get(symbol, None)

    def get_contract_by_name(self, name: str) -> ContractData | None:
        """
        Get contract by exchange symbol name.

        Parameters:
            name: The name of the contract

        Returns:
            ContractData: Contract data object if found, None otherwise
        """
        return self.name_contract_map.get(name, None)

    def parse_order_data(self, data: dict, gateway_name: str) -> OrderData:
        """
        Parse dict to order data.

        This function converts OKX order data into a VeighNa OrderData object.
        It extracts and maps all relevant fields from the exchange response.

        Parameters:
            data: Order data from OKX
            gateway_name: Gateway name for identification

        Returns:
            OrderData: VeighNa order object
        """
        contract: ContractData = self.get_contract_by_name(data["instId"])

        order_id: str = data["clOrdId"]
        if order_id:
            self.local_orderids.add(order_id)
        else:
            order_id = data["ordId"]

        order: OrderData = OrderData(
            symbol=contract.symbol,
            exchange=Exchange.GLOBAL,
            type=ORDERTYPE_OKX2VT[data["ordType"]],
            orderid=order_id,
            direction=DIRECTION_OKX2VT[data["side"]],
            offset=Offset.NONE,
            traded=float(data["accFillSz"]),
            price=float(data["px"]),
            volume=float(data["sz"]),
            datetime=parse_timestamp(data["cTime"]),
            status=STATUS_OKX2VT[data["state"]],
            gateway_name=gateway_name,
        )
        return order


class RestApi(RestClient):
    """The REST API of OkxGateway"""

    def __init__(self, gateway: OkxGateway) -> None:
        """
        The init method of the api.

        Parameters:
            gateway: the parent gateway object for pushing callback data.
        """
        super().__init__()

        self.gateway: OkxGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.key: str = ""
        self.secret: bytes = b""
        self.passphrase: str = ""
        self.simulated: bool = False

        self.product_ready: set = set()

    def sign(self, request: Request) -> Request:
        """
        Standard callback for signing a request.

        This method adds the necessary authentication parameters and signature
        to requests that require API key authentication.

        Parameters:
            request: Request object to be signed

        Returns:
            Request: Modified request with authentication parameters
        """
        # Public API does not need to sign
        if "public" in request.path:
            return request

        # Generate signature
        timestamp: str = generate_timestamp()

        if request.data:
            request.data = json.dumps(request.data)
        else:
            request.data = ""

        if request.params:
            path: str = request.path + "?" + urlencode(request.params)
        else:
            path = request.path

        msg: str = timestamp + request.method + path + request.data
        signature: bytes = generate_signature(msg, self.secret)

        # Add request header
        request.headers = {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": signature.decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }

        if self.simulated:
            request.headers["x-simulated-trading"] = "1"

        return request

    def connect(
        self,
        key: str,
        secret: str,
        passphrase: str,
        server: str,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        """
        Start server connection.

        This method establishes a connection to OKX REST API server
        using the provided credentials and configuration.

        Parameters:
            key: API Key for authentication
            secret: API Secret for request signing
            passphrase: API Passphrase for authentication
            server: Server type ("REAL", "AWS", or "DEMO")
            proxy_host: Proxy server hostname or IP
            proxy_port: Proxy server port
        """
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        if server == "DEMO":
            self.simulated = True

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        server_hosts: dict[str, str] = {
            "REAL": REAL_REST_HOST,
            "AWS": AWS_REST_HOST,
            "DEMO": DEMO_REST_HOST,
        }

        host: str = server_hosts[server]
        self.init(host, proxy_host, proxy_port)

        self.start()
        self.gateway.write_log("REST API started")

        self.query_time()
        self.query_contract()

    def query_time(self) -> None:
        """
        Query server time.

        This function sends a request to get the exchange server time,
        which is used to synchronize local time with server time.
        """
        self.add_request(
            "GET",
            "/api/v5/public/time",
            callback=self.on_query_time
        )

    def query_order(self) -> None:
        """
        Query open orders.

        This function sends a request to get all active orders
        that have not been fully filled or cancelled.
        """
        self.add_request(
            "GET",
            "/api/v5/trade/orders-pending",
            callback=self.on_query_order,
        )

    def query_contract(self) -> None:
        """
        Query available contracts.

        This function sends a request to get exchange information,
        including all available trading instruments and their specifications.
        """
        for inst_type in PRODUCT_OKX2VT.keys():
            self.add_request(
                "GET",
                "/api/v5/public/instruments",
                callback=self.on_query_contract,
                params={"instType": inst_type}
            )

    def on_query_time(self, packet: dict, request: Request) -> None:
        """
        Callback of server time query.

        This function processes the server time response and calculates
        the time difference between local and server time for logging.

        Parameters:
            packet: Response data from the server
            request: Original request object
        """
        timestamp: int = int(packet["data"][0]["ts"])
        server_time: datetime = datetime.fromtimestamp(timestamp / 1000)
        local_time: datetime = datetime.now()

        msg: str = f"Server time: {server_time}, local time: {local_time}"
        self.gateway.write_log(msg)

    def on_query_order(self, packet: dict, request: Request) -> None:
        """
        Callback of open orders query.

        This function processes the open orders response and
        creates OrderData objects for each active order.

        Parameters:
            packet: Response data from the server
            request: Original request object
        """
        for order_info in packet["data"]:
            order: OrderData = self.gateway.parse_order_data(
                order_info,
                self.gateway_name
            )
            self.gateway.on_order(order)

        self.gateway.write_log("Order data received")

    def on_query_contract(self, packet: dict, request: Request) -> None:
        """
        Callback of available contracts query.

        This function processes the exchange info response and
        creates ContractData objects for each trading instrument.

        Parameters:
            packet: Response data from the server
            request: Original request object
        """
        data: list = packet["data"]

        for d in data:
            name: str = d["instId"]
            product: Product = PRODUCT_OKX2VT[d["instType"]]
            net_position: bool = True

            if product == Product.SPOT:
                size: float = 1
            else:
                size = float(d["ctMult"])

            match product:
                case Product.SPOT:
                    symbol: str = name.replace("-", "") + "_SPOT_OKX"
                case Product.SWAP:
                    base, quote, _ = name.split("-")
                    symbol = base + quote + "_SWAP_OKX"
                case Product.FUTURES:
                    base, quote, expiry = name.split("-")
                    symbol = base + quote + "_" + expiry + "_OKX"

            contract: ContractData = ContractData(
                symbol=symbol,
                exchange=Exchange.GLOBAL,
                name=name,
                product=product,
                size=size,
                pricetick=float(d["tickSz"]),
                min_volume=float(d["minSz"]),
                history_data=True,
                net_position=net_position,
                gateway_name=self.gateway_name,
            )

            self.gateway.on_contract(contract)

        self.gateway.write_log(f"{d['instType']} contract data received")

        # Connect to websocket API after all contract data received
        self.product_ready.add(contract.product)

        if len(self.product_ready) == len(PRODUCT_OKX2VT):
            self.query_order()

            self.gateway.connect_ws_api()

    def on_error(
        self,
        exc: type,
        value: Exception,
        tb: TracebackType,
        request: Request
    ) -> None:
        """
        General error callback.

        This function is called when an exception occurs in REST API requests.
        It logs the exception details for troubleshooting.

        Parameters:
            exc: Type of the exception
            value: Exception instance
            tb: Traceback object
            request: Original request object
        """
        detail: str = self.exception_detail(exc, value, tb, request)

        msg: str = f"Exception catched by REST API: {detail}"
        self.gateway.write_log(msg)

    def query_history(self, req: HistoryRequest) -> list[BarData]:
        """
        Query kline history data.

        This function sends requests to get historical kline data
        for a specific trading instrument and time period.

        Parameters:
            req: History request object containing query parameters

        Returns:
            list[BarData]: List of historical kline data bars
        """
        # Validate symbol exists in contract map
        contract: ContractData | None = self.gateway.get_contract_by_symbol(req.symbol)
        if not contract:
            self.gateway.write_log(f"Query kline history failed, symbol not found: {req.symbol}")
            return []

        # Initialize buffer for storing bars
        buf: dict[datetime, BarData] = {}

        path: str = "/api/v5/market/history-candles"
        limit: str = "100"

        if not req.end:
            req.end = datetime.now()

        after: str = str(int(req.end.timestamp() * 1000))

        # Loop until no more data or request fails
        while True:
            # Create query params
            params: dict = {
                "instId": contract.name,
                "bar": INTERVAL_VT2OKX[req.interval],
                "limit": limit,
                "after": after
            }

            # Get response from server
            resp: Response = self.request(
                "GET",
                path,
                params=params
            )

            # Break loop if request is failed
            if resp.status_code // 100 != 2:
                log_msg: str = f"Query kline history failed, status code: {resp.status_code}, message: {resp.text}"
                self.gateway.write_log(log_msg)
                break
            else:
                data: dict = resp.json()
                bar_data: list = data.get("data", None)

                if not bar_data:
                    msg: str = data["msg"]
                    log_msg = f"No kline history data received, {msg}"
                    break

                for row in bar_data:
                    ts, op, hp, lp, cp, volume, turnover, _, _ = row

                    dt: datetime = parse_timestamp(ts)

                    bar: BarData = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=float(volume),
                        turnover=float(turnover),
                        open_price=float(op),
                        high_price=float(hp),
                        low_price=float(lp),
                        close_price=float(cp),
                        gateway_name=self.gateway_name
                    )
                    buf[bar.datetime] = bar

                begin: str = bar_data[-1][0]
                begin_dt: datetime = parse_timestamp(begin)
                end: str = bar_data[0][0]
                end_dt: datetime = parse_timestamp(end)

                log_msg = f"Query kline history finished, {req.symbol} - {req.interval.value}, {begin_dt} - {end_dt}"
                self.gateway.write_log(log_msg)

                # Break if all bars have been queried
                if begin_dt <= req.start:
                    break

                # Update start time
                after = begin

        index: list[datetime] = list(buf.keys())
        index.sort()

        history: list[BarData] = [buf[i] for i in index]
        return history


class PublicApi(WebsocketClient):
    """The public websocket API of OkxGateway"""

    def __init__(self, gateway: OkxGateway) -> None:
        """
        The init method of the api.

        Parameters:
            gateway: the parent gateway object for pushing callback data.
        """
        super().__init__()

        self.gateway: OkxGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.subscribed: dict[str, SubscribeRequest] = {}
        self.ticks: dict[str, TickData] = {}

        self.callbacks: dict[str, Callable] = {
            "tickers": self.on_ticker,
            "books5": self.on_depth
        }

    def connect(
        self,
        server: str,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        """
        Start server connection.

        This method establishes a websocket connection to OKX public data stream.

        Parameters:
            server: Server type ("REAL", "AWS", or "DEMO")
            proxy_host: Proxy server hostname or IP
            proxy_port: Proxy server port
        """
        server_hosts: dict[str, str] = {
            "REAL": REAL_PUBLIC_HOST,
            "AWS": AWS_PUBLIC_HOST,
            "DEMO": DEMO_PUBLIC_HOST,
        }

        host: str = server_hosts[server]
        self.init(host, proxy_host, proxy_port, 20)

        self.start()

    def subscribe(self, req: SubscribeRequest) -> None:
        """
        Subscribe to market data.

        This function sends subscription requests for ticker and depth data
        for the specified trading instrument.

        Parameters:
            req: Subscription request object containing symbol information
        """
        # Get contract by VeighNa symbol
        contract: ContractData | None = self.gateway.get_contract_by_symbol(req.symbol)
        if not contract:
            self.gateway.write_log(f"Failed to subscribe data, symbol not found: {req.symbol}")
            return

        # Add subscribe record
        self.subscribed[req.vt_symbol] = req

        # Create tick object
        tick: TickData = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            name=contract.name,
            datetime=datetime.now(UTC_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[contract.name] = tick

        # Send request to subscribe
        args: list = []
        for channel in ["tickers", "books5"]:
            args.append({
                "channel": channel,
                "instId": contract.name
            })

        packet: dict = {
            "op": "subscribe",
            "args": args
        }
        self.send_packet(packet)

    def on_connected(self) -> None:
        """
        Callback when server is connected.

        This function is called when the websocket connection to the server
        is successfully established. It logs the connection status and
        resubscribes to previously subscribed market data channels.
        """
        self.gateway.write_log("Public API connected")

        for req in list(self.subscribed.values()):
            self.subscribe(req)

    def on_disconnected(self) -> None:
        """
        Callback when server is disconnected.

        This function is called when the websocket connection is closed.
        It logs the disconnection status.
        """
        self.gateway.write_log("Public API disconnected")

    def on_packet(self, packet: dict) -> None:
        """
        Callback of data update.

        This function processes different types of market data updates,
        including ticker and depth data. It routes the data to the
        appropriate callback function based on the channel.

        Parameters:
            packet: JSON data received from websocket
        """
        if "event" in packet:
            event: str = packet["event"]
            if event == "subscribe":
                return
            elif event == "error":
                code: str = packet["code"]
                msg: str = packet["msg"]
                self.gateway.write_log(f"Public API request failed, status code: {code}, message: {msg}")
        else:
            channel: str = packet["arg"]["channel"]
            callback: Callable | None = self.callbacks.get(channel, None)

            if callback:
                data: list = packet["data"]
                callback(data)

    def on_error(self, exc: type, value: Exception, tb: TracebackType) -> None:
        """
        General error callback.

        This function is called when an exception occurs in the websocket connection.
        It logs the exception details for troubleshooting.

        Parameters:
            exc: Type of the exception
            value: Exception instance
            tb: Traceback object
        """
        detail: str = self.exception_detail(exc, value, tb)

        msg: str = f"Exception catched by Public API: {detail}"
        self.gateway.write_log(msg)

    def on_ticker(self, data: list) -> None:
        """
        Callback of ticker update.

        This function processes the ticker data updates and
        updates the corresponding TickData objects.

        Parameters:
            data: Ticker data from websocket
        """
        for d in data:
            tick: TickData = self.ticks[d["instId"]]

            tick.last_price = float(d["last"])
            tick.open_price = float(d["open24h"])
            tick.high_price = float(d["high24h"])
            tick.low_price = float(d["low24h"])
            tick.volume = float(d["vol24h"])
            tick.turnover = float(d["volCcy24h"])

            tick.datetime = parse_timestamp(d["ts"])
            self.gateway.on_tick(copy(tick))

    def on_depth(self, data: list) -> None:
        """
        Callback of depth update.

        This function processes the order book depth data updates
        and updates the corresponding TickData objects.

        Parameters:
            data: Depth data from websocket
        """
        for d in data:
            tick: TickData = self.ticks[d["instId"]]
            bids: list = d["bids"]
            asks: list = d["asks"]

            for n in range(min(5, len(bids))):
                price, volume, _, _ = bids[n]
                tick.__setattr__("bid_price_%s" % (n + 1), float(price))
                tick.__setattr__("bid_volume_%s" % (n + 1), float(volume))

            for n in range(min(5, len(asks))):
                price, volume, _, _ = asks[n]
                tick.__setattr__("ask_price_%s" % (n + 1), float(price))
                tick.__setattr__("ask_volume_%s" % (n + 1), float(volume))

            tick.datetime = parse_timestamp(d["ts"])
            self.gateway.on_tick(copy(tick))


class PrivateApi(WebsocketClient):
    """The private websocket API of OkxGateway"""

    def __init__(self, gateway: OkxGateway) -> None:
        """
        The init method of the api.

        Parameters:
            gateway: the parent gateway object for pushing callback data.
        """
        super().__init__()

        self.gateway: OkxGateway = gateway
        self.gateway_name: str = gateway.gateway_name
        self.local_orderids: set[str] = gateway.local_orderids

        self.key: str = ""
        self.secret: bytes = b""
        self.passphrase: str = ""

        self.reqid: int = 0
        self.order_count: int = 0
        self.connect_time: int = 0

        self.callbacks: dict[str, Callable] = {
            "login": self.on_login,
            "orders": self.on_order,
            "account": self.on_account,
            "positions": self.on_position,
            "order": self.on_send_order,
            "cancel-order": self.on_cancel_order,
            "error": self.on_api_error
        }

        self.reqid_order_map: dict[str, OrderData] = {}

    def connect(
        self,
        key: str,
        secret: str,
        passphrase: str,
        server: str,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        """
        Start server connection.

        This method establishes a websocket connection to OKX private data stream.

        Parameters:
            key: API Key for authentication
            secret: API Secret for request signing
            passphrase: API Passphrase for authentication
            server: Server type ("REAL", "AWS", or "DEMO")
            proxy_host: Proxy server hostname or IP
            proxy_port: Proxy server port
        """
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        server_hosts: dict[str, str] = {
            "REAL": REAL_PRIVATE_HOST,
            "AWS": AWS_PRIVATE_HOST,
            "DEMO": DEMO_PRIVATE_HOST,
        }

        host: str = server_hosts[server]
        self.init(host, proxy_host, proxy_port, 20)

        self.start()

    def on_connected(self) -> None:
        """
        Callback when server is connected.

        This function is called when the websocket connection to the server
        is successfully established. It logs the connection status and
        initiates the login process.
        """
        self.gateway.write_log("Private websocket API connected")
        self.login()

    def on_disconnected(self) -> None:
        """
        Callback when server is disconnected.

        This function is called when the websocket connection is closed.
        It logs the disconnection status.
        """
        self.gateway.write_log("Private API disconnected")

    def on_packet(self, packet: dict) -> None:
        """
        Callback of data update.

        This function processes different types of private data updates,
        including orders, account balance, and positions. It routes the data
        to the appropriate callback function.

        Parameters:
            packet: JSON data received from websocket
        """
        if "event" in packet:
            cb_name: str = packet["event"]
        elif "op" in packet:
            cb_name = packet["op"]
        else:
            cb_name = packet["arg"]["channel"]

        callback: Callable | None = self.callbacks.get(cb_name, None)
        if callback:
            callback(packet)

    def on_error(self, e: Exception) -> None:
        """
        General error callback.

        This function is called when an exception occurs in the websocket connection.
        It logs the exception details for troubleshooting.

        Parameters:
            e: The exception that was raised
        """
        msg: str = f"Private channel exception triggered: {e}"
        self.gateway.write_log(msg)

    def on_api_error(self, packet: dict) -> None:
        """
        Callback of API error.

        This function processes error responses from the websocket API.
        It logs the error details for troubleshooting.

        Parameters:
            packet: Error data from websocket
        """
        # Extract error code and message from the response
        code: str = packet["code"]
        msg: str = packet["msg"]

        # Log the error with details for debugging
        self.gateway.write_log(f"Private API request failed, status code: {code}, message: {msg}")

    def on_login(self, packet: dict) -> None:
        """
        Callback of user login.

        This function processes the login response and subscribes to
        private data channels if login is successful.

        Parameters:
            packet: Login response data from websocket
        """
        if packet["code"] == '0':
            self.gateway.write_log("Private API login successful")
            self.subscribe_topic()
        else:
            self.gateway.write_log("Private API login failed")

    def on_order(self, packet: dict) -> None:
        """
        Callback of order update.

        This function processes order updates and trade executions.
        It creates OrderData and TradeData objects and pushes them to the gateway.

        Parameters:
            packet: Order update data from websocket
        """
        # Extract order data from packet
        data: list = packet["data"]
        for d in data:
            # Create order object from data
            order: OrderData = self.gateway.parse_order_data(d, self.gateway_name)
            self.gateway.on_order(order)

            # Check if order is filled - skip trade creation if no fill size
            if d["fillSz"] == "0":
                return

            # Process trade data for filled or partially filled orders
            # Round trade volume number to meet minimum volume precision
            trade_volume: float = float(d["fillSz"])
            contract: ContractData = self.gateway.get_contract_by_symbol(order.symbol)
            if contract:
                trade_volume = round_to(trade_volume, contract.min_volume)

            # Create trade object and push to gateway
            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=d["tradeId"],
                direction=order.direction,
                offset=order.offset,
                price=float(d["fillPx"]),
                volume=trade_volume,
                datetime=parse_timestamp(d["uTime"]),
                gateway_name=self.gateway_name,
            )
            self.gateway.on_trade(trade)

    def on_account(self, packet: dict) -> None:
        """
        Callback of account balance update.

        This function processes account balance updates and creates
        AccountData objects for each asset.

        Parameters:
            packet: Account update data from websocket
        """
        if len(packet["data"]) == 0:
            return

        buf: dict = packet["data"][0]
        for detail in buf["details"]:
            account: AccountData = AccountData(
                accountid=detail["ccy"],
                balance=float(detail["eq"]),
                gateway_name=self.gateway_name,
            )
            account.available = float(detail["availEq"]) if len(detail["availEq"]) != 0 else 0.0
            account.frozen = account.balance - account.available
            self.gateway.on_account(account)

    def on_position(self, packet: dict) -> None:
        """
        Callback of position update.

        This function processes position updates and creates
        PositionData objects for each position.

        Parameters:
            packet: Position update data from websocket
        """
        data: list = packet["data"]
        for d in data:
            name: str = d["instId"]
            contract: ContractData = self.gateway.get_contract_by_name(name)

            pos: float = float(d["pos"])
            price: float = get_float_value(d, "avgPx")
            pnl: float = get_float_value(d, "upl")

            position: PositionData = PositionData(
                symbol=contract.symbol,
                exchange=Exchange.GLOBAL,
                direction=Direction.NET,
                volume=pos,
                price=price,
                pnl=pnl,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_position(position)

    def on_send_order(self, packet: dict) -> None:
        """
        Callback of send_order.

        This function processes the response to an order placement request.
        It handles errors and rejection cases.

        Parameters:
            packet: Order response data from websocket
        """
        data: list = packet["data"]

        # Wrong parameters
        if packet["code"] != "0":
            if not data:
                order: OrderData = self.reqid_order_map[packet["id"]]
                order.status = Status.REJECTED
                self.gateway.on_order(order)
                return

        # Failed to process
        for d in data:
            code: str = d["sCode"]
            if code == "0":
                return

            orderid: str = d["clOrdId"]
            order = self.gateway.get_order(orderid)
            if not order:
                return
            order.status = Status.REJECTED
            self.gateway.on_order(copy(order))

            msg: str = d["sMsg"]
            self.gateway.write_log(f"Send order failed, status code: {code}, message: {msg}")

    def on_cancel_order(self, packet: dict) -> None:
        """
        Callback of cancel_order.

        This function processes the response to an order cancellation request.
        It handles errors and logs appropriate messages.

        Parameters:
            packet: Cancel response data from websocket
        """
        # Wrong parameters
        if packet["code"] != "0":
            code: str = packet["code"]
            msg: str = packet["msg"]
            self.gateway.write_log(f"Cancel order failed, status code: {code}, message: {msg}")
            return

        # Failed to process
        data: list = packet["data"]
        for d in data:
            code = d["sCode"]
            if code == "0":
                return

            msg = d["sMsg"]
            self.gateway.write_log(f"Cancel order failed, status code: {code}, message: {msg}")

    def login(self) -> None:
        """
        User login.

        This function prepares and sends a login request to authenticate
        with the websocket API using API credentials.
        """
        timestamp: str = str(time.time())
        msg: str = timestamp + "GET" + "/users/self/verify"
        signature: bytes = generate_signature(msg, self.secret)

        packet: dict = {
            "op": "login",
            "args":
            [
                {
                    "apiKey": self.key,
                    "passphrase": self.passphrase,
                    "timestamp": timestamp,
                    "sign": signature.decode("utf-8")
                }
            ]
        }
        self.send_packet(packet)

    def subscribe_topic(self) -> None:
        """
        Subscribe to private data channels.

        This function sends subscription requests for order, account, and
        position updates after successful login.
        """
        packet: dict = {
            "op": "subscribe",
            "args": [
                {
                    "channel": "orders",
                    "instType": "ANY"
                },
                {
                    "channel": "account"
                },
                {
                    "channel": "positions",
                    "instType": "ANY"
                },
            ]
        }
        self.send_packet(packet)

    def send_order(self, req: OrderRequest) -> str:
        """
        Send new order to OKX.

        This function creates and sends a new order request to the exchange.
        It handles different order types and trading modes.

        Parameters:
            req: Order request object containing order details

        Returns:
            str: The VeighNa order ID if successful, empty string otherwise
        """
        # Validate order type is supported by OKX
        if req.type not in ORDERTYPE_VT2OKX:
            self.gateway.write_log(f"Send order failed, order type not supported: {req.type.value}")
            return ""

        # Validate symbol exists in contract map
        contract: ContractData | None = self.gateway.get_contract_by_symbol(req.symbol)
        if not contract:
            self.gateway.write_log(f"Send order failed, symbol not found: {req.symbol}")
            return ""

        # Generate unique local order ID
        self.order_count += 1
        count_str = str(self.order_count).rjust(6, "0")
        orderid = f"{self.connect_time}{count_str}"

        # Prepare order parameters for OKX API
        args: dict = {
            "instId": contract.name,
            "clOrdId": orderid,
            "side": DIRECTION_VT2OKX[req.direction],
            "ordType": ORDERTYPE_VT2OKX[req.type],
            "px": str(req.price),
            "sz": str(req.volume)
        }

        # Set trading mode based on product type
        # "cash" for spot trading, "cross" for futures/swap with cross margin
        if contract.product == Product.SPOT:
            args["tdMode"] = "cash"
        else:
            args["tdMode"] = "cross"

        # Create websocket request with unique request ID
        self.reqid += 1
        packet: dict = {
            "id": str(self.reqid),
            "op": "order",
            "args": [args]
        }
        self.send_packet(packet)

        # Create order data object and push to gateway
        order: OrderData = req.create_order_data(orderid, self.gateway_name)
        self.gateway.on_order(order)

        # Return VeighNa order ID (gateway_name.orderid)
        return str(order.vt_orderid)

    def cancel_order(self, req: CancelRequest) -> None:
        """
        Cancel existing order on OKX.

        This function sends a request to cancel an existing order on the exchange.
        It determines whether to use client order ID or exchange order ID.

        Parameters:
            req: Cancel request object containing order details
        """
        # Validate symbol exists in contract map
        contract: ContractData | None = self.gateway.get_contract_by_symbol(req.symbol)
        if not contract:
            self.gateway.write_log(f"Cancel order failed, symbol not found: {req.symbol}")
            return

        # Initialize cancel parameters with instrument ID
        args: dict = {"instId": contract.name}

        # Determine the type of order ID to use for cancellation
        # OKX supports both client order ID and exchange order ID for cancellation
        if req.orderid in self.local_orderids:
            # Use client order ID if it was created by this gateway instance
            args["clOrdId"] = req.orderid
        else:
            # Use exchange order ID if it came from another source
            args["ordId"] = req.orderid

        # Create websocket request with unique request ID
        self.reqid += 1
        packet: dict = {
            "id": str(self.reqid),
            "op": "cancel-order",
            "args": [args]
        }

        # Send the cancellation request
        self.send_packet(packet)


def generate_signature(msg: str, secret_key: bytes) -> bytes:
    """
    Generate signature from message.

    This function creates an HMAC-SHA256 signature required for
    authenticated API requests to OKX.

    Parameters:
        msg: Message to be signed
        secret_key: API secret key in bytes

    Returns:
        bytes: Base64 encoded signature
    """
    return base64.b64encode(hmac.new(secret_key, msg.encode(), hashlib.sha256).digest())


def generate_timestamp() -> str:
    """
    Generate current timestamp.

    This function creates an ISO format timestamp with milliseconds
    required for OKX API requests.

    Returns:
        str: ISO 8601 formatted timestamp with Z suffix
    """
    now: datetime = datetime.utcnow()
    timestamp: str = now.isoformat("T", "milliseconds")
    return timestamp + "Z"


def parse_timestamp(timestamp: str) -> datetime:
    """
    Parse timestamp to datetime.

    This function converts OKX timestamp to a datetime object
    with UTC timezone.

    Parameters:
        timestamp: OKX timestamp in milliseconds

    Returns:
        datetime: Datetime object with UTC timezone
    """
    dt: datetime = datetime.fromtimestamp(int(timestamp) / 1000)
    return dt.replace(tzinfo=UTC_TZ)


def get_float_value(data: dict, key: str) -> float:
    """
    Get decimal number from float value.

    This function safely extracts a float value from a dictionary
    and handles empty or missing values.

    Parameters:
        data: Dictionary containing the value
        key: Key to extract from the dictionary

    Returns:
        float: Extracted value or 0.0 if not found
    """
    data_str: str = data.get(key, "")
    if not data_str:
        return 0.0
    return float(data_str)
