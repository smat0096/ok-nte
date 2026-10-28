import unittest
from types import SimpleNamespace

from ok import Box
from ok.core.config_schema import build_config_fields

from src.tasks.auction import bid_price, options, venue
from src.tasks.auction.layout import build_boxes
from src.tasks.auction.options import (
    CONF_EXPECTED_VENUE,
    CONF_FIXED_PRICE,
    CONF_VENUE_BASE_PRICES,
    VENUE_CORAL,
    VENUE_PEARL,
    VENUE_SEASHELL,
)


class TestAuctionVenue(unittest.TestCase):
    def test_normalize_venue_accepts_current_label(self):
        self.assertEqual(venue.normalize_venue("当前：珊瑚场"), VENUE_CORAL)
        self.assertEqual(venue.normalize_venue("海贝场"), VENUE_SEASHELL)

    def test_normalize_venue_accepts_pearl_alias(self):
        self.assertEqual(venue.normalize_venue("真珠场"), VENUE_PEARL)
        self.assertEqual(venue.normalize_venue("珍珠"), VENUE_PEARL)

    def test_normalize_venue_rejects_unknown_and_empty_text(self):
        self.assertIsNone(venue.normalize_venue("未知会场"))
        self.assertIsNone(venue.normalize_venue(""))
        self.assertIsNone(venue.normalize_venue(None))

    def test_expected_venue_normalizes_partial_config(self):
        config = {CONF_EXPECTED_VENUE: "珊瑚"}
        self.assertEqual(venue.expected_venue(config), VENUE_CORAL)
        config = {CONF_EXPECTED_VENUE: ""}
        self.assertIsNone(venue.expected_venue(config))

    def test_venue_matches_aliases_and_partial_names(self):
        self.assertTrue(venue.venue_matches("珊瑚", "当前：珊瑚场"))
        self.assertTrue(venue.venue_matches("真珠", "珍珠场"))
        self.assertFalse(venue.venue_matches("海贝", "珊瑚场"))

    def test_asset_requirements_match_venue_tiers(self):
        self.assertEqual(venue.asset_requirement(VENUE_SEASHELL), 0)
        self.assertEqual(venue.asset_requirement(VENUE_CORAL), 1_000_000)
        self.assertEqual(venue.asset_requirement(VENUE_PEARL), 5_000_000)
        self.assertIsNone(venue.asset_requirement("未知会场"))

    def test_can_switch_requires_asset_and_threshold(self):
        self.assertFalse(venue.can_switch_to(VENUE_CORAL, None))
        self.assertFalse(venue.can_switch_to(VENUE_CORAL, 999_999))
        self.assertTrue(venue.can_switch_to(VENUE_CORAL, 1_000_000))
        self.assertFalse(venue.can_switch_to(VENUE_PEARL, 4_999_999))
        self.assertTrue(venue.can_switch_to(VENUE_PEARL, 5_000_000))
        self.assertTrue(venue.can_switch_to(VENUE_SEASHELL, 0))

    def test_base_price_prefers_current_venue(self):
        config = {
            CONF_FIXED_PRICE: 230_000,
            CONF_VENUE_BASE_PRICES[VENUE_CORAL]: 500_000,
        }
        self.assertEqual(venue.base_price_key(VENUE_CORAL), CONF_VENUE_BASE_PRICES[VENUE_CORAL])
        self.assertEqual(venue.resolve_base_price(config, VENUE_CORAL), 500_000)

    def test_base_price_falls_back_when_venue_price_is_zero(self):
        config = {
            CONF_FIXED_PRICE: 230_000,
            CONF_VENUE_BASE_PRICES[VENUE_PEARL]: 0,
        }
        self.assertEqual(venue.resolve_base_price(config, VENUE_PEARL), 230_000)

    def test_base_price_falls_back_for_unknown_venue_and_invalid_value(self):
        config = {
            CONF_FIXED_PRICE: 230_000,
            CONF_VENUE_BASE_PRICES[VENUE_CORAL]: "bad",
        }
        self.assertEqual(venue.resolve_base_price(config, "未知会场"), 230_000)
        self.assertEqual(venue.resolve_base_price(config, None), 230_000)
        self.assertEqual(venue.resolve_base_price(config, VENUE_CORAL), 230_000)

    def test_base_price_uses_default_when_nothing_configured(self):
        self.assertEqual(venue.resolve_base_price({}, VENUE_CORAL), 1)
        self.assertEqual(venue.resolve_base_price({}, VENUE_CORAL, default=7), 7)

    def test_bid_price_reads_current_venue_base_price(self):
        warnings = []
        task = SimpleNamespace(
            config={
                CONF_FIXED_PRICE: 230_000,
                CONF_VENUE_BASE_PRICES[VENUE_CORAL]: 500_000,
            },
            _current_venue=VENUE_CORAL,
            log_warning=warnings.append,
        )
        self.assertEqual(bid_price._read_base_price(task), 500_000)

        task._current_venue = VENUE_PEARL
        self.assertEqual(bid_price._read_base_price(task), 230_000)
        self.assertEqual(warnings, [])

    def test_bid_price_warns_and_falls_back_on_invalid_venue_price(self):
        warnings = []
        task = SimpleNamespace(
            config={
                CONF_FIXED_PRICE: 230_000,
                CONF_VENUE_BASE_PRICES[VENUE_CORAL]: "bad",
            },
            _current_venue=VENUE_CORAL,
            log_warning=warnings.append,
        )
        self.assertEqual(bid_price._read_base_price(task), 230_000)
        self.assertTrue(any(CONF_VENUE_BASE_PRICES[VENUE_CORAL] in w for w in warnings))

    def test_validate_venue_base_prices_rejects_invalid_values(self):
        task = SimpleNamespace(
            config={
                CONF_FIXED_PRICE: 1,
                CONF_VENUE_BASE_PRICES[VENUE_CORAL]: -1,
            }
        )
        with self.assertRaises(ValueError):
            bid_price._validate_venue_base_prices(task)

        task.config[CONF_VENUE_BASE_PRICES[VENUE_CORAL]] = "bad"
        with self.assertRaises(ValueError):
            bid_price._validate_venue_base_prices(task)

    def test_venue_options_excludes_current_venue_label(self):
        def screen(x, y, to_x, to_y):
            return Box(
                int(x * 1920),
                int(y * 1080),
                int((to_x - x) * 1920),
                int((to_y - y) * 1080),
            )

        boxes = build_boxes(screen)
        task = SimpleNamespace(
            ocr=lambda *args, **kwargs: [
                Box(1200, 600, 100, 30, name="海贝场"),
                Box(800, 500, 100, 30, name="珊瑚场"),
            ]
        )
        options = venue._venue_options(task, boxes)
        self.assertEqual([box.name for box in options], ["珊瑚场"])

    def test_venue_base_prices_are_visible_in_price_modes(self):
        for mode in (
            options.BID_MODE_CUSTOM,
            options.BID_MODE_ESTIMATE,
            options.BID_MODE_SMART,
            options.BID_MODE_EV,
        ):
            config = options.default_config()
            config[options.CONF_BID_MODE] = mode
            fields = build_config_fields(
                config,
                options.config_description(),
                options.config_type(),
                config,
            )
            keys = {field["key"] for field in fields}
            for key in CONF_VENUE_BASE_PRICES.values():
                self.assertIn(key, keys, f"mode={mode}")


if __name__ == "__main__":
    unittest.main()
