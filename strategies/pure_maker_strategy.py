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

    def __init__(self, *args: Any, strategy_label: str = "纯Maker", **kwargs: Any) -> None:
        # 强制单层订单，禁用重平衡
        kwargs.pop("max_orders", None)
        kwargs.pop("enable_rebalance", None)
        kwargs.pop("base_asset_target_percentage", None)
        kwargs.pop("rebalance_threshold", None)

        kwargs["max_orders"] = 1
        kwargs["enable_rebalance"] = False

        self._strategy_label = strategy_label
        
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
            **kwargs,
        )


class PureMakerStrategy:
    """根据市场类型返回对应的纯 Maker-Maker 策略实例。"""

    def __new__(cls, *args: Any, market_type: str = "spot", **kwargs: Any):
        market = (market_type or "spot").lower()
        if market == "perp":
            return _PerpPureMakerStrategy(*args, **kwargs)
        return _SpotPureMakerStrategy(*args, **kwargs)
