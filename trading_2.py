# trading.py

import MetaTrader5 as mt5
import pandas as pd
import time, json, datetime
from datetime import datetime, timedelta
import logging
from config import INITIAL_VOLUME, MIN_PROFIT_THRESHOLD, MIN_WIN_RATE, TIMEFRAME, PROFIT_LOCK_PERCENTAGE, MAX_CONSECUTIVE_LOSSES
from models import trading_state

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)

def calculate_win_rate(trades):
    if not trades:
        return 0
    winning_trades = sum(1 for profit in trades if profit > 0)
    return winning_trades / len(trades)

def adjust_trading_parameters(symbol, profit):
    """More conservative parameter adjustment"""
    state = trading_state.symbol_states[symbol]
    state.trades_count += 1
    state.trades_history.append(profit)
    
    # Update win rate
    state.win_rate = calculate_win_rate(state.trades_history[-10:])  # Consider last 10 trades
    
    # More gradual volume adjustments
    if state.win_rate > 0.6:
        state.volume = min(state.volume * 1.1, INITIAL_VOLUME * 1.5)  # Less aggressive increase
    elif state.win_rate < 0.4:
        state.volume = max(state.volume * 0.9, INITIAL_VOLUME * 0.7)  # Less aggressive decrease
    
    # More conservative profit threshold adjustment
    if profit > state.profit_threshold:
        state.profit_threshold *= 1.05  # Slower increase
    elif profit < 0:
        state.profit_threshold = max(MIN_PROFIT_THRESHOLD, state.profit_threshold * 0.95)  # Slower decrease

def should_trade_symbol(symbol):
    """More tolerant approach to symbol trading restrictions"""
    state = trading_state.symbol_states[symbol]
    
    if state.is_restricted:
        # Extend restriction time and make it more flexible
        if state.last_trade_time and (datetime.now() - state.last_trade_time).hours < 0.083:  # Increased from 1 hr to 5 min
            return False
        
        # More lenient restriction lifting
        if state.win_rate > MIN_WIN_RATE * 0.8:  # Lowered threshold
            state.is_restricted = False
            state.consecutive_losses = 0  # Reset consecutive losses
            logging.info(f"{symbol} restrictions lifted")
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

def get_signal(symbol):
    """Enhanced signal generation with multiple confirmations"""
    if not should_trade_symbol(symbol):
        return None, None, 0
    
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 50)
    if rates is None:
        return None, None, 0
        
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = calculate_indicators(df, symbol)
    
    current = df.iloc[-1]
    
    # Skip if volatility is too low
    if current.Volatility < trading_state.ta_params.volatility_threshold:
        return None, None, 0
    
    # Calculate trend strength and quality
    trend_score = 0
    
    # ADX trend strength
    if current.ADX > 25:  # Strong trend
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
    
    # Calculate potential profit based on ATR and trend strength
    potential_profit = current.ATR * abs(trend_score) * 10
    
    # Conservative mode adjustments
    if trading_state.is_conservative_mode:
        required_score = 3  # Higher requirement in conservative mode
        potential_profit *= 0.8  # Reduce expected profit for safety
    else:
        required_score = 2
    
    logging.info(f"{symbol} - Trend Score: {trend_score}, "
                f"ADX: {current.ADX:.2f}, RSI: {current.RSI:.2f}, "
                f"Potential Profit: {potential_profit:.5f}")
    
    if trend_score >= required_score:
        return "buy", current.ATR, potential_profit
    elif trend_score <= -required_score:
        return "sell", current.ATR, potential_profit
    
    return None, None, 0

def manage_open_positions(symbol):
    """Smart position management with automatic closure of profitable positions"""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    
    state = trading_state.symbol_states[symbol]
    max_acceptable_loss = -30
    
    for position in positions:
        current_profit = position.profit
        state.total_profit = current_profit
        
        # Update max profit if current profit is higher
        if current_profit > state.max_profit:
            state.max_profit = current_profit
        
        #Close position if profit is $5 or more
        if current_profit >= 5:
            close_result = close_position(position)
            if close_result:
                logging.info(f"{symbol} position closed with profit: {current_profit}")
                adjust_trading_parameters(symbol, current_profit)
        
        # Check if profit exceeds threshold
        elif current_profit >= state.profit_threshold:
            trading_state.is_conservative_mode = True
            trailing_stop = state.max_profit * PROFIT_LOCK_PERCENTAGE
            
            if current_profit < trailing_stop:
                close_position(position)
                logging.info(f"{symbol} position closed to lock in profit: {current_profit}")
                adjust_trading_parameters(symbol, current_profit)
        
        elif current_profit < max_acceptable_loss:
            # Only modify stop loss for extremely significant losses
            modify_stop_loss(position)

def validate_positions():
    """
    Comprehensive position validation and reconciliation
    Call this after any suspected manual intervention
    """
    positions = mt5.positions_get()
    if positions is None:
        return []
    
    validated_positions = []
    for position in positions:
        try:
            # Detailed position validation
            if position.volume <= 0:
                logging.warning(f"Invalid position detected: {position.ticket}")
                try:
                    # Attempt to close invalid positions
                    close_position(position)
                    # pass
                except Exception as e:
                    logging.error(f"Failed to close invalid position {position.ticket}: {e}")
                continue
            
            # Log position details for debugging
            logging.info(f"Validated Position: {position.symbol} "
                         f"Ticket: {position.ticket}, "
                         f"Volume: {position.volume}, "
                         f"Profit: {position.profit}, "
                         f"Open Price: {position.price_open}")
            
            validated_positions.append(position)
        
        except Exception as e:
            logging.error(f"Error validating position {position.ticket}: {e}")
    
    return validated_positions

def detect_manual_intervention(symbol):
    """
    Detect and handle manual interventions
    """
    # Get current positions
    current_positions = mt5.positions_get(symbol=symbol)
    
    # Retrieve the previous state from trading_state
    state = trading_state.symbol_states[symbol]
    
    # Check for unexpected changes
    if current_positions is None:
        # No positions exist when they should
        if hasattr(state, 'last_known_positions') and state.last_known_positions:
            logging.warning(f"Possible manual intervention detected for {symbol}: All positions closed")
            trading_state.manual_intervention_detected = True
            trading_state.last_manual_intervention_time = datetime.now()
            trading_state.manual_intervention_cooldown = 0.083  # 5 min cooldown
            return True
    
    # If positions exist, compare with last known state
    if hasattr(state, 'last_known_positions'):
        # Check for discrepancies in number of positions or their details
        if len(current_positions) != len(state.last_known_positions):
            logging.warning(f"Position count changed for {symbol}: Possible manual intervention")
            trading_state.manual_intervention_detected = True
            trading_state.last_manual_intervention_time = datetime.now()
            trading_state.manual_intervention_cooldown = 0.083  # 5 min cooldown
            return True
        
        # Check individual position details
        for curr_pos, last_pos in zip(current_positions, state.last_known_positions):
            if (curr_pos.volume != last_pos.volume or 
                abs(curr_pos.profit - last_pos.profit) > 0.01):  # Allow small floating-point differences
                logging.warning(f"Position details changed for {symbol}: Possible manual intervention")
                trading_state.manual_intervention_detected = True
                trading_state.last_manual_intervention_time = datetime.now()
                trading_state.manual_intervention_cooldown = 0.083  # 5 min cooldown
                return True
    
    return False

def close_position(position):
    """Close an open position"""
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": position.ticket,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
        "price": mt5.symbol_info_tick(position.symbol).bid if position.type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(position.symbol).ask,
        "deviation": 20,
        "magic": 234000,
        "comment": "close position",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    return result.retcode == mt5.TRADE_RETCODE_DONE

def modify_stop_loss(position):
    """Modify position's stop loss"""
    new_sl = position.price_open if position.profit < 0 else position.sl
    
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "sl": new_sl,
        "tp": position.tp
    }
    
    result = mt5.order_send(request)
    return result.retcode == mt5.TRADE_RETCODE_DONE

def place_order(symbol, direction, atr, volume):
    """
    Place a trading order with dynamic stop loss and take profit based on ATR
    """
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
    
    # Send the order
    result = mt5.order_send(request)
    
    if result is None:
        logging.error(f"Order failed for {symbol}: No result returned")
        return False
        
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logging.error(f"Order failed for {symbol}: {result.comment} (Error code: {result.retcode})")
        logging.info(f"Symbol {symbol} filling mode: {symbol_info.filling_mode}")
        
        # Try alternative filling mode if first attempt fails
        request["type_filling"] = mt5.ORDER_FILLING_IOC
        result = mt5.order_send(request)
        
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Second attempt failed for {symbol}")
            return False
    
    logging.info(f"Order Details: {json.dumps({
        'Symbol': symbol,
        'Direction': direction,
        'Volume': volume,
        'Atr': atr,
        'Price': price,
        'SL': sl,
        'TP': tp
    }, indent=2)}")

    return True

def symbol_trader(symbol):
    """Enhanced symbol trading loop with manual intervention handling"""
    while True:
        try:
            # Check for manual intervention
            if detect_manual_intervention(symbol):
                # Perform reconciliation
                validated_positions = validate_positions()
                
                # Adjust trading parameters
                state = trading_state.symbol_states[symbol]
                state.volume *= 0.8  # Reduce volume after intervention
                state.consecutive_losses += 1
                
                # Extended cooldown if too many interventions
                if state.consecutive_losses > 3:
                    state.is_restricted = True
                    logging.warning(f"{symbol} temporarily restricted due to multiple interventions")
            
            # Check manual intervention cooldown
            if trading_state.manual_intervention_cooldown > 0:
                current_time = datetime.now()
                if current_time - trading_state.last_manual_intervention_time > timedelta(hours=2):
                    trading_state.manual_intervention_detected = False
                    trading_state.manual_intervention_cooldown = 0
                else:
                    # Skip trading during cooldown 30s
                    time.sleep(30)
                    continue
            
            # Manage existing positions
            manage_open_positions(symbol)
            
            # Check if we should look for new trades
            if should_trade_symbol(symbol):
                positions = mt5.positions_get(symbol=symbol)
                
                if not positions:  # No open positions for this symbol
                    signal, atr, potential_profit = get_signal(symbol)
                    if signal and atr and potential_profit > 0:
                        state = trading_state.symbol_states[symbol]
                        
                        # Adjust volume based on recent interventions
                        adjusted_volume = state.volume * (0.9 ** state.consecutive_losses)
                        
                        success = place_order(symbol, signal, atr, adjusted_volume)
                        
                        if success:
                            state.last_trade_time = datetime.now()
                            state.consecutive_losses = max(0, state.consecutive_losses - 1)
                        else:
                            state.consecutive_losses += 1
                            
                            if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                                state.is_restricted = True
                                logging.warning(f"{symbol} restricted due to consecutive losses")
            
            # Store current positions for next iteration comparison
            state = trading_state.symbol_states[symbol]
            state.last_known_positions = mt5.positions_get(symbol=symbol)
            
            time.sleep(0.1)  # Check every 100ms
            
        except Exception as e:
            logging.error(f"Error in {symbol} trader: {e}")
            time.sleep(1)

def close_all_positions():
    """Close all open positions and return total profit/loss"""
    total_profit = 0
    positions = mt5.positions_get()
    
    if positions is None:
        logging.info("No positions to close")
        return 0
        
    for position in positions:
        # Prepare the request to close position
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": position.ticket,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "price": mt5.symbol_info_tick(position.symbol).bid if position.type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(position.symbol).ask,
            "deviation": 20,
            "magic": 234000,
            "comment": "close position on shutdown",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        # Send the request
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            profit = position.profit
            total_profit += profit
            logging.info(f"Closed position {position.ticket} for {position.symbol} with profit: {profit}")
        else:
            logging.error(f"Failed to close position {position.ticket}: {result.comment}")
    
    return total_profit

def shutdown():
    """Perform clean shutdown of the trading bot"""
    logging.info("Initiating shutdown sequence...")
    
    # Close all positions
    total_profit = close_all_positions()
    logging.info(f"Total profit from closed positions: {total_profit}")
    
    # Shutdown MT5 connection
    mt5.shutdown()
    logging.info("MT5 connection closed")
    
    # Final status log
    logging.info("Trading bot shutdown complete")