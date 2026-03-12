"""
Somatic Kernel Configuration Manager
Handles all configuration with validation and environment variable loading.
Architectural Choice: Singleton pattern ensures consistent config access
while allowing hot-reload capability for production adjustments.
"""

import os
import json
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN
import structlog

logger = structlog.get_logger()


@dataclass
class TradingConfig:
    """Immutable trading configuration with validation"""
    exchange: str
    trading_pair: str
    capital_slice: Decimal
    strategy_lookback: int
    band_width: float
    position_size_percentage: float
    profit_routing_threshold: Decimal
    
    def __post_init__(self):
        """Validate configuration on initialization"""
        if self.capital_slice <= Decimal('0'):
            raise ValueError("Capital slice must be positive")
        if not 0 < self.position_size_percentage <= 1:
            raise ValueError("Position size must be between 0 and 1")
        if self.strategy_lookback < 5:
            raise ValueError("Lookback period too short for meaningful analysis")


@dataclass
class FirebaseConfig:
    """Firebase configuration with credential validation"""
    credentials_path: str
    project_id: str
    
    def __post_init__(self):
        """Validate Firebase configuration"""
        if not os.path.exists(self.credentials_path):
            raise FileNotFoundError(
                f"Firebase credentials not found at: {self.credentials_path}"
            )


class ConfigManager:
    """Singleton configuration manager with environment variable support"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._load_environment()
        self.trading = self._create_trading_config()
        self.firebase = self._create_firebase_config()
        self.log_level = os.getenv('LOG_LEVEL', 'INFO')
        self.heartbeat_interval = int(os.getenv('HEARTBEAT_INTERVAL_MINUTES', '5'))
        
        self._validate_config()
        self._initialized = True
        logger.info("Configuration initialized successfully", config=self.to_dict())
    
    def _load_environment(self) -> None:
        """Load environment variables from .env file if exists"""
        env_path = os.path.join(os.path.dirname(__file__), '.env')
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path)
            logger.debug("Loaded environment from .env file")
        else:
            logger.warning(".env file not found, using system environment")
    
    def _create_trading_config(self) -> TradingConfig:
        """Create and validate trading configuration"""
        try:
            return TradingConfig(
                exchange=os.getenv('EXCHANGE', 'binance'),
                trading_pair=os.getenv('TRADING_PAIR', 'BTC/USDT'),
                capital_slice=Decimal(os.getenv('CAPITAL_SLICE', '50.0')),
                strategy_lookback=int(os.getenv('STRATEGY_LOOKBACK', '20')),
                band_width=float(os.getenv('BAND_WIDTH', '2.0')),
                position_size_percentage=float(os.getenv('POSITION_SIZE_PERCENTAGE', '0.95')),
                profit_routing_threshold=Decimal(os.getenv('PROFIT_ROUTING_THRESHOLD', '0.001')),
            )
        except Exception as e:
            logger.error("Failed to create trading config", error=str(e))
            raise
    
    def _create_firebase_config(self) -> FirebaseConfig:
        """Create and validate Firebase configuration"""
        return FirebaseConfig(
            credentials_path=os.getenv('FIREBASE_CREDENTIALS_PATH', './firebase-credentials.json'),
            project_id=os.getenv('FIREBASE_PROJECT_ID', 'somatic-kernel-prod'),
        )
    
    def _validate_config(self) -> None:
        """Comprehensive configuration validation"""
        # Check for required API keys
        if not os.getenv('BINANCE_API_KEY'):
            logger.warning("BINANCE_API_KEY not set - trading will fail")
        if not os.getenv('BINANCE_SECRET_KEY'):
            logger.warning("BINANCE_SECRET_KEY not set - trading will fail")
        
        # Validate wallet address format
        wallet = os.getenv('HARDWARE_WALLET_ADDRESS', '')
        if not wallet.startswith('0x') or len(wallet) != 42:
            logger.warning("Hardware wallet address appears invalid", address=wallet)
    
    def to_dict(self) -> Dict[str, Any]:
        """Return configuration as dictionary (excluding sensitive data)"""
        config_dict = {
            'trading': {k: str(v) if isinstance(v, Decimal) else v 
                       for k, v in asdict(self.trading).items()},
            'firebase': asdict(self.firebase),
            'log_level': self.log_level,
            'heartbeat_interval': self.heartbeat_interval,
        }
        return config_dict


# Global configuration instance
config = ConfigManager()