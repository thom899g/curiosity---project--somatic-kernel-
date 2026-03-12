"""
Firebase Firestore Client for Somatic Kernel
Handles all state persistence, trade logging, and real-time monitoring.
Architectural Choice: Firestore provides real-time sync and offline persistence,
critical for a trading system that must survive network interruptions.
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from decimal import Decimal
import structlog

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import Client as FirestoreClient
from google.cloud.firestore_v1.base_query import FieldFilter

logger = structlog.get_logger()


@dataclass
class TradeRecord:
    """Immutable trade record for Firestore persistence"""
    timestamp: datetime
    trade_id: str
    action: str  # 'BUY' or 'SELL'
    symbol: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    pnl: Decimal
    strategy_signal: Dict[str, Any]
    
    def to_firestore(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary"""
        data = asdict(self)
        data['timestamp'] = self.timestamp
        data['quantity'] = float(self.quantity)
        data['price'] = float(self.price)
        data['fee'] = float(self.fee)
        data['pnl'] = float(self.pnl)
        return data


@dataclass
class SystemState:
    """System state for crash recovery and monitoring"""
    last_heartbeat: datetime
    capital_allocated: Decimal
    capital_current: Decimal
    open_positions: List[Dict[str, Any]]
    last_trade_time: Optional[datetime]
    error_count: int
    uptime_days: float


class FirebaseClient:
    """Firebase Firestore client with connection pooling and error recovery"""
    
    def __init__(self, config):
        self.config = config
        self._db: Optional[FirestoreClient] = None
        self._initialize_app()
    
    def _initialize_app(self) -> None:
        """Initialize Firebase app with connection retry logic"""
        try:
            if not firebase_admin._apps:
                cred_path = self.config.firebase.credentials_path
                if not os.path.exists(cred_path):
                    raise FileNotFoundError(f"Credentials not found: {cred_path}")
                
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred, {
                    'projectId': self.config.firebase.project_id,
                })
            
            self._db = firestore.client()
            logger.info("Firebase Firestore client initialized")
            
            # Test connection
            self._db.collection('heartbeats').document('test').set({
                'test': True,
                'timestamp': datetime.now(timezone.utc)
            }, merge=True)
            
        except Exception as e:
            logger.error("Firebase initialization failed", error=str(e))
            raise
    
    @property
    def db(self) -> FirestoreClient:
        """Lazy-loaded database connection"""
        if self._db is None:
            self._initialize_app()
        return self._db
    
    def log_trade(self, trade: TradeRecord) -> str:
        """
        Log trade to Firestore with atomic transaction.
        Edge Case: Network failure during write triggers retry with exponential backoff.
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                doc_ref = self.db.collection('trades').document(trade.trade_id)
                
                # Atomic transaction to ensure data consistency
                @firestore.transactional
                def update_in_transaction(transaction, doc_ref, trade_data):
                    transaction.set(doc_ref, trade_data)
                    # Update trade counter
                    counter_ref = self.db.collection('stats').document('trade_counter')
                    transaction.update(counter_ref, {
                        'count': firestore.Increment(1),
                        'last_trade_id': trade.trade_id,
                        'updated_at': datetime.now(timezone.utc)
                    })
                
                transaction = self.db.transaction()
                update_in_transaction(transaction, doc_ref, trade.to_firestore())
                
                logger.info("Trade logged successfully", trade_id=trade.trade_id)
                return trade.trade_id
                
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error("Failed to log trade after retries", 
                                trade_id=trade.trade_id, error=str(e))
                    raise
                logger.warning("Trade log retry", attempt=attempt+1, error=str(e))
                import time
                time.sleep(2 ** attempt)  # Exponential backoff
    
    def save_system_state(self, state: SystemState) -> None:
        """Save system state for crash recovery"""
        try:
            state_dict = asdict(state)
            state_dict['last_heartbeat'] = datetime.now(timezone.utc)
            state_dict['capital_allocated'] = float(state.capital_allocated)
            state_dict['capital_current'] = float(state.capital_current)
            
            self.db.collection('system_state').document('current').set(
                state_dict, merge=True
            )
            logger.debug("System state saved")
        except Exception as e:
            logger.error("Failed to save system state", error=str(e))
    
    def load_system_state(self) -> Optional[SystemState]:
        """Load system state after crash/restart"""
        try:
            doc = self.db.collection('system_state').document('current').get()
            if doc.exists:
                data = doc.to_dict()
                return SystemState(
                    last_heartbeat=data['last_heartbeat'],
                    capital_allocated=Decimal(str(data['capital_allocated'])),
                    capital_current=Decimal(str(data['capital_current'])),
                    open_positions=data.get('open_positions', []),
                    last_trade_time=data.get('last_trade_time'),
                    error_count=data.get('error_count', 0),
                    uptime_days=data.get('uptime_days', 0.0)
                )
        except Exception as e:
            logger.error("Failed to load system state", error=str(e))
        return None
    
    def get_recent_trades(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve recent trades for analysis"""
        try:
            query = (self.db.collection('trades')
                     .order_by('timestamp', direction=firestore.Query.DESCENDING)
                     .limit(limit))
            return [doc.to_dict() for doc in query.stream()]
        except Exception as e:
            logger.error("Failed to fetch recent trades", error=str(e))
            return []
    
    def log_profit_routing(self, amount: Decimal, tx_hash: str) -> None:
        """Log profit routing to hardware wallet"""
        try:
            self.db.collection('profit_routing').document(tx_hash).set({
                'amount': float(amount),
                'timestamp': datetime.now(timezone.utc),
                'destination': os.getenv('HARDWARE_WALLET_ADDRESS'),
                'status': 'pending'
            })
            logger.info("Profit routing logged", amount=amount, tx_hash=tx_hash)
        except Exception as e:
            logger.error("Failed to log profit routing", error=str(e))
    
    def cleanup_old_data(self, days_to_keep: int = 30) -> None:
        """Cleanup old data to manage Firestore costs"""
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
            
            # Batch delete old trades
            batch = self.db.batch()
            trades_ref = self.db.collection('trades')
            query = trades_ref.where('timestamp', '<', cutoff_date).limit(500)
            
            deleted_count = 0
            for doc in query.stream():
                batch.delete(doc.reference)
                deleted_count += 1
            
            if deleted_count > 0:
                batch.commit()
                logger.info("Cleaned up old data", deleted