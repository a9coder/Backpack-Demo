"""纯 Maker-Maker 策略：仅挂买一/卖一，双向成交后继续循环。"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from logger import setup_logger
from strategies.market_maker import MarketMaker, format_balance
from strategies.perp_market_maker import PerpetualMarketMaker
from utils.helpers import round_to_precision, round_to_tick_size

logger = setup_logger("pure_maker_strategy")


class _PureMakerMixin:
    """纯 Maker 策略核心实现：仅在买一/卖一挂单，双向成交后继续。"""

    def __init__(
        self,
        *args: Any,
        strategy_label: str = "纯Maker",
        scale_in_price_step_pct: float = 0.0,
        scale_in_size_pct: float = 0.0,
        close_price_mode: str = "entry",
        next_round_delay_seconds: float = 3.0,
        **kwargs: Any,
    ) -> None:
        # 强制单层订单，禁用重平衡
        kwargs.pop("max_orders", None)
        kwargs.pop("enable_rebalance", None)
        kwargs.pop("base_asset_target_percentage", None)
        kwargs.pop("rebalance_threshold", None)

        kwargs["max_orders"] = 1
        kwargs["enable_rebalance"] = False

        self._strategy_label = strategy_label

        # 加仓配置（仅在永续合约策略中生效）
        self.scale_in_price_step_pct = max(0.0, float(scale_in_price_step_pct or 0.0))
        self.scale_in_size_pct = max(0.0, float(scale_in_size_pct or 0.0))
        self._scale_in_last_ref_price = 0.0
        self._scale_in_last_net = 0.0
        close_mode = str(close_price_mode or "entry").lower()
        if close_mode not in {"entry", "break_even"}:
            close_mode = "entry"
        self.close_price_mode = close_mode
        self._next_round_delay = max(0.0, float(next_round_delay_seconds or 0.0))
        self._next_round_scheduled = False
        self._next_round_thread: Optional[threading.Thread] = None
        self._restart_on_flat = False
        self._max_post_only_adjustments = 50
        self._current_close_order_id: Optional[str] = None
        self._scale_ladder_deployed = False
        
        # 成交追踪
        self._bid_filled = False
        self._ask_filled = False
        self._round_count = 0
        self._total_profit_quote = 0.0  # 累计利润（报价资产）
        
        # 请求限流
        self._request_intervals: Dict[str, float] = {
            "limit": 0.35,
        }
        self._last_request_ts: Dict[str, float] = {key: 0.0 for key in self._request_intervals}

        super().__init__(*args, **kwargs)

        self.max_orders = 1
        # 当前一轮挂单的目标数量与成交进度
        self._current_buy_order_qty = 0.0
        self._current_sell_order_qty = 0.0
        self._buy_filled_qty = 0.0
        self._sell_filled_qty = 0.0
        # 完全成交的容忍误差（防止精度问题）
        self._fill_tolerance = max(getattr(self, "min_order_size", 0.0) / 1000, 1e-8)

        logger.info("初始化纯 Maker-Maker 策略 (%s)", self._strategy_label)
    # ------------------------------------------------------------------
    # 挂单逻辑
    # ------------------------------------------------------------------
    def place_limit_orders(self) -> None:
        """仅在买一/卖一位置挂出 Post-Only 订单。

        逻辑：
        - 若当前一轮买/卖单尚未全部成交：不取消、不重下，继续等待成交
        - 仅在上一轮双向完全成交后，才取消残余订单并挂出下一轮
        """

        self.check_ws_connection()

        # 如果已有一轮挂单在进行，优先检查是否全部成交
        if self._current_buy_order_qty > 0.0 or self._current_sell_order_qty > 0.0:
            buy_done = (
                self._current_buy_order_qty <= 0.0
                or self._buy_filled_qty + self._fill_tolerance >= self._current_buy_order_qty
            )
            sell_done = (
                self._current_sell_order_qty <= 0.0
                or self._sell_filled_qty + self._fill_tolerance >= self._current_sell_order_qty
            )

            if not (buy_done and sell_done):
                logger.debug(
                    "当前一轮挂单尚未全部成交，保持原有挂单不变（买已完成=%s, 卖已完成=%s）",
                    buy_done,
                    sell_done,
                )
                return

            # 当前一轮已全部成交，可以开始新一轮
            self._round_count += 1
            logger.info(
                "✅ 第 %d 轮双向完全成交，累计估算利润约 %.8f %s，准备挂出新一轮",
                self._round_count,
                self._total_profit_quote,
                self.quote_asset,
            )
            # 重置进度，准备新一轮
            self._bid_filled = False
            self._ask_filled = False
            self._current_buy_order_qty = 0.0
            self._current_sell_order_qty = 0.0
            self._buy_filled_qty = 0.0
            self._sell_filled_qty = 0.0

        # 只有在上一轮结束（或首轮）时才会走到这里：可以取消旧订单并挂出新一轮
        self.cancel_existing_orders()

        bid_price, ask_price = self.get_market_depth()
        if bid_price is None or ask_price is None:
            logger.warning("无法获取买一/卖一价格，跳过本轮挂单")
            return

        buy_price = round_to_tick_size(bid_price, self.tick_size)
        sell_price = round_to_tick_size(ask_price, self.tick_size)

        # 确保价差足够
        if sell_price <= buy_price:
            sell_price = round_to_tick_size(buy_price + self.tick_size, self.tick_size)
            if sell_price <= buy_price:
                logger.warning("价差过窄无法安全挂单，跳过本轮")
                return

        buy_qty, sell_qty = self._determine_order_sizes(buy_price, sell_price)
        if buy_qty is None or sell_qty is None:
            logger.warning("无法计算挂单数量，跳过本轮")
            return

        # 记录本轮目标数量与进度
        self._current_buy_order_qty = buy_qty
        self._current_sell_order_qty = sell_qty
        self._buy_filled_qty = 0.0
        self._sell_filled_qty = 0.0
        self._bid_filled = False
        self._ask_filled = False

        self.active_buy_orders = []
        self.active_sell_orders = []

        # 只挂未成交的方向（首轮两侧都会挂出）
        if buy_qty >= self.min_order_size:
            buy_order = self._build_limit_order(
                side="Bid",
                price=buy_price,
                quantity=buy_qty,
            )
            result = self._submit_order(buy_order, slot="limit")
            if isinstance(result, dict) and "error" in result:
                logger.error(f"买单挂单失败: {result['error']}")
                # 若下单失败，则清空本轮买单目标，避免无限等待
                self._current_buy_order_qty = 0.0
                self._handle_order_submission_failure("Bid", result)
                return
            else:
                logger.info(
                    "🟢 买单已挂出: 价格 %s, 数量 %s",
                    format_balance(buy_price),
                    format_balance(buy_qty),
                )
                self.active_buy_orders.append(result)
                self.orders_placed += 1

        if sell_qty >= self.min_order_size:
            sell_order = self._build_limit_order(
                side="Ask",
                price=sell_price,
                quantity=sell_qty,
            )
            result = self._submit_order(sell_order, slot="limit")
            if isinstance(result, dict) and "error" in result:
                logger.error(f"卖单挂单失败: {result['error']}")
                # 若下单失败，则清空本轮卖单目标，避免无限等待
                self._current_sell_order_qty = 0.0
                self._handle_order_submission_failure("Ask", result)
                return
            else:
                logger.info(
                    "🔴 卖单已挂出: 价格 %s, 数量 %s",
                    format_balance(sell_price),
                    format_balance(sell_qty),
                )
                self.active_sell_orders.append(result)
                self.orders_placed += 1

    def _determine_order_sizes(self, buy_price: float, sell_price: float) -> Tuple[Optional[float], Optional[float]]:
        """根据余额决定单笔买/卖单量。"""

        if self.order_quantity is not None:
            quantity = max(
                self.min_order_size,
                round_to_precision(self.order_quantity, self.base_precision),
            )
            return quantity, quantity

        base_available, base_total = self.get_asset_balance(self.base_asset)
        quote_available, quote_total = self.get_asset_balance(self.quote_asset)

        reference_price = sell_price if sell_price else buy_price
        if reference_price <= 0:
            return None, None

        # 使用总资金的 10% 作为单笔订单规模
        allocation = 0.1
        quote_budget = quote_total * allocation
        base_budget = base_total * allocation

        if quote_budget <= 0 or base_budget <= 0:
            logger.warning("余额不足，无法挂出 Maker 订单")
            return None, None

        buy_qty = round_to_precision(quote_budget / reference_price, self.base_precision)
        sell_qty = round_to_precision(base_budget, self.base_precision)

        buy_qty = max(self.min_order_size, buy_qty)
        sell_qty = max(self.min_order_size, sell_qty)

        if quote_available < buy_qty * reference_price:
            logger.info(
                "可用报价资产不足 (%.8f)，将依赖自动赎回",
                quote_available,
            )
        if base_available < sell_qty:
            logger.info(
                "可用基础资产不足 (%.8f)，将依赖自动赎回",
                base_available,
            )

        return buy_qty, sell_qty

    # ------------------------------------------------------------------
    # 轮次控制
    # ------------------------------------------------------------------
    def _is_position_flat(self) -> bool:
        if hasattr(self, "get_position_state"):
            try:
                position_state = self.get_position_state()
            except Exception as exc:  # noqa: BLE001
                logger.warning("檢查倉位時出錯，暫不啟動下一輪: %s", exc)
                return False
            net = float(position_state.get("net", 0.0) or 0.0)
            tolerance = max(self._fill_tolerance, getattr(self, "min_order_size", 0.0) / 10)
            return abs(net) <= tolerance
        return True

    def _schedule_next_round(self) -> None:
        if getattr(self, "_stop_flag", False):
            return
        if self._next_round_scheduled:
            return
        self._next_round_scheduled = True

        def _fire() -> None:
            try:
                if self._next_round_delay > 0:
                    logger.info("倉位已平，%.1f 秒後啟動下一輪掛單", self._next_round_delay)
                    time.sleep(self._next_round_delay)
                else:
                    logger.info("倉位已平，立即啟動下一輪掛單")
                self.place_limit_orders()
            except Exception as exc:  # noqa: BLE001
                logger.error("啟動下一輪掛單時出錯: %s", exc)
            finally:
                self._next_round_scheduled = False

        self._next_round_thread = threading.Thread(target=_fire, daemon=True)
        self._next_round_thread.start()

    def _maybe_trigger_next_round(self) -> None:
        if not (self._bid_filled and self._ask_filled):
            return
        if not self._is_position_flat():
            logger.debug("倉位尚未完全平倉，暫不啟動下一輪")
            return
        self._schedule_next_round()

    def _determine_exit_price(self, position_state: Optional[Dict[str, Any]], fallback: float) -> float:
        if self.close_price_mode != "break_even" or not position_state:
            return fallback
        for key in ("break_even_price", "breakEvenPrice", "breakevenPrice"):
            price = position_state.get(key)
            if price:
                try:
                    price_val = float(price)
                except (TypeError, ValueError):
                    continue
                if price_val > 0:
                    return price_val
        avg_entry = position_state.get("avg_entry")
        if avg_entry:
            try:
                return float(avg_entry)
            except (TypeError, ValueError):
                return fallback
        return fallback

    def _reset_round_progress(self) -> None:
        """在強制結束當前一輪時重置掛單目標與狀態。"""
        self._current_buy_order_qty = 0.0
        self._current_sell_order_qty = 0.0
        self._buy_filled_qty = 0.0
        self._sell_filled_qty = 0.0
        self._bid_filled = False
        self._ask_filled = False
        self.active_buy_orders = []
        self.active_sell_orders = []
        self._scale_ladder_deployed = False
        self._current_close_order_id = None

    def _start_cycle_async(self, delay: float = 0.0) -> None:
        if getattr(self, "_stop_flag", False):
            return

        def _fire() -> None:
            try:
                if delay > 0:
                    time.sleep(delay)
                self.place_limit_orders()
            except Exception as exc:  # noqa: BLE001
                logger.error("重新啟動掛單時出錯: %s", exc)

        threading.Thread(target=_fire, daemon=True).start()

    def _place_emergency_close_order(self, position_state: Optional[Dict[str, Any]]) -> bool:
        if not position_state:
            return False
        min_qty = getattr(self, "min_order_size", 0.0)
        net = float(position_state.get("net", 0.0) or 0.0)
        if abs(net) < min_qty:
            return False

        qty = round_to_precision(abs(net), self.base_precision)
        if qty < min_qty:
            return False

        hedge_side = "Ask" if net > 0 else "Bid"
        fallback_price = float(position_state.get("avg_entry", 0.0) or 0.0)
        exit_price = self._determine_exit_price(position_state, fallback_price)
        result = self._place_post_only_perp_order(
            side=hedge_side,
            quantity=qty,
            price=exit_price,
            reduce_only=True,
        )
        if isinstance(result, dict) and "error" in result:
            logger.error("緊急平倉單下發失敗: %s", result.get("error"))
            return False

        logger.info(
            "已掛出緊急平倉單: 方向=%s, 價格=%.8f, 數量=%s",
            hedge_side,
            exit_price,
            format_balance(qty),
        )
        return True

    def _cancel_close_order(self) -> None:
        order_id = self._current_close_order_id
        if not order_id:
            return
        try:
            result = self.client.cancel_order(order_id, self.symbol)
            if isinstance(result, dict) and "error" in result:
                logger.warning("取消平倉單 %s 失敗: %s", order_id, result.get("error"))
            else:
                logger.info("已取消舊的平倉單 %s", order_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("取消平倉單 %s 出錯: %s", order_id, exc)
        finally:
            self._current_close_order_id = None

    def _handle_order_submission_failure(self, side: str, error: Any) -> None:
        logger.warning("方向 %s 掛單失敗，啟動恢復流程: %s", side, error)
        try:
            self.cancel_existing_orders()
        except Exception as exc:  # noqa: BLE001
            logger.error("恢復流程取消訂單失敗: %s", exc)

        self._reset_round_progress()

        position_state: Optional[Dict[str, Any]] = None
        if hasattr(self, "get_position_state"):
            try:
                position_state = self.get_position_state()
            except Exception as exc:  # noqa: BLE001
                logger.warning("恢復流程獲取倉位失敗: %s", exc)

        min_qty = getattr(self, "min_order_size", 0.0)
        net = 0.0
        if position_state:
            net = float(position_state.get("net", 0.0) or 0.0)

        if position_state and abs(net) >= min_qty:
            placed = self._place_emergency_close_order(position_state)
            self._restart_on_flat = placed
            if not placed:
                logger.warning("緊急平倉單未成功掛出，暫停重新循環，請檢查倉位")
        else:
            self._restart_on_flat = False
            # 略微延遲後重新開始一輪，避免立即命中相同價格
            self._start_cycle_async(delay=1.0)

    def _is_post_only_immediate_match_error(self, error_message: Optional[str]) -> bool:
        if not error_message:
            return False
        text = str(error_message).lower()
        if "immediately match" in text:
            return True
        if "post-only" in text or "post only" in text:
            return True
        if "would be taker" in text:
            return True
        return False

    def _adjust_price_for_post_only(self, side: str, price: float) -> float:
        tick = getattr(self, "tick_size", 0.0) or 0.0
        if tick <= 0:
            return price
        normalized_side = (side or "").lower()
        if normalized_side == "bid":
            new_price = price - tick
            if new_price <= 0:
                return price
            return round_to_tick_size(new_price, tick)
        new_price = price + tick
        return round_to_tick_size(new_price, tick)

    def _place_post_only_perp_order(
        self,
        *,
        side: str,
        quantity: float,
        price: float,
        reduce_only: bool,
    ) -> Any:
        """下發 Post-Only 限價單，若因即時成交被拒則自動調整一檔價格再試。"""
        current_price = price
        attempts = 0
        last_result: Any = None

        while attempts <= self._max_post_only_adjustments:
            last_result = self.open_position(
                side=side,
                quantity=quantity,
                price=current_price,
                order_type="Limit",
                reduce_only=reduce_only,
                post_only=True,
            )

            if not (isinstance(last_result, dict) and "error" in last_result):
                if reduce_only:
                    order_id = last_result.get("id")
                    self._current_close_order_id = str(order_id) if order_id else None
                return last_result

            error_text = str(last_result.get("error"))
            if not self._is_post_only_immediate_match_error(error_text):
                return last_result

            adjusted_price = self._adjust_price_for_post_only(side, current_price)
            if adjusted_price == current_price or adjusted_price <= 0:
                logger.error(
                    "Post-only 價格調整失敗 (方向=%s, 原價=%s)，無法繼續遠離當前價格",
                    side,
                    format_balance(current_price),
                )
                return last_result

            attempts += 1
            logger.warning(
                "Post-only 價格 %s 被拒 (立即成交)，嘗試第 %d 檔價格 %s",
                format_balance(current_price),
                attempts,
                format_balance(adjusted_price),
            )
            current_price = adjusted_price

        logger.error(
            "Post-only 價格已調整 %d 次仍無法成功下單 (方向=%s, 最終價格=%s)",
            self._max_post_only_adjustments,
            side,
            format_balance(current_price),
        )
        return last_result

    # ------------------------------------------------------------------
    # 成交后置处理
    # ------------------------------------------------------------------
    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """记录成交，不进行对冲，只更新本轮成交进度。

        僅當買單與賣單「全部成交」後，下一輪掛單才會在 `place_limit_orders` 中啟動。
        """

        super()._after_fill_processed(fill_info)

        side = fill_info.get("side")
        quantity = float(fill_info.get("quantity", 0) or 0)
        price = float(fill_info.get("price", 0) or 0)

        if not side or quantity <= 0 or price <= 0:
            logger.warning("成交信息不完整，跳过处理")
            return

        # 更新當前一輪的成交累計，僅當累計數量達到目標時才視為「完全成交」
        if side == "Bid":
            self._buy_filled_qty += quantity
            logger.info("💰 买单成交: 累计 %.8f / 目标 %.8f @ %.8f", self._buy_filled_qty, self._current_buy_order_qty, price)
            if self._current_buy_order_qty > 0.0 and self._buy_filled_qty + self._fill_tolerance >= self._current_buy_order_qty:
                self._bid_filled = True
                logger.info("✅ 买单已全部成交")
        elif side == "Ask":
            self._sell_filled_qty += quantity
            logger.info("💰 卖单成交: 累计 %.8f / 目标 %.8f @ %.8f", self._sell_filled_qty, self._current_sell_order_qty, price)
            if self._current_sell_order_qty > 0.0 and self._sell_filled_qty + self._fill_tolerance >= self._current_sell_order_qty:
                self._ask_filled = True
                logger.info("✅ 卖单已全部成交")

        # 永续合约纯Maker的加仓/对冲逻辑（基于成交事件触发）
        self._handle_perp_scale_and_hedge(side=side, quantity=quantity, price=price)
        self._maybe_trigger_next_round()

    # ------------------------------------------------------------------
    # 加仓与平仓逻辑（永续合约专用）
    # ------------------------------------------------------------------
    def _maybe_handle_scale_in(self) -> bool:
        """永续合约纯Maker的加仓/平仓逻辑。

        返回 True 表示本轮已处理加仓/平仓且不再执行常规挂单；
        返回 False 表示应继续执行常规挂单逻辑。
        """
        # 未开启加仓功能，直接执行常规挂单
        if getattr(self, "scale_in_price_step_pct", 0.0) <= 0.0 or getattr(self, "scale_in_size_pct", 0.0) <= 0.0:
            return False

        # 仅在永续合约策略中生效（需要有仓位信息和最大持仓限制）
        if not hasattr(self, "get_position_state") or not hasattr(self, "max_position"):
            return False

        try:
            position_state = self.get_position_state()
        except Exception as exc:
            logger.error("获取仓位状态失败，跳过加仓检查: %s", exc)
            return False

        net = float(position_state.get("net", 0.0) or 0.0)
        direction = position_state.get("direction")
        current_price = float(position_state.get("current_price", 0.0) or 0.0)
        avg_entry = float(position_state.get("avg_entry", 0.0) or 0.0)

        # 无有效仓位则退出加仓模式，交给常规挂单处理
        if abs(net) < getattr(self, "min_order_size", 0.0) or not current_price or not avg_entry:
            self._scale_in_last_ref_price = 0.0
            return False

        max_position = float(getattr(self, "max_position", 0.0) or 0.0)
        if max_position <= 0.0:
            # 有仓位但没有有效上限，暂不再挂普通Maker单
            return True

        # 初始化参考价格为当前平均成本
        if self._scale_in_last_ref_price <= 0.0:
            self._scale_in_last_ref_price = avg_entry

        step_ratio = self.scale_in_price_step_pct / 100.0
        should_scale_in = False

        if direction == "LONG":
            trigger_price = self._scale_in_last_ref_price * (1.0 - step_ratio)
            if current_price <= trigger_price:
                should_scale_in = True
        elif direction == "SHORT":
            trigger_price = self._scale_in_last_ref_price * (1.0 + step_ratio)
            if current_price >= trigger_price:
                should_scale_in = True
        else:
            # FLAT 或未知方向，退出加仓模式
            self._scale_in_last_ref_price = 0.0
            return False

        current_size = abs(net)

        # 未触发新一档加仓，但已有仓位 -> 保持加仓模式，不再挂常规Maker单
        if not should_scale_in:
            return True

        # 计算本次加仓数量：在当前仓位基础上增加 scale_in_size_pct%，但不超过 max_position
        target_size = min(
            max_position,
            current_size * (1.0 + self.scale_in_size_pct / 100.0),
        )
        add_qty = max(0.0, target_size - current_size)
        add_qty = round_to_precision(add_qty, self.base_precision)

        if add_qty < self.min_order_size:
            logger.info(
                "加仓目标数量 %s 低于最小下单单位 %s，跳过加仓",
                format_balance(add_qty),
                format_balance(self.min_order_size),
            )
            self._scale_in_last_ref_price = current_price
            return True

        logger.info(
            "触发加仓逻辑: 方向=%s, 当前价=%.8f, 参考价=%.8f, 当前仓位=%s, 计划加仓=%s, 最大仓位=%s",
            direction,
            current_price,
            self._scale_in_last_ref_price,
            format_balance(current_size),
            format_balance(add_qty),
            format_balance(max_position),
        )

        # 1) 取消当前所有挂单
        self.cancel_existing_orders()

        # 2) 在当前盘口附近挂出新的加仓单（Post-Only）
        bid_price, ask_price = self.get_market_depth()
        if bid_price is None or ask_price is None:
            logger.warning("无法获取买一/卖一价格，加仓挂单跳过")
            self._scale_in_last_ref_price = current_price
            return True

        if direction == "LONG":
            entry_side = "Bid"
            entry_price = round_to_tick_size(bid_price, self.tick_size)
            close_side = "long"
        else:
            entry_side = "Ask"
            entry_price = round_to_tick_size(ask_price, self.tick_size)
            close_side = "short"

        entry_result = self._place_post_only_perp_order(
            side=entry_side,
            quantity=add_qty,
            price=entry_price,
            reduce_only=False,
        )
        if isinstance(entry_result, dict) and "error" in entry_result:
            logger.error("加仓下单失败: %s", entry_result.get("error"))
            self._scale_in_last_ref_price = current_price
            return True

        # 3) 预估新的平均成本，并在成本价挂出平仓单
        expected_size = current_size + add_qty
        if expected_size <= 0:
            self._scale_in_last_ref_price = current_price
            return True

        new_avg_price = (avg_entry * current_size + entry_price * add_qty) / expected_size

        # 使用 reduceOnly 限價單，在成本價附近平掉「全部預期倉位」
        close_order_side = "Ask" if close_side == "long" else "Bid"
        exit_price = self._determine_exit_price(position_state, new_avg_price)
        close_result = self._place_post_only_perp_order(
            side=close_order_side,
            quantity=expected_size,
            price=exit_price,
            reduce_only=True,
        )
        if isinstance(close_result, dict) and "error" in close_result:
            logger.warning("平仓挂单失败: %s", close_result.get("error"))
        else:
            logger.info(
                "已在成本价挂出平仓单: 方向=%s, 价格=%.8f, 数量=%s",
                close_side,
                exit_price,
                format_balance(expected_size),
            )

        # 更新下一档加仓的参考价格
        self._scale_in_last_ref_price = current_price

        # 进入加仓模式后，本轮不再执行普通Maker挂单
        return True

    def _handle_perp_scale_and_hedge(self, side: str, quantity: float, price: float) -> None:
        """永续合约纯Maker的加仓/对冲逻辑（以成交事件为驱动）。"""
        # 若未配置加仓参数，或不是永续合约环境，则直接返回
        if getattr(self, "scale_in_price_step_pct", 0.0) <= 0.0 or getattr(self, "scale_in_size_pct", 0.0) <= 0.0:
            return
        if not hasattr(self, "get_position_state") or not hasattr(self, "max_position"):
            return

        try:
            position_state = self.get_position_state()
        except Exception as exc:
            logger.error("更新加仓/对冲时获取仓位失败: %s", exc)
            return

        net = float(position_state.get("net", 0.0) or 0.0)
        direction = position_state.get("direction")
        avg_entry = float(position_state.get("avg_entry", 0.0) or 0.0)
        current_price = float(position_state.get("current_price", 0.0) or 0.0)

        min_qty = getattr(self, "min_order_size", 0.0)
        prev_net = getattr(self, "_scale_in_last_net", 0.0)

        # 將極小倉位視為 0，避免噪音
        if abs(net) < min_qty / 10:
            net = 0.0

        # 情況 1: 倉位從非 0 回到 0 -> 認為本輪結束，取消加倉單並開啟下一輪
        if net == 0.0 and abs(prev_net) >= min_qty:
            logger.info("倉位已歸零，取消所有加倉/對沖掛單，準備進入下一輪純Maker循環")
            self._scale_in_last_ref_price = 0.0
            self._scale_in_last_net = 0.0
            try:
                self.cancel_existing_orders()
            except Exception as exc:
                logger.error("取消剩余掛單失敗: %s", exc)
            self._reset_round_progress()
            if self._restart_on_flat:
                self._restart_on_flat = False
                self._start_cycle_async(delay=0.0)
            else:
                self._schedule_next_round()
            return

        # 更新記錄的上一筆倉位
        self._scale_in_last_net = net

        # 沒有有效持倉或缺少成本價/當前價信息時，不進行加倉/對沖處理
        if net == 0.0 or not avg_entry or not current_price or direction not in ("LONG", "SHORT"):
            return

        current_size = abs(net)
        step_ratio = self.scale_in_price_step_pct / 100.0
        max_position = float(getattr(self, "max_position", 0.0) or 0.0)
        if max_position <= 0.0:
            return

        # 情況 2: 首筆建倉完成（上一筆為 0，當前有持倉） -> 掛出第一筆加倉單
        if abs(prev_net) < min_qty and current_size >= min_qty:
            logger.info("檢測到首筆建倉完成，準備掛出第一檔加倉單")
            self._scale_in_last_ref_price = avg_entry
            if not self._scale_ladder_deployed:
                deployed = self._place_scale_in_ladder(
                    direction=direction,
                    base_price=avg_entry,
                    current_size=current_size,
                    max_position=max_position,
                    step_ratio=step_ratio,
                )
                if deployed:
                    self._scale_ladder_deployed = True
            return

        # 情況 3: 同方向持倉增加，視為加倉成交 -> 取消舊對沖單，按新成本價重掛對沖
        if prev_net != 0.0 and (net > 0) == (prev_net > 0) and current_size > abs(prev_net) + min_qty / 10:
            logger.info(
                "檢測到加倉成交: 舊倉位=%s, 新倉位=%s",
                format_balance(prev_net),
                format_balance(net),
            )

            self._cancel_close_order()

            hedge_side = "Ask" if direction == "LONG" else "Bid"
            exit_price = self._determine_exit_price(position_state, avg_entry)
            hedge_price = round_to_tick_size(exit_price, self.tick_size)
            hedge_qty = round_to_precision(current_size, self.base_precision)

            if hedge_qty >= min_qty:
                logger.info(
                    "以成本價掛出新的對沖單: 方向=%s, 價格=%.8f, 數量=%s",
                    hedge_side,
                    hedge_price,
                    format_balance(hedge_qty),
                )
                self._place_post_only_perp_order(
                    side=hedge_side,
                    quantity=hedge_qty,
                    price=hedge_price,
                    reduce_only=True,
                )

            self._scale_in_last_ref_price = avg_entry

    def _place_scale_in_ladder(
        self,
        direction: str,
        base_price: float,
        current_size: float,
        max_position: float,
        step_ratio: float,
    ) -> bool:
        """根據當前持倉與配置一次性掛出剩餘所有加倉單。

        返回 True 表示至少成功掛出一筆加倉單。
        """
        min_qty = getattr(self, "min_order_size", 0.0)
        if max_position <= 0.0 or current_size >= max_position - min_qty / 2:
            logger.info("持倉已接近或達到最大上限，無需額外加倉單")
            return False

        if self.scale_in_size_pct <= 0.0 or step_ratio <= 0.0:
            logger.info("未設定有效的加倉步長/比例，跳過加倉梯度")
            return False

        price_step = abs(base_price) * step_ratio
        if price_step <= 0:
            logger.info("加倉價格步長無效，跳過加倉梯度")
            return False

        # 初始化
        remaining_size = current_size
        level = 0
        orders_placed = 0

        while remaining_size + min_qty <= max_position:
            target_size = min(
                max_position,
                remaining_size * (1.0 + self.scale_in_size_pct / 100.0),
            )
            add_qty = max(0.0, target_size - remaining_size)
            add_qty = round_to_precision(add_qty, self.base_precision)

            if add_qty < min_qty:
                logger.info(
                    "加倉梯度剩餘數量 %s 低於最小下單單位 %s，停止掛單",
                    format_balance(add_qty),
                    format_balance(min_qty),
                )
                break

            level += 1
            if direction == "LONG":
                price = base_price - price_step * level
                side = "Bid"
            else:
                price = base_price + price_step * level
                side = "Ask"

            price = round_to_tick_size(price, self.tick_size)
            if price <= 0:
                logger.warning("加倉價計算結果<=0（方向=%s），停止掛單", direction)
                break

            logger.info(
                "掛出加倉梯度 #%d: 方向=%s, 價格=%.8f, 數量=%s",
                level,
                side,
                price,
                format_balance(add_qty),
            )

            result = self._place_post_only_perp_order(
                side=side,
                quantity=add_qty,
                price=price,
                reduce_only=False,
            )
            if isinstance(result, dict) and "error" in result:
                logger.error("加倉梯度 #%d 下單失敗: %s", level, result.get("error"))
                break

            orders_placed += 1
            remaining_size = target_size

            # 若已達到最大倉位上限則停止
            if remaining_size >= max_position - min_qty / 2:
                break

        if orders_placed == 0:
            logger.info("未能掛出任何加倉梯度（方向=%s）", direction)
            return False

        logger.info("已掛出 %d 筆加倉梯度訂單", orders_placed)
        return True

    def run(self, duration_seconds: int = 3600, interval_seconds: int = 60):  # type: ignore[override]
        """純 Maker-Maker 策略運行入口（事件驅動，不使用 interval 輪詢）。

        - 初始在買一/賣一掛出對稱 Maker 單
        - 後續完整循環由成交事件驅動（參見 `_after_fill_processed` / `_handle_perp_scale_and_hedge`）
        - 不再在每次迭代中主動調用 `place_limit_orders`，也不輸出「等待 X 秒」日誌
        """
        logger.info(f"開始運行純 Maker-Maker 策略: {self.symbol}")
        logger.info(f"運行時間上限: {duration_seconds} 秒 (事件驅動模式, interval 參數將被忽略)")

        start_time = time.time()

        try:
            # 確保連接與數據流
            connection_status = self.check_ws_connection()
            if connection_status and getattr(self, "ws", None) is not None:
                try:
                    # 父類中已有的輔助方法，確保訂閲深度/行情/訂單更新流
                    self._ensure_data_streams()  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    logger.warning("初始化數據流時出錯: %s", exc)

            # 首次種子掛單：在買一/賣一掛出純 Maker 單
            self.place_limit_orders()

            # 事件驅動主循環：僅保持進程存活與適度打印統計，不做主動輪詢下單
            report_interval = 300  # 每 5 分鐘打印一次簡要統計
            last_report_time = start_time

            while time.time() - start_time < duration_seconds and not getattr(self, "_stop_flag", False):
                now_ts = time.time()

                # 定期打印統計，但不干預交易邏輯
                if now_ts - last_report_time >= report_interval:
                    try:
                        pnl_data = self.calculate_pnl()
                        self.estimate_profit(pnl_data)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("打印統計時出錯: %s", exc)
                    last_report_time = now_ts

                # 輕量級 sleep，避免 CPU 忙等，不進行額外網絡請求
                time.sleep(1)

            logger.info("\n=== 純 Maker-Maker 策略運行結束 ===")
            try:
                self.print_trading_stats()
            except Exception as exc:  # noqa: BLE001
                logger.error("打印最終交易統計時出錯: %s", exc)

        except KeyboardInterrupt:
            logger.info("\n用戶中斷，停止純 Maker-Maker 策略")
            try:
                self.print_trading_stats()
            except Exception as exc:  # noqa: BLE001
                logger.error("打印中斷時交易統計時出錯: %s", exc)

    # ------------------------------------------------------------------
    # 节流与工具
    # ------------------------------------------------------------------
    def _respect_request_interval(self, slot: str) -> None:
        interval = self._request_intervals.get(slot)
        if not interval:
            return
        last_ts = self._last_request_ts.get(slot, 0.0)
        now = time.monotonic()
        wait_for = interval - (now - last_ts)
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_ts[slot] = time.monotonic()

    def _submit_order(self, order: Dict[str, Any], slot: str) -> Any:
        self._respect_request_interval(slot)
        return self.client.execute_order(order)

    def _build_limit_order(self, side: str, price: float, quantity: float) -> Dict[str, str]:
        """依交易所特性构建限价订单负载。"""

        order = {
            "orderType": "Limit",
            "price": str(round_to_tick_size(price, self.tick_size)),
            "quantity": str(round_to_precision(quantity, self.base_precision)),
            "side": side,
            "symbol": self.symbol,
            "timeInForce": "GTC",
        }

        if getattr(self, "exchange", "backpack") == "backpack":
            order["postOnly"] = True
            order["autoLendRedeem"] = True
            order["autoLend"] = True

        return order


class _SpotPureMakerStrategy(_PureMakerMixin, MarketMaker):
    """现货纯 Maker-Maker 策略实现。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        base_spread_percentage: float = 0.0,
        order_quantity: Optional[float] = None,
        exchange: str = "backpack",
        exchange_config: Optional[Dict[str, Any]] = None,
        close_price_mode: str = "entry",
        next_round_delay_seconds: float = 3.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            base_spread_percentage=base_spread_percentage,
            order_quantity=order_quantity,
            exchange=exchange,
            exchange_config=exchange_config,
            strategy_label="现货纯Maker",
            close_price_mode=close_price_mode,
            next_round_delay_seconds=next_round_delay_seconds,
            **kwargs,
        )


class _PerpPureMakerStrategy(_PureMakerMixin, PerpetualMarketMaker):
    """永续合约纯 Maker-Maker 策略实现。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        base_spread_percentage: float = 0.0,
        order_quantity: Optional[float] = None,
        target_position: float = 0.0,
        max_position: float = 1.0,
        position_threshold: float = 0.1,
        inventory_skew: float = 0.0,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        exchange: str = "backpack",
        exchange_config: Optional[Dict[str, Any]] = None,
        scale_in_price_step_pct: float = 0.0,
        scale_in_size_pct: float = 0.0,
        close_price_mode: str = "entry",
        next_round_delay_seconds: float = 3.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            base_spread_percentage=base_spread_percentage,
            order_quantity=order_quantity,
            target_position=target_position,
            max_position=max_position,
            position_threshold=position_threshold,
            inventory_skew=inventory_skew,
            stop_loss=stop_loss,
            take_profit=take_profit,
            exchange=exchange,
            exchange_config=exchange_config,
            strategy_label="永续纯Maker",
            scale_in_price_step_pct=scale_in_price_step_pct,
            scale_in_size_pct=scale_in_size_pct,
            close_price_mode=close_price_mode,
            next_round_delay_seconds=next_round_delay_seconds,
            **kwargs,
        )


class PureMakerStrategy:
    """根据市场类型返回对应的纯 Maker-Maker 策略实例。"""

    def __new__(cls, *args: Any, market_type: str = "spot", **kwargs: Any):
        market = (market_type or "spot").lower()
        if market == "perp":
            return _PerpPureMakerStrategy(*args, **kwargs)
        return _SpotPureMakerStrategy(*args, **kwargs)
