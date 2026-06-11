"""
strategy.py  v1.2
=================
Capa de decisión. Recibe un ScoreResult y el estado actual
del sistema y decide exactamente qué hacer.

Cambios respecto a v1.1:
  - Corregidos nombres de atributos de IndicatorValues:
      iv.close  → iv.current_price
      iv.open   → (eliminado, se usa atr como proxy)
      iv.ema_7  → iv.ema7
      iv.ema_25 → iv.ema25
      iv.ema_50 → iv.ema50
      iv.ema_99 → iv.ema99
      iv.ema_200 → iv.ema200
  - Corregida firma de StrategyEvaluator.evaluate():
      parámetros extra inválidos eliminados
  - Integrada validación R/R de la calculadora de riesgo
      antes de aprobar cualquier entrada

No ejecuta órdenes. Devuelve Decision objects que orders.py ejecuta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from enum import Enum

from indicators import IndicatorValues, analyze_macro_trend
from scoring import ScoreResult, StrategyEvaluator
from config import cfg
from risk_manager import RiskManager
from regime_detector import RegimeResult

# ─────────────────────────────────────────────
# ENUMS Y ESTRUCTURAS DE SALIDA
# ─────────────────────────────────────────────

class Action(Enum):
    OPEN_LONG    = "open_long"
    OPEN_SHORT   = "open_short"
    CLOSE_LONG   = "close_long"
    CLOSE_SHORT  = "close_short"
    PARTIAL_CLOSE = "partial_close"
    MOVE_SL      = "move_sl"
    HOLD         = "hold"
    PAPER_ONLY   = "paper_only"


@dataclass
class TakeProfit:
    price:     float
    size_pct:  float
    order_id:  str = ""


@dataclass
class Decision:
    """Todo lo que orders.py necesita para ejecutar (o no) una operación."""
    action:    Action
    direction: str   = ""
    mode:      str   = ""

    entry_price: float = 0.0
    size_usdc:   float = 0.0
    leverage:    int   = 1

    sl_price:  float              = 0.0
    tp_levels: list[TakeProfit]   = field(default_factory=list)

    trailing_active:   bool  = False
    trailing_distance: float = 0.0
    trailing_trigger:  float = 0.0

    score:     Optional[ScoreResult] = None
    reason:    str                   = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    close_pct: float = 0.0

    # Resultado de la calculadora de riesgo (para logging)
    rr_ratio:  float = 0.0
    rr_valid:  bool  = True

    def __str__(self) -> str:
        if self.action in (Action.HOLD, Action.PAPER_ONLY):
            return f"[Decision] {self.action.value.upper()} — {self.reason}"
        lines = [
            f"[Decision] {self.action.value.upper()} | {self.mode} | x{self.leverage}",
            f"  Entrada: ${self.entry_price:,.2f}",
            f"  Tamaño:  ${self.size_usdc:.2f} USDC",
            f"  SL:      ${self.sl_price:,.2f}",
            f"  R/R:     1:{self.rr_ratio:.2f}",
        ]
        for i, tp in enumerate(self.tp_levels, 1):
            lines.append(f"  TP{i}: ${tp.price:,.2f} ({tp.size_pct:.0f}%)")
        if self.trailing_active:
            lines.append(
                f"  Trailing: {self.trailing_distance:.2f}% "
                f"(activa desde +{self.trailing_trigger:.2f}%)"
            )
        return "\n".join(lines)


@dataclass
class Position:
    """Estado de la posición abierta actual."""
    open:      bool  = False
    direction: str   = ""
    mode:      str   = ""

    entry_price:   float = 0.0
    size_usdc:     float = 0.0
    leverage:      int   = 1
    sl_price:      float = 0.0
    tp_levels:     list[TakeProfit] = field(default_factory=list)
    remaining_pct: float = 1.0

    trailing_active:   bool  = False
    trailing_distance: float = 0.0
    trailing_stop:     float = 0.0
    highest_price:     float = 0.0
    lowest_price:      float = 0.0
    breakeven_set:     bool  = False

    opened_at:      Optional[datetime]    = None
    score_at_open:  Optional[ScoreResult] = None
    regime_at_open: str                   = "volatile"


# ─────────────────────────────────────────────
# CALCULADORA DE RIESGO (integrada)
# ─────────────────────────────────────────────

@dataclass
class RRResult:
    """Resultado de la validación R/R pre-entrada."""
    valid:           bool
    rr_ratio:        float
    sl_distance_pct: float
    leverage:        float
    reasons:         list[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "✓ VÁLIDO" if self.valid else "✗ RECHAZADO"
        return (
            f"[RR {status}] R/R=1:{self.rr_ratio:.2f} | "
            f"SL={self.sl_distance_pct*100:.2f}% | "
            f"Lev=x{self.leverage:.1f}"
            + (f" | {', '.join(self.reasons)}" if self.reasons else "")
        )


def validate_rr(
    entry_price:  float,
    sl_price:     float,
    tp_price:     float,   # TP3 = target completo
    leverage:     int,
) -> RRResult:
    """
    Valida matemáticamente una operación antes de ejecutarla.

    Basada en calculadora_riesgo.py, adaptada al sistema del bot.
    Usa los límites configurados en cfg.risk.

    Parámetros:
        entry_price   Precio de entrada
        sl_price      Precio del stop loss
        tp_price      Precio del take profit final (TP3)
        leverage      Apalancamiento calculado

    Devuelve:
        RRResult con valid=True si los números cierran, False si no.
    """
    if entry_price <= 0 or sl_price <= 0 or tp_price <= 0:
        return RRResult(valid=False, rr_ratio=0.0, sl_distance_pct=0.0,
                        leverage=leverage, reasons=["Precios inválidos"])

    sl_distance  = abs(entry_price - sl_price) / entry_price
    tp_distance  = abs(tp_price - entry_price) / entry_price
    rr_ratio     = tp_distance / sl_distance if sl_distance > 0 else 0.0

    reasons = []

    if not cfg.risk.rr_check_enabled:
        return RRResult(valid=True, rr_ratio=rr_ratio,
                        sl_distance_pct=sl_distance, leverage=leverage)

    if rr_ratio < cfg.risk.min_rr_ratio:
        reasons.append(
            f"R/R 1:{rr_ratio:.2f} < mínimo 1:{cfg.risk.min_rr_ratio:.1f}"
        )

    if leverage > cfg.risk.max_leverage_allowed:
        reasons.append(
            f"Apalancamiento x{leverage} > máximo x{cfg.risk.max_leverage_allowed}"
        )

    if sl_distance < cfg.risk.min_sl_distance_pct:
        reasons.append(
            f"SL {sl_distance*100:.2f}% muy ajustado "
            f"(mín {cfg.risk.min_sl_distance_pct*100:.1f}%)"
        )

    return RRResult(
        valid           = len(reasons) == 0,
        rr_ratio        = round(rr_ratio, 2),
        sl_distance_pct = sl_distance,
        leverage        = leverage,
        reasons         = reasons,
    )


# ─────────────────────────────────────────────
# CONSTRUCTOR DE OPERACIONES
# ─────────────────────────────────────────────

class OperationBuilder:
    """
    Construye un Decision completo a partir de un ScoreResult y el régimen.
    Incluye validación R/R antes de aprobar la entrada.
    """

    def build_entry(
        self,
        score:        ScoreResult,
        indicators:   dict[str, IndicatorValues],
        capital:      float,
        regime:       RegimeResult,
        risk_manager: RiskManager,
    ) -> Decision:

        mode      = score.mode
        tf        = score.timeframe
        iv        = indicators.get(tf)
        direction = score.direction

        if iv is None:
            return Decision(action=Action.HOLD, reason=f"Sin datos {tf}")

        # ── Precio de entrada ─────────────────────────────────
        # current_price es el precio de cierre de la última vela
        entry_price = iv.current_price
        if entry_price <= 0:
            return Decision(action=Action.HOLD, reason="Precio de entrada inválido")

        # ── Stop Loss ─────────────────────────────────────────
        mode_params = cfg.modes.get(mode)
        sl_base     = mode_params.sl_base_pct
        atr_pct     = iv.atr / entry_price if (iv.atr > 0 and entry_price > 0) else sl_base
        sl_pct      = max(sl_base, min(sl_base * 1.5, atr_pct * 1.5))

        if direction == "long":
            sl_price = entry_price * (1 - sl_pct)
            # Anclar a EMA200 si está cerca y por debajo
            if iv.ema200 > 0 and iv.ema200 < entry_price:
                gap = (entry_price - iv.ema200) / entry_price
                if gap < sl_pct:
                    sl_price = iv.ema200 * 0.999
        else:
            sl_price = entry_price * (1 + sl_pct)
            if iv.ema200 > 0 and iv.ema200 > entry_price:
                gap = (iv.ema200 - entry_price) / entry_price
                if gap < sl_pct:
                    sl_price = iv.ema200 * 1.001

        # ── Tres TPs escalonados ──────────────────────────────
        tp_base    = score.approx_tp_pct
        tp_factors = [
            cfg.modes.tp1_distance_factor,
            cfg.modes.tp2_distance_factor,
            cfg.modes.tp3_distance_factor,
        ]
        tp_sizes = [
            cfg.modes.tp1_size_pct,
            cfg.modes.tp2_size_pct,
            cfg.modes.tp3_size_pct,
        ]

        tp_levels = []
        for factor, size in zip(tp_factors, tp_sizes):
            distance = tp_base * factor
            if direction == "long":
                tp_price = entry_price * (1 + distance)
                tp_price = self._adjust_tp_to_ema(tp_price, entry_price, iv, direction)
            else:
                tp_price = entry_price * (1 - distance)
                tp_price = self._adjust_tp_to_ema(tp_price, entry_price, iv, direction)
            tp_levels.append(TakeProfit(price=tp_price, size_pct=size))

        # ── Validación R/R (calculadora de riesgo integrada) ──
        tp3_price = tp_levels[-1].price if tp_levels else entry_price
        leverage  = score.leverage
        rr        = validate_rr(entry_price, sl_price, tp3_price, leverage)

        if not rr.valid:
            return Decision(
                action  = Action.HOLD,
                reason  = f"Validación R/R fallida: {', '.join(rr.reasons)}",
                rr_ratio = rr.rr_ratio,
                rr_valid = False,
            )

        # ── Tamaño de posición ────────────────────────────────
        size_usdc = risk_manager.position_size(
            capital          = capital,
            sl_pct           = abs(entry_price - sl_price) / entry_price,
            regime           = regime.regime,
            volatility_ratio = regime.volatility_ratio,
            confidence       = regime.confidence,
        )

        # ── Trailing stop ─────────────────────────────────────
        trailing_distance = mode_params.trailing_distance
        trailing_trigger  = mode_params.trailing_trigger

        return Decision(
            action            = Action.OPEN_LONG if direction == "long" else Action.OPEN_SHORT,
            direction         = direction,
            mode              = mode,
            entry_price       = entry_price,
            size_usdc         = size_usdc,
            leverage          = leverage,
            sl_price          = sl_price,
            tp_levels         = tp_levels,
            trailing_active   = True,
            trailing_distance = trailing_distance,
            trailing_trigger  = trailing_trigger,
            score             = score,
            rr_ratio          = rr.rr_ratio,
            rr_valid          = True,
            reason            = (
                f"N{score.signal_level} {score.normalized*100:.1f}% | "
                f"R/R=1:{rr.rr_ratio:.2f} | régimen={regime.regime}"
            ),
        )

    def _adjust_tp_to_ema(
        self,
        tp_price:    float,
        entry_price: float,
        iv:          IndicatorValues,
        direction:   str,
        margin:      float = 0.001,
    ) -> float:
        """
        Si hay una EMA entre el precio de entrada y el TP,
        ajusta el TP para quedar justo antes de esa EMA.
        Solo si la EMA está dentro del tolerance% del TP original.
        """
        # Nombres correctos de IndicatorValues
        emas = {
            "ema7":   iv.ema7,
            "ema25":  iv.ema25,
            "ema50":  iv.ema50,
            "ema99":  iv.ema99,
            "ema200": iv.ema200,
        }
        tolerance = cfg.orders.ema_tolerance_pct

        for name, ema_val in emas.items():
            if not ema_val or ema_val <= 0:
                continue
            if direction == "long":
                if entry_price < ema_val < tp_price:
                    if abs(ema_val - tp_price) / tp_price < tolerance:
                        tp_price = ema_val * (1 - margin)
            else:
                if tp_price < ema_val < entry_price:
                    if abs(ema_val - tp_price) / tp_price < tolerance:
                        tp_price = ema_val * (1 + margin)
        return tp_price


# ─────────────────────────────────────────────
# GESTOR DE POSICIÓN ABIERTA
# ─────────────────────────────────────────────

class PositionManager:
    """
    Gestiona la posición abierta en cada ciclo del bot.
    """

    def update(
        self,
        position:       Position,
        current_price:  float,
        indicators:     dict[str, IndicatorValues],
        counter_signal: Optional[ScoreResult] = None,
    ) -> Optional[Decision]:
        if not position.open:
            return None

        iv = indicators.get(cfg.modes.get(position.mode).timeframe_main)
        if iv is None:
            return None

        if position.direction == "long":
            position.highest_price = max(position.highest_price, current_price)
        else:
            position.lowest_price = min(
                position.lowest_price if position.lowest_price > 0 else current_price,
                current_price
            )

        # 1. Verificar SL
        sl_hit = (
            (position.direction == "long"  and current_price <= position.sl_price) or
            (position.direction == "short" and current_price >= position.sl_price)
        )
        if sl_hit:
            action = Action.CLOSE_LONG if position.direction == "long" else Action.CLOSE_SHORT
            return Decision(
                action    = action,
                direction = position.direction,
                mode      = position.mode,
                reason    = f"SL alcanzado @ ${current_price:,.2f}",
            )

        # 2. Verificar señal contraria
        if counter_signal and counter_signal.should_trade:
            if counter_signal.direction != position.direction:
                action = Action.CLOSE_LONG if position.direction == "long" else Action.CLOSE_SHORT
                return Decision(
                    action    = action,
                    direction = position.direction,
                    mode      = position.mode,
                    reason    = (
                        f"Señal contraria {counter_signal.direction} "
                        f"N{counter_signal.signal_level} "
                        f"{counter_signal.normalized*100:.1f}%"
                    ),
                )

        # 3. Trailing stop
        if position.trailing_active:
            trailing_decision = self._update_trailing(position, current_price)
            if trailing_decision:
                return trailing_decision

        # 4. Cierre parcial en EMAs intermedias
        if position.remaining_pct > 0.25 and iv:
            partial_decision = self._check_partial_close(position, current_price, iv)
            if partial_decision:
                return partial_decision

        return None

    def _update_trailing(
        self,
        position:      Position,
        current_price: float,
    ) -> Optional[Decision]:
        mode_params = cfg.modes.get(position.mode)
        trigger     = mode_params.trailing_trigger
        distance    = mode_params.trailing_distance

        if position.direction == "long":
            advance = (current_price - position.entry_price) / position.entry_price
            if advance >= trigger:
                new_stop = current_price * (1 - distance)
                if new_stop > position.trailing_stop:
                    position.trailing_stop = new_stop
                    if new_stop > position.sl_price:
                        position.sl_price = new_stop
                if current_price <= position.trailing_stop:
                    return Decision(
                        action    = Action.CLOSE_LONG,
                        direction = "long",
                        mode      = position.mode,
                        reason    = f"Trailing stop @ ${position.trailing_stop:,.2f}",
                    )
        else:
            advance = (position.entry_price - current_price) / position.entry_price
            if advance >= trigger:
                new_stop = current_price * (1 + distance)
                if position.trailing_stop == 0 or new_stop < position.trailing_stop:
                    position.trailing_stop = new_stop
                    if new_stop < position.sl_price or position.sl_price == 0:
                        position.sl_price = new_stop
                if current_price >= position.trailing_stop:
                    return Decision(
                        action    = Action.CLOSE_SHORT,
                        direction = "short",
                        mode      = position.mode,
                        reason    = f"Trailing stop @ ${position.trailing_stop:,.2f}",
                    )
        return None

    def _check_partial_close(
        self,
        position:      Position,
        current_price: float,
        iv:            IndicatorValues,
    ) -> Optional[Decision]:
        """
        Tabla de cierre parcial en EMAs intermedias (sección 9 del doc).
        """
        atr       = iv.atr if iv.atr > 0 else current_price * 0.005
        direction = position.direction

        # Nombres correctos de IndicatorValues
        ema_weights = {
            "ema200": (iv.ema200, 4),
            "ema99":  (iv.ema99,  3),
            "ema50":  (iv.ema50,  3),
            "ema25":  (iv.ema25,  2),
            "ema7":   (iv.ema7,   1),
        }

        for ema_name, (ema_val, ema_weight) in ema_weights.items():
            if not ema_val or ema_val <= 0:
                continue

            tolerance = cfg.orders.ema_tolerance_pct
            near = abs(current_price - ema_val) / current_price < tolerance

            if not near:
                continue

            ema_in_path = (
                (direction == "long"  and position.entry_price < ema_val <= current_price) or
                (direction == "short" and current_price <= ema_val < position.entry_price)
            )
            if not ema_in_path:
                continue

            # Fuerza de ruptura en múltiplos de ATR
            # Usamos atr_pct como proxy del tamaño de vela si no tenemos open
            candle_size     = iv.atr_pct * current_price if iv.atr_pct > 0 else atr * 0.5
            rupture_strength = candle_size / atr if atr > 0 else 0.5

            support_strong   = ema_weight >= 3
            support_moderate = ema_weight >= 2

            if rupture_strength > 1.0:
                close_pct = 30 if support_strong else 15
            elif rupture_strength > 0.5:
                if support_strong:     close_pct = 50
                elif support_moderate: close_pct = 35
                else:                  close_pct = 20
            else:
                close_pct = 60 if support_strong else 30

            close_pct = max(close_pct, 15)

            return Decision(
                action    = Action.PARTIAL_CLOSE,
                direction = direction,
                mode      = position.mode,
                close_pct = close_pct,
                reason    = (
                    f"Cierre parcial {close_pct}% en {ema_name} "
                    f"(rup={rupture_strength:.2f}×ATR, peso={ema_weight})"
                ),
            )

        return None

    def set_breakeven(self, position: Position) -> None:
        fee = cfg.capital.fee_worst_case
        if position.direction == "long":
            be = position.entry_price * (1 + fee)
            if be > position.sl_price:
                position.sl_price    = be
                position.breakeven_set = True
        else:
            be = position.entry_price * (1 - fee)
            if be < position.sl_price or position.sl_price == 0:
                position.sl_price    = be
                position.breakeven_set = True


# ─────────────────────────────────────────────
# CONTROLADOR PRINCIPAL DE STRATEGY
# ─────────────────────────────────────────────

class StrategyController:
    """
    Punto de entrada principal para el ciclo del bot.

    Coordina evaluación de señales, verificación de riesgo,
    construcción de la decisión y gestión de posición abierta.

    Uso en bot.py:
        controller = StrategyController(risk_manager=rm)
        decision = controller.cycle(
            indicators=indicators,
            macro=macro,
            regime=regime_result,
            capital=capital,
            position=current_position,
            active_modes={"scalp": True, "mediano": True, "swing": True},
            threshold_increment=0.0,
        )
    """

    def __init__(self, risk_manager: Optional[RiskManager] = None):
        self._evaluator    = StrategyEvaluator()
        self._builder      = OperationBuilder()
        self._pos_manager  = PositionManager()
        self._risk_manager = risk_manager or RiskManager()

    def cycle(
        self,
        indicators:          dict[str, IndicatorValues],
        macro,
        regime:              RegimeResult,
        capital:             float,
        position:            Position,
        active_modes:        Optional[dict[str, bool]] = None,
        threshold_increment: float = 0.0,
    ) -> Decision:
        """Corre un ciclo completo y devuelve la Decision."""

        # ── Modo revisión ─────────────────────────────────────
        if self._risk_manager.is_review_mode():
            return Decision(
                action = Action.PAPER_ONLY,
                reason = "Bot en modo revisión (paper trading)",
            )

        # Modos activos como lista
        modes_list = self._active_modes_list(active_modes)

        # ── Gestionar posición abierta ────────────────────────
        if position.open:
            # Evaluar posible señal contraria
            all_scores = self._evaluator.evaluate_all(
                indicators          = indicators,
                macro               = macro,
                active_modes        = modes_list,
                regime              = regime.regime,
                regime_confidence   = regime.confidence,
                threshold_increment = threshold_increment + regime.threshold_increment,
            )
            counter = next(
                (s for s in all_scores
                 if s.should_trade and s.direction != position.direction),
                None
            )
            current_price = self._get_current_price(indicators, position.mode)
            decision = self._pos_manager.update(
                position       = position,
                current_price  = current_price,
                indicators     = indicators,
                counter_signal = counter,
            )
            if decision and decision.action == Action.PARTIAL_CLOSE:
                self._pos_manager.set_breakeven(position)
            return decision or Decision(
                action = Action.HOLD,
                reason = "Posición abierta — sin acción"
            )

        # ── Evaluar entrada ───────────────────────────────────
        best = self._evaluator.evaluate(
            indicators          = indicators,
            macro               = macro,
            active_modes        = modes_list,
            regime              = regime.regime,
            regime_confidence   = regime.confidence,
            threshold_increment = threshold_increment + regime.threshold_increment,
        )

        if best is None:
            return Decision(action=Action.HOLD, reason="Sin señales que superen el umbral")

        # ── Verificar riesgo ──────────────────────────────────
        if self._risk_manager.is_blocked(
            mode      = best.mode,
            score_pct = best.normalized,
            level     = best.signal_level,
        ):
            status = self._risk_manager.get_status()
            return Decision(
                action = Action.HOLD,
                reason = f"Bloqueado por risk manager: {status['mode_status'].get(best.mode)}",
            )

        # ── Construir la decisión (incluye validación R/R) ────
        return self._builder.build_entry(
            score        = best,
            indicators   = indicators,
            capital      = capital,
            regime       = regime,
            risk_manager = self._risk_manager,
        )

    def record_trade_result(self, mode: str, won: bool, level: int = 1) -> None:
        self._risk_manager.record_trade(mode=mode, won=won, level=level)

    @staticmethod
    def _active_modes_list(active_modes: Optional[dict[str, bool]]) -> list[str]:
        """Convierte dict {modo: bool} a lista de modos activos."""
        if active_modes is None:
            return ["scalp", "mediano", "swing"]
        return [m for m, active in active_modes.items() if active]

    @staticmethod
    def _get_current_price(
        indicators: dict[str, IndicatorValues],
        mode: str,
    ) -> float:
        tf = cfg.modes.get(mode).timeframe_main
        iv = indicators.get(tf)
        return iv.current_price if iv else 0.0


if __name__ == "__main__":
    print("strategy.py v1.2 — importado correctamente.")
    print(f"  cfg integrado: capital={cfg.capital.initial_capital} USDC")
    print(f"  validate_rr disponible: min R/R = 1:{cfg.risk.min_rr_ratio}")

    # Test rápido de validate_rr
    rr = validate_rr(100000, 99000, 103000, leverage=3)
    print(f"  Test R/R válido: {rr}")
    assert rr.valid, "Debería ser válido"

    rr_bad = validate_rr(100000, 99800, 100300, leverage=3)
    print(f"  Test R/R inválido: {rr_bad}")
    assert not rr_bad.valid, "Debería ser inválido (R/R < 1.5)"

    print("  Tests pasados ✓")
