# trading.py

# import MetaTrader5 as mt5
import pandas as pd
import time
from datetime import datetime
import logging
from config import INITIAL_VOLUME, MIN_PROFIT_THRESHOLD, MIN_WIN_RATE, TIMEFRAME, PROFIT_LOCK_PERCENTAGE, MAX_CONSECUTIVE_LOSSES, mt5, POSITION_REVERSAL_THRESHOLD, NEUTRAL_CONFIDENCE_THRESHOLD, NEUTRAL_HOLD_DURATION, SHUTDOWN_EVENT
from models import trading_state, TradingStatistics
from ml_predictor import MLPredictor
from logging_config import setup_comprehensive_logging

setup_comprehensive_logging()

# Set up logging
#logging.basicConfig(
#    level=logging.INFO,
#    format='%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
#    datefmt='%Y-%m-%d %H:%M:%S',
#    handlers=[
#        logging.FileHandler('trading_bot.log'),
#        logging.StreamHandler()
#    ]
#)

def calculate_win_rate(trades):
    if not trades:
        return 0
    winning_trades = sum(1 for profit in trades if profit > 0)
    return winning_trades / len(trades)

def adjust_trading_parameters(symbol, profit):
    """Dynamically adjust trading parameters based on performance"""
    state = trading_state.symbol_states[symbol]

    # Add recent trade direction to memory
    state.recent_trade_directions.append('buy' if profit > 0 else 'sell')
    
    # Limit memory size
    if len(state.recent_trade_directions) > state.trade_direction_memory_size:
        state.recent_trade_directions.pop(0)
    
    # Update consecutive losses and total profit
    state.trades_count += 1
    state.trades_history.append(profit)
    
    # Update win rate
    state.win_rate = calculate_win_rate(state.trades_history[-10:])  # Consider last 10 trades
    
    # Adjust volume based on performance
    if state.win_rate > 0.6:  # Increase volume if winning consistently
        state.volume = min(state.volume * 1.2, INITIAL_VOLUME * 2)
    elif state.win_rate < 0.4:  # Decrease volume if losing
        state.volume = max(state.volume * 0.8, INITIAL_VOLUME * 0.5)
    
    # Adjust profit threshold based on volatility
    if profit > state.profit_threshold:
        state.profit_threshold *= 1.1  # Increase threshold on good performance
    elif profit < 0:
        state.profit_threshold = max(MIN_PROFIT_THRESHOLD, state.profit_threshold * 0.9)

def should_trade_symbol(symbol):
    """Determine if we should trade a symbol based on its performance"""
    state = trading_state.symbol_states[symbol]
        
    # Check recent trade performance
    recent_trades = state.trades_history[-3:]  # Last 3 trades
    if recent_trades:
        recent_performance = sum(recent_trades)
        
        # If recent trades have been consistently losing
        if recent_performance < 0:
            if state.last_trade_time and (datetime.now() - state.last_trade_time).total_seconds() < 120:  # 2-minute cooling period
                logging.info(f"{symbol} trade suppressed due to recent poor performance")
                return False
        
        # Prevent trading if recent trades are too volatile
        trade_variance = max(recent_trades) - min(recent_trades)
        if trade_variance > state.profit_threshold * 2:
            logging.info(f"{symbol} trade suppressed due to high trade volatility")
            return False
    
    if state.is_restricted:
        # Check if enough time has passed to retry
        if state.last_trade_time and (datetime.now() - state.last_trade_time).total_seconds() / 3600 < 1:
            return False
        
        # Reset restriction if conditions improve
        if state.win_rate > MIN_WIN_RATE:
            state.is_restricted = False
            logging.info(f"{symbol} restrictions lifted due to improved performance")
            return True
        return False
    
    return True

def get_market_volatility(symbol, timeframe=mt5.TIMEFRAME_M1, periods=20):
    """Calculate current market volatility"""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, periods)
    if rates is None:
        return None
    
    df = pd.DataFrame(rates)
    return df['high'].max() - df['low'].min()

def calculate_indicators(df, symbol):
    """Enhanced indicator calculation with volatility consideration"""
    params = trading_state.ta_params
    
    # Basic indicators
    df['SMA_short'] = df['close'].rolling(window=params.sma_short).mean()
    df['SMA_long'] = df['close'].rolling(window=params.sma_long).mean()
    
    # Enhanced RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=params.rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=params.rsi_period).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # Volatility indicators
    df['ATR'] = df['high'].rolling(window=params.atr_period).max() - df['low'].rolling(window=params.atr_period).min()
    df['Volatility'] = df['close'].rolling(window=10).std()
    
    # Trend strength
    df['ADX'] = calculate_adx(df)
    
    return df

def calculate_adx(df, period=14):
    """Calculate Average Directional Index"""
    df['TR'] = pd.DataFrame({
        'HL': (df['high'] - df['low']).abs(),
        'HD': (df['high'] - df['close'].shift(1)).abs(),
        'LD': (df['low'] - df['close'].shift(1)).abs()
    }).max(axis=1)
    
    df['+DM'] = (df['high'] - df['high'].shift(1)).clip(lower=0)
    df['-DM'] = (df['low'].shift(1) - df['low']).clip(lower=0)
    
    df['+DI'] = 100 * (df['+DM'].rolling(window=period).mean() / df['TR'].rolling(window=period).mean())
    df['-DI'] = 100 * (df['-DM'].rolling(window=period).mean() / df['TR'].rolling(window=period).mean())
    
    df['DX'] = 100 * ((df['+DI'] - df['-DI']).abs() / (df['+DI'] + df['-DI']))
    return df['DX'].rolling(window=period).mean()

def calculate_trend_score(current):
    """Calculate trend score based on traditional indicators"""
    trend_score = 0

    # ADX trend strength
    if current.ADX > 25:
        trend_score += 2

    # Moving average alignment
    if current.close > current.SMA_short > current.SMA_long:
        trend_score += 1
    elif current.close < current.SMA_short < current.SMA_long:
        trend_score -= 1

    # RSI extremes with trend confirmation
    if current.RSI < trading_state.ta_params.rsi_oversold and trend_score > 0:
        trend_score += 2
    elif current.RSI > trading_state.ta_params.rsi_overbought and trend_score < 0:
        trend_score -= 2

    return trend_score


def get_signal(symbol):
    """Enhanced signal generation with ML model integration and neutral state"""
    # Early exit checks
    if not should_trade_symbol(symbol):
        return None, None, 0

    # Fetch and validate historical rates
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 100)
    if rates is None:
        logging.warning(f"No rates available for {symbol}")
        return None, None, 0

    # Prepare data for analysis
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = calculate_indicators(df, symbol)
    current = df.iloc[-1]

    # Initial trend score calculation
    trend_score = calculate_trend_score(current)
    state = trading_state.symbol_states[symbol]

    try:
        # ML Prediction Integration
        ml_predictor = MLPredictor(symbol)
        ml_signal, ml_confidence, ml_predicted_return = ml_predictor.predict()

        # Neutral State Detection
        if (
            ml_confidence <= NEUTRAL_CONFIDENCE_THRESHOLD
            or abs(ml_predicted_return) < 0.0001
        ):
            logging.info(f"{symbol} entered NEUTRAL state")
            state.neutral_start_time = datetime.now()
            return "neutral", current.ATR, 0

        # Check if symbol is in neutral hold
        if state.neutral_start_time:
            neutral_duration = (
                datetime.now() - state.neutral_start_time
            ).total_seconds()
            if neutral_duration < NEUTRAL_HOLD_DURATION:
                logging.info(f"{symbol} still in neutral hold")
                return None, None, 0
            else:
                state.neutral_start_time = None  # Reset neutral state

        # Trade Direction Repetition Prevention
        if state.recent_trade_directions:
            recent_direction_count = state.recent_trade_directions.count(ml_signal)
            if recent_direction_count >= 2:
                logging.info(
                    f"{symbol} suppressing {ml_signal} due to recent similar trades"
                )
                return None, None, 0

        # Confidence and Prediction Quality Checks
        if not (ml_signal and ml_confidence >= 0.6):
            logging.info(f"{symbol} insufficient ML prediction confidence")
            return None, None, 0

        # Predicted Return Quality
        if abs(ml_predicted_return) < 0.0001 or ml_predicted_return < 0:
            logging.info(f"{symbol} suppressed: low/negative predicted return")
            return None, None, 0

        # ML Signal Impact on Trend Score
        if ml_signal == "buy" and ml_confidence > 0.6:
            trend_score += 2  # Boost buy confidence
        elif ml_signal == "sell" and ml_confidence > 0.6:
            trend_score -= 2  # Boost sell confidence

        # Potential Profit Calculation
        potential_profit = current.ATR * abs(trend_score) * 10

        # Conservative Mode Adjustments
        if trading_state.is_conservative_mode:
            required_score = 3
            potential_profit *= 0.8
        else:
            required_score = 2

        # Detailed Logging
        logging.info(
            f"********{symbol} Analysis: "
            f"********Trend Score: {trend_score}, "
            f"********ML Signal: {ml_signal}, "
            f"********Confidence: {ml_confidence:.2f}, "
            f"********Predicted Return: {ml_predicted_return:.5f}"
        )

        # Final Signal Determination
        if trend_score >= required_score:
            return "buy", current.ATR, potential_profit
        elif trend_score <= -required_score:
            return "sell", current.ATR, potential_profit

        return ml_signal, current.ATR, ml_predicted_return

    except Exception as e:
        logging.error(f"ML prediction error for {symbol}: {e}")
        return None, None, 0


def manage_open_positions(symbol, trading_stats=None):
    """Enhanced position management with position reversal"""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return

    state = trading_state.symbol_states[symbol]

    for position in positions:
        # Track position open time
        position_open_time = datetime.fromtimestamp(position.time)
        current_time = datetime.now()
        time_since_open = (current_time - position_open_time).total_seconds()

        current_profit = position.profit

        # Position Reversal Logic
        if current_profit <= POSITION_REVERSAL_THRESHOLD:
            # Close current position
            close_result = close_position(position)

            if close_result:
                # Determine reversal direction
                reversal_direction = (
                    "sell" if position.type == mt5.ORDER_TYPE_BUY else "buy"
                )

                # Get current market volatility for order parameters
                atr = get_market_volatility(symbol)

                logging.info(
                    f"{symbol} position reversed due to significant loss: {current_profit}"
                )

                # Place new position in opposite direction
                place_order(symbol, reversal_direction, atr, state.volume * 1.5)   # increase trade volume after reversal
                logging.info(
                    f"|||||||| {symbol} new position opened in opposite direction: {reversal_direction} |||||||||"
                )

                # Adjust trading parameters
                adjust_trading_parameters(symbol, current_profit)
                
                if close_result and trading_stats:
                    trading_stats.log_position_reversal(symbol)

                continue

        # other close conditions
        if time_since_open >= 30:
            if current_profit < 0:
                close_result = close_position(position)
                if close_result:
                    logging.info(
                        f"------- {symbol} position closed after 30 seconds due to negative profit --------"
                    )
                    adjust_trading_parameters(symbol, current_profit)
        else:
            if current_profit <= -15.80:
                close_result = close_position(position)
                if close_result:
                    logging.info(
                        f"-------- {symbol} position closed early due to significant profit drop --------"
                    )
                    adjust_trading_parameters(symbol, current_profit)


def place_order(symbol, direction, atr, volume, trading_stats=None, is_ml_signal=False):
    """Place a trading order with dynamic stop loss and take profit based on ATR"""
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logging.error(f"Failed to get symbol info for {symbol}")
        return False

    # Get current price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        logging.error(f"Failed to get price for {symbol}")
        return False

    # Adjust volume to meet minimum requirements
    volume = max(symbol_info.volume_min, min(volume, symbol_info.volume_max))

    # Calculate order parameters
    sl_distance = atr * 5  # Stop loss at 5 * ATR
    tp_distance = atr * 2.5  # Take profit at 2.5 * ATR

    if direction == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        sl = price - sl_distance
        tp = price + tp_distance
    else:  # sell
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        sl = price + sl_distance
        tp = price - tp_distance

    # Use the default filling mode based on what we see in the symbol info
    # From your log, filling_mode is 1 for all symbols
    filling_type = mt5.ORDER_FILLING_FOK

    # Prepare the trade request
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 234000,
        "comment": f"python script {direction}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    # data for analysis
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 50)
    if rates is None:
        logging.error(f"Failed to fetch rates for {symbol}")
        return None, None, 0
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = calculate_indicators(df, symbol)
    current = df.iloc[-1]

    # Initial trend score calculation
    trend_score = calculate_trend_score(current)

    potential_profit = current.ATR * abs(trend_score) * 10

    # Send the order
    result = mt5.order_send(request)

    if result is None:
        logging.error(f"Order failed for {symbol}: No result returned")
        return False

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logging.error(
            f"Order failed for {symbol}: {result.comment} (Error code: {result.retcode})"
        )
        logging.info(f"Symbol {symbol} filling mode: {symbol_info.filling_mode}")

        # Try alternative filling mode if first attempt fails
        request["type_filling"] = mt5.ORDER_FILLING_IOC
        result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Second attempt failed for {symbol}")
            return False

    logging.info(
        f"====> Order placed successfully for {symbol}: {direction.upper()} "
        f"=====>Volume: {volume}, Price: {price}, SL: {sl}, TP: {tp}"
    )

    if trading_stats:
        # Simulated profit for logging
        simulated_profit = potential_profit if direction == "buy" else -potential_profit
        trading_stats.log_trade(symbol, direction, simulated_profit, is_ml_signal)
    return True


def symbol_trader(symbol, trading_stats=None):
    # Symbol trading loop with neutral state handling and effective shutdown mechanism
    
    logging.info(f"Starting trading thread for {symbol}")

    while not SHUTDOWN_EVENT.is_set():
        try:
            # Only manage positions if shutdown has not been initiated
            manage_open_positions(symbol)

            # Prevent new trades during shutdown
            if not SHUTDOWN_EVENT.is_set() and should_trade_symbol(symbol):
                positions = mt5.positions_get(symbol=symbol)

                if not positions:  # No open positions for this symbol
                    signal, atr, potential_profit = get_signal(symbol)

                    # Skip trading if neutral or shutdown is in progress
                    if signal == "neutral" or SHUTDOWN_EVENT.is_set():
                        continue

                    success = False  # Initialize success flag
                    if signal and atr and potential_profit > 0:
                        state = trading_state.symbol_states[symbol]
                        success = place_order(
                            symbol,
                            signal,
                            atr,
                            state.volume,
                            trading_stats,
                            is_ml_signal=True,
                        )

                        if success:
                            state.last_trade_time = datetime.now()
                        else:
                            state.consecutive_losses += 1

                            if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                                state.is_restricted = True
                                logging.warning(
                                    f"{symbol} restricted due to consecutive losses"
                                )

            time.sleep(0.05)  # Check every 50ms

        except Exception as e:
            if not SHUTDOWN_EVENT.is_set():
                logging.error(f"Error in {symbol} trader: {e}")
            time.sleep(1)

    logging.info(f"Trading thread for {symbol} has stopped")


def close_position(position):
    try:
        if not mt5.initialize():
            logging.error(
                f"MT5 not initialized when trying to close position {position.ticket}"
            )
            return False

        # Get current market tick information
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            logging.error(f"Could not get tick information for {position.symbol}")
            return False

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": (
                mt5.ORDER_TYPE_SELL
                if position.type == mt5.ORDER_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            ),
            "price": (tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask),
            "deviation": 50,  # Increased deviation
            "magic": 234000,
            "comment": "close position",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logging.debug(f"Close position request for {position.ticket}: {request}")

        # Send order with timeout
        result = mt5.order_send(request)

        # Error checking
        if result is None:
            logging.error(
                f"Failed to send close order for position {position.ticket}. Returned None."
            )
            return False

        # Check return code
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logging.info(f"Successfully closed position {position.ticket}")
            return True
        else:
            logging.warning(
                f"Failed to close position {position.ticket}. "
                f"Return code: {result.retcode}, "
                f"Comment: {result.comment}"
            )
            return False

    except Exception as e:
        logging.error(f"Exception when closing position {position.ticket}: {e}")
        return False


def modify_stop_loss(position):
    """Modify position's stop loss"""
    new_sl = position.price_open if position.profit < 0 else position.sl

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "sl": new_sl,
        "tp": position.tp,
    }

    result = mt5.order_send(request)
    return result.retcode == mt5.TRADE_RETCODE_DONE
