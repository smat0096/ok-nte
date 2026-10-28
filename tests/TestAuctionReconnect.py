"""断线类打断处理的回归测试 (见 auction.recovery.handle_reconnect_popup /
handle_start_screen)。

断线时游戏弹出全屏「提示」面板盖住所有界面特征, 点「重试」后还可能掉回
初始界面(启动/登录画面), 两种情况原有掉线回场(大世界探测)都不触发, 任务
卡死。这里用合成帧与桩任务覆盖: 亮度门控(暗帧/空帧不做 OCR), 按钮识别与
点击, 点击后等界面消失的轮询, 超时路径, 任务侧入口的嵌套防护与转发顺序,
以及整轮失败兜底里断线类打断优先的顺序。坐标换算按 1080p 基准。
"""

import unittest
from unittest import mock

import numpy as np
from ok import Box

from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask
from src.tasks.auction import recovery
from src.tasks.auction.layout import (
    BOX_RECONNECT_BTN,
    BOX_RECONNECT_PANEL,
    POS_ENTER_GAME,
)


def _screen(x, y, to_x, to_y):
    return Box(int(x * 1920), int(y * 1080), int((to_x - x) * 1920), int((to_y - y) * 1080))


def _frame(value):
    return np.full((1080, 1920, 3), value, dtype=np.uint8)


class _StubTask:
    """recovery 断线类处理函数的最小任务桩: 记录 OCR/点击/睡眠/模板与日志。"""

    def __init__(self, ocr_results=None, ocr_always=None, template_results=None,
                 template_always=None):
        self.ocr_results = list(ocr_results or [])
        self.ocr_always = ocr_always
        self.template_results = list(template_results or [])
        self.template_always = template_always
        self.ocr_calls = []
        self.template_calls = []
        self.clicks = []
        self.sleeps = []
        self.frames = 0
        self.logs = []
        self.foreground = True
        self.front_calls = 0

    def box_of_screen(self, x, y, to_x=None, to_y=None):
        return _screen(x, y, to_x, to_y)

    def ocr(self, box=None, match=None, log=True, **kwargs):
        self.ocr_calls.append((box, match))
        if self.ocr_always is not None:
            return self.ocr_always
        if self.ocr_results:
            return self.ocr_results.pop(0)
        return []

    def find_one(self, feature_name=None, **kwargs):
        self.template_calls.append(feature_name)
        if self.template_always is not None:
            return self.template_always
        if self.template_results:
            return self.template_results.pop(0)
        return None

    def is_foreground(self):
        return self.foreground

    def bring_to_front(self, *args, **kwargs):
        self.front_calls += 1

    def operate_click(self, target, *args, **kwargs):
        self.clicks.append((target, args))

    def sleep(self, timeout):
        self.sleeps.append(timeout)

    def next_frame(self):
        self.frames += 1
        return None

    def log_info(self, message):
        self.logs.append(("info", message))

    def log_warning(self, message):
        self.logs.append(("warning", message))


def _retry_button():
    return Box(820, 700, 140, 50, name="重试")


class TestReconnectPanelBrightness(unittest.TestCase):
    def test_bright_dialog_panel(self):
        box = _screen(*BOX_RECONNECT_PANEL)
        self.assertGreater(recovery.reconnect_panel_brightness(_frame(235), box), 200)

    def test_dark_game_frame(self):
        box = _screen(*BOX_RECONNECT_PANEL)
        self.assertLess(recovery.reconnect_panel_brightness(_frame(40), box), 100)

    def test_none_frame_and_out_of_screen_box_are_zero(self):
        self.assertEqual(recovery.reconnect_panel_brightness(None, _screen(*BOX_RECONNECT_PANEL)), 0.0)
        out_of_screen = Box(5000, 5000, 10, 10)
        self.assertEqual(recovery.reconnect_panel_brightness(_frame(235), out_of_screen), 0.0)


class TestHandleReconnectPopup(unittest.TestCase):
    def test_dark_frame_skips_ocr_and_click(self):
        task = _StubTask(ocr_always=[_retry_button()])
        self.assertFalse(recovery.handle_reconnect_popup(task, _frame(40)))
        self.assertEqual(task.ocr_calls, [])
        self.assertEqual(task.clicks, [])

    def test_none_frame_skips_ocr(self):
        task = _StubTask(ocr_always=[_retry_button()])
        self.assertFalse(recovery.handle_reconnect_popup(task, None))
        self.assertEqual(task.ocr_calls, [])

    def test_bright_frame_without_button_does_not_click(self):
        task = _StubTask(ocr_results=[[]])
        self.assertFalse(recovery.handle_reconnect_popup(task, _frame(235)))
        # 亮度达标时应做过一次按钮 OCR, 但没有可点的「重试」
        self.assertEqual(len(task.ocr_calls), 1)
        self.assertEqual(task.clicks, [])

    def test_clicks_retry_and_waits_until_dialog_gone(self):
        button = _retry_button()
        task = _StubTask(ocr_results=[[button], [button], []])
        self.assertTrue(recovery.handle_reconnect_popup(task, _frame(235)))
        self.assertEqual(task.clicks, [(button, ())])
        # 第二次仍见到按钮 → 睡了一拍; 第三次弹窗消失 → 返回 True
        self.assertEqual(task.sleeps, [0.5])
        warnings = [msg for level, msg in task.logs if level == "warning"]
        self.assertTrue(any("重试" in msg for msg in warnings))
        self.assertTrue(any("消失" in msg for level, msg in task.logs if level == "info"))

    def test_button_box_is_used_for_ocr(self):
        button = _retry_button()
        task = _StubTask(ocr_results=[[button], []])
        recovery.handle_reconnect_popup(task, _frame(235))
        expected = _screen(*BOX_RECONNECT_BTN)
        for box, match in task.ocr_calls:
            self.assertEqual((box.x, box.y, box.width, box.height),
                             (expected.x, expected.y, expected.width, expected.height))
            self.assertIs(match, recovery.RE_RECONNECT_BTN)

    def test_dialog_persists_until_timeout_returns_true(self):
        button = _retry_button()
        task = _StubTask(ocr_always=[button])
        with mock.patch.object(recovery, "RECONNECT_GONE_TIMEOUT", 0):
            self.assertTrue(recovery.handle_reconnect_popup(task, _frame(235)))
        # 已点击但弹窗未消失: 只点一次, 不重复点击
        self.assertEqual(task.clicks, [(button, ())])
        self.assertTrue(any("仍在" in msg for level, msg in task.logs if level == "warning"))


class TestHandleStartScreen(unittest.TestCase):
    def test_no_template_returns_false_without_click(self):
        task = _StubTask()
        self.assertFalse(recovery.handle_start_screen(task))
        self.assertEqual(task.template_calls, [recovery.Labels.login_setting])
        self.assertEqual(task.clicks, [])

    def test_clicks_enter_game_and_waits_until_screen_gone(self):
        icon = Box(1750, 80, 40, 40, name="login_setting")
        task = _StubTask(template_results=[icon, None])
        self.assertTrue(recovery.handle_start_screen(task))
        # 点击点与 wait_login 同源; 已在前台时不抢前台
        self.assertEqual(task.clicks, [(POS_ENTER_GAME[0], (POS_ENTER_GAME[1],))])
        self.assertEqual(task.front_calls, 0)
        warnings = [msg for level, msg in task.logs if level == "warning"]
        self.assertTrue(any("进入游戏" in msg for msg in warnings))

    def test_brings_to_front_when_not_foreground(self):
        icon = Box(1750, 80, 40, 40, name="login_setting")
        task = _StubTask(template_results=[icon, None])
        task.foreground = False
        self.assertTrue(recovery.handle_start_screen(task))
        self.assertEqual(task.front_calls, 1)
        self.assertIn(3, task.sleeps)
        self.assertEqual(task.clicks, [(POS_ENTER_GAME[0], (POS_ENTER_GAME[1],))])

    def test_screen_persists_until_timeout_returns_true(self):
        icon = Box(1750, 80, 40, 40, name="login_setting")
        task = _StubTask(template_always=icon)
        with mock.patch.object(recovery, "START_SCREEN_GONE_TIMEOUT", 0):
            self.assertTrue(recovery.handle_start_screen(task))
        # 已点击但界面未离开: 只点一次, 不重复点击
        self.assertEqual(len(task.clicks), 1)
        self.assertTrue(any("仍在" in msg for level, msg in task.logs if level == "warning"))


class TestTaskInterruptWrapper(unittest.TestCase):
    def _make_task(self):
        task = object.__new__(AutoBidAuctionTask)
        task._handling_interrupt_popup = False
        return task

    def test_wrapper_runs_reconnect_then_start_screen(self):
        task = self._make_task()
        order = []
        with mock.patch.object(AutoBidAuctionTask, "frame", new_callable=mock.PropertyMock), \
                mock.patch.object(recovery, "handle_reconnect_popup",
                                  side_effect=lambda *a: order.append("reconnect") or False), \
                mock.patch.object(recovery, "handle_start_screen",
                                  side_effect=lambda *a: order.append("start") or True):
            self.assertTrue(task._handle_interrupt_popups())
        self.assertEqual(order, ["reconnect", "start"])
        self.assertFalse(task._handling_interrupt_popup)

    def test_wrapper_short_circuits_when_reconnect_handled(self):
        task = self._make_task()
        order = []
        with mock.patch.object(AutoBidAuctionTask, "frame", new_callable=mock.PropertyMock), \
                mock.patch.object(recovery, "handle_reconnect_popup",
                                  side_effect=lambda *a: order.append("reconnect") or True), \
                mock.patch.object(recovery, "handle_start_screen",
                                  side_effect=lambda *a: order.append("start") or True):
            self.assertTrue(task._handle_interrupt_popups())
        self.assertEqual(order, ["reconnect"])

    def test_wrapper_skips_when_already_handling(self):
        task = self._make_task()
        task._handling_interrupt_popup = True
        with mock.patch.object(recovery, "handle_reconnect_popup") as reconnect, \
                mock.patch.object(recovery, "handle_start_screen") as start:
            self.assertFalse(task._handle_interrupt_popups())
            reconnect.assert_not_called()
            start.assert_not_called()
        self.assertTrue(task._handling_interrupt_popup)

    def test_wrapper_restores_flag_on_exception(self):
        task = self._make_task()
        with mock.patch.object(AutoBidAuctionTask, "frame", new_callable=mock.PropertyMock), \
                mock.patch.object(recovery, "handle_reconnect_popup",
                                  side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                task._handle_interrupt_popups()
        self.assertFalse(task._handling_interrupt_popup)


class TestBlockingPopupOrder(unittest.TestCase):
    def test_interrupts_are_handled_before_monthly_card(self):
        task = _StubTask()
        order = []
        task._handle_interrupt_popups = lambda: order.append("interrupt") or True
        task.find_monthly_card = lambda: order.append("monthly")
        task.handle_monthly_card = lambda: order.append("monthly_click")
        recovery.handle_blocking_popup(task, None)
        self.assertEqual(order, ["interrupt"])


if __name__ == "__main__":
    unittest.main()
