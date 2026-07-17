import os
import sys
import time
import logging
from dotenv import load_dotenv
from openalgo import api

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Set to True to place actual orders. Keep False for Paper Trading / Dry Run.
LIVE_TRADING = True

# Order Parameters
SYMBOL = "RELIANCE"
EXCHANGE = "NSE"
QUANTITY = 1            # Number of shares to buy/sell
LIMIT_PRICE = 1305   # Set your custom trigger limit price here
ACTION = "BUY"          # "BUY" or "SELL"
PRODUCT = "CNC"         # "MIS" (Intraday) or "CNC" (Delivery) or "NRML" (F&O)
CHECK_INTERVAL_SECONDS = 60 # Check price every 1 minute

# Strategy Name
STRATEGY_NAME = os.getenv('STRATEGY_NAME', 'Reliance Multi-Broker Limit')

# Setup Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("reliance_order.log")
    ]
)
logger = logging.getLogger(__name__)

def execute_order(execution_client, ltp):
    if not LIVE_TRADING:
        msg = f"[DRY RUN] Target reached (LTP: ₹{ltp}). Would place LIMIT {ACTION} order: {QUANTITY} share(s) of {SYMBOL} at price {LIMIT_PRICE}"
        logger.info(msg)
        try:
            execution_client.telegram(username="rajan1112", message=msg)
        except Exception as e:
            logger.warning(f"Failed to send Telegram notification: {e}")
        return True

    logger.info(f"Target reached (LTP: ₹{ltp}). Placing LIMIT {ACTION} order on execution broker for {QUANTITY} share(s) of {SYMBOL} at price {LIMIT_PRICE}...")
    try:
        resp = execution_client.placeorder(
            strategy=STRATEGY_NAME,
            symbol=SYMBOL,
            action=ACTION,
            exchange=EXCHANGE,
            price_type="LIMIT",
            price=LIMIT_PRICE,
            product=PRODUCT,
            quantity=QUANTITY
        )
        logger.info(f"Order response: {resp}")
        
        # Send status update to Telegram
        msg = f"Order Success! {ACTION} {QUANTITY} {SYMBOL} @ LIMIT {LIMIT_PRICE} (Trigger LTP: ₹{ltp}). Response: {resp}"
        execution_client.telegram(username="rajan1112", message=msg)
        return True
        
    except Exception as e:
        logger.error(f"Failed to execute LIMIT order: {e}")
        try:
            execution_client.telegram(username="rajan1112", message=f"Order Failed! LIMIT {ACTION} {SYMBOL} @ {LIMIT_PRICE}. Error: {e}")
        except Exception:
            pass
        return False

def run_strategy():
    # Load environment variables from .env file
    load_dotenv()
    
    # --------------------------------------------------------------------------
    # Initialize DATA CLIENT (e.g. Dhan on Port 5000)
    # --------------------------------------------------------------------------
    data_api_key = os.getenv('OPENALGO_DATA_API_KEY') or os.getenv('OPENALGO_API_KEY')
    data_host    = os.getenv('OPENALGO_DATA_HOST') or os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
    
    if not data_api_key:
        logger.error("Error: OPENALGO_DATA_API_KEY or OPENALGO_API_KEY environment variable not set")
        sys.exit(1)
        
    data_client = api(api_key=data_api_key, host=data_host)
    logger.info(f"Initialized DATA client (getting prices). Server: {data_host}")

    # --------------------------------------------------------------------------
    # Initialize EXECUTION CLIENT (e.g. Zerodha on Port 5001)
    # --------------------------------------------------------------------------
    exec_api_key = os.getenv('OPENALGO_EXEC_API_KEY') or os.getenv('OPENALGO_API_KEY')
    exec_host    = os.getenv('OPENALGO_EXEC_HOST') or os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
    
    if not exec_api_key:
        logger.error("Error: OPENALGO_EXEC_API_KEY or OPENALGO_API_KEY environment variable not set")
        sys.exit(1)
        
    exec_client = api(api_key=exec_api_key, host=exec_host)
    logger.info(f"Initialized EXECUTION client (placing orders). Server: {exec_host}")

    logger.info(f"Starting continuous multi-broker monitoring for {SYMBOL}...")
    logger.info(f"Target Price: ₹{LIMIT_PRICE} | Action: {ACTION} | Interval: {CHECK_INTERVAL_SECONDS}s")
    
    try:
        while True:
            # Fetch current price (LTP) from the data client
            ltp = None
            try:
                quote = data_client.quotes(symbol=SYMBOL, exchange=EXCHANGE)
                if isinstance(quote, dict) and quote.get("status") == "success":
                    data = quote.get("data", {})
                    ltp = data.get("ltp")
                else:
                    logger.warning(f"Could not retrieve quotes data from data broker: {quote}")
            except Exception as e:
                logger.error(f"Error fetching quotes from data broker: {e}")

            if ltp is not None:
                logger.info(f"Current LTP of {SYMBOL} (from Data Broker): ₹{ltp} (Target: ₹{LIMIT_PRICE})")
                
                # Check trigger condition
                trigger_hit = False
                if ACTION.upper() == "BUY" and ltp <= LIMIT_PRICE:
                    logger.info(f"Buy Condition Met: LTP ₹{ltp} <= Limit Price ₹{LIMIT_PRICE}")
                    trigger_hit = True
                elif ACTION.upper() == "SELL" and ltp >= LIMIT_PRICE:
                    logger.info(f"Sell Condition Met: LTP ₹{ltp} >= Limit Price ₹{LIMIT_PRICE}")
                    trigger_hit = True
                
                if trigger_hit:
                    # Place order on the execution client
                    success = execute_order(exec_client, ltp)
                    if success:
                        logger.info("Order executed successfully. Exiting strategy monitoring.")
                        break
            else:
                logger.warning("LTP is unavailable. Will retry next minute.")

            # Sleep for the configured check interval (e.g. 1 minute)
            time.sleep(CHECK_INTERVAL_SECONDS)
            
    except KeyboardInterrupt:
        logger.info("Strategy execution interrupted by user. Exiting.")

if __name__ == "__main__":
    run_strategy()
