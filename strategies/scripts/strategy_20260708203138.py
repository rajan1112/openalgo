import os
import sys
import time
import logging
import datetime
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from openalgo import api

# ==============================================================================
# CONFIGURATION 
# ==============================================================================
# Set to True to place actual orders. Keep False for Paper Trading / Dry Run.
LIVE_TRADING = True

# Risk & Size Parameters
LOTS = 5
LOT_SIZE = 65  # Nifty 50 lot size for 2026
QUANTITY = LOTS * LOT_SIZE  # 325 shares

# Timezone Configuration
import pytz
IST = pytz.timezone("Asia/Kolkata")

# Time Constants
TRADE_START_TIME = datetime.time(9, 20)
TRADE_END_TIME = datetime.time(15, 20)
ENTRY_CUTOFF_TIME = datetime.time(15, 15)

# Strategy Name
STRATEGY_NAME = os.getenv('STRATEGY_NAME', 'Supertrend Pivots Nifty')

# Setup Logger
def ist_converter(secs=None):
    if secs is None:
        secs = time.time()
    return datetime.datetime.fromtimestamp(secs, IST).timetuple()

logging.Formatter.converter = staticmethod(ist_converter)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("strategy_run.log")
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# CORE INDICATOR FUNCTIONS
# ==============================================================================
def calculate_pivots(high, low, close):
    """Calculate daily R1 and S1 pivots based on previous day's OHLC."""
    P = (high + low + close) / 3
    R1 = (2 * P) - low
    S1 = (2 * P) - high
    return R1, S1

def calculate_supertrend(df, period=7, multiplier=3):
    """
    Calculate Supertrend (7, 3) on Nifty 5-minute candles.
    Returns df with 'supertrend' column (+1 for bullish, -1 for bearish).
    """
    high = df['high']
    low = df['low']
    close = df['close']
    
    # Calculate True Range (TR)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Calculate Average True Range (ATR)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    
    # Basic Upper & Lower Bands
    hl2 = (high + low) / 2
    basic_ub = hl2 + multiplier * atr
    basic_lb = hl2 - multiplier * atr
    
    final_ub = np.zeros(len(df))
    final_lb = np.zeros(len(df))
    trend = np.ones(len(df))
    
    final_ub[0] = basic_ub.iloc[0]
    final_lb[0] = basic_lb.iloc[0]
    
    for i in range(1, len(df)):
        # Upper Band
        if basic_ub.iloc[i] < final_ub[i-1] or close.iloc[i-1] > final_ub[i-1]:
            final_ub[i] = basic_ub.iloc[i]
        else:
            final_ub[i] = final_ub[i-1]
            
        # Lower Band
        if basic_lb.iloc[i] > final_lb[i-1] or close.iloc[i-1] < final_lb[i-1]:
            final_lb[i] = basic_lb.iloc[i]
        else:
            final_lb[i] = final_lb[i-1]
            
        # Trend Direction
        if close.iloc[i] > final_ub[i]:
            trend[i] = 1
        elif close.iloc[i] < final_lb[i]:
            trend[i] = -1
        else:
            trend[i] = trend[i-1]
            
    df['supertrend'] = trend
    return df

def adjust_dataframe_timezone(df):
    """
    Adjusts the index of history DataFrame to correct any timezone shifts
    introduced by naive datetime.timestamp() calls on non-IST servers.
    """
    if df is None or isinstance(df, dict) or df.empty:
        return df
    local_now = datetime.datetime.now()
    local_offset = local_now.astimezone().utcoffset() or datetime.timedelta(0)
    excess_shift = datetime.timedelta(hours=5, minutes=30) - local_offset
    if excess_shift != datetime.timedelta(0) and isinstance(df.index, pd.DatetimeIndex):
        df.index = df.index - excess_shift
    return df

# ==============================================================================
# INSTRUMENT & ORDER UTILITIES
# ==============================================================================
def get_nifty_pivots(client):
    """Fetch yesterday's Nifty daily OHLC to compute daily R1 and S1 pivots."""
    today = datetime.datetime.now(IST).date()
    start_date = (today - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")
    
    df = client.history(
        symbol="NIFTY",
        exchange="NSE_INDEX",
        interval="D",
        start_date=start_date,
        end_date=end_date
    )
    df = adjust_dataframe_timezone(df)
    
    if isinstance(df, dict) or df.empty:
        raise ValueError("Could not fetch sufficient Nifty daily historical data from OpenAlgo.")
    
    # Filter out today's candle to get the completed daily candles.
    completed_candles = df[df.index.date < today]
    if completed_candles.empty:
        raise ValueError("No completed daily candles found.")
        
    yesterday_candle = completed_candles.iloc[-1]
    
    logger.info(f"Yesterday's Nifty Spot OHLC: Open={yesterday_candle['open']:.2f}, High={yesterday_candle['high']:.2f}, Low={yesterday_candle['low']:.2f}, Close={yesterday_candle['close']:.2f}")
    return calculate_pivots(yesterday_candle['high'], yesterday_candle['low'], yesterday_candle['close'])

def get_nearest_expiry(client):
    """Get the nearest active Nifty option expiry date."""
    df = client.instruments(exchange="NFO")
    if isinstance(df, dict) or df.empty:
        raise ValueError("Failed to fetch instruments from NFO")
        
    # For broker-agnostic compatibility (works for both Zerodha and Dhan):
    # Dhan does not populate 'name' for options, so we match on 'symbol' starting with NIFTY followed by a digit.
    nifty_opts = df[df["symbol"].str.match(r"^NIFTY\d") & df["instrumenttype"].isin(["CE", "PE"])]
    if nifty_opts.empty:
        raise ValueError("No Nifty options found in NFO segment.")
        
    expiries = sorted(list(set(pd.to_datetime(nifty_opts["expiry"]).dt.date)))
    today = datetime.datetime.now(IST).date()
    for exp in expiries:
        if exp >= today:
            logger.info(f"Identified nearest Nifty Option Expiry: {exp}")
            return exp
    return expiries[0]

def find_option_contract(client, expiry, strike, option_type):
    """Find Nifty option contract trading symbol matching strike & expiry."""
    df = client.instruments(exchange="NFO")
    if isinstance(df, dict) or df.empty:
        logger.error("Failed to fetch instruments for matching contract.")
        return None
        
    # For broker-agnostic compatibility (works for both Zerodha and Dhan):
    matches = df[
        df["symbol"].str.match(r"^NIFTY\d") &
        (pd.to_datetime(df["expiry"]).dt.date == expiry) &
        (df["strike"].astype(float).astype(int) == int(strike)) &
        (df["instrumenttype"] == option_type)
    ]
    
    if not matches.empty:
        return matches.iloc[0]["symbol"]
    return None

def place_market_order(client, symbol, transaction_type, quantity):
    """Place a market order (Sell/Buy) on OpenAlgo."""
    if not LIVE_TRADING:
        msg = f"[DRY RUN] Would place market order: {transaction_type} {quantity} shares of {symbol}"
        logger.info(msg)
        client.telegram(username="trinetra1",message=msg)
        return {"status": "success", "order_id": "DRY_RUN_ID"}
        
        
    try:
        resp = client.placeorder(
            strategy=STRATEGY_NAME,
            symbol=symbol,
            action=transaction_type,
            exchange="NFO",
            price_type="MARKET",
            product="MIS",
            quantity=quantity
        )
        logger.info(f"Order placed successfully: {resp}")
        order_id = resp.get("orderid") or resp.get("order_id") or "UNKNOWN_ID"
        return {"status": "success", "order_id": order_id}
    except Exception as e:
        logger.error(f"Failed to place order for {symbol}: {e}")
        return {"status": "failed", "error": str(e)}

# ==============================================================================
# MAIN STRATEGY LOOP
# ==============================================================================
def run_strategy():
    # Load environment variables from .env file
    load_dotenv()
    
    ########
    data_api_key = os.getenv('OPENALGO_DATA_API_KEY') 
    data_host    = os.getenv('OPENALGO_DATA_HOST') 
    ws_url  = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')
    
    if not data_api_key:
        logger.error("Error: OPENALGO_DATA_API_KEY or OPENALGO_API_KEY environment variable not set")
        sys.exit(1)
        
    data_client = api(api_key=data_api_key, host=data_host,ws_url=ws_url)
    logger.info(f"Initialized DATA client (getting prices). Server: {data_host}")

    # --------------------------------------------------------------------------
    # Initialize EXECUTION CLIENT (e.g. Zerodha on Port 5001)
    # --------------------------------------------------------------------------
    exec_api_key = os.getenv('OPENALGO_EXEC_API_KEY')  
    exec_host    = os.getenv('OPENALGO_EXEC_HOST')  
    
    if not exec_api_key:
        logger.error("Error: OPENALGO_EXEC_API_KEY or OPENALGO_API_KEY environment variable not set")
        sys.exit(1)
        
    exec_client = api(api_key=exec_api_key, host=exec_host)
    logger.info(f"Initialized EXECUTION client (placing orders). Server: {exec_host}")


    ###########






    api_key = os.getenv('OPENALGO_API_KEY')
    host    = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
    ws_url  = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')
    
    if not api_key:
        logger.error("Error: OPENALGO_API_KEY environment variable not set")
        sys.exit(1)
        
    # Init client
    #client = api(api_key=api_key, host=host, ws_url=ws_url)
    exec_client.telegram(username="trinetra1",message="Strategy Started")
    

    # 1. Fetch Pivot Levels
    logger.info("Initializing strategy setup...")
    R1, S1 = get_nifty_pivots(data_client)
    logger.info(f"Today's Pivot Levels -> R1: {R1:.2f} | S1: {S1:.2f}")
    
    # 2. Get Expiry
    target_expiry = get_nearest_expiry(data_client)
    
    # State variables
    active_position = None  # Holds dict of active trade info or None
    trades_today = 0
    last_processed_timestamp = None
    
    logger.info("Starting live monitoring loop. Checking every 30 seconds...")
    
    try:
        while True:
            try:
                now = datetime.datetime.now(IST)
                current_time = now.time()
                
                # Restrict trade window
                if current_time < TRADE_START_TIME:
                    time.sleep(10)
                    continue
                    
                # 3:20 PM Hard Square-off
                if current_time >= TRADE_END_TIME:
                    if active_position:
                        logger.info("3:20 PM reached. Squaring off active positions...")
                        place_market_order(
                            exec_client, 
                            active_position["option_symbol"], 
                            "BUY", 
                            QUANTITY
                        )
                        active_position = None
                    logger.info("Trading window closed for today. Exiting.")
                    break
                    
                # Fetch latest 5m candles to calculate Supertrend
                # Fetch last 3 days of 5m candles to ensure stable ATR calculation
                start_date = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
                end_date = now.strftime("%Y-%m-%d")
                
                df = data_client.history(
                    symbol="NIFTY",
                    exchange="NSE_INDEX",
                    interval="5m",
                    start_date=start_date,
                    end_date=end_date
                )
                df = adjust_dataframe_timezone(df)
                
                if isinstance(df, dict) or df.empty:
                    logger.warning("Waiting for sufficient 5m spot candles data...")
                    time.sleep(30)
                    continue
                    
                # Verify required columns
                required_cols = ['open', 'high', 'low', 'close']
                if not all(col in df.columns for col in required_cols):
                    logger.warning("Missing required OHLC columns in DataFrame. Retrying...")
                    time.sleep(30)
                    continue
                
                df = calculate_supertrend(df)
                
                # Use the last completed candle (index -2) to make decisions
                completed_candle = df.iloc[-2]
                completed_ts = df.index[-2]
                
                if completed_ts != last_processed_timestamp:
                    last_processed_timestamp = completed_ts
                    nifty_close = completed_candle['close']
                    supertrend = completed_candle['supertrend']
                    
                    # Check Signals
                    bullish_signal = (nifty_close > R1) and (supertrend == 1)
                    bearish_signal = (nifty_close < S1) and (supertrend == -1)
                    
                    # Exit Logic: Stop Loss if Supertrend direction reverses
                    if active_position:
                        sl_triggered = False
                        if active_position["type"] == "SHORT_PUT" and supertrend == -1:
                            logger.info(f"Supertrend reversed to bearish on completed candle {completed_ts}. Stop Loss triggered for SHORT_PUT.")
                            sl_triggered = True
                        elif active_position["type"] == "SHORT_CALL" and supertrend == 1:
                            logger.info(f"Supertrend reversed to bullish on completed candle {completed_ts}. Stop Loss triggered for SHORT_CALL.")
                            sl_triggered = True
                            
                        if sl_triggered:
                            resp = place_market_order(
                                exec_client, 
                                active_position["option_symbol"], 
                                "BUY", 
                                QUANTITY
                            )
                            if resp["status"] == "success":
                                active_position = None
                                
                    # Entry Logic: Trigger trade if conditions met & limit not hit
                    if not active_position and trades_today < 3 and current_time < ENTRY_CUTOFF_TIME:
                        if bullish_signal:
                            atm_strike = int(round(nifty_close / 50) * 50)
                            opt_symbol = find_option_contract(data_client, target_expiry, atm_strike, "PE")
                            if opt_symbol:
                                logger.info(f"Bullish signal triggered on completed candle {completed_ts}. Selling ATM Put: {opt_symbol} at strike {atm_strike}")
                                resp = place_market_order(data_client, opt_symbol, "SELL", QUANTITY)
                                if resp["status"] == "success":
                                    active_position = {
                                        "type": "SHORT_PUT",
                                        "option_symbol": opt_symbol,
                                        "strike": atm_strike
                                    }
                                    trades_today += 1
                                    
                        elif bearish_signal:
                            atm_strike = int(round(nifty_close / 50) * 50)
                            opt_symbol = find_option_contract(data_client, target_expiry, atm_strike, "CE")
                            if opt_symbol:
                                logger.info(f"Bearish signal triggered on completed candle {completed_ts}. Selling ATM Call: {opt_symbol} at strike {atm_strike}")
                                resp = place_market_order(exec_client, opt_symbol, "SELL", QUANTITY)
                                if resp["status"] == "success":
                                    active_position = {
                                        "type": "SHORT_CALL",
                                        "option_symbol": opt_symbol,
                                        "strike": atm_strike
                                    }
                                    trades_today += 1
                    
                    # Print status update on new candle completion
                    pos_desc = active_position['option_symbol'] if active_position else "FLAT"
                    logger.info(
                        f"New Completed Candle: {completed_ts} | Close: {nifty_close:.2f} | "
                        f"ST: {'BULL' if supertrend == 1 else 'BEAR'} | Position: {pos_desc} | Trades Today: {trades_today}/3"
                    )
                
                # Sleep until next check (30 seconds)
                time.sleep(30)
                
            except Exception as e:
                logger.error(f"Error in strategy monitor loop: {e}", exc_info=True)
                time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Strategy execution interrupted by user. Exiting.")

if __name__ == "__main__":
    logger.info("Initializing OpenAlgo Trading Engine...")
    run_strategy()
