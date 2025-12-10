"""纯 Maker-Maker 策略：仅挂买一/卖一，双向成交后继续循环。"""
from __future__ import annotations

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

        # 若永续合约版本启用了加仓逻辑且当前有仓位，加仓/平仓逻辑将接管本轮挂单
        if self._maybe_handle_scale_in():
            return

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

        # 利潤估算暫保持簡化處理，可根據實際需求再精細化
        if self._bid_filled and self._ask_filled:
            # 等待下一轮掛單時在 place_limit_orders 中統計輪次與利潤
            pass

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

        entry_result = self.open_position(
            side=entry_side,
            quantity=add_qty,
            price=entry_price,
            order_type="Limit",
            reduce_only=False,
            post_only=True,
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
        close_result = self.open_position(
            side=close_order_side,
            quantity=expected_size,
            price=new_avg_price,
            order_type="Limit",
            post_only=True,
        )
        if isinstance(close_result, dict) and "error" in close_result:
            logger.warning("平仓挂单失败: %s", close_result.get("error"))
        else:
            logger.info(
                "已在成本价挂出平仓单: 方向=%s, 价格=%.8f, 数量=%s",
                close_side,
                new_avg_price,
                format_balance(expected_size),
            )

        # 更新下一档加仓的参考价格
        self._scale_in_last_ref_price = current_price

        # 进入加仓模式后，本轮不再执行普通Maker挂单
        return True

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
            **kwargs,
        )


class PureMakerStrategy:
    """根据市场类型返回对应的纯 Maker-Maker 策略实例。"""

    def __new__(cls, *args: Any, market_type: str = "spot", **kwargs: Any):
        market = (market_type or "spot").lower()
        if market == "perp":
            return _PerpPureMakerStrategy(*args, **kwargs)
        return _SpotPureMakerStrategy(*args, **kwargs)
