import asyncio
import json
from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import os
import shioaji as sj
from dotenv import load_dotenv
from shioaji import TickFOPv1, BidAskFOPv1, Exchange
from datetime import datetime
from loguru import logger
import uvicorn

logger.remove()

logger.add(
    "log.txt",
    level="INFO",
    retention="7 days",
    enqueue=True,
)

load_dotenv()

API_KEY = os.getenv('API_Key')
SECRET_KEY = os.getenv('Secret_Key')

api = sj.Shioaji()
api.login(API_KEY, SECRET_KEY, contracts_timeout=5000)
logger.info(api.Contracts.status)

main_loop = None 
index_data = None
future_data = None

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        if not self.active_connections: return
        disconnected = []
        for connection in self.active_connections:
            try:
                await asyncio.wait_for(connection.send_text(message), timeout=1.0)
            except asyncio.TimeoutError:
                disconnected.append(connection)
            except Exception:
                disconnected.append(connection)
        # 移除已斷線的連線
        for conn in disconnected:
            if conn in self.active_connections:
                self.active_connections.remove(conn)

manager = ConnectionManager()

# 處理系統事件
@api.quote.on_event
def event_callback(resp_code: int, event_code: int, info: str, event: str):
    logger.info(f'Response Code: {resp_code} | Event Code: {event_code} | Info: {info} | Event: {event}')

# 忽略委託成交回報
def order_cb(stat, msg):
    return

api.set_order_callback(order_cb)

# 獲取所有選擇權
def get_all_open_contracts_delivery_date():
    contracts_list = {}
    option_contracts = api.Contracts.Options
    tx_options = [name for name in dir(option_contracts) if name.startswith('TX')]
    for code_name in tx_options:
        if hasattr(api.Contracts.Options, code_name):
            contract_stream = getattr(api.Contracts.Options, code_name)
            monthly_dates = sorted(list(set(contract.delivery_date for contract in contract_stream)))
            for monthly_date in monthly_dates:
                contracts_list[monthly_date] = code_name
        else:
            logger.warning(f"找不到商品代號 {code_name}")
    return sorted(contracts_list.keys()), contracts_list

# 根據市場價獲取訂閱清單
def get_subscribe_list(code_name, delivery_date, index):

    if hasattr(api.Contracts.Options, code_name):

        call_contracts = []
        put_contracts = []

        contract_stream = getattr(api.Contracts.Options, code_name)
        call_contracts = [c for c in contract_stream if c.strike_price > index-200 and c.strike_price < index+2000 and c.option_right == 'C' and c.delivery_date == delivery_date]
        call_contracts.sort(key=lambda x: x.strike_price)

        put_contracts = [c for c in contract_stream if c.strike_price < index+200 and c.strike_price > index-2000 and c.option_right == 'P' and c.delivery_date == delivery_date]
        put_contracts.sort(key=lambda x: x.strike_price, reverse=True)

        return call_contracts, put_contracts
    else:
        logger.warning(f"找不到商品代號 {code_name}")
        return [], []

@api.quote.on_quote 
def quote_callback(topic: str, quote: dict): 
    if topic == "I/TSE/001":
        data = {
            "type": "index",
            "code": "大盤指數",
            "price": float(quote.get("Close")),
            "volume": int(quote.get("VolSum")),
            "time": f"{quote.get("Date")} {quote.get("Time")[:11]}"
        }
        index_data.update(data)

        if main_loop and manager.active_connections and data:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps(data)), 
                main_loop
            )

@api.on_tick_fop_v1()
def tick_callback(exchange: Exchange, tick: TickFOPv1):
    data = {}
    if "TXF" in tick.code:

        data = {
            "type": "future",
            "code": "近月期貨",
            "price": float(tick.close),
            "volume": int(tick.volume),
            "time": str(tick.datetime)[:22]
        }
        future_data.update(data)

        if main_loop and manager.active_connections and data:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps(data)), 
                main_loop
            )

@api.on_bidask_fop_v1()
def bidask_callback(exchange: Exchange, bidask: BidAskFOPv1):
    data = {
        "type": "bidask",
        "code": bidask.code,
        "bid": float(bidask.bid_price[0]) if bidask.bid_price else 0,
        "ask": float(bidask.ask_price[0]) if bidask.ask_price else 0,
        "bid_vol": int(bidask.bid_volume[0]) if bidask.bid_volume else 0,
        "ask_vol": int(bidask.ask_volume[0]) if bidask.ask_volume else 0,
        "time": str(bidask.datetime)
    }

    if main_loop and manager.active_connections:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(json.dumps(data)), 
            main_loop
        )

# 啟動與關閉
@asynccontextmanager
async def lifespan(app: FastAPI):
    
    api_usage = api.usage()  
    remaining_bytes = float(api_usage.remaining_bytes)/1024
    logger.info(f"伺服器啟動... (連線數量 : {api_usage.connections})")
    logger.info(f"剩餘流量: {remaining_bytes/1024:.2f} MB ({remaining_bytes:.0f} KB)")
    print(f"剩餘流量: {remaining_bytes/1024:.2f} MB ({remaining_bytes:.0f} KB)")

    global main_loop
    main_loop = asyncio.get_running_loop()
    
    TXF = api.Contracts.Futures.TXF.TXFR1
    TSE = api.Contracts.Indexs.TSE.TSE001

    try:
        index_snapshot = api.snapshots([TXF, TSE])
        snapshots_used_bytes = remaining_bytes - float(api.usage().remaining_bytes)/1024
        logger.info(f"查詢 snapshot 使用流量: {snapshots_used_bytes/1024:.2f} MB ({snapshots_used_bytes:.0f} KB)")
        print(f"查詢 snapshot 使用流量: {snapshots_used_bytes/1024:.2f} MB ({snapshots_used_bytes:.0f} KB)")

        global index_data
        global future_data

        index_data = {"type": "index","code": "大盤指數","price": float(index_snapshot[1].close),"volume": int(index_snapshot[1].volume),"time": str(datetime.fromtimestamp(index_snapshot[1].ts/1e9 - 28800))}
        future_data = {"type": "future","code": "近月期貨","price": float(index_snapshot[0].close),"volume": int(index_snapshot[0].volume),"time": str(datetime.fromtimestamp(index_snapshot[0].ts/1e9 - 28800))[:22]}

        app.state.setup_index_price = [future_data, index_data]
    
    except Exception as e:
        logger.error(f"查詢 snapshot 發生錯誤: {e}")
        index_data = {"type": "index","code": TSE.exchange + TSE.code,"price": 0.0,"volume": 0,"time": ""}
        future_data = {"type": "future","code": TXF.code,"price": 0.0,"volume": 0,"time": ""}
    
    logger.info(f"訂閱大盤... 初始指數價格: {index_data['price']}  期貨價格: {future_data['price']}")
    api.quote.subscribe(
        TXF, 
        quote_type = sj.constant.QuoteType.Tick,
        version = sj.constant.QuoteVersion.v1
    )

    api.quote.subscribe(
        TSE, 
        quote_type = sj.constant.QuoteType.Tick,
        version = sj.constant.QuoteVersion.v1
    )

    app.state.subscribe_date = "123456"
    app.state.subscribe_list = []
    app.state.strategies = []

    try:
        yield
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("伺服器關閉...")
        try:
            ending_remaining_bytes = float(api.usage().remaining_bytes)
            used_bytes = remaining_bytes - ending_remaining_bytes/1024
            logger.info(f"使用流量: {used_bytes/1024:.2f} MB ({used_bytes:.0f} KB)")
            logger.info(f"剩餘流量: {ending_remaining_bytes/1024/1024:.2f} MB ({ending_remaining_bytes/1024:.0f} KB)")
            print(f"使用流量: {used_bytes/1024:.2f} MB ({used_bytes:.0f} KB)")
            print(f"剩餘流量: {ending_remaining_bytes/1024/1024:.2f} MB ({ending_remaining_bytes/1024:.0f} KB)")
            api.logout()
            logger.info("已安全登出")
        except Exception as e:
            logger.error(f"登出時發生錯誤: {e}")
    

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def get():
    return FileResponse("index.html")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        logger.info("重新建立連線")
        await send_initial_data(websocket)

        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)

            if data.get("type") == "change_date":
                new_date = data.get("date")
                logger.info(f"使用者切換日期至: {new_date}")
                await send_initial_data(websocket, new_date)

    except WebSocketDisconnect:
        manager.disconnect(websocket)

async def send_initial_data(websocket: WebSocket, target_date: str = ""):

    await switch_contract_subscription(target_date)

    config = {
        "type": "config",
        "strategies": app.state.strategies,
        "available_dates":app.state.available_dates,
        "current_date": app.state.subscribe_date
    }
    await websocket.send_text(json.dumps(config))
    await websocket.send_text(json.dumps(index_data))
    await websocket.send_text(json.dumps(future_data))

async def switch_contract_subscription(target_date=""):

    all_delivery_dates, contracts_list = get_all_open_contracts_delivery_date()
    app.state.available_dates = all_delivery_dates

    if app.state.subscribe_date == target_date or (app.state.subscribe_date == all_delivery_dates[0] and target_date == ""):
        return

    if app.state.subscribe_list:
        logger.info("取消訂閱舊合約...")
        for sub in app.state.subscribe_list:
            api.quote.unsubscribe(sub, sj.constant.QuoteType.BidAsk)
        app.state.subscribe_list.clear()
        logger.info("取消訂閱舊合約完成")

    index_price = future_data["price"] if future_data["time"] > index_data["time"] else index_data["price"]
    

    app.state.subscribe_date = target_date if target_date else all_delivery_dates[0]
    call, put = get_subscribe_list(contracts_list[app.state.subscribe_date], app.state.subscribe_date, index_price)

    app.state.strategies.clear()

    for i in range(len(call)-1):
        app.state.strategies.append({
            "side": "Call",
            "short_code": call[i].code,
            "short_desc": f"Sell {call[i].strike_price}",
            "long_code": call[i+1].code,
            "long_desc": f"Buy {call[i+1].strike_price}"
        })

    for i in range(len(put)-1):
        app.state.strategies.append({
            "side": "Put",
            "short_code": put[i].code,
            "short_desc": f"Sell {put[i].strike_price}",
            "long_code": put[i+1].code,
            "long_desc": f"Buy {put[i+1].strike_price}"
        })

    logger.info("訂閱新合約...")
    for c in call + put:
        api.quote.subscribe(
            c, 
            quote_type = sj.constant.QuoteType.BidAsk,
            version = sj.constant.QuoteVersion.v1
        )
    
    
    app.state.subscribe_list = call + put

    logger.info(f"訂閱新合約完成，共訂閱 {len(call) + len(put)} 檔合約")


if __name__ == "__main__":
    logger.info("啟動伺服器...")
    uvicorn.run(app, port=8000)
    logger.info("伺服器已關閉")