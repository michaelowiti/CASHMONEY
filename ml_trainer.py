# ml_trainer.py

import numpy as np
import pandas as pd
import MetaTrader5 as mt5
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from sklearn.metrics import classification_report, mean_squared_error
import joblib
import logging
import os
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

class MLTrader:
    def __init__(self, symbols, timeframe=mt5.TIMEFRAME_M1, look_back=50):
        self.symbols = symbols
        self.timeframe = timeframe
        self.look_back = look_back
        self.models = {}
        
    def fetch_historical_data(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, self.timeframe, 0, 2000)
        if rates is None:
            raise ValueError(f"Failed to fetch data for {symbol}")
        return pd.DataFrame(rates)

    
    def feature_engineering(self, df):
        """Create advanced features for ML model"""
        # Technical indicators
        df['SMA_10'] = df['close'].rolling(window=10).mean()
        df['SMA_50'] = df['close'].rolling(window=50).mean()
        
        # Momentum indicators
        df['RSI'] = self._calculate_rsi(df['close'])
        df['MACD'] = self._calculate_macd(df['close'])
        
        # Volatility
        df['ATR'] = self._calculate_atr(df)
        
        # Price changes
        df['price_change_1'] = df['close'].pct_change()
        df['price_change_5'] = df['close'].pct_change(5)

        # Predict based on future price movements
        df['future_close'] = df['close'].shift(-1)  # Shift -1 for next-period prediction
        df['target_return'] = (df['future_close'] - df['close']) / df['close']
        df['target_direction'] = np.where(df['target_return'] > 0, 1, 0)  # 1 for uptrend, 0 for downtrend

        
        return df
    
    def _calculate_rsi(self, prices, periods=14):
        """Calculate Relative Strength Index"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=periods).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
    
    def _calculate_macd(self, prices, slow=26, fast=12, signal=9):
        """Calculate Moving Average Convergence Divergence"""
        exp1 = prices.ewm(span=fast, adjust=False).mean()
        exp2 = prices.ewm(span=slow, adjust=False).mean()
        macd = exp1 - exp2
        signal_line = macd.ewm(span=signal, adjust=False).mean()
        return macd - signal_line
    
    def _calculate_atr(self, df, period=14):
        """Calculate Average True Range"""
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        return ranges.max(axis=1).rolling(window=period).mean()
    
    def prepare_data(self, symbol):
        try:
            df = self.fetch_historical_data(symbol)
            df = self.feature_engineering(df)
            logging.info(f"Data shape for {symbol} after feature engineering: {df.shape}")
            df.dropna(inplace=True)
            logging.info(f"Data shape for {symbol} after dropping NaNs: {df.shape}")
            
            if len(df) < 100:
                raise ValueError("Insufficient data after feature engineering")
            
            features = ['SMA_10', 'SMA_50', 'RSI', 'MACD', 'ATR', 'price_change_1', 'price_change_5']
            X = df[features]
            y_direction = df['target_direction']
            y_return = df['target_return']
            
            return X, y_direction, y_return
        except Exception as e:
            logging.error(f"Error preparing data for {symbol}: {e}")
            return None, None, None


    
    def train_models(self):
        """Train ML models for each symbol"""
        for symbol in self.symbols:
            logging.info(f"Training models for {symbol}")
            
            # Prepare data
            X, y_direction, y_return = self.prepare_data(symbol)
            if X is None:
                logging.warning(f"Skipping {symbol} due to insufficient data")
                continue
            
            # Split data
            try:
                X_train, X_test, y_dir_train, y_dir_test, y_ret_train, y_ret_test = train_test_split(
                    X, y_direction, y_return, test_size=0.2, random_state=42
                )
            except ValueError as e:
                logging.error(f"Error splitting data for {symbol}: {e}")
                continue
            
            # Scale features
            scaler = StandardScaler()
            X_train_scaled = scaler.fit_transform(X_train)
            X_test_scaled = scaler.transform(X_test)
            
            # Train direction classifier
            try:
                dir_classifier = RandomForestClassifier(n_estimators=100, random_state=42)
                dir_classifier.fit(X_train_scaled, y_dir_train)
                
                # Train return predictor
                return_predictor = GradientBoostingRegressor(n_estimators=100, random_state=42)
                return_predictor.fit(X_train_scaled, y_ret_train)
                
                # Evaluate models
                dir_pred = dir_classifier.predict(X_test_scaled)
                ret_pred = return_predictor.predict(X_test_scaled)
                
                logging.info(f"Direction Classification Report for {symbol}:")
                logging.info(classification_report(y_dir_test, dir_pred))
                logging.info(f"Return Prediction MSE: {mean_squared_error(y_ret_test, ret_pred)}")
                
                # Save models and scaler
                os.makedirs('ml_models', exist_ok=True)
                joblib.dump(dir_classifier, f'ml_models/{symbol}_direction_model.pkl')
                joblib.dump(return_predictor, f'ml_models/{symbol}_return_model.pkl')
                joblib.dump(scaler, f'ml_models/{symbol}_scaler.pkl')
                
                # Save metadata
                try:
                    features = X.columns.tolist()
                    model_metadata = {
                        "features": features,
                        "scaler": scaler,
                        "model_params": dir_classifier.get_params()
                    }
                    joblib.dump(model_metadata, f'ml_models/{symbol}_metadata.pkl')
                except Exception as e:
                    logging.error(f"Error saving metadata for {symbol}: {e}")


                
            except Exception as e:
                logging.error(f"Error training models for {symbol}: {e}")
                continue
        
        logging.info("Model training completed.")


if __name__ == "__main__":
    # Initialize MT5 connection
    if not mt5.initialize():
        logging.error("MT5 initialization failed")
        exit()
    
    # Symbols from config to train models
    symbols = ["EURAUD", "AUDUSD", "GBPJPY", "EURJPY", "USDJPY", "USDCHF", "GBPUSD", "EURUSD", "NZDUSD", "USDSEK", "USDCNH", "USDCAD"]
    
    ml_trainer = MLTrader(symbols)
    ml_trainer.train_models()
    
    mt5.shutdown()