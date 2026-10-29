"""拍卖会场域: 会场名归一化, 验资门槛, 自动切换会场与按会场选基础价。

模块函数的第一个参数 task 是 AutoBidAuctionTask 实例, 允许访问面由
contracts.AuctionVenueOps 窄协议声明。当前会场的缓存 _current_venue 由任务
实例持有: 每轮进入匹配前由 ensure_expected_venue 刷新, 出价时
bid_price._read_base_price 据此选择对应会场的基础价。本模块只读写结论, 不改写
用户配置; 切换失败只告警, 不阻断本轮匹配 (基础价按当前会场取, 继续跑仍安全)。

会场全集与验资门槛在 options.VENUE_ASSET_REQUIREMENTS: 海贝场无门槛,
珊瑚场 100 万, 珍珠场 500 万 (游戏内也可能显示为「真珠场」)。切换入口是主界面
右侧的「选择会场」按钮 (layout.BOX_VENUE_SELECT), 面板里的候选会场按 OCR 文本
查找后点击目标会场。
"""

import time

from ok import TaskDisabledException

from src.tasks.auction import config_read as auction_config_read
from src.tasks.auction.contracts import AuctionVenueOps
from src.tasks.auction.layout import (
    RE_VENUE_ENTER,
    RE_VENUE_OPTION,
    RE_VENUE_SELECT,
    AuctionBoxes,
)
from src.tasks.auction.options import (
    CONF_EXPECTED_VENUE,
    CONF_FIXED_PRICE,
    CONF_VENUE_BASE_PRICES,
    CONF_VENUE_INSTRUMENT_GROUPS,
    CONF_VENUE_INSTRUMENT_SEQUENCES,
    VENUE_ALIASES,
    VENUE_ASSET_REQUIREMENTS,
    VENUES,
)

# 切换会场前的资产读取预算: 主界面资产是验资门槛的唯一依据, 读不出就不切换。
VENUE_ASSET_READ_TIMEOUT = 5
# 单次自动切换的总预算: 打开面板 / 等待候选 / 点击目标 / 确认都在这个窗口内完成。
VENUE_SWITCH_TIMEOUT = 20
# 打开「选择会场」按钮后的面板出现等待, 以及点选会场后的界面稳定等待。
VENUE_PANEL_WAIT = 4
VENUE_CONFIRM_WAIT = 5
# 面板与会场文字的轮询节奏。
VENUE_POLL_INTERVAL = 0.4
# 「进入/确认」按钮只在屏幕下半部分找, 避开主界面顶部的功能按钮。
VENUE_CONFIRM_MIN_Y_RATIO = 0.55


def normalize_venue(text: object) -> str | None:
    """把会场文字归一化为 VENUES 中的会场名, 未知时返回 None。

    按别称包含匹配: OCR 只读到「珊瑚」或游戏写成「真珠场」时也能归一到
    「珊瑚场」/「珍珠场」; 空文本返回 None, 不猜。
    """
    compact = str(text or "").strip()
    if not compact:
        return None
    for venue in VENUES:
        if any(alias in compact for alias in VENUE_ALIASES[venue]):
            return venue
    return None


def venue_matches(expected: object, current: object) -> bool:
    """期望会场与当前会场是否一致。

    双方都能归一化时按会场身份比较 (「真珠场」命中「珍珠场」); 有一方是未知
    会场名时退回原来的包含匹配, 保持旧配置「珊瑚」能命中「珊瑚场」的行为。
    """
    expected_raw = str(expected or "").strip()
    current_raw = str(current or "").strip()
    if not expected_raw or not current_raw:
        return False
    expected_venue = normalize_venue(expected_raw)
    current_venue = normalize_venue(current_raw)
    if expected_venue is not None and current_venue is not None:
        return expected_venue == current_venue
    return expected_raw in current_raw


def expected_venue(config: dict) -> str | None:
    """读取「期望会场名」并归一化; 未配置或不是已知会场时返回 None。"""
    return normalize_venue(config.get(CONF_EXPECTED_VENUE, ""))


def asset_requirement(venue: str | None) -> int | None:
    """会场的验资门槛; 未知会场返回 None。"""
    if venue is None:
        return None
    return VENUE_ASSET_REQUIREMENTS.get(venue)


def can_switch_to(venue: str | None, asset: int | None) -> bool:
    """资产是否达到目标会场验资门槛。

    资产未读出(None)时保守返回 False, 不做可能扣入场费的切换; 门槛为 0 的
    海贝场在读到任意非负资产时即可切换。
    """
    requirement = asset_requirement(venue)
    if requirement is None or asset is None:
        return False
    return asset >= requirement


def base_price_key(venue: str | None) -> str | None:
    """当前会场对应的基础价配置键; 未知会场返回 None。"""
    if venue is None:
        return None
    return CONF_VENUE_BASE_PRICES.get(venue)


def instrument_group_key(venue: str | None) -> str | None:
    """当前会场对应的仪器组配置键; 未知会场返回 None。"""
    if venue is None:
        return None
    return CONF_VENUE_INSTRUMENT_GROUPS.get(venue)


def instrument_sequence_key(venue: str | None) -> str | None:
    """当前会场对应的仪器槽位序列配置键; 未知会场返回 None。"""
    if venue is None:
        return None
    return CONF_VENUE_INSTRUMENT_SEQUENCES.get(venue)


def resolve_base_price(config: dict, venue: str | None, default: int = 1) -> int:
    """按当前会场取基础价: 会场专属值 > 0 时用它, 否则用通用「基础价」。

    会场未知或专属值为 0 时走通用值, 老配置的单一「基础价」继续生效;
    专属值非法时由 config_read 静默回退 0, 同样落到通用值。
    """
    key = base_price_key(venue)
    if key is not None:
        value = auction_config_read.read_int(config, key, 0)
        if value > 0:
            return value
    return auction_config_read.read_int(config, CONF_FIXED_PRICE, default)


def ensure_expected_venue(
    task: AuctionVenueOps, boxes: AuctionBoxes, override: str | None = None
) -> bool:
    """每轮匹配前核对期望会场, 资产达到验资门槛时自动切换。

    override 是任务侧会话级锁存的目标会场(满仓无法清理时切海贝场, 见任务侧
    _handle_uncleanable_inventory): 非 None 时优先于用户配置的「期望会场名」,
    本次运行内每轮都把会场拉回目标。归一化失败(空文本)时回退用户配置。

    返回是否发生了切换。当前会场已经是期望会场时只读一次会场文字; 未配置
    期望会场、会场文字读不出、资产读不出或资产不够门槛时都不切换, 其中
    资产不够只写 debug 日志 (无限轮模式下每轮一条 info 会淹没真正的切换记录)。
    切换失败只告警, 不阻断本轮匹配: 基础价会按当前会场取值, 继续跑仍安全。
    """
    current_raw = task._read_current_venue()
    current = normalize_venue(current_raw)
    task._current_venue = current
    if current is not None:
        task.info_set("当前会场", current)

    target = normalize_venue(override) if override else expected_venue(task.config)
    if target is None:
        return False
    if current == target:
        return False
    if not current_raw:
        task.log_debug("当前会场未识别, 跳过本次自动切换")
        return False

    asset = _read_asset(task, boxes)
    requirement = asset_requirement(target)
    if requirement is None:
        return False
    if asset is None:
        task.log_warning(
            f"当前会场「{current_raw}」不是期望的「{target}」, 但资产读取失败, 本次不切换"
        )
        return False
    if not can_switch_to(target, asset):
        task.log_debug(
            f"当前会场「{current_raw}」不是期望的「{target}」, "
            f"资产 {asset} 未达到验资门槛 {requirement}, 留在当前会场匹配"
        )
        return False

    task.log_info(
        f"当前会场「{current_raw}」不是期望的「{target}」, "
        f"资产 {asset} 达到验资门槛 {requirement}, 尝试切换会场"
    )
    task.info_set("当前阶段", "切换会场")
    deadline = time.monotonic() + VENUE_SWITCH_TIMEOUT
    try:
        switched = switch_venue(task, boxes, target, deadline)
    except TaskDisabledException:
        raise
    except Exception as e:
        task._log_aux_error("自动切换会场失败", e)
        switched = False
    finally:
        task.info_set("当前阶段", "匹配中")
    if switched:
        task._current_venue = target
        task.info_set("当前会场", target)
        task.log_info(f"已切换到期望会场「{target}」")
        return True
    # 切换失败也要以面板收起后的真实会场为准: 界面可能其实切过去了, 只是验证帧
    # 读不到; 不刷新的话第一口会用旧会场的基础价.
    current_after = normalize_venue(task._read_current_venue())
    if current_after is not None:
        task._current_venue = current_after
        task.info_set("当前会场", current_after)
    task.log_warning(f"未能切换到期望会场「{target}」, 继续在当前会场匹配")
    return False


def switch_venue(
    task: AuctionVenueOps, boxes: AuctionBoxes, target: str, deadline: float
) -> bool:
    """打开「选择会场」面板并点选目标会场, 返回当前会场是否已变为目标。

    点击目标候选后先等主界面「当前：XXX场」变成目标; 面板若还开着且出现
    「进入/确认」按钮, 再补点一次。失败路径统一由 _close_venue_panel 收起
    面板, 避免它挡住「开始匹配」。会场的最终确认以主界面文字为准, 点击成功
    本身不算切换成功。
    """
    try:
        options = _venue_options(task, boxes)
        target_box = _find_venue_option(options, target)
        if target_box is None:
            if not _click_venue_selector(task, boxes, deadline):
                task.log_warning("未找到可点击的「选择会场」按钮")
                return False
            options = _wait_venue_options(task, boxes, deadline)
            target_box = _find_venue_option(options, target)
        if target_box is None:
            names = " / ".join(
                sorted({str(box.name).strip() for box in options if str(box.name).strip()})
            )
            task.log_warning(
                f"「选择会场」面板里没有找到「{target}」, 当前候选: {names or '未识别'}"
            )
            return False

        task.operate_click(target_box, after_sleep=1)
        if _wait_current_venue(task, target, deadline):
            return True
        if _venue_options(task, boxes):
            confirm = _find_enter_button(task, boxes)
            if confirm is not None:
                task.operate_click(confirm, after_sleep=1)
                if _wait_current_venue(task, target, deadline):
                    return True
        return False
    finally:
        _close_venue_panel(task, boxes)


def _read_asset(task: AuctionVenueOps, boxes: AuctionBoxes) -> int | None:
    """读主界面资产; 失败按 None 返回, 由调用方跳过切换。"""
    try:
        ops = task._reading_ops()
        return ops.asset_once(ops, boxes.main_asset, VENUE_ASSET_READ_TIMEOUT)
    except TaskDisabledException:
        raise
    except Exception as e:
        task._log_aux_error("会场切换前的资产读取失败", e, quiet=True)
        return None


def _click_venue_selector(
    task: AuctionVenueOps, boxes: AuctionBoxes, deadline: float
) -> bool:
    """点击主界面「选择会场」按钮。

    只认 OCR 到「选择会场」按钮的帧: 找不到按钮说明不在主界面或界面还没加载,
    此时不按标定坐标盲点, 交给下一轮重试。
    """
    budget = task._optional_timeout(deadline, VENUE_PANEL_WAIT)
    if budget is None:
        return False
    found = task.wait_ocr(
        box=boxes.venue_select,
        match=RE_VENUE_SELECT,
        time_out=budget,
        raise_if_not_found=False,
        settle_time=0.5,
    )
    if not found:
        return False
    # 点击整个按钮框的中心(圆形图标), 文字只是图标下方的标签, 不一定在点击热区内.
    task.operate_click(boxes.venue_select, after_sleep=0.8)
    return True


def _find_venue_option(options: list, target: str):
    """从会场候选里取目标会场的 OCR 框; 找不到返回 None。"""
    return next((box for box in options if normalize_venue(box.name) == target), None)


def _venue_options(task: AuctionVenueOps, boxes: AuctionBoxes) -> list:
    """读屏幕上的会场候选, 排除主界面已有的当前会场文字与「选择会场」按钮。"""
    found = task.ocr(match=RE_VENUE_OPTION, log=False) or []
    return [
        box
        for box in found
        if not _inside(box, boxes.current_venue) and not _inside(box, boxes.venue_select)
    ]


def _wait_venue_options(
    task: AuctionVenueOps, boxes: AuctionBoxes, deadline: float
) -> list:
    """在预算内等待会场候选出现, 超时返回空列表。"""
    end = min(deadline, time.monotonic() + VENUE_PANEL_WAIT)
    while time.monotonic() < end:
        options = _venue_options(task, boxes)
        if options:
            return options
        task.next_frame()
        remaining = end - time.monotonic()
        if remaining > 0:
            task.sleep(min(VENUE_POLL_INTERVAL, remaining))
    return []


def _wait_current_venue(
    task: AuctionVenueOps, target: str, deadline: float
) -> bool:
    """在预算内等待主界面「当前：XXX场」变成目标会场。"""
    end = min(deadline, time.monotonic() + VENUE_CONFIRM_WAIT)
    while time.monotonic() < end:
        if normalize_venue(task._read_current_venue()) == target:
            return True
        task.next_frame()
        remaining = end - time.monotonic()
        if remaining > 0:
            task.sleep(min(VENUE_POLL_INTERVAL, remaining))
    return False


def _find_enter_button(task: AuctionVenueOps, boxes: AuctionBoxes):
    """在选择面板底部找「进入/确认」按钮; 找不到返回 None。

    只看屏幕下半部分, 并排除「入场费用 / 验资要求」这类说明文字: 它们同样
    包含「入场」, 但点文字不会触发进入。多个候选时取最靠下、最靠右的一个,
    与游戏弹窗主按钮的位置一致。
    """
    found = task.ocr(match=RE_VENUE_ENTER, log=False) or []
    min_y = task.height * VENUE_CONFIRM_MIN_Y_RATIO
    candidates = []
    for box in found:
        name = str(box.name).replace(" ", "")
        if any(word in name for word in ("费用", "门槛", "验资", "要求")):
            continue
        if _inside(box, boxes.match) or _inside(box, boxes.venue_select):
            continue
        if _inside(box, boxes.current_venue):
            continue
        if box.center()[1] >= min_y:
            candidates.append(box)
    if not candidates:
        return None
    return max(candidates, key=lambda box: (box.center()[1], box.center()[0]))


def _close_venue_panel(task: AuctionVenueOps, boxes: AuctionBoxes) -> None:
    """收起仍未关闭的选择面板, 最多两次 ESC。

    这里不看切换预算: 面板留在主界面上会挡住「开始匹配」, 清理失败比多花 1 秒
    严重得多; 两次 ESC 覆盖「确认框 + 选择面板」两层。
    """
    if not _venue_options(task, boxes):
        return
    task.log_info("收起未关闭的「选择会场」面板")
    for _ in range(2):
        task.send_key("esc", after_sleep=0.5)
        if not _venue_options(task, boxes):
            return


def _inside(box, region) -> bool:
    """box 中心是否落在 region 内; 用于排除主界面已有的会场文字与按钮。"""
    center_x, center_y = box.center()
    return (
        region.x <= center_x <= region.x + region.width
        and region.y <= center_y <= region.y + region.height
    )
