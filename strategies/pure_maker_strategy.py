"""纯 Maker-Maker 刷交易量策略

逻辑流程：
1. 在买一/卖一挂 Post-Only 订单（订单A和订单B）
2. 当订单A成交后，更新反向订单B的价格为当前仓位的 breakEvenPrice
3. 挂加仓订单，由 scale_in_price_step_pct 和 scale_in_size_pct 控制，最大不超过 max_position
4. 每当加仓订单成交后，更新反向订单B的价格为当前仓位的 breakEvenPrice
5. 当反向订单B成交后，仓位归0，等待3秒，进入下一轮
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from logger import setup_logger
from strategies.perp_market_maker import PerpetualMarketMaker
from strategies.market_maker import format_balance
from utils.helpers import round_to_precision, round_to_tick_size

logger = setup_logger("pure_maker_strategy")


class OrderRole(Enum):
    """订单角色"""
    ENTRY_BID = "entry_bid"      # 入场买单（订单A，多方向）
    ENTRY_ASK = "entry_ask"      # 入场卖单（订单A，空方向）
    HEDGE = "hedge"              # 对冲/平仓单（订单B）
    SCALE_IN = "scale_in"        # 加仓单


@dataclass
class TrackedOrder:
    """追踪的订单信息"""
    order_id: str
    role: OrderRole
    side: str           # "Bid" 或 "Ask"
    price: float
    quantity: float
    filled_qty: float = 0.0
    is_active: bool = True
    
    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.quantity - self.filled_qty)
    
    @property
    def is_fully_filled(self) -> bool:
        return self.filled_qty >= self.quantity - 1e-10


@dataclass
class RoundState:
    """一轮交易的状态"""
    round_id: int = 0
    entry_order: Optional[TrackedOrder] = None     # 入场订单A
    hedge_order: Optional[TrackedOrder] = None     # 对冲订单B
    scale_in_orders: List[TrackedOrder] = field(default_factory=list)  # 加仓订单列表
    position_direction: Optional[str] = None       # "LONG" 或 "SHORT"
    is_completed: bool = False


class PureMakerStrategy(PerpetualMarketMaker):
    """纯 Maker-Maker 刷交易量策略
    
    继承自 PerpetualMarketMaker，复用其仓位管理和订单执行能力。
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        order_quantity: Optional[float] = None,
        max_position: float = 1.0,
        scale_in_price_step_pct: float = 1.0,
        scale_in_size_pct: float = 50.0,
        next_round_delay_seconds: float = 3.0,
        exchange: str = "backpack",
        exchange_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """
        初始化策略
        
        Args:
            order_quantity: 每轮入场订单的数量
            max_position: 最大持仓量
            scale_in_price_step_pct: 加仓价格步长百分比（如 1.0 表示每下跌/上涨 1% 加一次仓）
            scale_in_size_pct: 加仓数量百分比（如 50.0 表示每次加仓数量为当前仓位的 50%）
            next_round_delay_seconds: 一轮结束后等待多少秒开始下一轮
        """
        # 禁用父类的重平衡和库存偏移
        kwargs["enable_rebalance"] = False
        kwargs["inventory_skew"] = 0.0
        kwargs["target_position"] = 0.0
        kwargs["base_spread_percentage"] = 0.0
        
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            max_position=max_position,
            exchange=exchange,
            exchange_config=exchange_config,
            order_quantity=order_quantity,
            **kwargs,
        )
        
        # 策略参数
        self.order_quantity = order_quantity
        self.scale_in_price_step_pct = max(0.0, float(scale_in_price_step_pct))
        self.scale_in_size_pct = max(0.0, float(scale_in_size_pct))
        self.next_round_delay = max(0.0, float(next_round_delay_seconds))
        
        # 状态追踪
        self._round_state = RoundState()
        self._round_count = 0
        self._total_volume = 0.0
        
        # 线程安全锁
        self._state_lock = threading.RLock()
        self._order_lock = threading.Lock()
        
        # 成交事件去重
        self._processed_fill_ids: Set[str] = set()
        self._recent_fill_ids: deque = deque(maxlen=1000)
        
        # 订单追踪表 order_id -> TrackedOrder
        self._tracked_orders: Dict[str, TrackedOrder] = {}
        
        # 控制标志
        self._stop_flag = False
        self._next_round_scheduled = False
        self._next_round_lock = threading.Lock()
        
        logger.info("=" * 60)
        logger.info("初始化纯 Maker-Maker 刷量策略")
        logger.info("  交易对: %s", symbol)
        logger.info("  单笔数量: %s", format_balance(order_quantity) if order_quantity else "自动计算")
        logger.info("  最大仓位: %s", format_balance(max_position))
        logger.info("  加仓价格步长: %.2f%%", scale_in_price_step_pct)
        logger.info("  加仓数量比例: %.2f%%", scale_in_size_pct)
        logger.info("  轮次间隔: %.1f 秒", next_round_delay_seconds)
        logger.info("=" * 60)

    # ============================================================
    # 核心流程控制
    # ============================================================
    
    def _start_new_round(self) -> None:
        """开始新一轮交易"""
        with self._state_lock:
            self._round_count += 1
            self._round_state = RoundState(round_id=self._round_count)
            self._tracked_orders.clear()
        
        logger.info("")
        logger.info("=" * 50)
        logger.info("🚀 开始第 %d 轮交易", self._round_count)
        logger.info("=" * 50)
        
        # 获取买一/卖一价格
        bid_price, ask_price = self.get_market_depth()
        if bid_price is None or ask_price is None:
            logger.error("❌ 无法获取买一/卖一价格，跳过本轮")
            self._schedule_next_round()
            return
        
        logger.info("📊 当前盘口: 买一 %.8f | 卖一 %.8f | 价差 %.4f%%", 
                    bid_price, ask_price, (ask_price - bid_price) / bid_price * 100)
        
        # 计算订单数量
        qty = self._calculate_order_quantity(bid_price)
        if qty is None or qty < self.min_order_size:
            logger.error("❌ 订单数量计算失败或过小，跳过本轮")
            self._schedule_next_round()
            return
        
        # 在买一和卖一挂单
        buy_price = round_to_tick_size(bid_price, self.tick_size)
        sell_price = round_to_tick_size(ask_price, self.tick_size)
        
        # 确保价差足够
        if sell_price <= buy_price:
            sell_price = round_to_tick_size(buy_price + self.tick_size, self.tick_size)
        
        logger.info("📝 准备挂单: 买单 %.8f x %s | 卖单 %.8f x %s",
                    buy_price, format_balance(qty), sell_price, format_balance(qty))
        
        # 挂买单（入场单A - 可能形成多头）
        buy_order = self._place_post_only_order(
            side="Bid",
            price=buy_price,
            quantity=qty,
            role=OrderRole.ENTRY_BID,
        )
        if not buy_order:
            logger.error("❌ 买单挂单失败，取消本轮")
            self._cancel_all_tracked_orders()
            self._schedule_next_round()
            return
        
        # 挂卖单（入场单A - 可能形成空头）
        sell_order = self._place_post_only_order(
            side="Ask",
            price=sell_price,
            quantity=qty,
            role=OrderRole.ENTRY_ASK,
        )
        if not sell_order:
            logger.error("❌ 卖单挂单失败，取消本轮")
            self._cancel_all_tracked_orders()
            self._schedule_next_round()
            return
        
        logger.info("✅ 第 %d 轮挂单完成，等待成交...", self._round_count)

    def _schedule_next_round(self) -> None:
        """调度下一轮交易"""
        if self._stop_flag:
            return
        
        with self._next_round_lock:
            if self._next_round_scheduled:
                logger.debug("下一轮已在调度中，跳过")
                return
            self._next_round_scheduled = True
        
        def _delayed_start():
            try:
                if self.next_round_delay > 0:
                    logger.info("⏳ 等待 %.1f 秒后开始下一轮...", self.next_round_delay)
                    time.sleep(self.next_round_delay)
                
                if not self._stop_flag:
                    self._start_new_round()
            except Exception as e:
                logger.error("启动下一轮时出错: %s", e)
            finally:
                with self._next_round_lock:
                    self._next_round_scheduled = False
        
        threading.Thread(target=_delayed_start, daemon=True).start()

    # ============================================================
    # 订单管理
    # ============================================================
    
    def _place_post_only_order(
        self,
        side: str,
        price: float,
        quantity: float,
        role: OrderRole,
        reduce_only: bool = False,
        max_retries: int = 10,
    ) -> Optional[TrackedOrder]:
        """下 Post-Only 限价单，自动处理价格调整"""
        
        current_price = price
        
        for attempt in range(max_retries):
            with self._order_lock:
                result = self.open_position(
                    side=side,
                    quantity=quantity,
                    price=current_price,
                    order_type="Limit",
                    reduce_only=reduce_only,
                    post_only=True,
                )
            
            if isinstance(result, dict) and "error" in result:
                error_msg = str(result.get("error", "")).lower()
                
                # 检查是否是 Post-Only 立即成交的错误
                if "immediately match" in error_msg or "post-only" in error_msg or "would be taker" in error_msg:
                    # 调整价格远离盘口
                    if side == "Bid":
                        current_price = round_to_tick_size(current_price - self.tick_size, self.tick_size)
                    else:
                        current_price = round_to_tick_size(current_price + self.tick_size, self.tick_size)
                    
                    if current_price <= 0:
                        logger.error("价格调整后<=0，无法下单")
                        return None
                    
                    logger.warning("Post-Only 被拒（第 %d 次），调整价格至 %.8f", attempt + 1, current_price)
                    continue
                else:
                    logger.error("下单失败: %s", result.get("error"))
                    return None
            
            # 成功下单
            order_id = result.get("id")
            if not order_id:
                logger.error("下单成功但未返回订单ID")
                return None
            
            tracked = TrackedOrder(
                order_id=str(order_id),
                role=role,
                side=side,
                price=current_price,
                quantity=quantity,
            )
            
            with self._state_lock:
                self._tracked_orders[tracked.order_id] = tracked
            
            role_name = {
                OrderRole.ENTRY_BID: "入场买单",
                OrderRole.ENTRY_ASK: "入场卖单",
                OrderRole.HEDGE: "对冲单",
                OrderRole.SCALE_IN: "加仓单",
            }.get(role, str(role))
            
            logger.info("📤 %s已挂出: ID=%s, 方向=%s, 价格=%.8f, 数量=%s",
                        role_name, order_id, side, current_price, format_balance(quantity))
            
            return tracked
        
        logger.error("达到最大重试次数，无法下单")
        return None

    def _update_hedge_order_price(self, new_price: float) -> bool:
        """更新对冲单的价格（取消旧单+下新单）"""
        with self._state_lock:
            hedge_order = self._round_state.hedge_order
            if not hedge_order or not hedge_order.is_active:
                logger.warning("没有活跃的对冲单需要更新")
                return False
            
            old_price = hedge_order.price
            old_id = hedge_order.order_id
            side = hedge_order.side
            quantity = hedge_order.remaining_qty
            
            if abs(new_price - old_price) < self.tick_size / 2:
                logger.debug("新价格与旧价格相同，跳过更新")
                return True
        
        logger.info("📝 更新对冲单价格: %.8f → %.8f", old_price, new_price)
        
        # 1. 取消旧订单
        self._cancel_order_by_id(old_id)
        
        # 2. 下新订单
        new_order = self._place_post_only_order(
            side=side,
            price=new_price,
            quantity=quantity,
            role=OrderRole.HEDGE,
            reduce_only=True,
        )
        
        if new_order:
            with self._state_lock:
                self._round_state.hedge_order = new_order
            logger.info("✅ 对冲单价格已更新: 新ID=%s, 新价格=%.8f", new_order.order_id, new_price)
            return True
        else:
            logger.error("❌ 更新对冲单价格失败")
            return False

    def _cancel_order_by_id(self, order_id: str) -> bool:
        """取消指定订单"""
        try:
            result = self.client.cancel_order(order_id, self.symbol)
            if isinstance(result, dict) and "error" in result:
                error_msg = str(result.get("error", "")).lower()
                if "not found" in error_msg or "does not exist" in error_msg:
                    logger.debug("订单 %s 已不存在（可能已成交）", order_id)
                    return True
                logger.warning("取消订单 %s 失败: %s", order_id, result.get("error"))
                return False
            
            logger.info("🗑️ 已取消订单: %s", order_id)
            
            with self._state_lock:
                if order_id in self._tracked_orders:
                    self._tracked_orders[order_id].is_active = False
            
            return True
        except Exception as e:
            logger.error("取消订单 %s 时出错: %s", order_id, e)
            return False

    def _cancel_all_tracked_orders(self) -> None:
        """取消所有追踪的订单"""
        with self._state_lock:
            order_ids = list(self._tracked_orders.keys())
        
        for order_id in order_ids:
            self._cancel_order_by_id(order_id)

    def _cancel_entry_orders_except(self, keep_side: Optional[str] = None) -> None:
        """取消入场订单（保留指定方向的）"""
        with self._state_lock:
            orders_to_cancel = []
            for order in self._tracked_orders.values():
                if order.role in (OrderRole.ENTRY_BID, OrderRole.ENTRY_ASK):
                    if keep_side and order.side == keep_side:
                        continue
                    if order.is_active:
                        orders_to_cancel.append(order.order_id)
        
        for order_id in orders_to_cancel:
            self._cancel_order_by_id(order_id)

    # ============================================================
    # 加仓逻辑
    # ============================================================
    
    def _place_scale_in_orders(self, direction: str, entry_price: float, current_position: float) -> None:
        """挂加仓订单梯队"""
        if self.scale_in_price_step_pct <= 0 or self.scale_in_size_pct <= 0:
            logger.debug("未配置加仓参数，跳过加仓单")
            return
        
        if current_position >= self.max_position - self.min_order_size / 2:
            logger.info("当前仓位已达最大限制，无需加仓单")
            return
        
        price_step_ratio = self.scale_in_price_step_pct / 100.0
        size_ratio = self.scale_in_size_pct / 100.0
        
        remaining_capacity = self.max_position - current_position
        current_size = current_position
        level = 0
        base_price = entry_price
        
        scale_in_orders = []
        
        while current_size < self.max_position - self.min_order_size / 2:
            level += 1
            
            # 计算加仓价格
            if direction == "LONG":
                # 多头加仓：价格下跌时加仓
                scale_price = base_price * (1.0 - price_step_ratio * level)
                scale_side = "Bid"
            else:
                # 空头加仓：价格上涨时加仓
                scale_price = base_price * (1.0 + price_step_ratio * level)
                scale_side = "Ask"
            
            scale_price = round_to_tick_size(scale_price, self.tick_size)
            if scale_price <= 0:
                break
            
            # 计算加仓数量
            add_qty = current_size * size_ratio
            add_qty = min(add_qty, remaining_capacity)
            add_qty = round_to_precision(add_qty, self.base_precision)
            
            if add_qty < self.min_order_size:
                logger.debug("加仓数量 %s 低于最小单位，停止加仓", format_balance(add_qty))
                break
            
            # 下加仓单
            order = self._place_post_only_order(
                side=scale_side,
                price=scale_price,
                quantity=add_qty,
                role=OrderRole.SCALE_IN,
                reduce_only=False,
            )
            
            if order:
                scale_in_orders.append(order)
                current_size += add_qty
                remaining_capacity -= add_qty
                logger.info("📈 加仓单 #%d: 价格=%.8f, 数量=%s", level, scale_price, format_balance(add_qty))
            else:
                logger.warning("加仓单 #%d 挂单失败", level)
                break
            
            # 防止无限循环
            if level >= 20:
                break
        
        with self._state_lock:
            self._round_state.scale_in_orders = scale_in_orders
        
        if scale_in_orders:
            logger.info("✅ 已挂出 %d 个加仓单", len(scale_in_orders))

    # ============================================================
    # 成交事件处理
    # ============================================================
    
    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """处理成交事件（覆盖父类方法）"""
        super()._after_fill_processed(fill_info)
        
        # 去重检查
        fill_id = fill_info.get("fill_id") or fill_info.get("trade_id") or fill_info.get("tradeId")
        if fill_id:
            fill_id_str = str(fill_id)
            if fill_id_str in self._processed_fill_ids:
                logger.debug("跳过重复的成交事件: %s", fill_id_str)
                return
            
            self._processed_fill_ids.add(fill_id_str)
            self._recent_fill_ids.append(fill_id_str)
            
            # 清理旧的记录
            if len(self._processed_fill_ids) > 2000:
                while len(self._recent_fill_ids) > 500:
                    old_id = self._recent_fill_ids.popleft()
                    self._processed_fill_ids.discard(old_id)
        
        order_id = str(fill_info.get("order_id", ""))
        side = fill_info.get("side")
        quantity = float(fill_info.get("quantity", 0) or 0)
        price = float(fill_info.get("price", 0) or 0)
        
        if not order_id or not side or quantity <= 0:
            logger.warning("成交信息不完整: %s", fill_info)
            return
        
        logger.info("💰 收到成交通知: 订单=%s, 方向=%s, 价格=%.8f, 数量=%s",
                    order_id, side, price, format_balance(quantity))
        
        # 更新追踪的订单状态
        with self._state_lock:
            tracked = self._tracked_orders.get(order_id)
            if tracked:
                tracked.filled_qty += quantity
                logger.info("   └─ 订单角色=%s, 已成交=%s/%s", 
                            tracked.role.value, 
                            format_balance(tracked.filled_qty),
                            format_balance(tracked.quantity))
        
        # 更新成交量统计
        self._total_volume += quantity * price
        
        # 获取当前仓位状态
        position_state = self.get_position_state()
        net = float(position_state.get("net", 0.0) or 0.0)
        direction = position_state.get("direction")
        break_even_price = float(position_state.get("break_even_price", 0.0) or 0.0)
        avg_entry = float(position_state.get("avg_entry", 0.0) or 0.0)
        
        logger.info("   └─ 当前仓位: net=%.8f, 方向=%s, 均价=%.8f, 盈亏平衡价=%.8f",
                    net, direction, avg_entry, break_even_price)
        
        # 处理成交逻辑
        self._handle_fill_logic(order_id, side, quantity, price, net, direction, break_even_price, avg_entry)

    def _handle_fill_logic(
        self,
        order_id: str,
        side: str,
        quantity: float,
        price: float,
        net: float,
        direction: str,
        break_even_price: float,
        avg_entry: float,
    ) -> None:
        """处理成交后的逻辑"""
        with self._state_lock:
            tracked = self._tracked_orders.get(order_id)
            if not tracked:
                logger.debug("未追踪的订单成交: %s", order_id)
                return
            
            role = tracked.role
            round_state = self._round_state
        
        # 情况1: 入场单成交
        if role in (OrderRole.ENTRY_BID, OrderRole.ENTRY_ASK):
            self._on_entry_order_filled(tracked, net, direction, break_even_price, avg_entry)
        
        # 情况2: 加仓单成交
        elif role == OrderRole.SCALE_IN:
            self._on_scale_in_order_filled(tracked, net, direction, break_even_price)
        
        # 情况3: 对冲单成交
        elif role == OrderRole.HEDGE:
            self._on_hedge_order_filled(tracked, net)

    def _on_entry_order_filled(
        self, 
        order: TrackedOrder, 
        net: float, 
        direction: str,
        break_even_price: float,
        avg_entry: float,
    ) -> None:
        """入场单成交处理"""
        logger.info("🎯 入场单成交！方向=%s, 仓位=%s", direction, format_balance(net))
        
        with self._state_lock:
            # 记录入场订单
            self._round_state.entry_order = order
            self._round_state.position_direction = direction
        
        # 取消另一侧的入场单
        if order.side == "Bid":
            self._cancel_entry_orders_except(keep_side="Bid")
        else:
            self._cancel_entry_orders_except(keep_side="Ask")
        
        # 计算对冲价格（使用 breakEvenPrice）
        hedge_price = break_even_price if break_even_price > 0 else avg_entry
        if hedge_price <= 0:
            hedge_price = order.price  # 回退到入场价格
        
        hedge_price = round_to_tick_size(hedge_price, self.tick_size)
        
        # 确定对冲方向
        current_size = abs(net)
        if direction == "LONG":
            hedge_side = "Ask"  # 多头需要卖出平仓
        else:
            hedge_side = "Bid"  # 空头需要买入平仓
        
        logger.info("📤 准备挂对冲单: 方向=%s, 价格=%.8f, 数量=%s", 
                    hedge_side, hedge_price, format_balance(current_size))
        
        # 挂对冲单
        hedge_order = self._place_post_only_order(
            side=hedge_side,
            price=hedge_price,
            quantity=current_size,
            role=OrderRole.HEDGE,
            reduce_only=True,
        )
        
        if hedge_order:
            with self._state_lock:
                self._round_state.hedge_order = hedge_order
            logger.info("✅ 对冲单已挂出")
        else:
            logger.error("❌ 对冲单挂单失败")
        
        # 挂加仓订单
        self._place_scale_in_orders(direction, avg_entry if avg_entry > 0 else order.price, current_size)

    def _on_scale_in_order_filled(
        self,
        order: TrackedOrder,
        net: float,
        direction: str,
        break_even_price: float,
    ) -> None:
        """加仓单成交处理"""
        logger.info("📈 加仓单成交！当前仓位=%s", format_balance(net))
        
        # 更新对冲单价格为新的 breakEvenPrice
        if break_even_price > 0:
            new_price = round_to_tick_size(break_even_price, self.tick_size)
            
            # 同时更新对冲单的数量为当前仓位
            current_size = abs(net)
            with self._state_lock:
                if self._round_state.hedge_order:
                    self._round_state.hedge_order.quantity = current_size
            
            logger.info("📝 加仓后更新对冲单: 新价格=%.8f, 新数量=%s", new_price, format_balance(current_size))
            self._update_hedge_order_price(new_price)
        else:
            logger.warning("无法获取 breakEvenPrice，跳过对冲单价格更新")

    def _on_hedge_order_filled(self, order: TrackedOrder, net: float) -> None:
        """对冲单成交处理"""
        logger.info("🏁 对冲单成交！")
        
        # 检查仓位是否归零
        tolerance = self.min_order_size / 10
        if abs(net) <= tolerance:
            logger.info("✅ 仓位已归零！第 %d 轮完成", self._round_count)
            logger.info("📊 累计刷量: %.2f %s", self._total_volume, self.quote_asset)
            
            with self._state_lock:
                self._round_state.is_completed = True
            
            # 取消所有剩余订单（如加仓单）
            self._cancel_remaining_scale_in_orders()
            
            # 调度下一轮
            self._schedule_next_round()
        else:
            logger.info("   └─ 仓位未完全归零 (剩余 %.8f)，等待继续平仓", net)

    def _cancel_remaining_scale_in_orders(self) -> None:
        """取消剩余的加仓单"""
        with self._state_lock:
            scale_in_orders = self._round_state.scale_in_orders
        
        for order in scale_in_orders:
            if order.is_active and not order.is_fully_filled:
                self._cancel_order_by_id(order.order_id)

    # ============================================================
    # 辅助方法
    # ============================================================
    
    def _calculate_order_quantity(self, reference_price: float) -> Optional[float]:
        """计算订单数量"""
        if self.order_quantity is not None and self.order_quantity > 0:
            return round_to_precision(self.order_quantity, self.base_precision)
        
        # 自动计算：使用最大仓位的一定比例
        qty = self.max_position * 0.2  # 使用最大仓位的 20% 作为单笔订单
        qty = round_to_precision(qty, self.base_precision)
        
        if qty < self.min_order_size:
            qty = self.min_order_size
        
        return qty

    def place_limit_orders(self) -> None:
        """覆盖父类方法，改为启动新一轮"""
        self._start_new_round()

    # ============================================================
    # 运行入口
    # ============================================================
    
    def run(self, duration_seconds: int = 3600, interval_seconds: int = 60) -> None:
        """运行策略（事件驱动模式）"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("开始运行纯 Maker-Maker 刷量策略")
        logger.info("  运行时长: %d 秒", duration_seconds)
        logger.info("  模式: 事件驱动")
        logger.info("=" * 60)
        
        start_time = time.time()
        self._stop_flag = False
        
        try:
            # 确保 WebSocket 连接
            self.check_ws_connection()
            if self.ws is not None:
                try:
                    self._ensure_data_streams()
                except Exception as e:
                    logger.warning("初始化数据流时出错: %s", e)
            
            # 启动第一轮
            self._start_new_round()
            
            # 主循环：保持运行并定期输出统计
            report_interval = 300  # 每5分钟输出一次统计
            last_report = start_time
            
            while time.time() - start_time < duration_seconds and not self._stop_flag:
                now = time.time()
                
                # 定期统计
                if now - last_report >= report_interval:
                    self._print_stats()
                    last_report = now
                
                time.sleep(1)
            
            logger.info("")
            logger.info("=" * 60)
            logger.info("策略运行结束")
            self._print_stats()
            logger.info("=" * 60)
            
        except KeyboardInterrupt:
            logger.info("用户中断，停止策略")
            self._stop_flag = True
        finally:
            self._cancel_all_tracked_orders()

    def _print_stats(self) -> None:
        """打印统计信息"""
        logger.info("")
        logger.info("📊 统计信息")
        logger.info("  完成轮数: %d", self._round_count)
        logger.info("  累计刷量: %.2f %s", self._total_volume, self.quote_asset)
        
        try:
            position_state = self.get_position_state()
            net = float(position_state.get("net", 0.0) or 0.0)
            logger.info("  当前仓位: %s", format_balance(net))
        except Exception:
            pass

    def stop(self) -> None:
        """停止策略"""
        logger.info("收到停止信号")
        self._stop_flag = True
        super().stop()


# 工厂函数，保持兼容性
def create_pure_maker_strategy(*args, **kwargs) -> PureMakerStrategy:
    """创建纯 Maker-Maker 策略实例"""
    return PureMakerStrategy(*args, **kwargs)
