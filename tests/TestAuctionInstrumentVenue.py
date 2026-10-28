import unittest
from unittest.mock import Mock

from src.tasks.AutoBidAuctionTask import AutoBidAuctionTask
from src.tasks.auction.options import (
    CONF_INSTRUMENT_GROUP,
    CONF_INSTRUMENT_SEQUENCE,
    CONF_VENUE_INSTRUMENT_GROUPS,
    CONF_VENUE_INSTRUMENT_SEQUENCES,
    INSTRUMENT_GROUP_INHERIT,
    INSTRUMENT_GROUP_NONE,
    INSTRUMENT_GROUP_SUPREME,
    VENUE_CORAL,
    VENUE_SEASHELL,
)


class TestAuctionInstrumentVenue(unittest.TestCase):
    def _make_task(self, config, venue):
        task = object.__new__(AutoBidAuctionTask)
        task.config = config
        task._current_venue = venue
        task._instrument_group = INSTRUMENT_GROUP_SUPREME
        task._instrument_group_ready = True
        task._instrument_slots = []
        task._instrument_seq_raw = None
        task._instrument_seq = 0
        task._assist_enabled = Mock(return_value=True)
        task.log_info = Mock()
        task.log_warning = Mock()
        return task

    def test_instrument_group_uses_venue_override(self):
        config = {
            CONF_INSTRUMENT_GROUP: "初级泛用仪器组",
            CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]: "高级泛用仪器组",
        }
        task = self._make_task(config, VENUE_CORAL)

        task._refresh_instrument_group()

        self.assertEqual(task._instrument_group, "高级泛用仪器组")
        self.assertFalse(task._instrument_group_ready)

    def test_instrument_group_inherit_and_unknown_venue_use_global(self):
        config = {
            CONF_INSTRUMENT_GROUP: "初级泛用仪器组",
            CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]: INSTRUMENT_GROUP_INHERIT,
        }
        task = self._make_task(config, VENUE_CORAL)
        task._refresh_instrument_group()
        self.assertEqual(task._instrument_group, "初级泛用仪器组")

        task = self._make_task(config, None)
        task._refresh_instrument_group()
        self.assertEqual(task._instrument_group, "初级泛用仪器组")

    def test_instrument_sequence_uses_venue_override(self):
        config = {
            CONF_INSTRUMENT_SEQUENCE: "5",
            CONF_VENUE_INSTRUMENT_SEQUENCES[VENUE_CORAL]: "12",
        }
        task = self._make_task(config, VENUE_CORAL)

        task._refresh_instrument_slots()

        self.assertEqual(task._instrument_slots, [1, 2])
        self.assertEqual(task._instrument_seq_raw, "12")

    def test_instrument_sequence_blank_venue_value_falls_back_to_global(self):
        config = {
            CONF_INSTRUMENT_SEQUENCE: "5",
            CONF_VENUE_INSTRUMENT_SEQUENCES[VENUE_CORAL]: "",
        }
        task = self._make_task(config, VENUE_CORAL)

        self.assertEqual(task._instrument_sequence_raw_for_venue(), "5")
        task._refresh_instrument_slots()
        self.assertEqual(task._instrument_slots, [5])

    def test_unknown_venue_uses_global_sequence(self):
        config = {
            CONF_INSTRUMENT_SEQUENCE: "3",
            CONF_VENUE_INSTRUMENT_SEQUENCES[VENUE_SEASHELL]: "12",
        }
        task = self._make_task(config, None)
        self.assertEqual(task._instrument_sequence_raw_for_venue(), "3")

    def test_venue_group_none_disables_current_venue(self):
        config = {
            CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]: INSTRUMENT_GROUP_NONE,
        }
        task = self._make_task(config, VENUE_CORAL)
        self.assertFalse(task._instrument_enabled_for_venue())

        task.config[CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]] = "高级泛用仪器组"
        self.assertTrue(task._instrument_enabled_for_venue())

    def test_venue_group_inherit_follows_global_switch(self):
        config = {
            CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]: INSTRUMENT_GROUP_INHERIT,
        }
        task = self._make_task(config, VENUE_CORAL)
        self.assertTrue(task._instrument_enabled_for_venue())

        task._assist_enabled = Mock(return_value=False)
        self.assertFalse(task._instrument_enabled_for_venue())

    def test_none_venue_skips_group_and_sequence_validation(self):
        config = {
            CONF_INSTRUMENT_GROUP: INSTRUMENT_GROUP_SUPREME,
            CONF_INSTRUMENT_SEQUENCE: "",
            CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]: INSTRUMENT_GROUP_NONE,
            CONF_VENUE_INSTRUMENT_SEQUENCES[VENUE_CORAL]: "9",
        }
        task = self._make_task(config, VENUE_CORAL)
        task._validate_instrument_group()
        task._validate_instrument_sequence()

    def test_validate_rejects_invalid_venue_group(self):
        config = {
            CONF_INSTRUMENT_GROUP: INSTRUMENT_GROUP_SUPREME,
            CONF_VENUE_INSTRUMENT_GROUPS[VENUE_CORAL]: "不存在的仪器组",
        }
        task = self._make_task(config, VENUE_CORAL)
        with self.assertRaises(ValueError):
            task._validate_instrument_group()

    def test_validate_rejects_invalid_venue_sequence(self):
        config = {
            CONF_INSTRUMENT_SEQUENCE: "",
            CONF_VENUE_INSTRUMENT_SEQUENCES[VENUE_CORAL]: "9",
        }
        task = self._make_task(config, VENUE_CORAL)
        with self.assertRaises(ValueError):
            task._validate_instrument_sequence()


if __name__ == "__main__":
    unittest.main()
