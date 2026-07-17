import os
import sys
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
LIMIT_PRICE = 1305   # Set your custom limit price here
ACTION = "BUY"          # "BUY" or "SELL"
PRODUCT = "CNC"         # "MIS" (Intraday) or "CNC" (Delivery) or "NRML" (F&O)

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
        msg = f"[DRY RUN] Current Price: ₹{ltp if ltp else 'N/A'}. Would place LIMIT {ACTION} order: {QUANTITY} share(s) of {SYMBOL} at price {LIMIT_PRICE}"
        logger.info(msg)
        try:
            execution_client.telegram(username="rajan1112", message=msg)
        except Exception as e:
            logger.warning(f"Failed to send Telegram notification: {e}")
        return True

    logger.info(f"Placing LIMIT {ACTION} order on execution broker for {QUANTITY} share(s) of {SYMBOL} at price {LIMIT_PRICE} (LTP: ₹{ltp if ltp else 'N/A'})...")
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
        msg = f"Order Success! {ACTION} {QUANTITY} {SYMBOL} @ LIMIT {LIMIT_PRICE} (LTP: ₹{ltp if ltp else 'N/A'}). Response: {resp}"
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
    data_api_key = os.getenv('OPENALGO_DATA_API_KEY') 
    data_host    = os.getenv('OPENALGO_DATA_HOST') 
    
    if not data_api_key:
        logger.error("Error: OPENALGO_DATA_API_KEY or OPENALGO_API_KEY environment variable not set")
        sys.exit(1)
        
    data_client = api(api_key=data_api_key, host=data_host)
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

    # Fetch current price (LTP) once from the data client
    ltp = None
    try:
        quote = data_client.quotes(symbol=SYMBOL, exchange=EXCHANGE)
        if isinstance(quote, dict) and quote.get("status") == "success":
            data = quote.get("data", {})
            ltp = data.get("ltp")
            logger.info(f"Current LTP of {SYMBOL} (from Data Broker): ₹{ltp}")
        else:
            logger.warning(f"Could not retrieve quotes data from data broker: {quote}")
    except Exception as e:
        logger.error(f"Error fetching quotes from data broker: {e}")

    # Place order on the execution client immediately
    execute_order(exec_client, ltp)

if __name__ == "__main__":
    run_strategy()
