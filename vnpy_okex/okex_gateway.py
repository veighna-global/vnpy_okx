import base64
import hashlib
import hmac
import json
import sys
import time
from copy import copy
from datetime import date, datetime
from threading import Lock
from urllib.parse import urlencode
from typing import Dict, Optional, Any, List
from types import TracebackType

from requests import ConnectionError
from pytz import timezone
from requests.models import Response
from vnpy.api.rest.rest_client import RequestStatus

from vnpy.event.engine import EventEngine
from vnpy.api.rest import Request, RestClient
from vnpy.api.websocket import WebsocketClient
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Interval,
    Offset,
    OrderType,
    Product,
    Status,
    OptionType
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.utility import get_folder_path, round_to
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


CHINA_TZ: timezone = timezone("China/Shanghai")

# 实盘和模拟盘REST API地址
REST_HOST: str = "https://www.okex.com"

# 实盘Websocket API地址
PUBLIC_WEBSOCKET_HOST: str = "wss://ws.okex.com:8443/ws/v5/public"
PRIVATE_WEBSOCKET_HOST: str = "wss://ws.okex.com:8443/ws/v5/private"

# 模拟盘Websocket API地址
TEST_PUBLIC_WEBSOCKET_HOST: str = "wss://wspap.okex.com:8443/ws/v5/public?brokerId=9999"
TEST_PRIVATE_WEBSOCKET_HOST: str = "wss://wspap.okex.com:8443/ws/v5/private?brokerId=9999"

# 委托状态映射
STATUS_OKEX2VT: Dict[str, Status] = {
    "live": Status.NOTTRADED,
    "partially_filled": Status.PARTTRADED,
    "filled": Status.ALLTRADED,
    "canceled": Status.CANCELLED
}

# 委托类型映射
ORDERTYPE_OKEX2VT: Dict[str, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT
}
ORDERTYPE_VT2OKEX = {v: k for k, v in ORDERTYPE_OKEX2VT.items()}

# 开平方向映射
SIDE_OKEX2VT: Dict[str, Direction] = {
    "buy": Direction.LONG,
    "sell": Direction.SHORT
}
SIDE_VT2OKEX: Dict[Direction, str] = {v: k for k, v in SIDE_OKEX2VT.items()}

DIRECTION_OKEX2VT: Dict[str, Direction] = {
    "long": Direction.LONG,
    "short": Direction.SHORT,
    "net": Direction.NET
}

# 数据频率映射
INTERVAL_VT2OKEX: Dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1H",
    Interval.DAILY: "1D",
}

# 产品类型映射
PRODUCT_OKEX2VT: Dict[str, Product] = {
    "SWAP": Product.FUTURES,
    "SPOT": Product.SPOT,
    "FUTURES": Product.FUTURES,
    "OPTION": Product.OPTION
}
PRODUCT_VT2OKEX: Dict[Product, str] = {v: k for k, v in PRODUCT_OKEX2VT.items()}

# 期权类型映射
OPTIONTYPE_OKEXO2VT: Dict[str, OptionType] = {
    "C": OptionType.CALL,
    "P": OptionType.PUT
}

# 合约数据全局缓存字典
symbol_contract_map: Dict[str, ContractData] = {}


class OkexGateway(BaseGateway):
    """
    vn.py用于对接欧易统一账户的交易接口。
    """

    default_setting = {
        "API Key": "",
        "Secret Key": "",
        "Passphrase": "",
        "会话数": 3,
        "代理地址": "",
        "代理端口": "",
        "服务器": ["TEST", "REAL"]
    }

    exchanges = [Exchange.OKEX]

    def __init__(self, event_engine: EventEngine, gateway_name: str = "OKEX") -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.rest_api: "OkexRestApi" = OkexRestApi(self)
        self.ws_pub_api: "OkexWebsocketPublicApi" = OkexWebsocketPublicApi(self)
        self.ws_pri_api: "OkexWebsocketPrivateApi" = OkexWebsocketPrivateApi(self)

        self.orders: Dict[str, OrderData] = {}

    def connect(self, setting: dict) -> None:
        """连接交易接口"""
        key: str = setting["API Key"]
        secret: str = setting["Secret Key"]
        passphrase: str = setting["Passphrase"]
        session_number: int = setting["会话数"]
        proxy_host: str = setting["代理地址"]
        proxy_port: str = setting["代理端口"]
        server: str = setting["服务器"]

        if server == "REAL":
            self.rest_api.simulated = False
        else:
            self.rest_api.simulated = True

        if proxy_port.isdigit():
            proxy_port = int(proxy_port)
        else:
            proxy_port = 0

        self.rest_api.connect(
            key,
            secret,
            passphrase,
            session_number,
            proxy_host,
            proxy_port
        )
        self.ws_pub_api.connect(
            proxy_host,
            proxy_port,
            server
        )
        self.ws_pri_api.connect(
            key,
            secret,
            passphrase,
            proxy_host,
            proxy_port,
            server
        )

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        self.ws_pub_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.rest_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.rest_api.cancel_order(req)

    def query_account(self) -> None:
        """查询资金"""
        pass

    def query_position(self) -> None:
        """查询持仓"""
        pass

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        return self.rest_api.query_history(req)

    def close(self) -> None:
        """关闭连接"""
        self.rest_api.stop()
        self.ws_pub_api.stop()
        self.ws_pri_api.stop()

    def on_order(self, order: OrderData) -> None:
        """推送委托"""
        self.orders[order.orderid] = order      # 缓存委托数据
        super().on_order(order)                 # 然后正常推送

    def get_order(self, orderid: str) -> Optional[OrderData]:
        """获取委托"""
        return self.orders.get(orderid, None)


class OkexRestApi(RestClient):
    """"""

    def __init__(self, gateway: OkexGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: OkexGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.key: str = ""
        self.secret: str = ""
        self.passphrase: str = ""

        self.order_count: int = 10000
        self.order_count_lock: Lock = Lock()

        self.connect_time: int = 0

        self.simulated: bool = False

    def sign(self, request: Request) -> Request:
        """生成欧易V5签名"""
        # 签名
        timestamp: str = generate_timestamp()
        request.data = json.dumps(request.data)

        if request.params:
            path: str = request.path + "?" + urlencode(request.params)
        else:
            path: str = request.path

        msg: str = timestamp + request.method + path + request.data
        signature: bytes = generate_signature(msg, self.secret)

        # 添加请求头
        request.headers = {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": signature,
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
        session_number: int,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        """连接REST服务器"""
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        self.init(REST_HOST, proxy_host, proxy_port)
        self.start(session_number)
        self.gateway.write_log("REST API启动成功")

        self.query_time()
        self.query_contract()
        self.query_accounts()
        self.query_position()

    def new_order_id(self) -> int:
        """本地委托计数"""
        with self.order_count_lock:
            self.order_count += 1
            return self.order_count

    def send_order(self, req: OrderRequest) -> str:
        """
        委托下单
        
        1. 只支持单币种保证金模式
        2. 只支持全仓模式
        3. 只有币币交易和期权交易支持净仓模式，下单时请注意持仓方向
        """
        contract: ContractData = symbol_contract_map.get(req.symbol, None)
        if not contract:
            self.gateway.write_log(f"找不到该合约代码{req.symbol}")
            return

        # 生成本地委托号
        orderid: str = f"a{self.connect_time}_{self._new_order_id()}"

        data: dict = {
            "instId": req.symbol,
            "clOrdId": orderid,
            "side": SIDE_VT2OKEX[req.direction],
            "ordType": ORDERTYPE_VT2OKEX[req.type],
            "px": str(req.price)
        }
        if req.offset == Offset.OPEN:
            if req.direction == Direction.LONG:
                data["posSide"] = "long"
            else:
                data["posSide"] = "short"
        elif req.offset == Offset.CLOSE:
            if req.direction == Direction.LONG:
                data["posSide"] = "short"
            else:
                data["posSide"] = "long"

        if contract.product == Product.SPOT:
            data["tdMode"] = "cash"
            data["sz"] = str(req.volume)
        else:
            data["tdMode"] = "cross"
            data["sz"] = str(int(req.volume))

        order: OrderData = req.create_order_data(orderid, self.gateway_name)

        self.add_request(
            "POST",
            "/api/v5/trade/order",
            callback=self.on_send_order,
            data=data,
            extra=order,
            on_failed=self.on_send_order_failed,
            on_error=self.on_send_order_error,
        )

        self.gateway.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        data: dict = {
            "clOrdId": req.orderid,
            "instID": req.symbol
        }
        self.add_request(
            "POST",
            "/api/v5/trade/cancel-order/",
            data=data,
            callback=self.on_cancel_order,
            on_error=self.on_cancel_order_error,
            on_failed=self.on_cancel_order_failed,
            extra=req
        )

    def query_contract(self) -> None:
        """查询合约"""
        contracts: List[str] = ["SPOT", "SWAP", "FUTURES", "OPTION"]
        ulys: List[str] = ["EOS-USD", "ETH-USD", "BTC-USD"]
        for contract in contracts:
            if contract == "OPTION":
                for uly in ulys:
                    data: dict = {
                        "instType": "OPTION",
                        "uly": uly
                    }
                    self._query_contract(data)
            else:
                data: dict = {
                    "instType": contract
                }
                self._query_contract(data)

    def query_contract(self, data: dict) -> None:
        """查询合约请求"""
        self.add_request(
            "GET",
            "/api/v5/public/instruments",
            params=data,
            callback=self.on_query_contracts
        )

    def query_accounts(self) -> None:
        """查询资金"""
        self.add_request(
            "GET",
            "/api/v5/account/balance",
            callback=self.on_query_accounts
        )

    def query_orders(self) -> None:
        """查询未成交委托"""
        self.add_request(
            "GET",
            "/api/v5/trade/orders-pending",
            callback=self.on_query_order,
        )

    def query_position(self) -> None:
        """查询持仓"""
        self.add_request(
            "GET",
            "/api/v5/account/positions",
            callback=self.on_query_position
        )

    def query_time(self) -> None:
        """查询时间"""
        self.add_request(
            "GET",
            "/api/v5/public/time",
            callback=self.on_query_time
        )

    def on_query_contracts(self, data: dict, request: Request) -> None:
        """合约查询回报"""
        if not data["data"]:
            return

        for d in data["data"]:
            symbol: str = d["instId"]
            product: Product = PRODUCT_OKEX2VT[d["instType"]]
            size: float = float(d["ctMult"])

            contract: ContractData = ContractData(
                symbol=symbol,
                exchange=Exchange.OKEX,
                name=symbol,
                product=product,
                size=size,
                pricetick=float(d["tickSz"]),
                min_volume=float(d["minSz"]),
                history_data=True,
                gateway_name=self.gateway_name,
            )

            if product == Product.OPTION:
                contract.option_strike = float(d["stk"])
                contract.option_type = OPTIONTYPE_OKEXO2VT[d["optType"]]
                contract.option_expiry = parse_timestamp(d["expTime"])
                contract.option_portfolio = d["uly"]
                contract.option_index = d["stk"]
                contract.net_position = True
                contract.option_underlying = "_".join([
                    contract.option_portfolio,
                    contract.option_expiry.strftime("%Y%m%d")
                ])

            elif product == Product.SPOT:
                contract.net_position = True
                contract.size = 1

            self.gateway.on_contract(contract)

            symbol_contract_map[contract.symbol] = contract

        msg: str = f"{PRODUCT_VT2OKEX[product]}合约信息查询成功"
        self.gateway.write_log(msg)

        # 查询合约信息完毕后启动Websocket API
        self.gateway.ws_pub_api.start()
        self.gateway.ws_pri_api.start()

        # 查询合约信息完毕后查询未成交订单
        self.query_orders()

    def on_query_accounts(self, data: dict, request: Request) -> None:
        """资金查询回报"""
        for d in data["data"]:
            for detail in d["details"]:
                account: AccountData = parse_account_details(
                    detail,
                    self.gateway_name
                )
                self.gateway.on_account(account)

        self.gateway.write_log("账户资金查询成功")

    def on_query_position(self, data: dict, request) -> None:
        """持仓查询回报"""
        for d in data["data"]:
            symbol: str = d["instId"]
            position: PositionData = parse_position_data(
                d, 
                symbol,
                self.gateway_name
            )
            self.gateway.on_position(position)

    def on_query_order(self, data: dict, request: Request) -> None:
        """未成交委托查询回报"""
        for order_info in data["data"]:
            order: OrderData = parse_order_data(
                order_info,
                self.gateway_name
            )
            self.gateway.on_order(order)

    def on_query_time(self, data: dict, request: Request) -> None:
        """时间查询回报"""
        timestamp: int = int(data["data"][0]["ts"])
        server_time: datetime = datetime.fromtimestamp(timestamp/1000)
        local_time: datetime = datetime.now()
        msg: str = f"服务器时间：{server_time}，本机时间：{local_time}"
        self.gateway.write_log(msg)

    def on_send_order_failed(self, status_code: str, request: Request) -> None:
        """委托下单失败服务器报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        order.time = datetime.now().strftime("%H:%M:%S.%f")
        self.gateway.on_order(order)

        msg: str = f"委托失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_order_error(
        self,
        exception_type: type,
        exception_value: Exception,
        tb: TracebackType,
        request: Request
    ) -> None:
        """委托下单回报函数报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_send_order(self, data: dict, request: Request) -> None:
        """委托下单回报"""
        order: OrderData = request.extra

        if data["code"] != "0":
            order.status = Status.REJECTED
            self.gateway.on_order(order)

            for d in data["data"]:
                code: str = d["sCode"]
                msg: str = d["sMsg"]

            if code == "51000":
                self.gateway.write_log(f"委托失败, 状态码：{code}, 信息{msg}。请检查持仓方向")
            else:
                self.gateway.write_log(f"委托失败, 状态码：{code}, 信息{msg}")

    def on_cancel_order_error(
        self,
        exception_type: type,
        exception_value: Exception,
        tb: TracebackType,
        request: Request
    ) -> None:
        """委托撤单回报函数报错回报"""
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_cancel_order(self, data: dict, request: Request) -> None:
        """Websocket推送的委托撤单状态回报"""
        code: str = data["code"]
        if code == "0":
            self.gateway.write_log("撤单成功")
        else:
            for d in data["data"]:
                code: str = d["sCode"]
                msg: str = d["sMsg"]
                self.gateway.write_log(f"撤单失败, 状态码：{code}, 信息{msg}")

    def on_cancel_order_failed(self, status_code: int, request: Request) -> None:
        """委托撤单失败服务器报错回报"""
        req: CancelRequest = request.extra
        order: OrderData = self.gateway.get_order(req.orderid)
        if order:
            order.status = Status.REJECTED
            self.gateway.on_order(order)

    def on_failed(self, status_code: int, request: Request) -> None:
        """请求失败回报"""
        msg = f"请求失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_error(
        self,
        exception_type: type,
        exception_value: Exception,
        tb: TracebackType,
        request: Request
    ) -> None:
        """触发异常回报"""
        msg: str = f"触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb, request)
        )

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """
        查询历史数据

        K线数据每个粒度最多可获取最近1440条
        """
        buf: Dict[datetime, BarData] = {}
        end_time: str = ""
        path: str = "/api/v5/market/candles"

        for i in range(15):
            # 创建查询参数
            params: dict = {
                "instId": req.symbol,
                "bar": INTERVAL_VT2OKEX[req.interval]
            }

            if end_time:
                params["after"] = end_time

            # 从服务器获取响应
            resp: Response = self.request(
                "GET",
                path,
                params=params
            )

            # 如果请求失败则终止循环
            if resp.status_code // 100 != 2:
                msg = f"获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.gateway.write_log(msg)
                break
            else:
                data: dict = resp.json()

                if not data["data"]:
                    m = data["msg"]
                    msg = f"获取历史数据为空, {m}"
                    break

                for l in data["data"]:
                    ts, o, h, l, c, vol, _ = l
                    dt = parse_timestamp(ts)
                    bar = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=float(vol),
                        open_price=float(o),
                        high_price=float(h),
                        low_price=float(l),
                        close_price=float(c),
                        gateway_name=self.gateway_name
                    )
                    buf[bar.datetime] = bar

                begin: str = data["data"][-1][0]
                end: str = data["data"][0][0]
                msg: str = f"获取历史数据成功，{req.symbol} - {req.interval.value}，{parse_timestamp(begin)} - {parse_timestamp(end)}"
                self.gateway.write_log(msg)

                # 更新结束时间
                end_time = begin

        index: List[datetime] = list(buf.keys())
        index.sort()

        history: List[BarData] = [buf[i] for i in index]
        return history


class OkexWebsocketPublicApi(WebsocketClient):
    """"""

    def __init__(self, gateway: OkexGateway) -> None:
        """构造函数"""
        super().__init__()

        self.ping_interval: int = 20

        self.gateway: OkexGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.subscribed: Dict[str, SubscribeRequest] = {}
        self.callbacks: Dict[str, callable] = {}
        self.ticks: Dict[str, TickData] = {}

    def connect(
        self,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """连接Websocket公共频道"""
        if server == "REAL":
            self.init(PUBLIC_WEBSOCKET_HOST, proxy_host, proxy_port)
        else:
            self.init(TEST_PUBLIC_WEBSOCKET_HOST, proxy_host, proxy_port)

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        if req.symbol not in symbol_contract_map:
            self.gateway.write_log(f"找不到该合约代码{req.symbol}")
            return

        self.subscribed[req.vt_symbol] = req

        tick: TickData = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            name=req.symbol,
            datetime=datetime.now(),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol] = tick

        instId: str = req.symbol
        channel_ticker: dict = {
            "channel": "tickers",
            "instId": instId
        }
        channel_depth: dict = {
            "channel": "books5",
            "instId": instId
        }

        req: dict = {
            "op": "subscribe",
            "args": [channel_ticker, channel_depth]
        }
        self.send_packet(req)

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("Websocket Public API连接成功")
        self.subscribe_public_topic()

        for req in list(self.subscribed.values()):
            self.subscribe(req)

    def on_disconnected(self) -> None:
        """连接断开回报"""
        self.gateway.write_log("Websocket Public API连接断开")

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        if "event" in packet:
            event: str = packet["event"]
            if event == "subscribe":
                return
            elif event == "error":
                code: str = packet["code"]
                msg: str = packet["msg"]
                self.gateway.write_log(f"Websocket Public API请求异常, 状态码：{code}, 信息{msg}")

        else:
            channel: str = packet["arg"]["channel"]
            data: list = packet["data"]
            callback: callable = self.callbacks.get(channel, None)

            if callback:
                for d in data:
                    callback(d)

    def on_error(self, exception_type: type, exception_value: Exception, tb) -> None:
        """触发异常回报"""
        msg: str = f"公共频道触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb)
        )

    def subscribe_public_topic(self) -> None:
        """订阅公共频道"""
        self.callbacks["tickers"] = self.on_ticker
        self.callbacks["books5"] = self.on_depth

        # 必须至少订阅一个推送，否则若干秒后会被服务器断开连接
        req: SubscribeRequest = SubscribeRequest("BTC-USDT", Exchange.OKEX)
        self.subscribe(req)

    def on_ticker(self, d) -> None:
        """订阅行情回报"""
        symbol: str = d["instId"]
        tick: TickData = self.ticks.get(symbol, None)
        if not tick:
            return

        # 过滤掉最新成交价为0的数据
        last_price: float = float(d["last"])
        if not last_price:
            return

        tick.last_price = last_price
        tick.open_price = float(d["open24h"])
        tick.high_price = float(d["high24h"])
        tick.low_price = float(d["low24h"])
        tick.volume = float(d["vol24h"])
        tick.datetime = parse_timestamp(d["ts"])

        self.gateway.on_tick(copy(tick))

    def on_depth(self, d) -> None:
        """订阅深度行情回报"""
        symbol: str = d["instId"]
        tick: TickData = self.ticks.get(symbol, None)
        if not tick:
            return

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


class OkexWebsocketPrivateApi(WebsocketClient):
    """"""

    def __init__(self, gateway: OkexGateway) -> None:
        """构造函数"""
        super().__init__()

        self.ping_interval: int = 20

        self.gateway: OkexGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.key: str = ""
        self.secret: str = ""
        self.passphrase: str = ""

        self.callbacks: Dict[str, callable] = {}

    def connect(
        self,
        key: str,
        secret: str,
        passphrase: str,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """连接Websocket私有频道"""
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        if server == "REAL":
            self.init(PRIVATE_WEBSOCKET_HOST, proxy_host, proxy_port)
        else:
            self.init(TEST_PRIVATE_WEBSOCKET_HOST, proxy_host, proxy_port)

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("Websocket Private API连接成功")
        self.login()

    def on_disconnected(self) -> None:
        """连接断开回报"""
        self.gateway.write_log("Websocket Private API连接断开")

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        if "event" in packet:
            event: str = packet["event"]
            if event == "subscribe":
                return
            elif event == "error":
                code: str = packet["code"]
                msg: str = packet["msg"]
                self.gateway.write_log(f"Websocket Private API请求异常, 状态码：{code}, 信息{msg}")
            elif event == "login":
                self.on_login(packet)
        else:
            channel: str = packet["arg"]["channel"]
            data: list = packet["data"]
            callback: callable = self.callbacks.get(channel, None)

            if callback:
                for d in data:
                    callback(d)

    def on_error(self, exception_type: type, exception_value: Exception, tb) -> None:
        """触发异常回报"""
        msg: str = f"私有频道触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb)
        )

    def login(self) -> None:
        """用户登录"""
        timestamp: str = str(time.time())
        msg: str = timestamp + "GET" + "/users/self/verify"
        signature: bytes = generate_signature(msg, self.secret)

        req: dict = {
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
        self.send_packet(req)

        self.callbacks["login"] = self.on_login

    def subscribe_private_topic(self) -> None:
        """订阅私有频道"""
        self.callbacks["orders"] = self.on_order
        self.callbacks["account"] = self.on_account
        self.callbacks["positions"] = self.on_position

        # 订阅委托更新频道
        req = {
            "op": "subscribe",
            "args": [{
                "channel": "orders",
                "instType": "ANY"
            }]
        }
        self.send_packet(req)

        # 订阅账户更新频道
        req = {
            "op": "subscribe",
            "args": [{
                "channel": "account"
            }]
        }
        self.send_packet(req)

        # 订阅持仓更新频道
        req = {
            "op": "subscribe",
            "args": [{
                "channel": "positions",
                "instType": "ANY"
            }]
        }
        self.send_packet(req)

    def on_login(self, data: dict) -> None:
        """用户登录请求回报"""
        if data["code"] == '0':
            self.gateway.write_log("Websocket Private API登录成功")
            self.subscribe_private_topic()
        else:
            self.gateway.write_log("Websocket Private API登录失败")

    def on_order(self, data: dict) -> None:
        """委托更新推送"""
        order: OrderData = parse_order_data(data, gateway_name=self.gateway_name)
        self.gateway.on_order(copy(order))

        traded_volume: float = float(data.get("fillSz", 0))

        contract: ContractData = symbol_contract_map.get(order.symbol, None)
        if contract:
            traded_volume = round_to(traded_volume, contract.min_volume)

        if traded_volume != 0:
            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=data["tradeId"],
                direction=order.direction,
                offset=order.offset,
                price=float(data["fillPx"]),
                volume=traded_volume,
                datetime=order.datetime,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_trade(trade)

    def on_account(self, data: dict) -> None:
        """资金更新推送"""
        for detail in data["details"]:
            account: AccountData = parse_account_details(
                detail,
                self.gateway_name
            )
            self.gateway.on_account(account)

    def on_position(self, data: dict) -> None:
        """持仓更新推送"""
        symbol: str = data["instId"]
        if data["posSide"] == "long":
            long_position: PositionData = parse_position_data(
                data,
                symbol,
                self.gateway_name
            )
            self.gateway.on_position(long_position)
        elif data["posSide"] == "short":
            short_position: PositionData = parse_position_data(
                data,
                symbol,
                self.gateway_name
            )
            self.gateway.on_position(short_position)
        else:
            net_position: PositionData = parse_position_data(
                data,
                symbol,
                self.gateway_name
            )
            self.gateway.on_position(net_position)


def generate_signature(msg: str, secret_key: str) -> bytes:
    """生成签名"""
    return base64.b64encode(hmac.new(secret_key, msg.encode(), hashlib.sha256).digest())


def generate_timestamp() -> str:
    """生成时间戳"""
    now: datetime = datetime.utcnow()
    timestamp: str = now.isoformat("T", "milliseconds")
    return timestamp + "Z"


def parse_timestamp(timestamp: str) -> datetime:
    """解析回报时间戳"""
    dt: datetime = datetime.fromtimestamp(int(timestamp)/1000)
    return CHINA_TZ.localize(dt)


def parse_position_data(data: dict, symbol: str, gateway_name: str) -> PositionData:
    """解析回报数据为PositionData"""
    position: int = int(data["pos"])
    direction: Direction = DIRECTION_OKEX2VT.get(data['posSide'], None)
    if not direction:
        direction = Direction.NET

    availpos: float = get_float_value(data, "availPos")
    price: float = get_float_value(data, "avgPx")
    pnl: float = get_float_value(data, "upl")

    position: PositionData = PositionData(
        symbol=symbol,
        exchange=Exchange.OKEX,
        direction=direction,
        volume=position,
        frozen=float(position - availpos),
        price=price,
        pnl=pnl,
        gateway_name=gateway_name,
    )
    return position


def parse_account_details(detail: dict, gateway_name: str) -> AccountData:
    """解析回报数据为AccountData"""
    account: AccountData = AccountData(
        accountid=detail["ccy"],
        balance=float(detail["eq"]),
        frozen=float(detail["ordFrozen"]),
        gateway_name=gateway_name,
    )
    return account


def parse_order_data(data: dict, gateway_name: str) -> OrderData:
    """解析回报数据为OrderData"""
    posside: Direction = DIRECTION_OKEX2VT.get(data["posSide"], None)
    side: Direction = SIDE_OKEX2VT[data["side"]]

    order_id: str = data["clOrdId"]
    if not order_id:
        order_id: str = data["ordId"]

    order = OrderData(
        symbol=data["instId"],
        exchange=Exchange.OKEX,
        type=ORDERTYPE_OKEX2VT[data["ordType"]],
        orderid=order_id,
        direction=side,
        traded=float(data["accFillSz"]),
        price=float(data["px"]),
        volume=float(data["sz"]),
        datetime=parse_timestamp(data["uTime"]),
        status=STATUS_OKEX2VT[data["state"]],
        gateway_name=gateway_name,
    )

    if not posside or posside == Direction.NET:
        order.offset = Offset.NONE
    elif posside == Direction.LONG:
        if side == Direction.LONG:
            order.offset = Offset.OPEN
        else:
            order.offset = Offset.CLOSE
    else:
        if side == Direction.LONG:
            order.offset = Offset.CLOSE
        else:
            order.offset = Offset.OPEN
    return order


def get_float_value(data: dict, key: str) -> float:
    """获取字典中对应键的浮点数值"""
    if key not in data:
        return 0.0
    return float(data[key])
