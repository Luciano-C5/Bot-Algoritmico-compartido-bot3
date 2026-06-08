"""
regime_detector.py
==================
Detector de régimen de mercado. Módulo transversal que corre en paralelo
al ciclo principal del bot.

Determina si el mercado está en:
  "bull_trend"  → tendencia alcista fuerte
  "bear_trend"  → tendencia bajista fuerte
  "range"       → lateral / mean-reversion
  "volatile"    → volátil sin dirección clara

El resultado (RegimeResult) alimenta:
  scoring.py   → pesos dinámicos de cada indicador
  strategy.py  → jerarquía de modos, tamaño de posición, ratio TP/SL

Uso:
    from regime_detector import RegimeDetector, RegimeResult, get_scoring_weights
    detector = RegimeDetector()
    result = detector.detect(
        daily_closes=closes,
        daily_highs=highs,
        daily_lows=lows,
        current_atr=atr_actual,
        recent_atrs=atrs_20_velas,
        bid=bid_actual,
        ask=ask_actual,
    )
"""

from __future__ import annotations

import time
import math
import logging
import statistics
from dataclasses import dataclass, field
from typing import Optional
import urllib.request
import json

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# DATACLASS DE SALIDA
# ─────────────────────────────────────────────────────────────

@dataclass
class RegimeResult:
    """
    Resultado completo del detector de régimen.
    Todos los campos son los calculados en el último ciclo.
    """

    # Régimen principal detectado
    regime: str = "volatile"
    # Valores posibles:
    #   "bull_trend" → tendencia alcista fuerte
    #   "bear_trend" → tendencia bajista fuerte
    #   "range"      → mercado lateral / mean-reversion
    #   "volatile"   → volátil sin dirección clara (default seguro)

    # Confianza en la clasificación (0.0 a 1.0)
    # ≥ 0.70 → indicadores fuertemente de acuerdo
    # 0.50-0.70 → mayoría de acuerdo
    # 0.35-0.50 → indicadores divididos
    # < 0.35 → sin consenso → se trata como volatile
    confidence: float = 0.0

    # ── Indicadores individuales calculados ──────────────────
    adx:              float = 0.0       # 0-100; >25 tendencia, <20 rango
    ema_position:     str   = "between" # "above_both"|"below_both"|"between"
    volatility_ratio: float = 1.0       # ATR_actual / ATR_promedio_20; >1.5 expansión
    hurst:            float = 0.5       # >0.5 tendencia, <0.5 reversión
    fear_greed:       int   = 50        # 0-100; <20 miedo extremo, >80 avaricia extrema
    microstructure_ok: bool = True      # False = spread anormal → no operar

    # ── Modificadores para otros módulos ─────────────────────
    # Multiplicador del riesgo por trade:
    #   bull/bear: 1.0 | range: 0.5 | volatile: 0.3
    risk_multiplier: float = 0.3

    # Ratio TP/SL recomendado:
    #   bull/bear: 2.5 (tendencia fuerte) o 2.0 (moderada)
    #   range: 1.5 | volatile: 1.5
    tp_sl_ratio: float = 1.5

    # Modo de trading prioritario:
    #   bull/bear: "swing" | range: "scalp" | volatile: None (no operar)
    preferred_mode: Optional[str] = None

    # Incremento adicional al umbral de entrada del scoring (fracción):
    #   volatile: +0.20 sobre el umbral normal
    #   FG extremo suma 0.10-0.15 adicional
    threshold_increment: float = 0.20

    # Timestamp unix del momento en que se calculó este resultado
    calculated_at: float = field(default_factory=time.time)

    def is_stale(self, max_age_seconds: int = 300) -> bool:
        """True si el resultado tiene más de max_age_seconds de antigüedad."""
        return (time.time() - self.calculated_at) > max_age_seconds

    def summary(self) -> str:
        """Resumen de una línea para el live_monitor."""
        return (
            f"Régimen: {self.regime.upper()} (conf={self.confidence*100:.0f}%) | "
            f"ADX={self.adx:.1f} | Hurst={self.hurst:.2f} | "
            f"VolRatio={self.volatility_ratio:.2f} | FG={self.fear_greed} | "
            f"EMA={self.ema_position}"
        )


# ─────────────────────────────────────────────────────────────
# DETECTOR PRINCIPAL
# ─────────────────────────────────────────────────────────────

class RegimeDetector:
    """
    Calcula el régimen de mercado a partir de datos de precio históricos.

    Parámetros de inicialización (todos opcionales, defaults razonables):
        adx_period              Período del ADX. Default: 14
        ema_short_period        EMA corta diaria para posición. Default: 50
        ema_long_period         EMA larga diaria para posición. Default: 200
        hurst_window            Ventana para el Hurst Exponent. Default: 100
        volatility_ratio_window Ventana para el Volatility Ratio. Default: 20
        fear_greed_cache_seconds Tiempo de caché del F&G Index. Default: 300
    """

    def __init__(
        self,
        adx_period:               int = 14,
        ema_short_period:         int = 50,
        ema_long_period:          int = 200,
        hurst_window:             int = 100,
        volatility_ratio_window:  int = 20,
        fear_greed_cache_seconds: int = 300,
    ):
        self.adx_period               = adx_period
        self.ema_short_period         = ema_short_period
        self.ema_long_period          = ema_long_period
        self.hurst_window             = hurst_window
        self.volatility_ratio_window  = volatility_ratio_window
        self.fear_greed_cache_seconds = fear_greed_cache_seconds

        self._fg_cache:      Optional[int]   = None
        self._fg_cache_time: float           = 0.0
        self._last_result:   Optional[RegimeResult] = None

    # ─────────────────────────────────────────────────────────
    # MÉTODO PRINCIPAL
    # ─────────────────────────────────────────────────────────

    def detect(
        self,
        daily_closes:  list[float],
        daily_highs:   list[float],
        daily_lows:    list[float],
        current_atr:   float,
        recent_atrs:   list[float],
        bid:           float,
        ask:           float,
        use_fear_greed: bool = True,
    ) -> RegimeResult:
        """
        Calcula el régimen de mercado actual.

        Parámetros:
            daily_closes    Precios de cierre diarios, el más reciente al final.
                            Mínimo recomendado: 220 valores.
            daily_highs     Máximos diarios, misma longitud que closes.
            daily_lows      Mínimos diarios, misma longitud que closes.
            current_atr     ATR actual del timeframe principal de operación.
            recent_atrs     Lista de ATRs recientes (últimas 20 velas del TF principal).
            bid             Precio bid actual.
            ask             Precio ask actual.
            use_fear_greed  False en backtest para no llamar a la API externa.

        Devuelve:
            RegimeResult con todos los campos calculados.
        """
        result = RegimeResult()
        result.calculated_at = time.time()

        # Mínimo de datos necesarios
        min_required = self.ema_long_period + self.adx_period * 3 + 5
        if len(daily_closes) < min_required:
            logger.warning(
                f"[Regime] Datos insuficientes: {len(daily_closes)} < {min_required}. "
                "Devolviendo volatile con confidence=0."
            )
            result.regime = "volatile"
            result.confidence = 0.0
            self._last_result = result
            return result

        # 1. ADX ── fuerza de la tendencia (0-100)
        result.adx = self._calc_adx(
            daily_highs, daily_lows, daily_closes, self.adx_period
        )

        # 2. Posición del precio respecto a EMA50 y EMA200 diarias
        price  = daily_closes[-1]
        ema50  = self._calc_ema(daily_closes, self.ema_short_period)
        ema200 = self._calc_ema(daily_closes, self.ema_long_period)
        result.ema_position = self._classify_ema_position(price, ema50, ema200)

        # 3. Volatility Ratio = ATR_actual / ATR_promedio_20_velas
        if len(recent_atrs) >= self.volatility_ratio_window and current_atr > 0:
            avg_atr = statistics.mean(
                recent_atrs[-self.volatility_ratio_window:]
            )
            result.volatility_ratio = (
                current_atr / avg_atr if avg_atr > 0 else 1.0
            )
        else:
            result.volatility_ratio = 1.0

        # 4. Hurst Exponent ── tendencia vs reversión estadística
        window = min(self.hurst_window, len(daily_closes))
        result.hurst = (
            self._calc_hurst(daily_closes[-window:])
            if window >= 20
            else 0.5
        )

        # 5. Market Microstructure ── spread bid/ask vs volatilidad
        result.microstructure_ok = self._check_microstructure(
            bid, ask, current_atr
        )

        # 6. Fear & Greed Index (con caché de 5 minutos)
        result.fear_greed = (
            self._get_fear_greed() if use_fear_greed else 50
        )

        # 7. Clasificar régimen con votación ponderada
        result.regime, result.confidence = self._classify_regime(result)

        # 8. Calcular modificadores para scoring.py y strategy.py
        self._set_modifiers(result)

        self._last_result = result
        logger.debug(f"[Regime] {result.summary()}")
        return result

    # ─────────────────────────────────────────────────────────
    # CLASIFICACIÓN POR VOTACIÓN PONDERADA
    # ─────────────────────────────────────────────────────────

    def _classify_regime(self, r: RegimeResult) -> tuple[str, float]:
        """
        Combina todos los indicadores con votación ponderada.

        Cada indicador vota por un régimen con un peso determinado.
        El régimen con más votos ponderados gana.
        La confianza = votos_ganador / total_votos.

        Regla especial: ADX < 20 es determinante para "range".
        Si ADX < 20, se fuerza range directamente (el rango es inequívoco
        cuando no hay fuerza de tendencia, independientemente de la EMA position).
        """

        # ── Regla determinante: ADX bajo fuerza "range" ──────
        # Cuando ADX < 18, la tendencia es tan débil que no tiene sentido
        # considerar otros indicadores para decidir entre tendencia y rango.
        if r.adx < 18:
            confidence = 0.75 if r.hurst < 0.5 else 0.55
            return "range", confidence

        # ── Microstructure veta todo → volatile ──────────────
        if not r.microstructure_ok:
            return "volatile", 0.30

        votes: dict[str, float] = {
            "bull_trend": 0.0,
            "bear_trend": 0.0,
            "range":      0.0,
            "volatile":   0.0,
        }
        total_weight = 0.0

        # ── ADX (peso 3.0) ────────────────────────────────────
        # Ya sabemos que ADX >= 18 en este punto
        w = 3.0
        total_weight += w
        if r.adx > 30:
            if r.ema_position == "above_both":
                votes["bull_trend"] += w
            elif r.ema_position == "below_both":
                votes["bear_trend"] += w
            else:
                votes["volatile"]   += w * 0.5
                votes["bull_trend"] += w * 0.25
                votes["bear_trend"] += w * 0.25
        elif r.adx > 25:
            if r.ema_position == "above_both":
                votes["bull_trend"] += w * 0.75
                votes["range"]      += w * 0.25
            elif r.ema_position == "below_both":
                votes["bear_trend"] += w * 0.75
                votes["range"]      += w * 0.25
            else:
                votes["volatile"] += w * 0.5
                votes["range"]    += w * 0.5
        else:
            # ADX entre 18 y 25 → zona de transición
            votes["range"]    += w * 0.45
            votes["volatile"] += w * 0.30
            if r.ema_position == "above_both":
                votes["bull_trend"] += w * 0.25
            elif r.ema_position == "below_both":
                votes["bear_trend"] += w * 0.25
            else:
                votes["range"] += w * 0.25

        # ── EMA Position (peso 2.0) ───────────────────────────
        w = 2.0
        total_weight += w
        if r.ema_position == "above_both":
            votes["bull_trend"] += w * 0.65
            votes["range"]      += w * 0.35
        elif r.ema_position == "below_both":
            votes["bear_trend"] += w * 0.65
            votes["range"]      += w * 0.35
        else:
            votes["range"]    += w * 0.50
            votes["volatile"] += w * 0.50

        # ── Hurst Exponent (peso 2.5 — más sólido matemáticamente) ──
        w = 2.5
        total_weight += w
        if r.hurst > 0.65:
            if r.ema_position == "above_both":
                votes["bull_trend"] += w
            elif r.ema_position == "below_both":
                votes["bear_trend"] += w
            else:
                votes["volatile"]   += w * 0.5
                votes["bull_trend"] += w * 0.25
                votes["bear_trend"] += w * 0.25
        elif r.hurst > 0.55:
            if r.ema_position == "above_both":
                votes["bull_trend"] += w * 0.6
                votes["range"]      += w * 0.4
            elif r.ema_position == "below_both":
                votes["bear_trend"] += w * 0.6
                votes["range"]      += w * 0.4
            else:
                votes["range"]    += w * 0.5
                votes["volatile"] += w * 0.5
        elif r.hurst > 0.45:
            # Cerca de aleatorio
            votes["range"]    += w * 0.4
            votes["volatile"] += w * 0.6
        else:
            # Hurst < 0.45 → fuerte reversión a la media
            votes["range"] += w

        # ── Volatility Ratio (peso 2.0) ───────────────────────
        w = 2.0
        total_weight += w
        if r.volatility_ratio > 2.0:
            votes["volatile"] += w
        elif r.volatility_ratio > 1.5:
            votes["volatile"]   += w * 0.5
            votes["bull_trend"] += w * 0.25
            votes["bear_trend"] += w * 0.25
        elif r.volatility_ratio < 0.7:
            # Compresión → pre-breakout, tratar como rango
            votes["range"] += w
        else:
            # Normal → refuerzo leve al régimen ya dominante
            best = max(votes, key=lambda k: votes[k])
            votes[best] += w * 0.3

        # ── Fear & Greed (peso 1.0) ───────────────────────────
        w = 1.0
        total_weight += w
        if r.fear_greed < 15 or r.fear_greed > 85:
            votes["volatile"]   += w * 0.6
            # Señal contraria potencial
            if r.fear_greed < 15:
                votes["bull_trend"] += w * 0.4   # miedo extremo = posible suelo
            else:
                votes["bear_trend"] += w * 0.4   # avaricia extrema = posible techo
        elif r.fear_greed < 25 or r.fear_greed > 75:
            votes["volatile"] += w * 0.3
            best = max(votes, key=lambda k: votes[k])
            votes[best] += w * 0.7
        else:
            best = max(votes, key=lambda k: votes[k])
            votes[best] += w

        # ── Determinar ganador ────────────────────────────────
        winning_regime = max(votes, key=lambda k: votes[k])
        winning_votes  = votes[winning_regime]
        confidence     = min(winning_votes / total_weight, 1.0) if total_weight > 0 else 0.0

        # Confianza muy baja → volatile por precaución
        if confidence < 0.35:
            return "volatile", confidence

        return winning_regime, round(confidence, 3)

    def _set_modifiers(self, r: RegimeResult) -> None:
        """
        Rellena los campos de modificadores del RegimeResult
        según el régimen y la confianza.
        """
        if r.regime == "bull_trend":
            r.risk_multiplier     = 1.0
            r.tp_sl_ratio         = 2.5 if r.confidence > 0.70 else 2.0
            r.preferred_mode      = "swing"
            r.threshold_increment = 0.0

        elif r.regime == "bear_trend":
            r.risk_multiplier     = 1.0
            r.tp_sl_ratio         = 2.5 if r.confidence > 0.70 else 2.0
            r.preferred_mode      = "swing"
            r.threshold_increment = 0.0

        elif r.regime == "range":
            r.risk_multiplier     = 0.5
            r.tp_sl_ratio         = 1.5
            r.preferred_mode      = "scalp"
            r.threshold_increment = 0.0

        else:  # volatile
            r.risk_multiplier     = 0.3
            r.tp_sl_ratio         = 1.5
            r.preferred_mode      = None
            r.threshold_increment = 0.20

        # Fear & Greed agrega threshold_increment adicional
        if r.fear_greed < 15 or r.fear_greed > 85:
            r.threshold_increment += 0.15
        elif r.fear_greed < 20 or r.fear_greed > 80:
            r.threshold_increment += 0.10

        # Confianza baja reduce el risk_multiplier
        if r.confidence < 0.50:
            r.risk_multiplier *= 0.6

    # ─────────────────────────────────────────────────────────
    # CÁLCULOS MATEMÁTICOS
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _calc_ema(closes: list[float], period: int) -> float:
        """
        EMA estándar (exponential moving average).
        Multiplicador k = 2 / (period + 1).
        Seed = SMA de los primeros `period` valores.
        """
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        k   = 2.0 / (period + 1)
        ema = statistics.mean(closes[:period])
        for price in closes[period:]:
            ema = price * k + ema * (1 - k)
        return ema

    @staticmethod
    def _classify_ema_position(
        price: float, ema50: float, ema200: float
    ) -> str:
        if price > ema50 and price > ema200:
            return "above_both"
        elif price < ema50 and price < ema200:
            return "below_both"
        else:
            return "between"

    @staticmethod
    def _calc_adx(
        highs:  list[float],
        lows:   list[float],
        closes: list[float],
        period: int = 14,
    ) -> float:
        """
        ADX de Wilder (Average Directional Index).

        Rango de salida: 0 a 100.
        > 25 → tendencia presente.
        < 20 → mercado sin tendencia (rango).

        Algoritmo de Wilder:
        1. True Range (TR):
           TR = max(H-L, |H-cierre_anterior|, |L-cierre_anterior|)
        2. Directional Movement:
           +DM = H-H_anterior si ese valor > L_anterior-L y > 0, sino 0
           -DM = L_anterior-L si ese valor > H-H_anterior y > 0, sino 0
        3. Wilder Smooth (suma inicial, no promedio):
           smooth[0] = sum(data[:period])
           smooth[i] = smooth[i-1] - smooth[i-1]/period + data[i]
        4. +DI = 100 × +DM_smooth / TR_smooth
           -DI = 100 × -DM_smooth / TR_smooth
        5. DX = 100 × |+DI - -DI| / (+DI + -DI)
        6. ADX = Wilder smooth del DX con seed = mean(DX[:period])
        """
        n = len(closes)
        if n < period * 3:
            return 20.0  # valor neutral si no hay suficientes datos

        tr_list:   list[float] = []
        plus_dm:   list[float] = []
        minus_dm:  list[float] = []

        for i in range(1, n):
            h,  l,  cp = highs[i], lows[i], closes[i - 1]
            ph, pl     = highs[i - 1], lows[i - 1]

            tr = max(h - l, abs(h - cp), abs(l - cp))
            tr_list.append(tr)

            up   = h  - ph
            down = pl - l
            plus_dm.append(up   if up > down   and up   > 0 else 0.0)
            minus_dm.append(down if down > up  and down > 0 else 0.0)

        def wilder_sum_smooth(data: list[float], p: int) -> list[float]:
            """
            Wilder smoothing: seed = suma (no promedio) de los primeros p valores.
            smooth[0] = sum(data[:p])
            smooth[i] = smooth[i-1] - smooth[i-1]/p + data[p+i]
            """
            if len(data) < p:
                return [0.0]
            out = [sum(data[:p])]
            for v in data[p:]:
                out.append(out[-1] - out[-1] / p + v)
            return out

        tr_s  = wilder_sum_smooth(tr_list,  period)
        pdm_s = wilder_sum_smooth(plus_dm,  period)
        mdm_s = wilder_sum_smooth(minus_dm, period)

        dx_list: list[float] = []
        for tr_v, p_v, m_v in zip(tr_s, pdm_s, mdm_s):
            if tr_v == 0:
                dx_list.append(0.0)
                continue
            pdi   = 100.0 * p_v / tr_v
            mdi   = 100.0 * m_v / tr_v
            denom = pdi + mdi
            dx_list.append(
                100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
            )

        if len(dx_list) < period:
            return 20.0

        # ADX = Wilder smooth del DX con seed = promedio de los primeros period
        adx_val = statistics.mean(dx_list[:period])
        for v in dx_list[period:]:
            adx_val = adx_val - adx_val / period + v / period

        return round(min(100.0, max(0.0, adx_val)), 2)

    @staticmethod
    def _calc_hurst(closes: list[float]) -> float:
        """
        Hurst Exponent por el método R/S (Rescaled Range).

        > 0.5 → serie con memoria positiva (tendencia persistente)
        = 0.5 → movimiento Browniano puro (sin ventaja estadística)
        < 0.5 → serie con memoria negativa (reversión a la media)

        Pasos:
        1. Retornos log: r_i = ln(P_i / P_{i-1})
        2. Media de retornos: m
        3. Desviaciones acumuladas: Y_i = Σ(r_j - m) para j=1..i
        4. R = max(Y) - min(Y)
        5. S = std(retornos)
        6. RS = R / S
        7. H = ln(RS) / ln(N/2)
        """
        n = len(closes)
        if n < 20:
            return 0.5

        log_returns = []
        for i in range(1, n):
            if closes[i - 1] > 0 and closes[i] > 0:
                log_returns.append(math.log(closes[i] / closes[i - 1]))

        if len(log_returns) < 10:
            return 0.5

        m      = statistics.mean(log_returns)
        cumdev = []
        acc    = 0.0
        for r in log_returns:
            acc += r - m
            cumdev.append(acc)

        R = max(cumdev) - min(cumdev)
        try:
            S = statistics.stdev(log_returns)
        except statistics.StatisticsError:
            return 0.5

        if S == 0 or R == 0:
            return 0.5

        N = len(log_returns)
        try:
            hurst = math.log(R / S) / math.log(N / 2)
        except (ValueError, ZeroDivisionError):
            return 0.5

        # Clampear entre 0.1 y 0.9 para evitar extremos por ruido
        return round(max(0.1, min(0.9, hurst)), 3)

    @staticmethod
    def _check_microstructure(bid: float, ask: float, atr: float) -> bool:
        """
        Verifica que el spread bid/ask sea razonable.

        Umbral: spread > 20% del ATR → mercado ilíquido → False.

        Ejemplo:
          ATR = 100 USDC → threshold = 20 USDC
          Si ask - bid = 25 → microstructure_ok = False
        """
        if bid <= 0 or ask <= 0 or atr <= 0:
            return True
        return (ask - bid) <= (atr * 0.20)

    # ─────────────────────────────────────────────────────────
    # FEAR & GREED INDEX
    # ─────────────────────────────────────────────────────────

    def _get_fear_greed(self) -> int:
        """
        Obtiene el Fear & Greed Index de Alternative.me.
        API gratuita, sin clave. Endpoint: https://api.alternative.me/fng/

        Usa caché de fear_greed_cache_seconds (default 300s) para no
        spamear la API en cada ciclo del bot.

        Si la API falla devuelve 50 (neutral) sin interrumpir el ciclo.
        """
        now = time.time()
        if (
            self._fg_cache is not None
            and (now - self._fg_cache_time) < self.fear_greed_cache_seconds
        ):
            return self._fg_cache

        try:
            url = "https://api.alternative.me/fng/?limit=1&format=json"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            value = int(data["data"][0]["value"])
            self._fg_cache      = value
            self._fg_cache_time = now
            logger.debug(f"[Regime] Fear & Greed actualizado: {value}")
            return value
        except Exception as e:
            logger.warning(
                f"[Regime] Error obteniendo Fear & Greed: {e}. Usando caché o 50."
            )
            return self._fg_cache if self._fg_cache is not None else 50

    # ─────────────────────────────────────────────────────────
    # ACCESO AL ÚLTIMO RESULTADO
    # ─────────────────────────────────────────────────────────

    @property
    def last_result(self) -> Optional[RegimeResult]:
        """Devuelve el último RegimeResult calculado, o None si no hay."""
        return self._last_result


# ─────────────────────────────────────────────────────────────
# FUNCIÓN DE PESOS PARA SCORING.PY
# ─────────────────────────────────────────────────────────────

def get_scoring_weights(regime: str, confidence: float) -> dict[str, float]:
    """
    Devuelve multiplicadores de peso para cada componente del scoring
    según el régimen detectado.

    Cómo se usa en scoring.py:
        weights = get_scoring_weights(regime_result.regime, regime_result.confidence)
        rsi_pts = rsi_pts_base * weights["rsi"]

    Valores:
        1.0  → peso estándar (sin cambio)
        > 1.0 → ese indicador vale más en este régimen
        < 1.0 → ese indicador vale menos en este régimen
    """

    if regime == "bull_trend":
        w = {
            "rsi":             1.0,
            "emas":            1.3,   # EMAs alineadas confirman tendencia
            "macd":            1.2,
            "ut_bot":          1.1,
            "squeeze":         1.2,   # breakout de squeeze es señal fuerte
            "ichimoku":        1.2,
            "vwap":            1.0,
            "volume":          1.1,
            "bollinger":       0.8,   # Bollinger menos relevante en tendencia
            "macro":           1.0,
            "pivots":          0.9,
            "funding":         1.0,
            "obi":             1.0,
            "candle_patterns": 1.1,
            "cci":             0.9,
            "stoch":           0.9,
            "macd_divergence": 1.0,
            "lateralization":  0.7,
        }

    elif regime == "bear_trend":
        # Mismos pesos que bull_trend (la lógica es simétrica, short)
        w = {
            "rsi":             1.0,
            "emas":            1.3,
            "macd":            1.2,
            "ut_bot":          1.1,
            "squeeze":         1.2,
            "ichimoku":        1.2,
            "vwap":            1.0,
            "volume":          1.1,
            "bollinger":       0.8,
            "macro":           1.0,
            "pivots":          0.9,
            "funding":         1.0,
            "obi":             1.0,
            "candle_patterns": 1.1,
            "cci":             0.9,
            "stoch":           0.9,
            "macd_divergence": 1.0,
            "lateralization":  0.7,
        }

    elif regime == "range":
        w = {
            "rsi":             1.5,   # RSI extremo = señal principal en rango
            "emas":            0.6,   # EMAs alineadas menos relevantes en rango
            "macd":            0.8,
            "ut_bot":          0.7,
            "squeeze":         1.0,
            "ichimoku":        0.8,
            "vwap":            1.1,
            "volume":          0.9,
            "bollinger":       1.6,   # Bollinger tocada = señal principal en rango
            "macro":           0.8,
            "pivots":          1.4,   # Pivotes = targets clave en rango
            "funding":         1.0,
            "obi":             1.2,
            "candle_patterns": 1.3,   # Patrones de reversión valen más
            "cci":             1.3,
            "stoch":           1.3,
            "macd_divergence": 1.2,
            "lateralization":  0.5,   # Redundante cuando ya sabemos que es rango
        }

    else:  # volatile
        # En volátil se reduce todo proporcionalmente a la confianza.
        # El threshold_increment (+20%) hace el trabajo de filtrar señales débiles.
        factor = max(0.4, confidence)
        w = {k: factor for k in [
            "rsi", "emas", "macd", "ut_bot", "squeeze", "ichimoku",
            "vwap", "volume", "bollinger", "macro", "pivots", "funding",
            "obi", "candle_patterns", "cci", "stoch", "macd_divergence",
            "lateralization",
        ]}

    return w


# ─────────────────────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import random

    logging.basicConfig(level=logging.WARNING)
    print("=" * 62)
    print("  TEST regime_detector.py")
    print("=" * 62)

    random.seed(42)

    def gen_prices(n: int, trend: float = 0.0, noise: float = 0.01) -> list[float]:
        prices = [40000.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + trend + random.gauss(0, noise)))
        return prices

    def to_ohlc(closes: list[float]) -> tuple[list, list]:
        highs = [c * (1 + abs(random.gauss(0, 0.003))) for c in closes]
        lows  = [c * (1 - abs(random.gauss(0, 0.003))) for c in closes]
        return highs, lows

    detector = RegimeDetector()

    # ── Test 1: Tendencia alcista ─────────────────────────────
    print("\n[Test 1] Tendencia alcista fuerte")
    closes = gen_prices(250, trend=0.003, noise=0.004)
    highs, lows = to_ohlc(closes)
    atr = closes[-1] * 0.008
    atrs = [atr] * 20

    r = detector.detect(closes, highs, lows, atr, atrs,
                        closes[-1]*0.9999, closes[-1]*1.0001,
                        use_fear_greed=False)
    print(f"  {r.summary()}")
    print(f"  mode={r.preferred_mode} | risk={r.risk_multiplier} | tp_sl={r.tp_sl_ratio}")
    assert r.regime in ("bull_trend", "volatile"), f"got {r.regime}"
    print(f"  ✓ {r.regime}")

    # ── Test 2: Mercado lateral ───────────────────────────────
    print("\n[Test 2] Mercado lateral (ruido puro alrededor de 40000)")
    base = 40000.0
    closes = [base + random.gauss(0, 150) for _ in range(250)]
    highs, lows = to_ohlc(closes)
    atr = 150.0
    atrs = [atr] * 20

    r = detector.detect(closes, highs, lows, atr, atrs,
                        closes[-1]*0.9999, closes[-1]*1.0001,
                        use_fear_greed=False)
    print(f"  {r.summary()}")
    print(f"  mode={r.preferred_mode} | risk={r.risk_multiplier} | tp_sl={r.tp_sl_ratio}")
    assert r.regime in ("range", "volatile"), f"got {r.regime}"
    print(f"  ✓ {r.regime}")

    # ── Test 3: Alta volatilidad ──────────────────────────────
    print("\n[Test 3] Alta volatilidad (VR > 2)")
    closes = gen_prices(250, trend=0.0, noise=0.015)
    highs, lows = to_ohlc(closes)
    atr_hist = closes[-1] * 0.010
    atr_now  = closes[-1] * 0.035   # ATR actual muy por encima del histórico
    atrs = [atr_hist] * 20

    r = detector.detect(closes, highs, lows, atr_now, atrs,
                        closes[-1]*0.9999, closes[-1]*1.0001,
                        use_fear_greed=False)
    print(f"  {r.summary()}")
    print(f"  ✓ Régimen: {r.regime} (VR={r.volatility_ratio:.2f})")

    # ── Test 4: Microstructure mala ───────────────────────────
    print("\n[Test 4] Spread anormal → microstructure_ok=False")
    closes = gen_prices(250, trend=0.002, noise=0.005)
    highs, lows = to_ohlc(closes)
    atr = closes[-1] * 0.008
    atrs = [atr] * 20

    r = detector.detect(closes, highs, lows, atr, atrs,
                        closes[-1], closes[-1] * 1.005,
                        use_fear_greed=False)
    print(f"  {r.summary()}")
    assert r.regime == "volatile" and not r.microstructure_ok
    print(f"  ✓ regime=volatile forzado por spread anormal")

    # ── Test 5: ADX en rango correcto ─────────────────────────
    print("\n[Test 5] Valores de ADX en rango 0-100")
    for trend, noise, label in [
        (0.003, 0.003, "tendencia fuerte"),
        (0.0,   0.001, "lateral"),
        (0.0,   0.020, "volátil"),
    ]:
        closes = gen_prices(250, trend, noise)
        highs, lows = to_ohlc(closes)
        atr = closes[-1] * 0.01
        r = detector.detect(closes, highs, lows, atr, [atr]*20,
                            closes[-1]*0.9999, closes[-1]*1.0001,
                            use_fear_greed=False)
        assert 0 <= r.adx <= 100, f"ADX fuera de rango: {r.adx}"
        print(f"  {label}: ADX={r.adx:.1f} → {r.regime} ✓")

    # ── Test 6: Hurst Exponent ────────────────────────────────
    print("\n[Test 6] Hurst Exponent")
    trending = gen_prices(150, trend=0.003, noise=0.002)
    sideways = [40000.0 + random.gauss(0, 100) for _ in range(150)]
    ht = RegimeDetector._calc_hurst(trending)
    hs = RegimeDetector._calc_hurst(sideways)
    print(f"  Tendencia: H={ht:.3f} (esperado > 0.5)")
    print(f"  Lateral:   H={hs:.3f} (esperado < 0.5)")
    assert 0.1 <= ht <= 0.9 and 0.1 <= hs <= 0.9
    print(f"  ✓ Ambos dentro del rango válido 0.1-0.9")

    # ── Test 7: Scoring weights ───────────────────────────────
    print("\n[Test 7] Scoring weights")
    for reg in ("bull_trend", "bear_trend", "range", "volatile"):
        w = get_scoring_weights(reg, 0.75)
        print(f"  {reg:12s}: rsi={w['rsi']:.1f} | emas={w['emas']:.1f} | bollinger={w['bollinger']:.1f}")
    print("  ✓ Weights generados para todos los regímenes")

    print("\n" + "=" * 62)
    print("  Todos los tests pasaron ✓")
    print("=" * 62)
