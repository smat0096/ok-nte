"""满仓清理独立开关与「满仓无法清理时」动作的回归测试。

覆盖 (见 src/tasks/auction/options.py / sell.py / venue.py /
AutoBidAuctionTask):
- 出售模式三选一与旧值「满仓时清理」的兜底映射
- read_full_cleanup / read_full_stuck_actions 的取值清洗
- run_round_end_sell 的门控矩阵 (间隔模式 / 独立清理开关 / 满仓结论采信)
- _run_round_end_sell 的跨轮满仓标记落账与「无法自动清理」的立即触发
- 动作分发 (停仪器/切场/关机/停止挂机) 与满仓锁存的影响
"""

import unittest
from unittest import mock

from ok import WaitFailedException

from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask
from src.tasks.auction import options as auction_options
from src.tasks.auction import sell
from src.tasks.auction import venue as auction_venue
from src.tasks.auction.layout import PostRoundState
from src.tasks.auction.options import (
    ASSIST_INSTRUMENT,
    CONF_ASSIST_FEATURES,
    CONF_FULL_CLEANUP,
    CONF_FULL_STUCK_ACTIONS,
    CONF_SELL_INTERVAL,
    CONF_SELL_MODE,
    CONF_SELL_QUALITIES,
    FULL_STUCK_SHUTDOWN,
    FULL_STUCK_STOP_INSTRUMENTS,
    FULL_STUCK_STOP_TASK,
    FULL_STUCK_SWITCH_SEASHELL,
    SELL_MODE_FULL,
    SELL_MODE_INTERVAL,
    SELL_MODE_OFF,
    SELL_MODE_ONE_CLICK,
    VENUE_SEASHELL,
)
from src.tasks.mixin.RoundMixin import RoundState


def make_task(config):
    """构造不触发框架初始化的任务实例, 只挂测试触达的成员。"""
    task = object.__new__(AutoBidAuctionTask)
    task.config = dict(config)
    task._round_state = RoundState(total=0, index=1)
    task.log_info = mock.Mock()
    task.log_warning = mock.Mock()
    task.log_debug = mock.Mock()
    task.log_error = mock.Mock()
    return task


class TestFullCleanupOptions(unittest.TestCase):
    def test_sell_modes_exclude_full_cleanup(self):
        self.assertNotIn(SELL_MODE_FULL, auction_options.SELL_MODES)
        self.assertEqual(sell.normalize_mode(SELL_MODE_FULL), SELL_MODE_OFF)

    def test_defaults_enable_cleanup_without_stuck_actions(self):
        config = auction_options.default_config()
        self.assertIs(config[CONF_FULL_CLEANUP], True)
        self.assertEqual(config[CONF_FULL_STUCK_ACTIONS], [])

    def test_sell_qualities_only_parented_by_cleanup_switch(self):
        # 同一键不得挂在两个可同时激活的父条件下(框架重排缺陷, 见 auction-notes 1.3):
        # 「出售品质」只允许出现在「满仓时清理」的子配置里。
        parents = []
        for key, spec in auction_options.config_type().items():
            for sub_configs in (spec.get("sub_configs"),):
                for subs in (sub_configs or {}).values():
                    if CONF_SELL_QUALITIES in subs:
                        parents.append(key)
        self.assertEqual(parents, [CONF_FULL_CLEANUP])


class TestFullCleanupReaders(unittest.TestCase):
    def test_read_full_cleanup_safety_default(self):
        self.assertTrue(sell.read_full_cleanup({}))
        self.assertTrue(sell.read_full_cleanup({CONF_FULL_CLEANUP: "yes"}))
        self.assertTrue(sell.read_full_cleanup({CONF_FULL_CLEANUP: None}))
        self.assertFalse(sell.read_full_cleanup({CONF_FULL_CLEANUP: False}))

    def test_read_full_stuck_actions_cleans_input(self):
        config = {
            CONF_FULL_STUCK_ACTIONS: [
                FULL_STUCK_SHUTDOWN,
                "乱写的",
                FULL_STUCK_STOP_TASK,
                FULL_STUCK_SHUTDOWN,
            ]
        }
        self.assertEqual(
            sell.read_full_stuck_actions(config),
            [FULL_STUCK_SHUTDOWN, FULL_STUCK_STOP_TASK],
        )

    def test_read_full_stuck_actions_dirty_values_empty(self):
        self.assertEqual(sell.read_full_stuck_actions({CONF_FULL_STUCK_ACTIONS: "停止挂机"}), [])
        self.assertEqual(sell.read_full_stuck_actions({}), [])


class TestRunRoundEndSellGating(unittest.TestCase):
    """run_round_end_sell 的门控矩阵: 间隔模式 x 独立清理开关 x 满仓结论。"""

    def _run(self, config, state=None, detect=None, sold=False):
        task = make_task(config)
        with (
            mock.patch.object(sell, "detect_inventory_full", return_value=detect) as detect_mock,
            mock.patch.object(sell, "run_collections", return_value=sold) as run_mock,
        ):
            outcome = sell.run_round_end_sell(task, object(), None, state=state)
        return outcome, detect_mock, run_mock

    def test_cleanup_switch_alone_sells_on_full(self):
        # 独立开关(等价旧「满仓时清理」模式): 出售模式不出售, 满仓仍触发出售。
        outcome, detect_mock, run_mock = self._run(
            {CONF_SELL_MODE: SELL_MODE_OFF, CONF_FULL_CLEANUP: True},
            detect=True,
            sold=False,
        )
        self.assertTrue(outcome)
        detect_mock.assert_called_once()
        run_mock.assert_called_once()
        self.assertTrue(run_mock.call_args.kwargs["require_sale"])

    def test_cleanup_disabled_skips_detection_and_hint(self):
        # 开关关闭: 不检测, 也不采信结算后观测的满仓结论(含横幅落账)。
        outcome, detect_mock, run_mock = self._run(
            {CONF_SELL_MODE: SELL_MODE_OFF, CONF_FULL_CLEANUP: False},
            state=PostRoundState(inventory_full=True),
            detect=True,
        )
        self.assertIsNone(outcome)
        detect_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_interval_mode_without_cleanup_ignores_full(self):
        # 间隔模式 + 开关关闭: 退化为严格按间隔, 满仓结论不提前触发出售。
        outcome, detect_mock, run_mock = self._run(
            {
                CONF_SELL_MODE: SELL_MODE_INTERVAL,
                CONF_SELL_INTERVAL: 3,
                CONF_FULL_CLEANUP: False,
            },
            state=PostRoundState(inventory_full=True),
            detect=True,
        )
        self.assertIsNone(outcome)
        detect_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_interval_hit_sells_even_without_cleanup(self):
        task_config = {
            CONF_SELL_MODE: SELL_MODE_INTERVAL,
            CONF_SELL_INTERVAL: 1,
            CONF_FULL_CLEANUP: False,
        }
        outcome, _, run_mock = self._run(
            task_config,
            state=PostRoundState(inventory_full=True),
            sold=False,
        )
        # 间隔命中出售, 但满仓结论被忽略(require_sale=False), 落账返回 None。
        self.assertIsNone(outcome)
        run_mock.assert_called_once()
        self.assertFalse(run_mock.call_args.kwargs["require_sale"])

    def test_interval_with_cleanup_keeps_full_trigger(self):
        task_config = {
            CONF_SELL_MODE: SELL_MODE_INTERVAL,
            CONF_SELL_INTERVAL: 5,
            CONF_FULL_CLEANUP: True,
        }
        outcome, _, run_mock = self._run(task_config, detect=True, sold=True)
        self.assertFalse(outcome)
        run_mock.assert_called_once()
        self.assertTrue(run_mock.call_args.kwargs["require_sale"])

    def test_legacy_full_mode_value_preserved_by_switch(self):
        # 老配置存了旧档位「满仓时清理」: 模式兜底为不出售, 默认开启的开关接续行为。
        outcome, _, run_mock = self._run(
            {CONF_SELL_MODE: SELL_MODE_FULL},
            detect=True,
            sold=False,
        )
        self.assertTrue(outcome)
        run_mock.assert_called_once()

    def test_one_click_and_cleanup_off_never_sells(self):
        outcome, detect_mock, run_mock = self._run(
            {CONF_SELL_MODE: SELL_MODE_ONE_CLICK, CONF_FULL_CLEANUP: False},
            detect=True,
        )
        self.assertIsNone(outcome)
        detect_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_undetermined_detection_never_lands(self):
        # 检测「未测出」(None)不能落账为满仓, 也不能当已清掉。
        outcome, _, run_mock = self._run(
            {CONF_SELL_MODE: SELL_MODE_OFF, CONF_FULL_CLEANUP: True},
            detect=None,
        )
        self.assertIsNone(outcome)
        run_mock.assert_not_called()


class TestRoundEndSellLanding(unittest.TestCase):
    """落账即触发: 出售域一返回「仍满仓」就执行「满仓无法清理时」的动作。"""

    def _landing_task(self):
        task = make_task({})
        task._inventory_stuck = False
        task._handle_uncleanable_inventory = mock.Mock()
        return task

    def test_outcome_true_lands_and_triggers_action(self):
        task = self._landing_task()
        boxes = object()
        with mock.patch.object(sell, "run_round_end_sell", return_value=True):
            self.assertTrue(task._run_round_end_sell(boxes))
        self.assertTrue(task._inventory_stuck)
        task._handle_uncleanable_inventory.assert_called_once_with(boxes)

    def test_outcome_false_clears_flag_without_action(self):
        task = self._landing_task()
        task._inventory_stuck = True
        with mock.patch.object(sell, "run_round_end_sell", return_value=False):
            self.assertFalse(task._run_round_end_sell(object()))
        self.assertFalse(task._inventory_stuck)
        task._handle_uncleanable_inventory.assert_not_called()

    def test_outcome_none_keeps_flag_without_action(self):
        task = self._landing_task()
        task._inventory_stuck = True
        with mock.patch.object(sell, "run_round_end_sell", return_value=None):
            self.assertIsNone(task._run_round_end_sell(object()))
        # None 不落账: 标记保持上一份结论, 也不触发动作。
        self.assertTrue(task._inventory_stuck)
        task._handle_uncleanable_inventory.assert_not_called()


class TestStuckRoundRetry(unittest.TestCase):
    """_run_single_round 满仓分支的重试出售经真实链路触发动作。"""

    def _make_stuck_task(self, detect_sequence):
        task = make_task({})
        task._inventory_stuck = True
        task._reset_round_state = mock.Mock()
        task.add_failed = mock.Mock()
        task.add_success = mock.Mock()
        task._exec_auction_round = mock.Mock(return_value=True)
        task._post_round_state = PostRoundState()
        task._detect_inventory_full = mock.Mock(side_effect=detect_sequence)
        task._handle_uncleanable_inventory = mock.Mock()
        return task

    def test_stuck_retry_with_still_full_outcome_triggers_action(self):
        task = self._make_stuck_task([True])
        boxes = object()
        # 真实链路: _try_sell_collections -> _run_round_end_sell, 出售域返回 True。
        with mock.patch.object(sell, "run_round_end_sell", return_value=True):
            task._run_single_round(boxes)
        task._handle_uncleanable_inventory.assert_called_once_with(boxes)
        self.assertTrue(task._inventory_stuck)

    def test_stuck_retry_cleared_keeps_auction_going(self):
        task = self._make_stuck_task([True])
        with mock.patch.object(sell, "run_round_end_sell", return_value=False):
            task._run_single_round(object())
        task._handle_uncleanable_inventory.assert_not_called()
        self.assertFalse(task._inventory_stuck)

    def test_sell_budget_exhaustion_does_not_trigger_action(self):
        task = self._make_stuck_task([True])
        with mock.patch.object(
            sell, "run_round_end_sell", side_effect=WaitFailedException("预算耗尽")
        ):
            task._run_single_round(object())
        # 结果未知: 不触发动作, 标记保持原值, 下一轮重试后再裁决。
        task._handle_uncleanable_inventory.assert_not_called()
        self.assertTrue(task._inventory_stuck)

    def test_recovered_stuck_flag_resumes_auction(self):
        task = self._make_stuck_task([False])
        task._run_single_round(object())
        task._handle_uncleanable_inventory.assert_not_called()
        task._exec_auction_round.assert_called_once()
        self.assertFalse(task._inventory_stuck)


class TestUncleanableActions(unittest.TestCase):
    def _make_task(self, actions):
        task = make_task({CONF_FULL_STUCK_ACTIONS: actions})
        task._instrument_stuck_disabled = False
        task._inventory_stuck_venue = None
        task._current_venue = None
        task.info_set = mock.Mock()
        task.stop_rounds = mock.Mock()
        task._stuck_disable_instruments = mock.Mock()
        task._stuck_switch_to_seashell = mock.Mock()
        task._stuck_shutdown = mock.Mock()
        return task

    def test_empty_actions_keep_retrying(self):
        task = self._make_task([])
        task._handle_uncleanable_inventory(object())
        task._stuck_disable_instruments.assert_not_called()
        task._stuck_switch_to_seashell.assert_not_called()
        task._stuck_shutdown.assert_not_called()
        task.stop_rounds.assert_not_called()

    def test_stop_instruments_single_selection(self):
        task = self._make_task([FULL_STUCK_STOP_INSTRUMENTS])
        task._handle_uncleanable_inventory(object())
        task._stuck_disable_instruments.assert_called_once()
        task._stuck_switch_to_seashell.assert_not_called()
        task.stop_rounds.assert_not_called()

    def test_stop_instruments_combines_with_venue_switch(self):
        task = self._make_task([FULL_STUCK_STOP_INSTRUMENTS, FULL_STUCK_SWITCH_SEASHELL])
        boxes = object()
        task._handle_uncleanable_inventory(boxes)
        task._stuck_disable_instruments.assert_called_once()
        task._stuck_switch_to_seashell.assert_called_once_with(boxes)
        task.stop_rounds.assert_not_called()

    def test_shutdown_wins_over_stop_task(self):
        task = self._make_task([FULL_STUCK_SHUTDOWN, FULL_STUCK_STOP_TASK])
        task._handle_uncleanable_inventory(object())
        task._stuck_shutdown.assert_called_once()
        # 关机分支内部自行 stop_rounds, 这里不重复执行「停止挂机」。
        task.stop_rounds.assert_not_called()

    def test_stop_task_stops_rounds(self):
        task = self._make_task([FULL_STUCK_STOP_TASK])
        task._handle_uncleanable_inventory(object())
        task.stop_rounds.assert_called_once()

    def test_stuck_disable_instruments_sets_idempotent_latch(self):
        task = self._make_task([])
        AutoBidAuctionTask._stuck_disable_instruments(task)
        self.assertTrue(task._instrument_stuck_disabled)
        AutoBidAuctionTask._stuck_disable_instruments(task)
        # 幂等: 第二次不重复告警。
        task.log_warning.assert_called_once()

    def test_stuck_shutdown_requests_shutdown_and_stops(self):
        task = self._make_task([])
        with mock.patch("src.tasks.AutoBidAuctionTask._request_windows_shutdown") as shutdown_mock:
            AutoBidAuctionTask._stuck_shutdown(task)
        shutdown_mock.assert_called_once_with(AutoBidAuctionTask.SHUTDOWN_DELAY_SECONDS)
        task.stop_rounds.assert_called_once()

    def test_stuck_shutdown_failure_is_reported(self):
        task = self._make_task([])
        with mock.patch(
            "src.tasks.AutoBidAuctionTask._request_windows_shutdown",
            side_effect=OSError("denied"),
        ):
            AutoBidAuctionTask._stuck_shutdown(task)
        task.log_error.assert_called()
        task.stop_rounds.assert_called_once()


class TestStuckLatches(unittest.TestCase):
    def test_instrument_latch_disables_instruments(self):
        task = make_task({CONF_ASSIST_FEATURES: [ASSIST_INSTRUMENT]})
        task._instrument_stuck_disabled = False
        self.assertTrue(task._venue_instrument_enabled(VENUE_SEASHELL))
        task._instrument_stuck_disabled = True
        self.assertFalse(task._venue_instrument_enabled(VENUE_SEASHELL))

    def test_ensure_expected_venue_passes_latched_override(self):
        task = make_task({})
        task._inventory_stuck_venue = VENUE_SEASHELL
        with mock.patch.object(
            auction_venue, "ensure_expected_venue", return_value=False
        ) as ensure_mock:
            task._ensure_expected_venue(object())
        ensure_mock.assert_called_once()
        self.assertEqual(ensure_mock.call_args.kwargs["override"], VENUE_SEASHELL)

    def test_venue_override_wins_over_configured_expected(self):
        task = make_task({auction_options.CONF_EXPECTED_VENUE: "珍珠场"})
        task._current_venue = None
        task.info_set = mock.Mock()
        task._read_current_venue = mock.Mock(return_value="当前：珊瑚场")
        with (
            mock.patch.object(auction_venue, "_read_asset", return_value=0),
            mock.patch.object(auction_venue, "switch_venue", return_value=True) as switch_mock,
        ):
            switched = auction_venue.ensure_expected_venue(
                task, object(), override=VENUE_SEASHELL
            )
        self.assertTrue(switched)
        # 切换目标是锁存的海贝场, 不是配置里的珍珠场。
        self.assertEqual(switch_mock.call_args.args[2], VENUE_SEASHELL)
        self.assertEqual(task._current_venue, VENUE_SEASHELL)


if __name__ == "__main__":
    unittest.main()
