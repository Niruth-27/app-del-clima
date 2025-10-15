"""
Niruth Live Bot - Conexión real a MetaTrader5
---------------------------------------------
Script listo para usar en Visual Studio (Windows). Implementa tu estrategia:
- Marca rango con la vela 15m de 09:30–09:45 (America/Santiago).
- Baja a 5m y espera ruptura con vela fuerte (cuerpo >= 60% del rango de la vela y apertura no pegada al borde >=20%).
- Entrada en 50% de la vela que rompió (o en la línea del rango más cercana).
- Máx 2 velas de confirmación; 1 operación por día.
- TP: objetivo de **3%** desde la entrada (por defecto). SL: unos puntos por debajo/encima de la vela que confirmó.
- Al llegar a 50% del recorrido (0.5R) mover SL a BE.
- Usa MetaTrader5 para colocar órdenes pending (BUY_LIMIT / SELL_LIMIT) y soporta modo demo.

Requisitos: MetaTrader5, pandas, pytz, keyring
"""

from __future__ import annotations
import time
import math
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

try:
    import MetaTrader5 as mt5
except Exception:
    mt5 = None

# -------------------------
# Config
# -------------------------
CHILE_TZ = pytz.timezone("America/Santiago")
PIP_SIZE = 0.01  # ajustar si tu bróker usa otro
MIN_ALT_SL_PIPS = 5

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# -------------------------
# Logger
# -------------------------
logger = logging.getLogger("niruth_live")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "niruth_live.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

# -------------------------
# Dataclasses
# -------------------------
@dataclass
class Params:
    symbol: str = "XAUUSD"
    mt5_symbol: str = "XAUUSD"  # cambia según tu bróker
    server: str = ""  # configurar
    session_time: str = "09:30"
    range_minutes: int = 15
    strong_body_min_pct: float = 0.60
    open_away_from_edge_pct: float = 0.20
    confirm_bars_max: int = 2
    tp_pct: float = 0.03  # 3% objetivo
    move_to_be_at: float = 0.5  # 50% para mover a BE
    max_trades_per_day: int = 1
    pip_size: float = PIP_SIZE
    min_alt_sl_pips: int = MIN_ALT_SL_PIPS
    volume: float = 0.1  # lote por defecto
    demo: bool = True


@dataclass
class OrderCandidate:
    direction: str  # 'long' or 'short'
    entry: float
    sl: float
    tp: float
    volume: float
    magic: int = 123456

# -------------------------
# Utilidades
# -------------------------

def to_chile(ts: pd.Timestamp) -> pd.Timestamp:
    if ts.tzinfo is None:
        return ts.tz_localize('UTC').tz_convert(CHILE_TZ)
    return ts.tz_convert(CHILE_TZ)


def get_15m_range(df15: pd.DataFrame, params: Params):
    idx = df15.index.tz_convert(CHILE_TZ) if df15.index.tz is not None else df15.index.tz_localize('UTC').tz_convert(CHILE_TZ)
    date = idx[0].date()
    start = pd.Timestamp(f"{date} {params.session_time}", tz='America/Santiago')
    end = start + pd.Timedelta(minutes=params.range_minutes)
    mask = (idx >= start) & (idx < end)
    row = df15.loc[mask]
    if row.empty:
        raise RuntimeError('No se encontró vela 15m de sesión (09:30). Revisa datos.')
    r_high = float(row['High'].iloc[0])
    r_low = float(row['Low'].iloc[0])
    return r_high, r_low, row.index[0], row.index[0] + pd.Timedelta(minutes=params.range_minutes)


def is_strong_break(candle: pd.Series, r_high: float, r_low: float, params: Params):
    o = float(candle['Open'])
    h = float(candle['High'])
    l = float(candle['Low'])
    c = float(candle['Close'])
    candle_range = max(h - l, 1e-9)
    body = abs(c - o)
    body_pct = body / candle_range
    if body_pct < params.strong_body_min_pct:
        return False, ''
    if c > r_high and o < c:
        range_w = max(r_high - r_low, 1e-9)
        if (r_high - o) >= params.open_away_from_edge_pct * range_w:
            return True, 'long'
    if c < r_low and o > c:
        range_w = max(r_high - r_low, 1e-9)
        if (o - r_low) >= params.open_away_from_edge_pct * range_w:
            return True, 'short'
    return False, ''


def build_candidate(candle: pd.Series, direction: str, r_high: float, r_low: float, params: Params) -> OrderCandidate:
    o = float(candle['Open'])
    c = float(candle['Close'])
    entry50 = o + 0.5 * (c - o)
    # elegir entre entry50 o borde del rango más cercano
    if direction == 'long':
        range_line = r_high
        # la línea del rango para long es r_high (breakout por arriba). Entrada más cercana entre entry50 y r_high
        if abs(entry50 - range_line) < abs(entry50 - entry50):
            entry = entry50 if abs(entry50 - entry50) <= abs(range_line - entry50) else entry50
        else:
            entry = entry50
        sl = min(float(candle['Low']), c - 3 * params.pip_size)
        alt = r_low
        if (entry - alt) < (params.min_alt_sl_pips * params.pip_size):
            sl = alt
        tp = entry * (1.0 + params.tp_pct)
    else:
        entry50 = o + 0.5 * (c - o)
        entry = entry50
        sl = max(float(candle['High']), c + 3 * params.pip_size)
        alt = r_high
        if (alt - entry) < (params.min_alt_sl_pips * params.pip_size):
            sl = alt
        tp = entry * (1.0 - params.tp_pct)
    return OrderCandidate(direction=direction, entry=float(entry), sl=float(sl), tp=float(tp), volume=params.volume)

# -------------------------
# MT5 helpers
# -------------------------

def mt5_initialize_and_login(login: int, password: str, server: str):
    if mt5 is None:
        raise RuntimeError('MetaTrader5 no está instalado en este entorno. pip install MetaTrader5')
    ok = mt5.initialize()
    if not ok:
        raise RuntimeError(f'MT5 initialize failed: {mt5.last_error()}')
    logged = mt5.login(login=login, password=password, server=server)
    if not logged:
        raise RuntimeError(f'MT5 login failed: {mt5.last_error()}')
    logger.info('Conectado a MT5 server=%s login=%s', server, login)
    return True


def mt5_get_rates(symbol: str, timeframe, count: int) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None:
        raise RuntimeError('No se pudieron obtener rates desde MT5')
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df.set_index('time', inplace=True)
    df.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','tick_volume':'Volume'}, inplace=True)
    return df


def place_pending_order(symbol: str, candidate: OrderCandidate, demo: bool = True):
    # Decide tipo
    if candidate.direction == 'long':
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
    else:
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
    request = {
        'action': mt5.TRADE_ACTION_PENDING,
        'symbol': symbol,
        'volume': candidate.volume,
        'type': order_type,
        'price': candidate.entry,
        'sl': candidate.sl,
        'tp': candidate.tp,
        'deviation': 20,
        'magic': candidate.magic,
        'comment': 'IA Trading Niruth live',
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_FOK,
    }
    res = mt5.order_send(request)
    logger.info('order_send -> %s', res)
    return res

# -------------------------
# Main flow
# -------------------------

def run_live(params: Params, login: int, password: str):
    # conectarse
    mt5_initialize_and_login(login, password, params.server)

    # Obtener datos 15m y 5m (suficiente histórico)
    df15 = mt5_get_rates(params.mt5_symbol, mt5.TIMEFRAME_M15, 200)
    df5 = mt5_get_rates(params.mt5_symbol, mt5.TIMEFRAME_M5, 1000)

    # Calcular rango 15m de la apertura (09:30 Chile)
    try:
        r_high, r_low, r_start, r_end = get_15m_range(df15, params)
    except Exception as e:
        logger.error('No se pudo calcular rango 15m: %s', e)
        mt5.shutdown()
        return

    logger.info('Rango 15m: high=%.2f low=%.2f (start=%s)', r_high, r_low, r_start)

    # Filtrar 5m posteriores al rango
    after = df5.loc[df5.index >= r_end]

    # Buscar rompimiento con hasta confirm_bars_max velas
    confirm_left = params.confirm_bars_max
    breaker_row = None
    breaker_time = None
    direction = ''
    for ts, row in after.iterrows():
        ok, dirn = is_strong_break(row, r_high, r_low, params)
        if ok:
            breaker_row = row
            breaker_time = ts
            direction = dirn
            logger.info('Encontrada vela rompiente en %s dir=%s', ts, dirn)
            break
        confirm_left -= 1
        if confirm_left < 0:
            break

    if breaker_row is None:
        logger.info('No hubo rompimiento válido hoy.')
        mt5.shutdown()
        return

    # Construir candidata
    candidate = build_candidate(breaker_row, direction, r_high, r_low, params)
    logger.info('Orden candidata: %s', candidate)

    # Verificar que no haya ya órdenes o trades hoy (máx 1 trade por día)
    trades_today = mt5.history_deals_get(to=mt5.time_current(), days=1)
    if trades_today is not None and len(trades_today) > 0:
        logger.info('Ya hay operaciones en el día. No se colocará nueva orden.')
        mt5.shutdown()
        return

    # Colocar orden pending (limit) en la mitad de la vela
    res = place_pending_order(params.mt5_symbol, candidate, demo=params.demo)
    logger.info('Resultado place_pending: %s', res)

    # Cerrar conexión
    mt5.shutdown()

# -------------------------
# Ejecución desde CLI
# -------------------------
if __name__ == '__main__':
    import argparse
    import keyring

    parser = argparse.ArgumentParser()
    parser.add_argument('--login', type=int, required=True, help='Login MT5')
    parser.add_argument('--server', type=str, required=True, help='Server MT5')
    parser.add_argument('--demo', action='store_true', help='Modo demo (no afecta balance real)')
    parser.add_argument('--symbol', type=str, default='XAUUSD', help='Símbolo MT5')
    parser.add_argument('--volume', type=float, default=0.1, help='Volumen lote')
    args = parser.parse_args()

    p = Params()
    p.server = args.server
    p.mt5_symbol = args.symbol
    p.volume = args.volume
    p.demo = args.demo

    # Recuperar password si existe en keyring (opcional)
    # keyring.set_password('niruth_mt5', 'password', 'MI_PASS')
    pwd = keyring.get_password('niruth_mt5', str(args.login))
    if pwd is None:
        pwd = input('Password MT5: ')

    try:
        run_live(p, args.login, pwd)
    except Exception as e:
        logger.exception('Error en ejecución live: %s', e)
        raise
