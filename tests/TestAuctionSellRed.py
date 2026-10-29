"""「拍卖成功出售红」与「低于此价才卖红」的回归测试。

2026-10-29 用户实测 bug: 勾选出售红并填了「低于此价才卖红」后, 成交价不低于
上限时 sell_on_settlement_screen 直接 return, 连「一键出售」都没点, 一件没卖。
结算界面的品质默认勾选除红以外的全部(用户 2026-10-29 澄清), 因此:
- 补点品质红 = 把红加进勾选, 一键出售连红整包全卖;
- 不点红 = 只卖除红以外的品质, 红色藏品保留入库;
- 判定只决定点不点红, 两条分支都必须点到「一键出售」(见 sell._should_select_sell_red)。
"""

import time
import unittest
from unittest import mock

from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask
from src.tasks.auction import sell
from src.tasks.auction.options import (
    ASSIST_SELL_RED,
    CONF_ASSIST_FEATURES,
    CONF_SELL_RED_MAX,
)


def make_task(config):
    """构造不触发框架初始化的任务实例, 只挂测试触达的成员。"""
    task = object.__new__(AutoBidAuctionTask)
    task.config = dict(config)
    task.log_info = mock.Mock()
    task.log_warning = mock.Mock()
    task.log_debug = mock.Mock()
    task.log_error = mock.Mock()
    return task


class TestShouldSelectSellRed(unittest.TestCase):
    def _task(self):
        return make_task({})

    def test_zero_limit_always_selects_red(self):
        self.assertTrue(sell._should_select_sell_red(self._task(), 123, 0))
        self.assertTrue(sell._should_select_sell_red(self._task(), None, 0))

    def test_below_limit_selects_red(self):
        self.assertTrue(sell._should_select_sell_red(self._task(), 50, 100))

    def test_at_limit_keeps_red(self):
        # 正好等于上限按「不低于」处理: 不点红, 红色藏品保留。
        self.assertFalse(sell._should_select_sell_red(self._task(), 100, 100))

    def test_above_limit_keeps_red(self):
        self.assertFalse(sell._should_select_sell_red(self._task(), 500, 100))

    def test_unreadable_value_keeps_red(self):
        task = self._task()
        self.assertFalse(sell._should_select_sell_red(task, None, 100))
        task.log_warning.assert_called_once()


class TestSellOnSettlementScreen(unittest.TestCase):
    """结算界面一键出售流程: 判定只决定点不点红, 不拦截出售本身。"""

    def _run(self, config, result_value):
        task = make_task(config)
        task.operate_click = mock.Mock()
        # 结算后的「获得物品」提示检测: 返回 None 走告警分支即可, 与本回归无关。
        task.wait_ocr = mock.Mock(return_value=None)
        boxes = mock.Mock()
        found = mock.Mock(name="one_click_button")
        with mock.patch.object(sell, "detect_one_click_sell", return_value=[found]):
            sell.sell_on_settlement_screen(
                task, boxes, time.monotonic() + 30, result_value=result_value
            )
        return task

    def _clicked_red(self, task) -> bool:
        """是否点过品质红圆点(坐标常量是数字, 一键出售按钮是 OCR 命中框列表)。"""
        return any(
            isinstance(call.args[0], (int, float))
            for call in task.operate_click.call_args_list
        )

    def _clicked_one_click(self, task) -> bool:
        return any(
            not isinstance(call.args[0], (int, float))
            for call in task.operate_click.call_args_list
        )

    def _red_config(self, sell_limit):
        return {
            CONF_ASSIST_FEATURES: [ASSIST_SELL_RED],
            CONF_SELL_RED_MAX: sell_limit,
        }

    def test_above_limit_still_sells_but_keeps_red(self):
        # 回归主场景: 金额超过上限也必须卖(除红以外的品质), 只是不点红。
        task = self._run(self._red_config(100), result_value=500)
        self.assertFalse(self._clicked_red(task))
        self.assertTrue(self._clicked_one_click(task))

    def test_below_limit_selects_red_and_sells_all(self):
        task = self._run(self._red_config(100), result_value=50)
        self.assertTrue(self._clicked_red(task))
        self.assertTrue(self._clicked_one_click(task))

    def test_unreadable_value_keeps_red(self):
        task = self._run(self._red_config(100), result_value=None)
        self.assertFalse(self._clicked_red(task))
        self.assertTrue(self._clicked_one_click(task))

    def test_zero_limit_always_selects_red(self):
        task = self._run(self._red_config(0), result_value=999)
        self.assertTrue(self._clicked_red(task))
        self.assertTrue(self._clicked_one_click(task))

    def test_without_sell_red_keeps_red(self):
        # 未勾选辅助时从不点红: 只卖除红以外的品质。
        task = self._run({CONF_ASSIST_FEATURES: []}, result_value=50)
        self.assertFalse(self._clicked_red(task))
        self.assertTrue(self._clicked_one_click(task))


if __name__ == "__main__":
    unittest.main()
