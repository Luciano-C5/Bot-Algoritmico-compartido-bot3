"""
config.py
=========
Fuente única de verdad para todos los parámetros del sistema.

Todos los módulos importan desde acá:
    from config import cfg

Cuando Flask modifica un parámetro, llama a cfg.apply_change() con uno
de estos tres modos de persistencia:

  "session"    → solo esta sesión. Al reiniciar el bot, el valor vuelve
                 al que estaba en config.py. Útil para probar algo rápido.

  "persistent" → persiste en config_override.json. Sobrevive reinicios,
                 pero config.py queda sin tocar. Para deshacerlo, borrar
                 ese archivo o usar Flask para volver al valor original.

  "permanent"  → modifica config.py directamente. El valor nuevo QUEDA
                 en el código base. No hay forma de deshacer automática
                 (salvo editar el archivo a mano o usar git).

NUNCA poner claves API acá. Las claves van en keys.enc (cifradas).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
import json
import os
import re

# ─────────────────────────────────────────────
# SECCIÓN 1: ENTORNO Y CONEXIÓN
# ─────────────────────────────────────────────

@dataclass
class NetworkConfig:
    """URLs y configuración de conexión a Binance."""

    testnet: bool = True

    base_url_testnet: str = "https://testnet.binancefuture.com"
    base_url_prod:    str = "https://fapi.binance.com"
    ws_url_testnet:   str = "wss://stream.binancefuture.com"
    ws_url_prod:      str = "wss://fstream.binance.com"

    symbol: str = "BTCUSDC"

    rest_timeout: int = 10

    # "isolated" = pérdida limitada al margen de esa posición (más seguro)
    # "cross"    = usa todo el balance como margen
    margin_type: str = "isolated"

    @property
    def base_url(self) -> str:
        return self.base_url_testnet if self.testnet else self.base_url_prod

    @property
    def ws_url(self) -> str:
        return self.ws_url_testnet if self.testnet else self.ws_url_prod


# ─────────────────────────────────────────────
# SECCIÓN 2: CAPITAL Y COMISIONES
# ─────────────────────────────────────────────

@dataclass
class CapitalConfig:
    """Parámetros de capital y costos de operación."""

    initial_capital: float = 100.0
    position_size_pct: float = 1.0  # 100% = una posición a la vez

    # El sistema siempre calcula el PEOR caso (taker en ambos lados = 0.10%)
    fee_maker: float = 0.0000   # 0%
    fee_taker: float = 0.0005   # 0.05%

    @property
    def fee_worst_case(self) -> float:
        return self.fee_taker * 2


# ─────────────────────────────────────────────
# SECCIÓN 3: APALANCAMIENTO POR NIVEL
# ─────────────────────────────────────────────

@dataclass
class LeverageConfig:
    level_1: int = 5   # señal fuerte  (N1 ≥ 57%)
    level_2: int = 3   # señal moderada (N2 ≥ 45% + macro)
    level_3: int = 2   # señal débil   (N3 ≥ 35% + macro muy fuerte)

    # Con divergencia 1D/1W se reduce al 60%
    divergence_multiplier: float = 0.6

    def get_leverage(self, level: int, macro_divergence: bool = False) -> int:
        base = {1: self.level_1, 2: self.level_2, 3: self.level_3}.get(level, 1)
        if macro_divergence:
            return max(1, int(base * self.divergence_multiplier))
        return base


# ─────────────────────────────────────────────
# SECCIÓN 4: UMBRALES DE ENTRADA
# ─────────────────────────────────────────────

@dataclass
class ThresholdConfig:
    """
    Umbrales como fracción del puntaje máximo posible.
    Parámetros clave para optimizer_v2.py.
    """

    max_score: float = 143.0

    level_1: float = 0.57   # ~81.5 pts — señal fuerte, siempre opera
    level_2: float = 0.45   # ~64.4 pts — señal moderada + macro
    level_3: float = 0.35   # ~50.1 pts — señal débil + macro muy fuerte

    # Incrementos automáticos según condición (se suman al umbral base)
    increment_macro_divergence:  float = 0.15
    increment_wall_street_open:  float = 0.10
    increment_week_close:        float = 0.10
    increment_day_close:         float = 0.05
    increment_low_volume_hour:   float = 0.05
    increment_news_moderate:     float = 0.15

    def get_threshold(self, level: int) -> float:
        return {1: self.level_1, 2: self.level_2, 3: self.level_3}.get(level, 1.0)


# ─────────────────────────────────────────────
# SECCIÓN 5: PARÁMETROS DE OPERACIÓN POR MODO
# ─────────────────────────────────────────────

@dataclass
class ModeParams:
    timeframe_main:    str   = "15m"
    timeframe_confirm: str   = "5m"
    tp_base_pct:       float = 0.006   # % sobre capital
    sl_base_pct:       float = 0.004
    trailing_distance: float = 0.0027  # % desde precio actual
    trailing_trigger:  float = 0.0030  # % de avance para activar
    eval_interval_seconds: int = 60
    pause_candles:     int   = 20      # velas de pausa tras 3 pérdidas consec


@dataclass
class ModesConfig:
    scalp: ModeParams = field(default_factory=lambda: ModeParams(
        timeframe_main="15m", timeframe_confirm="5m",
        tp_base_pct=0.006, sl_base_pct=0.004,
        trailing_distance=0.0027, trailing_trigger=0.0030,
        eval_interval_seconds=60, pause_candles=20,
    ))
    mediano: ModeParams = field(default_factory=lambda: ModeParams(
        timeframe_main="1h", timeframe_confirm="15m",
        tp_base_pct=0.010, sl_base_pct=0.005,
        trailing_distance=0.0045, trailing_trigger=0.0050,
        eval_interval_seconds=300, pause_candles=20,
    ))
    swing: ModeParams = field(default_factory=lambda: ModeParams(
        timeframe_main="4h", timeframe_confirm="1h",
        tp_base_pct=0.025, sl_base_pct=0.008,
        trailing_distance=0.015, trailing_trigger=0.010,
        eval_interval_seconds=900, pause_candles=20,
    ))

    level_3_tp_pct: float = 0.0015   # TP reducido para señal débil

    # Distribución de los 3 TPs (deben sumar 100)
    tp1_size_pct: float = 40.0
    tp2_size_pct: float = 35.0
    tp3_size_pct: float = 25.0

    # Distancia de cada TP como fracción del TP base
    tp1_distance_factor: float = 0.4
    tp2_distance_factor: float = 0.7
    tp3_distance_factor: float = 1.0

    def get(self, mode: str) -> ModeParams:
        return {"scalp": self.scalp, "mediano": self.mediano, "swing": self.swing}[mode]


# ─────────────────────────────────────────────
# SECCIÓN 5b: PARÁMETROS DE INDICADORES
# ─────────────────────────────────────────────

@dataclass
class IndicatorConfig:
    """
    Parámetros de cálculo de los indicadores técnicos.
    Usados por indicators.py. Separados de ModesConfig para
    que el optimizer pueda tocarlos independientemente.
    """

    # UT Bot
    ut_bot_atr_period: int   = 10    # período ATR para el trailing stop del UT Bot
    ut_bot_key_value:  float = 1.0   # multiplicador ATR del UT Bot
    ut_bot_near_pct:   float = 0.2   # % de distancia para considerar "cerca del stop"

    # ADX
    adx_period: int = 14    # período del ADX de Wilder

    # Hurst Exponent
    hurst_min_candles: int = 100    # mínimo de velas para calcular Hurst

    # RSI
    rsi_period: int = 14

    # Bollinger Bands
    bb_length: int   = 20
    bb_std:    float = 2.0

    # ATR (general)
    atr_period: int = 14

    # CCI
    cci_length: int = 20

    # Estocástico
    stoch_k: int = 14
    stoch_d: int = 3
    stoch_smooth: int = 3

    # MACD
    macd_fast:   int = 12
    macd_slow:   int = 26
    macd_signal: int = 9

    # Ichimoku
    ichi_tenkan: int = 9
    ichi_kijun:  int = 26
    ichi_senkou: int = 52

    # Volatility Ratio
    volatility_ratio_window: int = 20   # ventana para el ATR promedio


# ─────────────────────────────────────────────
# SECCIÓN 6: GESTIÓN DE RIESGO
# ─────────────────────────────────────────────

@dataclass
class RiskConfig:
    max_consecutive_losses_per_mode: int   = 3
    max_daily_losses:                int   = 5
    winrate_window:                  int   = 20
    review_mode_winrate_threshold:   float = 0.38
    review_mode_days:                int   = 30
    override_pause_level:            int   = 1    # nivel que puede romper pausa
    state_file:                      str   = "risk_state.json"
    daily_reset_hour_utc:            int   = 0    # medianoche UTC = 21:00 ARG

    # Calculadora de riesgo — validación pre-entrada
    min_rr_ratio:          float = 1.5   # ratio R/R mínimo para operar (1:1.5)
    max_leverage_allowed:  int   = 10    # apalancamiento máximo absoluto
    min_sl_distance_pct:   float = 0.003 # SL mínimo 0.3% — evita barridos
    rr_check_enabled:      bool  = True  # permite desactivar el check en backtest


# ─────────────────────────────────────────────
# SECCIÓN 7: GESTIÓN DE ÓRDENES
# ─────────────────────────────────────────────

@dataclass
class OrderConfig:
    entry_order_type:    str = "LIMIT"
    entry_time_in_force: str = "GTX"    # Post-Only: maker o rechazada
    sl_order_type:       str = "STOP_MARKET"
    tp_order_type:       str = "LIMIT"
    tp_time_in_force:    str = "GTX"

    entry_reprice_threshold:      float = 0.005  # 0.5% subió sin ejecutar + indicadores fuertes → repricing
    entry_cancel_threshold:       float = 0.005  # 0.5% subió + indicadores débiles → cancelar
    entry_max_wait_candles:       int   = 2       # velas lateralizando sin ejecutar → cancelar
    ema_tolerance_pct:            float = 0.002   # tolerancia para detectar llegada a EMA
    ema_tp_margin_pct:            float = 0.001   # margen al poner TP antes de EMA
    limit_close_timeout_seconds:  int   = 30      # espera antes de cerrar a market


# ─────────────────────────────────────────────
# SECCIÓN 8: DATOS HISTÓRICOS
# ─────────────────────────────────────────────

@dataclass
class DataConfig:
    data_dir:      str   = "data"
    csv_filename:  str   = "BTCUSDC_1m.csv"
    backtest_days: int   = 1000

    candles_live: dict = field(default_factory=lambda: {
        "1m": 500, "5m": 300, "15m": 200,
        "1h": 200, "4h": 150, "1d": 200, "1w": 100,
    })
    candles_backtest_lookback: dict = field(default_factory=lambda: {
        "1m": 500, "5m": 300, "15m": 200,
        "1h": 200, "4h": 150, "1d": 200, "1w": 100,
    })

    train_pct: float = 0.70
    val_pct:   float = 0.15
    test_pct:  float = 0.15

    refresh_intervals: dict = field(default_factory=lambda: {
        "1m": 15, "5m": 30, "15m": 60,
        "1h": 120, "4h": 300, "1d": 600, "1w": 3600,
    })

    @property
    def csv_path(self) -> str:
        return os.path.join(self.data_dir, self.csv_filename)


# ─────────────────────────────────────────────
# SECCIÓN 9: NOTICIAS Y SENTIMIENTO
# ─────────────────────────────────────────────

@dataclass
class NewsConfig:
    enable_cryptopanic:       bool  = True
    enable_fear_greed:        bool  = True
    enable_rss_cointelegraph: bool  = True
    enable_rss_coindesk:      bool  = True
    cryptopanic_token:        str   = ""   # token gratuito, no es secreto crítico
    check_interval:           int   = 300  # segundos

    fg_pause_15min_low:  int = 15
    fg_pause_15min_high: int = 85
    fg_pause_60min_low:  int = 20
    fg_pause_60min_high: int = 80
    fg_pause_4h_low:     int = 10
    fg_pause_4h_high:    int = 90

    pause_moderate_news_minutes:  int   = 15
    pause_high_impact_minutes:    int   = 60
    pause_extreme_news_minutes:   int   = 240
    threshold_increment_moderate: float = 0.15

    geo_big_move_threshold:   float = 0.02
    geo_small_move_threshold: float = 0.005


# ─────────────────────────────────────────────
# SECCIÓN 10: TELEGRAM
# ─────────────────────────────────────────────

@dataclass
class TelegramConfig:
    enabled:  bool = True
    bot_token: str = ""   # cargar desde keys.enc al arrancar
    chat_id:   str = ""   # cargar desde keys.enc al arrancar

    notify_on_open:         bool = True
    notify_on_close:        bool = True
    notify_on_pause:        bool = True
    notify_strong_signal:   bool = True
    notify_review_mode:     bool = True
    notify_security_access: bool = True

    min_signal_level_notify: int = 0


# ─────────────────────────────────────────────
# SECCIÓN 11: SEGURIDAD
# ─────────────────────────────────────────────

@dataclass
class SecurityConfig:
    keys_file:            str  = "keys.enc"
    flask_port:           int  = 5000
    flask_host:           str  = "127.0.0.1"
    require_2fa:          bool = True
    two_fa_expiry_seconds: int = 300
    log_access_ip:        bool = True
    logs_dir:             str  = "historial"
    log_filename:         str  = "bot_log.enc"


# ─────────────────────────────────────────────
# SECCIÓN 12: HISTORIAL Y LOGS
# ─────────────────────────────────────────────

@dataclass
class LogConfig:
    historial_dir:        str  = "historial"
    log_every_trade:      bool = True
    csv_interval_hours:   int  = 12
    csv_generation_hours: list = field(default_factory=lambda: [0, 12])
    encrypt_logs:         bool = True


# ─────────────────────────────────────────────
# SECCIÓN 13: OPTIMIZADOR
# ─────────────────────────────────────────────

@dataclass
class OptimizerConfig:
    threshold_level_1_range: list = field(
        default_factory=lambda: [0.50, 0.53, 0.55, 0.57, 0.60, 0.63, 0.65])
    threshold_level_2_range: list = field(
        default_factory=lambda: [0.38, 0.40, 0.42, 0.45, 0.48, 0.50])
    threshold_level_3_range: list = field(
        default_factory=lambda: [0.30, 0.32, 0.35, 0.38, 0.40])

    scalp_tp_range:   list = field(default_factory=lambda: [0.004, 0.005, 0.006, 0.007, 0.008])
    scalp_sl_range:   list = field(default_factory=lambda: [0.003, 0.004, 0.005])
    mediano_tp_range: list = field(default_factory=lambda: [0.008, 0.010, 0.012, 0.015])
    mediano_sl_range: list = field(default_factory=lambda: [0.004, 0.005, 0.006])
    swing_tp_range:   list = field(default_factory=lambda: [0.020, 0.025, 0.030])
    swing_sl_range:   list = field(default_factory=lambda: [0.006, 0.008, 0.010])

    optimize_metric: str = "sharpe"   # "sharpe", "profit_factor", "total_return"
    min_trades:      int = 30
    workers: Optional[int] = None


# ─────────────────────────────────────────────
# OBJETO DE CONFIGURACIÓN GLOBAL
# ─────────────────────────────────────────────

# Mapeo de nombres de parámetro → (sección, atributo, tipo)
# Usado por apply_change() para saber dónde escribir cada valor.
_PARAM_MAP = {
    # Capital
    "capital.initial_capital":     ("capital",   "initial_capital",   float),
    "capital.position_size_pct":   ("capital",   "position_size_pct", float),
    # Red
    "network.testnet":             ("network",   "testnet",           bool),
    "network.symbol":              ("network",   "symbol",            str),
    "network.margin_type":         ("network",   "margin_type",       str),
    # Apalancamiento
    "leverage.level_1":            ("leverage",  "level_1",           int),
    "leverage.level_2":            ("leverage",  "level_2",           int),
    "leverage.level_3":            ("leverage",  "level_3",           int),
    "leverage.divergence_multiplier": ("leverage", "divergence_multiplier", float),
    # Umbrales
    "threshold.level_1":           ("threshold", "level_1",           float),
    "threshold.level_2":           ("threshold", "level_2",           float),
    "threshold.level_3":           ("threshold", "level_3",           float),
    "threshold.increment_macro_divergence": ("threshold", "increment_macro_divergence", float),
    "threshold.increment_wall_street_open": ("threshold", "increment_wall_street_open", float),
    "threshold.increment_week_close":       ("threshold", "increment_week_close",       float),
    "threshold.increment_day_close":        ("threshold", "increment_day_close",        float),
    # Modos — scalp
    "modes.scalp.tp_base_pct":     ("modes.scalp", "tp_base_pct",     float),
    "modes.scalp.sl_base_pct":     ("modes.scalp", "sl_base_pct",     float),
    "modes.scalp.trailing_distance": ("modes.scalp", "trailing_distance", float),
    "modes.scalp.trailing_trigger":  ("modes.scalp", "trailing_trigger",  float),
    # Modos — mediano
    "modes.mediano.tp_base_pct":   ("modes.mediano", "tp_base_pct",   float),
    "modes.mediano.sl_base_pct":   ("modes.mediano", "sl_base_pct",   float),
    "modes.mediano.trailing_distance": ("modes.mediano", "trailing_distance", float),
    "modes.mediano.trailing_trigger":  ("modes.mediano", "trailing_trigger",  float),
    # Modos — swing
    "modes.swing.tp_base_pct":     ("modes.swing", "tp_base_pct",     float),
    "modes.swing.sl_base_pct":     ("modes.swing", "sl_base_pct",     float),
    "modes.swing.trailing_distance": ("modes.swing", "trailing_distance", float),
    "modes.swing.trailing_trigger":  ("modes.swing", "trailing_trigger",  float),
    # Indicadores
    "indicators.ut_bot_atr_period":    ("indicators", "ut_bot_atr_period",    int),
    "indicators.ut_bot_key_value":     ("indicators", "ut_bot_key_value",     float),
    "indicators.ut_bot_near_pct":      ("indicators", "ut_bot_near_pct",      float),
    "indicators.adx_period":           ("indicators", "adx_period",           int),
    "indicators.hurst_min_candles":    ("indicators", "hurst_min_candles",    int),
    # Riesgo
    "risk.max_consecutive_losses_per_mode": ("risk", "max_consecutive_losses_per_mode", int),
    "risk.max_daily_losses":       ("risk",  "max_daily_losses",      int),
    "risk.review_mode_winrate_threshold": ("risk", "review_mode_winrate_threshold", float),
    "risk.min_rr_ratio":           ("risk",  "min_rr_ratio",          float),
    "risk.max_leverage_allowed":   ("risk",  "max_leverage_allowed",  int),
    "risk.min_sl_distance_pct":    ("risk",  "min_sl_distance_pct",   float),
    "risk.rr_check_enabled":       ("risk",  "rr_check_enabled",      bool),
    # Órdenes
    "orders.entry_reprice_threshold": ("orders", "entry_reprice_threshold", float),
    "orders.entry_cancel_threshold":  ("orders", "entry_cancel_threshold",  float),
    "orders.limit_close_timeout_seconds": ("orders", "limit_close_timeout_seconds", int),
}


@dataclass
class Config:
    """
    Configuración completa del sistema.
    Importar en todos los módulos: from config import cfg
    """

    network:    NetworkConfig    = field(default_factory=NetworkConfig)
    capital:    CapitalConfig    = field(default_factory=CapitalConfig)
    leverage:   LeverageConfig   = field(default_factory=LeverageConfig)
    threshold:  ThresholdConfig  = field(default_factory=ThresholdConfig)
    modes:      ModesConfig      = field(default_factory=ModesConfig)
    indicators: IndicatorConfig  = field(default_factory=IndicatorConfig)
    risk:       RiskConfig       = field(default_factory=RiskConfig)
    orders:     OrderConfig      = field(default_factory=OrderConfig)
    data:       DataConfig       = field(default_factory=DataConfig)
    news:       NewsConfig       = field(default_factory=NewsConfig)
    telegram:   TelegramConfig   = field(default_factory=TelegramConfig)
    security:   SecurityConfig   = field(default_factory=SecurityConfig)
    log:        LogConfig        = field(default_factory=LogConfig)
    optimizer:  OptimizerConfig  = field(default_factory=OptimizerConfig)

    # ─────────────────────────────────────────
    # SISTEMA DE CAMBIOS CON TRES MODOS
    # ─────────────────────────────────────────

    def apply_change(
        self,
        param: str,
        value: Any,
        mode: str = "session",
    ) -> str:
        """
        Aplica un cambio de parámetro con el modo de persistencia elegido.

        Parámetros:
            param   Nombre del parámetro en formato "seccion.atributo"
                    Ej: "threshold.level_1", "modes.scalp.tp_base_pct"
            value   Nuevo valor (se convierte al tipo correcto automáticamente)
            mode    Uno de tres valores:
                    "session"    — solo esta sesión, se pierde al reiniciar
                    "persistent" — sobrevive reinicios (config_override.json)
                    "permanent"  — modifica config.py directamente

        Devuelve:
            String con el resultado para mostrar en Flask/terminal.
        """
        if param not in _PARAM_MAP:
            return f"[Config] ERROR: parámetro desconocido '{param}'"

        section_path, attr, typ = _PARAM_MAP[param]

        try:
            if typ == bool:
                if isinstance(value, str):
                    typed_value = value.lower() in ("true", "1", "yes", "si")
                else:
                    typed_value = bool(value)
            else:
                typed_value = typ(value)
        except (ValueError, TypeError) as e:
            return f"[Config] ERROR: no se pudo convertir '{value}' a {typ.__name__}: {e}"

        obj = self._resolve_section(section_path)
        old_value = getattr(obj, attr)
        setattr(obj, attr, typed_value)

        msg = f"[Config] {param}: {old_value} → {typed_value}"

        if mode == "session":
            msg += " (solo esta sesión)"

        elif mode == "persistent":
            self._save_override(param, typed_value)
            msg += " (guardado en config_override.json — sobrevive reinicios)"

        elif mode == "permanent":
            result = self._write_to_source(param, attr, typed_value, section_path)
            if result:
                msg += f" (PERMANENTE — config.py modificado)"
            else:
                msg += " (advertencia: no se pudo escribir en config.py, aplicado como persistent)"
                self._save_override(param, typed_value)

        else:
            return f"[Config] ERROR: modo '{mode}' desconocido. Usar 'session', 'persistent' o 'permanent'."

        return msg

    def _resolve_section(self, section_path: str) -> Any:
        parts = section_path.split(".")
        obj = self
        for part in parts:
            obj = getattr(obj, part)
        return obj

    def _save_override(self, param: str, value: Any) -> None:
        path = "config_override.json"
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[param] = value
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _write_to_source(
        self, param: str, attr: str, value: Any, section_path: str
    ) -> bool:
        config_file = os.path.abspath(__file__)
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                content = f.read()
            if isinstance(value, bool):
                new_val_str = "True" if value else "False"
            elif isinstance(value, str):
                new_val_str = f'"{value}"'
            elif isinstance(value, float):
                new_val_str = f"{value}"
            else:
                new_val_str = str(value)
            pattern = rf"(^\s+{re.escape(attr)}\s*:\s*\w+\s*=\s*)([^\n#]+)"
            replacement = rf"\g<1>{new_val_str}"
            new_content, count = re.subn(
                pattern, replacement, content, count=1, flags=re.MULTILINE
            )
            if count == 0:
                return False
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(new_content)
            return True
        except Exception:
            return False

    def load_overrides(self, path: str = "config_override.json") -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            loaded = 0
            for param, value in data.items():
                if param in _PARAM_MAP:
                    section_path, attr, typ = _PARAM_MAP[param]
                    try:
                        if typ == bool and isinstance(value, str):
                            typed_value = value.lower() in ("true", "1", "yes")
                        else:
                            typed_value = typ(value)
                        obj = self._resolve_section(section_path)
                        setattr(obj, attr, typed_value)
                        loaded += 1
                    except Exception:
                        pass
            if loaded:
                print(f"[Config] {loaded} override(s) cargados desde {path}")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[Config] Error cargando overrides: {e}")

    def clear_overrides(self, path: str = "config_override.json") -> str:
        if os.path.exists(path):
            os.remove(path)
            return f"[Config] Overrides eliminados. Al reiniciar se usarán los valores de config.py."
        return "[Config] No había overrides guardados."

    def summary(self) -> str:
        env = "TESTNET" if self.network.testnet else "PRODUCCIÓN ⚠"
        lines = [
            "=" * 55,
            f"  BOT CONFIG — {env}",
            "=" * 55,
            f"  Par:        {self.network.symbol}",
            f"  Margen:     {self.network.margin_type}",
            f"  Capital:    ${self.capital.initial_capital:.2f} USDC",
            f"  Comisión:   {self.capital.fee_worst_case*100:.2f}% (peor caso)",
            "",
            f"  Apalancamiento:  N1=x{self.leverage.level_1}  N2=x{self.leverage.level_2}  N3=x{self.leverage.level_3}",
            f"  Umbrales:        N1={self.threshold.level_1*100:.0f}%  N2={self.threshold.level_2*100:.0f}%  N3={self.threshold.level_3*100:.0f}%",
            "",
            f"  Scalp:    TP={self.modes.scalp.tp_base_pct*100:.2f}%  SL={self.modes.scalp.sl_base_pct*100:.2f}%  TF={self.modes.scalp.timeframe_main}",
            f"  Mediano:  TP={self.modes.mediano.tp_base_pct*100:.2f}%  SL={self.modes.mediano.sl_base_pct*100:.2f}%  TF={self.modes.mediano.timeframe_main}",
            f"  Swing:    TP={self.modes.swing.tp_base_pct*100:.2f}%  SL={self.modes.swing.sl_base_pct*100:.2f}%  TF={self.modes.swing.timeframe_main}",
            "",
            f"  Indicadores: UT Bot ATR={self.indicators.ut_bot_atr_period} key={self.indicators.ut_bot_key_value}",
            f"               ADX period={self.indicators.adx_period}  Hurst min={self.indicators.hurst_min_candles}",
            "",
            f"  Riesgo:   max {self.risk.max_consecutive_losses_per_mode} pérd consec / "
            f"{self.risk.max_daily_losses} pérd diarias",
            f"  R/R mín:  1:{self.risk.min_rr_ratio}  Lev máx: x{self.risk.max_leverage_allowed}",
            f"  Revisión: winrate < {self.risk.review_mode_winrate_threshold*100:.0f}% "
            f"en {self.risk.winrate_window} ops → {self.risk.review_mode_days}d paper",
            "=" * 55,
        ]
        return "\n".join(lines)

    def params_list(self) -> list[dict]:
        result = []
        for param, (section_path, attr, typ) in _PARAM_MAP.items():
            obj = self._resolve_section(section_path)
            result.append({
                "param": param,
                "value": getattr(obj, attr),
                "type":  typ.__name__,
            })
        return result


# ─────────────────────────────────────────────
# INSTANCIA GLOBAL
# ─────────────────────────────────────────────

cfg = Config()
cfg.load_overrides()


# ─────────────────────────────────────────────
# TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print(cfg.summary())

    print("\n── Verificaciones básicas ──")
    assert cfg.network.symbol == "BTCUSDC"
    assert cfg.leverage.get_leverage(1) == 5
    assert cfg.leverage.get_leverage(1, macro_divergence=True) == 3
    assert cfg.capital.fee_worst_case == 0.001
    assert cfg.modes.get("scalp").timeframe_main == "15m"
    assert cfg.modes.get("swing").timeframe_main == "4h"
    # IndicatorConfig
    assert cfg.indicators.ut_bot_atr_period == 10
    assert cfg.indicators.ut_bot_key_value == 1.0
    assert cfg.indicators.adx_period == 14
    assert cfg.indicators.hurst_min_candles == 100
    # RiskConfig nuevos campos
    assert cfg.risk.min_rr_ratio == 1.5
    assert cfg.risk.max_leverage_allowed == 10
    assert cfg.risk.rr_check_enabled == True
    print("  Verificaciones básicas ✓")

    print("\n── Test apply_change (indicadores) ──")
    msg = cfg.apply_change("indicators.ut_bot_key_value", 1.5, mode="session")
    print(f"  {msg}")
    assert cfg.indicators.ut_bot_key_value == 1.5
    cfg.indicators.ut_bot_key_value = 1.0  # restaurar
    print("  ✓")

    print("\n── Test apply_change (riesgo) ──")
    msg = cfg.apply_change("risk.min_rr_ratio", 2.0, mode="session")
    print(f"  {msg}")
    assert cfg.risk.min_rr_ratio == 2.0
    cfg.risk.min_rr_ratio = 1.5
    print("  ✓")

    print("\n── Test params_list ──")
    params = cfg.params_list()
    assert len(params) > 0
    print(f"  {len(params)} parámetros listados ✓")

    print("\nTodos los tests pasaron ✓")
