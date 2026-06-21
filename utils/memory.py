import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from utils.api_client import LibraryAPIClient
from utils.config import ConfigManager
from utils.console import console, logger
from utils.models import BookingResult, MemoryRecord

HISTORY_FILE = Path("./data/booking_history.json")
HTML_TEMPLATE_FILE = Path("./docs/memory-report-preview.html")
DEFAULT_HTML_OUTPUT = Path("./data/library_memory_report.html")


@dataclass
class MemoryFilters:
    user: str
    year: int | None = None
    date_from: date | None = None
    date_to: date | None = None


@dataclass
class MemorySyncResult:
    fetched_count: int
    imported_count: int
    merged_count: int
    source_label: str


def save_results(results: list[BookingResult]) -> int:
    """保存成功的预约结果到本地历史文件，返回新增记录数。"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load_all_records()
    existing_keys = _build_record_keys(existing)

    new_records: list[MemoryRecord] = []
    imported_count = 0

    for result in results:
        normalized = normalize_booking_result(result)
        if normalized is None:
            continue

        if _record_exists(existing_keys, normalized):
            continue

        imported_count += 1
        new_records.append(normalized)
        _register_record_keys(existing_keys, normalized)

    if not new_records:
        return 0

    merged = merge_records(existing + new_records)
    _write_records(merged)
    return imported_count


async def sync_remote_history(
    user: str,
    include_no_seat: bool = False,
    detail_limit: int | None = 100,
    session_cookies: dict[str, str] | None = None,
) -> MemorySyncResult:
    """从图书馆后台同步真实预约历史。"""
    if not session_cookies:
        raise ValueError(
            "Remote sync requires an existing hdu.huitu.zhishulib.com session "
            "cookie or an exported JSON file. This project does not collect "
            "unified authentication passwords."
        )

    config_manager = ConfigManager()
    async with LibraryAPIClient(
        config_manager,
        session_cookies=session_cookies,
    ) as client:
        client.uid = user

        remote_items = await client.get_booking_history(
            include_no_seat=include_no_seat,
            detail_limit=detail_limit,
        )

    remote_records = normalize_remote_records(remote_items, expected_user=user)
    existing = load_all_records()
    before_keys = _build_record_keys(existing)

    imported_count = 0
    for record in remote_records:
        if _record_exists(before_keys, record):
            continue
        imported_count += 1
        _register_record_keys(before_keys, record)

    merged = merge_records(existing + remote_records)
    _write_records(merged)

    merged_for_user = [record for record in merged if record.user == user]
    return MemorySyncResult(
        fetched_count=len(remote_records),
        imported_count=imported_count,
        merged_count=len(merged_for_user),
        source_label=describe_data_source(merged_for_user),
    )


def normalize_booking_result(result: BookingResult) -> MemoryRecord | None:
    """将预约结果标准化为记忆记录。"""
    if not result.success:
        return None

    begin_dt = parse_booking_datetime(result)
    if begin_dt is None:
        return None

    duration_hours = parse_duration_hours(result)
    if duration_hours <= 0:
        return None

    created_at = datetime.now()
    end_dt = begin_dt + timedelta(hours=duration_hours)
    seat_number = result.seat_number or extract_seat_number(result.seat_info)
    floor_id = result.floor_id
    floor_name = result.floor_name or extract_floor_name(result.seat_info)
    room_name = result.room_name
    record_id = build_record_id(
        source="local",
        user=result.user,
        booking_id=None,
        begin_dt=begin_dt,
        floor_id=floor_id,
        seat_number=seat_number,
        seat_info=result.seat_info,
        duration_hours=duration_hours,
    )

    return MemoryRecord(
        record_id=record_id,
        source="local",
        booking_id=None,
        user=result.user,
        seat_info=result.seat_info,
        room_name=room_name,
        floor_id=floor_id,
        floor_name=floor_name,
        seat_number=seat_number,
        begin_time=begin_dt,
        end_time=end_dt,
        duration_hours=duration_hours,
        status="success",
        created_at=created_at,
        raw=result.model_dump(),
    )


def normalize_remote_records(
    items: list[dict],
    expected_user: str | None = None,
) -> list[MemoryRecord]:
    """将后台预约列表标准化为统一记忆记录。"""
    records: list[MemoryRecord] = []

    for item in items:
        record = normalize_remote_record(item, expected_user=expected_user)
        if record is not None:
            records.append(record)

    return merge_records(records)


def normalize_remote_record(
    item: dict,
    expected_user: str | None = None,
) -> MemoryRecord | None:
    """标准化单条远端预约记录。"""
    if not isinstance(item, dict):
        return None

    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    booking = (
        _pick_mapping(detail.get("booking"))
        or _pick_mapping(item.get("booking"))
        or _pick_mapping(item)
    )
    booking_id = stringify(
        extract_first_value(
            item,
            detail,
            booking,
            keys=["bookingId", "booking_id", "id"],
        )
    )

    begin_dt = parse_datetime_value(
        extract_first_value(
            detail,
            booking,
            item,
            keys=[
                "begin_time",
                "beginTime",
                "start_time",
                "startTime",
                "time",
                "orderTime",
            ],
        )
    )
    if begin_dt is None:
        return None

    duration_hours = parse_duration_value(
        extract_first_value(
            detail,
            booking,
            item,
            keys=["duration", "hours", "use_hours"],
        )
    )
    if duration_hours <= 0:
        return None

    created_at = parse_datetime_value(
        extract_first_value(
            detail,
            booking,
            item,
            keys=[
                "create_time",
                "createTime",
                "created_at",
                "createdAt",
                "orderTime",
                "time",
            ],
        )
    ) or begin_dt

    user = resolve_remote_user(
        item=item,
        detail=detail,
        booking=booking,
        expected_user=expected_user,
    )
    if not user:
        return None

    space = _pick_mapping(booking.get("space")) or _pick_mapping(item.get("space")) or {}
    seat = _pick_mapping(booking.get("seat")) or _pick_mapping(item.get("seat")) or {}

    floor_id = stringify(
        extract_first_value(space, keys=["space_id", "spaceId", "id"])
        or extract_first_value(
            booking,
            item,
            keys=["space_id", "spaceId", "content_id", "contentId"],
        )
    )
    floor_name = stringify(
        extract_first_value(
            space,
            booking,
            item,
            keys=[
                "space",
                "space_name",
                "spaceName",
                "name",
                "title",
                "format",
                "roomName",
            ],
        )
    )
    room_name = stringify(
        extract_first_value(
            item,
            detail,
            space,
            keys=[
                "room_name",
                "roomName",
                "category_name",
                "categoryName",
                "title",
                "format",
            ],
        )
    )
    seat_number = stringify(
        extract_first_value(
            seat,
            booking,
            item,
            keys=[
                "seat",
                "seat_name",
                "seatName",
                "seatNum",
                "name",
                "title",
                "num",
                "number",
            ],
        )
    )
    seat_info = format_seat_info(
        room_name=room_name,
        floor_id=floor_id,
        floor_name=floor_name,
        seat_number=seat_number,
    )
    status = parse_remote_status(item, detail, booking)
    end_time = begin_dt + timedelta(hours=duration_hours)

    record_id = build_record_id(
        source="remote",
        user=user,
        booking_id=booking_id,
        begin_dt=begin_dt,
        floor_id=floor_id,
        seat_number=seat_number,
        seat_info=seat_info,
        duration_hours=duration_hours,
    )

    return MemoryRecord(
        record_id=record_id,
        source="remote",
        booking_id=booking_id,
        user=user,
        seat_info=seat_info,
        room_name=room_name,
        floor_id=floor_id,
        floor_name=floor_name,
        seat_number=seat_number,
        begin_time=begin_dt,
        end_time=end_time,
        duration_hours=duration_hours,
        status=status,
        created_at=created_at,
        raw=item,
    )


def build_record_id(
    source: str,
    user: str,
    booking_id: str | None,
    begin_dt: datetime,
    floor_id: str | None,
    seat_number: str | None,
    seat_info: str,
    duration_hours: float,
) -> str:
    """构造稳定记录 ID。"""
    if booking_id:
        return f"{source}-{user}-booking-{booking_id}"

    seat_key = (
        (seat_number or seat_info).strip()
        .replace(" ", "_")
        .replace(",", "")
        .replace("/", "-")
        .replace("\\", "-")
    )
    duration_key = f"{duration_hours:g}".replace(".", "_")
    floor_key = (floor_id or "unknown").replace(" ", "_")
    return (
        f"{source}-{user}-{begin_dt.strftime('%Y%m%d%H%M')}-"
        f"{floor_key}-{seat_key}-{duration_key}"
    )


def parse_booking_datetime(result: BookingResult) -> datetime | None:
    """从预约结果解析开始时间。"""
    if result.begin_timestamp:
        return datetime.fromtimestamp(result.begin_timestamp)

    if not result.booking_time:
        return None

    try:
        return datetime.strptime(result.booking_time, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def parse_duration_hours(result: BookingResult) -> float:
    """从预约结果解析时长（小时）。"""
    if result.duration_hours is not None:
        return float(result.duration_hours)

    if not result.duration:
        return 0.0

    value = str(result.duration).strip().lower()
    if value.endswith("h"):
        value = value[:-1]

    try:
        return float(value)
    except ValueError:
        return 0.0


def extract_seat_number(seat_info: str) -> str | None:
    """从座位展示文本中提取座位号。"""
    marker = "Seat "
    if marker not in seat_info:
        return None
    return seat_info.split(marker, 1)[1].strip()


def extract_floor_name(seat_info: str) -> str | None:
    """本地草稿兼容：从 seat_info 无法可靠取楼层中文名时返回空。"""
    return None


def load_all_records() -> list[MemoryRecord]:
    """加载全部历史记录。"""
    if not HISTORY_FILE.exists():
        return []

    try:
        with open(HISTORY_FILE, encoding="utf-8") as file:
            raw_data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(raw_data, list):
        return []

    records: list[MemoryRecord] = []
    for item in raw_data:
        record = parse_record(item)
        if record is not None:
            records.append(repair_remote_record_user(record))

    return merge_records(records)


def parse_record(item: dict) -> MemoryRecord | None:
    """兼容旧数据格式并转换为统一记录。"""
    if not isinstance(item, dict):
        return None

    try:
        if "begin_time" in item and "duration_hours" in item:
            return MemoryRecord.model_validate(item)

        booking_time = item.get("booking_time")
        duration = item.get("duration")
        user = item.get("user")
        seat_info = item.get("seat_info")
        if not booking_time or not duration or not user or not seat_info:
            return None

        begin_dt = datetime.strptime(booking_time, "%Y-%m-%d %H:%M")
        duration_hours = float(str(duration).rstrip("h"))
        created_at_text = item.get("recorded_at")
        created_at = (
            datetime.fromisoformat(created_at_text) if created_at_text else begin_dt
        )
        floor_id = stringify(item.get("floor_id"))
        seat_number = stringify(item.get("seat_number")) or extract_seat_number(seat_info)
        record_id = build_record_id(
            source="local",
            user=user,
            booking_id=None,
            begin_dt=begin_dt,
            floor_id=floor_id,
            seat_number=seat_number,
            seat_info=seat_info,
            duration_hours=duration_hours,
        )
        return MemoryRecord(
            record_id=record_id,
            source=item.get("source", "local"),
            booking_id=stringify(item.get("booking_id")),
            user=user,
            seat_info=seat_info,
            room_name=stringify(item.get("room_name")),
            floor_id=floor_id,
            floor_name=stringify(item.get("floor_name")),
            seat_number=seat_number,
            begin_time=begin_dt,
            end_time=begin_dt + timedelta(hours=duration_hours),
            duration_hours=duration_hours,
            status=item.get("status", "success"),
            created_at=created_at,
            raw=item.get("raw", item),
        )
    except (ValueError, TypeError):
        return None


def repair_remote_record_user(record: MemoryRecord) -> MemoryRecord:
    """Repair older remote records saved with the platform's internal uid."""
    if record.source != "remote" or not isinstance(record.raw, dict):
        return record

    detail = _pick_mapping(record.raw.get("detail"))
    booking = (
        _pick_mapping(detail.get("booking"))
        or _pick_mapping(record.raw.get("booking"))
        or _pick_mapping(record.raw)
    )
    resolved_user = resolve_remote_user(
        item=record.raw,
        detail=detail,
        booking=booking,
        expected_user=None,
    )
    if not resolved_user or resolved_user == record.user:
        return record

    record_id = build_record_id(
        source=record.source,
        user=resolved_user,
        booking_id=record.booking_id,
        begin_dt=record.begin_time,
        floor_id=record.floor_id,
        seat_number=record.seat_number,
        seat_info=record.seat_info,
        duration_hours=record.duration_hours,
    )
    return record.model_copy(update={"user": resolved_user, "record_id": record_id})


def sort_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    return sorted(records, key=lambda record: (record.begin_time, record.record_id))


def merge_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """按 bookingId 或会话指纹去重，优先保留远端且字段更完整的记录。"""
    merged: list[MemoryRecord] = []
    booking_map: dict[str, int] = {}
    session_map: dict[str, int] = {}

    for record in sort_records(records):
        booking_key = _build_booking_key(record)
        session_key = _build_session_key(record)
        index = None

        if booking_key and booking_key in booking_map:
            index = booking_map[booking_key]
        elif session_key in session_map:
            index = session_map[session_key]

        if index is None:
            merged.append(record)
            new_index = len(merged) - 1
            if booking_key:
                booking_map[booking_key] = new_index
            session_map[session_key] = new_index
            continue

        existing = merged[index]
        chosen = merge_record_pair(existing, record)
        merged[index] = chosen

        chosen_booking_key = _build_booking_key(chosen)
        chosen_session_key = _build_session_key(chosen)
        if chosen_booking_key:
            booking_map[chosen_booking_key] = index
        session_map[chosen_session_key] = index

    return sort_records(merged)


def merge_record_pair(left: MemoryRecord, right: MemoryRecord) -> MemoryRecord:
    """合并两条可能重复的记录。"""
    preferred = choose_preferred_record(left, right)
    other = right if preferred is left else left

    payload = preferred.model_dump()
    payload["record_id"] = preferred.record_id
    payload["source"] = "remote" if {left.source, right.source} == {"remote", "local"} else preferred.source
    payload["booking_id"] = preferred.booking_id or other.booking_id
    payload["seat_info"] = _choose_string(preferred.seat_info, other.seat_info) or preferred.seat_info
    payload["room_name"] = _choose_string(preferred.room_name, other.room_name)
    payload["floor_id"] = _choose_string(preferred.floor_id, other.floor_id)
    payload["floor_name"] = _choose_string(preferred.floor_name, other.floor_name)
    payload["seat_number"] = _choose_string(preferred.seat_number, other.seat_number)
    payload["end_time"] = preferred.end_time or other.end_time
    payload["status"] = preferred.status or other.status
    payload["created_at"] = min(left.created_at, right.created_at)
    payload["raw"] = preferred.raw or other.raw
    return MemoryRecord.model_validate(payload)


def choose_preferred_record(left: MemoryRecord, right: MemoryRecord) -> MemoryRecord:
    """选择更适合作为主记录的一条。"""
    left_score = _record_score(left)
    right_score = _record_score(right)
    if right_score > left_score:
        return right
    return left


def _record_score(record: MemoryRecord) -> tuple[int, int, int]:
    completeness = sum(
        1
        for value in [
            record.booking_id,
            record.room_name,
            record.floor_id,
            record.floor_name,
            record.seat_number,
            record.end_time,
        ]
        if value
    )
    source_score = 1 if record.source == "remote" else 0
    status_score = 1 if record.status == "success" else 0
    return source_score, completeness, status_score


def _write_records(records: list[MemoryRecord]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = [record.model_dump(mode="json") for record in sort_records(records)]
    with open(HISTORY_FILE, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def load_records(filters: MemoryFilters) -> list[MemoryRecord]:
    """按用户和时间范围筛选记录。"""
    records = [record for record in load_all_records() if record.user == filters.user]
    filtered: list[MemoryRecord] = []

    for record in records:
        record_date = record.begin_time.date()

        if filters.year is not None and record.begin_time.year != filters.year:
            continue
        if filters.date_from is not None and record_date < filters.date_from:
            continue
        if filters.date_to is not None and record_date > filters.date_to:
            continue

        filtered.append(record)

    return sort_records(filtered)


def compute_stats(records: list[MemoryRecord]) -> dict | None:
    """计算图书馆记忆统计。"""
    valid_records = [record for record in records if record.status == "success"]
    if not valid_records:
        return None

    total_sessions = len(valid_records)
    day_buckets = compute_merged_hours_by_day(valid_records)
    total_hours = round(sum(day_buckets.values()), 2)

    favorite_seat = _pick_top_value(record.seat_info for record in valid_records)
    favorite_floor = _pick_top_value(
        record.floor_name or record.floor_id or "未知楼层" for record in valid_records
    )
    favorite_room = _pick_top_value(
        record.room_name or "未知空间" for record in valid_records
    )

    period_counts = Counter(
        classify_period(record.begin_time.hour) for record in valid_records
    )
    favorite_period = _pick_top_counter(period_counts)

    visit_dates = sorted({record.begin_time.date() for record in valid_records})
    first_date = visit_dates[0]
    last_date = visit_dates[-1]
    max_streak = compute_streak(visit_dates)

    month_buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sessions": 0, "hours": 0.0}
    )
    floor_buckets: Counter[str] = Counter()

    for record in valid_records:
        month_key = record.begin_time.strftime("%Y-%m")
        month_buckets[month_key]["sessions"] += 1

        floor_key = record.floor_name or record.floor_id or "未知楼层"
        floor_buckets[floor_key] += 1

    for day, hours in day_buckets.items():
        month_key = day.strftime("%Y-%m")
        month_buckets[month_key]["hours"] += hours

    most_active_month = _pick_top_counter(
        Counter(
            {
                month: int(values["sessions"])
                for month, values in month_buckets.items()
            }
        )
    )
    longest_day_date, longest_day_hours = max(
        day_buckets.items(), key=lambda item: (item[1], item[0])
    )

    seat_counter = Counter(record.seat_info for record in valid_records)
    favorite_seat_count = seat_counter.get(favorite_seat, 0)

    return {
        "summary": {
            "total_sessions": total_sessions,
            "total_hours": total_hours,
            "days_equivalent": round(total_hours / 24, 2),
            "first_date": first_date.isoformat(),
            "last_date": last_date.isoformat(),
            "favorite_seat": favorite_seat,
            "favorite_seat_count": favorite_seat_count,
            "favorite_floor": favorite_floor,
            "favorite_room": favorite_room,
            "favorite_period": favorite_period,
            "max_streak": max_streak,
            "most_active_month": most_active_month,
            "longest_day_date": longest_day_date.isoformat(),
            "longest_day_hours": round(longest_day_hours, 2),
        },
        "timeline": {
            "months": [
                {
                    "label": month,
                    "sessions": int(values["sessions"]),
                    "hours": round(values["hours"], 2),
                }
                for month, values in sorted(month_buckets.items())
            ],
            "milestones": build_milestones(
                first_date=first_date,
                last_date=last_date,
                max_streak=max_streak,
                most_active_month=most_active_month,
                longest_day_date=longest_day_date,
                longest_day_hours=longest_day_hours,
            ),
        },
        "habits": {
            "periods": build_period_breakdown(period_counts),
            "floors": build_floor_breakdown(floor_buckets, total_sessions),
        },
    }


def _pick_top_value(values) -> str:
    return _pick_top_counter(Counter(values))


def _pick_top_counter(counter: Counter) -> str:
    if not counter:
        return "N/A"
    return sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))[0][0]


def classify_period(hour: int) -> str:
    if 6 <= hour <= 11:
        return "早晨"
    if 12 <= hour <= 17:
        return "下午"
    if 18 <= hour <= 23:
        return "晚上"
    return "凌晨"


def compute_streak(dates: list[date]) -> int:
    if not dates:
        return 0

    streak = 1
    max_streak = 1
    for index in range(1, len(dates)):
        if (dates[index] - dates[index - 1]).days == 1:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 1
    return max_streak


def compute_merged_hours_by_day(records: list[MemoryRecord]) -> dict[date, float]:
    day_intervals: dict[date, list[tuple[datetime, datetime]]] = defaultdict(list)
    for record in records:
        for day, start, end in iter_daily_intervals(record):
            day_intervals[day].append((start, end))

    return {
        day: round(compute_merged_interval_hours(intervals), 2)
        for day, intervals in day_intervals.items()
    }


def iter_daily_intervals(record: MemoryRecord):
    start = record.begin_time
    end = record.end_time or start + timedelta(hours=record.duration_hours)
    if end <= start:
        return

    cursor = start
    while cursor.date() < end.date():
        day_end = datetime.combine(
            cursor.date() + timedelta(days=1),
            datetime.min.time(),
        )
        yield cursor.date(), cursor, day_end
        cursor = day_end

    yield cursor.date(), cursor, end


def compute_merged_interval_hours(
    intervals: list[tuple[datetime, datetime]],
) -> float:
    if not intervals:
        return 0.0

    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals, key=lambda item: item[0]):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue

        previous_start, previous_end = merged[-1]
        if end > previous_end:
            merged[-1] = (previous_start, end)

    seconds = sum((end - start).total_seconds() for start, end in merged)
    return seconds / 3600


def build_period_breakdown(period_counts: Counter[str]) -> list[dict]:
    descriptions = {
        "早晨": "更偏向清醒启动型学习，通常节奏直接而明确。",
        "下午": "偏向持续推进型学习，适合作为稳定补强时段。",
        "晚上": "是你最常出现的时间窗口，夜间节奏最明显。",
        "凌晨": "偶发但存在，通常代表某些压力更高的节点。",
    }
    colors = {
        "早晨": "linear-gradient(90deg, #ffc774, #ff9e6a)",
        "下午": "linear-gradient(90deg, #7cffc1, #5ee1aa)",
        "晚上": "linear-gradient(90deg, #73d8ff, #4b7cff)",
        "凌晨": "linear-gradient(90deg, #ff8cc8, #b07cff)",
    }
    order = ["早晨", "下午", "晚上", "凌晨"]
    return [
        {
            "label": period,
            "count": period_counts.get(period, 0),
            "detail": descriptions[period],
            "color": colors[period],
        }
        for period in order
    ]


def build_floor_breakdown(
    floor_buckets: Counter[str], total_sessions: int
) -> list[dict]:
    rows = []
    for floor_name, sessions in floor_buckets.most_common():
        ratio = round((sessions / total_sessions) * 100, 2) if total_sessions else 0
        rows.append(
            {
                "name": floor_name,
                "sessions": sessions,
                "ratio": ratio,
            }
        )
    return rows


def build_spatial_floor_distribution(
    records: list[MemoryRecord],
    total_sessions: int,
) -> list[dict]:
    floor_counts: Counter[str] = Counter()
    floor_hours: dict[str, float] = defaultdict(float)

    for record in records:
        floor_name = record.floor_name or record.floor_id or "未知楼层"
        floor_counts[floor_name] += 1
        floor_hours[floor_name] += record.duration_hours

    max_sessions = max(floor_counts.values(), default=1)
    rows = []
    for rank, (floor_name, sessions) in enumerate(floor_counts.most_common(), start=1):
        ratio = round((sessions / total_sessions) * 100, 2) if total_sessions else 0
        rows.append(
            {
                "rank": rank,
                "name": floor_name,
                "sessions": sessions,
                "hours": round(floor_hours[floor_name], 2),
                "ratio": ratio,
                "level": round(sessions / max_sessions, 3) if max_sessions else 0,
            }
        )
    return rows


def build_territory_roast(
    coverage_percent: int,
    floor_distribution: list[dict],
    favorite_seat_count: int,
    total_sessions: int,
) -> str:
    floor_count = len(floor_distribution)
    top_floor = floor_distribution[0] if floor_distribution else {}
    top_floor_name = top_floor.get("name", "主战场")
    top_floor_ratio = float(top_floor.get("ratio", 0))
    seat_ratio = favorite_seat_count / total_sessions if total_sessions else 0

    if floor_count >= 10 and coverage_percent >= 90:
        return (
            f"你这不是找座位，是给全馆做压力测试。{floor_count} 个楼层都留下记录，"
            f"最后还是在【{top_floor_name}】反复回档，主打一个又野又专一。"
        )
    if top_floor_ratio >= 45:
        return (
            f"嘴上说全馆都行，身体很诚实地把【{top_floor_name}】设成默认出生点。"
            "这不是领地意识，这是楼层户口本。"
        )
    if seat_ratio >= 0.2:
        return (
            "你对座位的感情已经超过正常同学关系。别人叫预约，你这更像每天回家打卡。"
        )
    if floor_count >= 7:
        return (
            f"{floor_count} 个楼层来回切换，像是在图书馆里跑支线任务。"
            f"但最高频楼层还是【{top_floor_name}】，系统已经识别你的主基地。"
        )
    return (
        f"活动范围不算夸张，但【{top_floor_name}】存在感很强。"
        "图书馆地图不用打开，你的路线已经形成肌肉记忆。"
    )


def build_milestones(
    first_date: date,
    last_date: date,
    max_streak: int,
    most_active_month: str,
    longest_day_date: date,
    longest_day_hours: float,
) -> list[dict]:
    return [
        {
            "date": first_date.isoformat(),
            "title": "第一次出现在图书馆记忆里",
            "copy": "从这一天开始，你的学习轨迹在这里被正式记录下来。",
        },
        {
            "date": most_active_month,
            "title": "最活跃月份",
            "copy": "这一月是整段记录里最密集的阶段，说明你的学习投入明显提升。",
        },
        {
            "date": longest_day_date.isoformat(),
            "title": "单日最长学习时长",
            "copy": f"这一天累计达到 {longest_day_hours:g} 小时，是整段时间里的强度峰值。",
        },
        {
            "date": last_date.isoformat(),
            "title": "最近一次到访",
            "copy": f"当前已记录的最长连续打卡为 {max_streak} 天，这段节奏值得被保留下来。",
        },
    ]


def build_h5_contract(
    records: list[MemoryRecord],
    summary: dict,
    habits: dict,
    timeline: dict,
    filters: MemoryFilters,
) -> dict:
    valid_records = [record for record in records if record.status == "success"]
    total_sessions = summary["total_sessions"]
    total_hours = summary["total_hours"]
    total_days = len({record.begin_time.date() for record in valid_records})
    low_frequency = total_sessions < 20
    yearly = build_yearly_comparison(valid_records)
    yearly_observations = build_yearly_month_observations(valid_records)
    monthly_heatmap = build_monthly_heatmap(valid_records)
    semester_stats = build_semester_stats(valid_records)
    trend_type = classify_trend([item["value"] for item in yearly])
    coverage_percent = estimate_coverage_percent(valid_records)
    floor_distribution = build_spatial_floor_distribution(
        valid_records,
        total_sessions,
    )
    favorite_seat_ratio = (
        summary["favorite_seat_count"] / total_sessions if total_sessions else 0
    )
    early_start_sessions = sum(
        1
        for record in valid_records
        if record.begin_time.hour < 8
        or (record.begin_time.hour == 8 and record.begin_time.minute <= 30)
    )
    morning_sessions = sum(
        1 for record in valid_records if 5 <= record.begin_time.hour < 11
    )
    late_checkout_sessions = sum(
        1
        for record in valid_records
        if get_record_end(record).hour >= 21
    )
    evening_sessions = sum(
        1 for record in valid_records if record.begin_time.hour >= 18
    )
    meme_title, title_tag, sharp_evaluation = classify_meme_profile(
        total_sessions=total_sessions,
        total_days=total_days,
        total_hours=total_hours,
    )
    extra_tags = build_extra_tags(
        favorite_period=summary["favorite_period"],
        favorite_seat_ratio=favorite_seat_ratio,
        coverage_percent=coverage_percent,
        trend_type=trend_type,
        total_sessions=total_sessions,
        total_hours=total_hours,
        early_start_sessions=early_start_sessions,
        morning_sessions=morning_sessions,
        late_checkout_sessions=late_checkout_sessions,
        evening_sessions=evening_sessions,
    )
    first_record = valid_records[0] if valid_records else None

    return {
        "user_profile": {
            "name": "HDU 同学",
            "student_id": filters.user,
            "student_id_mask": mask_student_id(filters.user),
        },
        "meme_stats": {
            "title": meme_title,
            "title_tag": title_tag,
            "sharp_evaluation": sharp_evaluation,
            "extra_tags": extra_tags,
            "habit_signals": {
                "early_start_sessions": early_start_sessions,
                "morning_sessions": morning_sessions,
                "late_checkout_sessions": late_checkout_sessions,
                "evening_sessions": evening_sessions,
            },
            "trend_type": trend_type,
            "defeat_percentage": estimate_defeat_percentage(
                total_sessions, total_hours, low_frequency
            ),
            "share_title": (
                f"经图书馆官方鉴定，我的成分是【{meme_title}】，"
                "来看看你的绝密报告？"
            ),
        },
        "global_stats": {
            "total_days": total_days,
            "total_hours": total_hours,
            "total_sessions": total_sessions,
            "first_booking": {
                "date": first_record.begin_time.date().isoformat()
                if first_record
                else summary["first_date"],
                "seat_code": first_record.seat_info
                if first_record
                else summary["favorite_seat"],
            },
            "is_low_frequency": low_frequency,
        },
        "spatial_stats": {
            "most_loved_seat": summary["favorite_seat"],
            "coverage_percent": coverage_percent,
            "seat_meme": classify_seat_meme(favorite_seat_ratio, coverage_percent),
            "favorite_seat_count": summary["favorite_seat_count"],
            "favorite_floor": summary["favorite_floor"],
            "visited_floor_count": len(floor_distribution),
            "favorite_floor_ratio": floor_distribution[0]["ratio"]
            if floor_distribution
            else 0,
            "floor_distribution": floor_distribution,
            "territory_roast": build_territory_roast(
                coverage_percent=coverage_percent,
                floor_distribution=floor_distribution,
                favorite_seat_count=summary["favorite_seat_count"],
                total_sessions=total_sessions,
            ),
        },
        "temporal_stats": {
            "peak_time": find_peak_time(valid_records),
            "earliest_time": find_earliest_time(valid_records),
            "latest_time": find_latest_time(valid_records),
            "yearly_comparison": [item["value"] for item in yearly],
            "year_labels": [item["label"] for item in yearly],
            "yearly_observations": yearly_observations,
            "monthly_heatmap": monthly_heatmap,
            "top_months": build_top_months(monthly_heatmap),
            "periods": habits["periods"],
            "exam_pulse": build_exam_pulse_stats(valid_records),
        },
        "semester_stats": semester_stats,
        "low_frequency_copy": build_low_frequency_copy(summary)
        if low_frequency
        else "",
    }


def mask_student_id(value: str) -> str:
    if len(value) <= 4:
        return value
    if len(value) <= 8:
        return f"{value[:2]}****{value[-2:]}"
    return f"{value[:4]}****{value[-3:]}"


def build_yearly_comparison(records: list[MemoryRecord]) -> list[dict]:
    buckets = Counter(record.begin_time.year for record in records)
    if not buckets:
        return []

    years = sorted(buckets)
    if len(years) > 4:
        years = years[-4:]

    return [{"label": str(year), "value": buckets[year]} for year in years]


def build_yearly_month_observations(records: list[MemoryRecord]) -> list[dict]:
    if not records:
        return []

    day_hours = compute_merged_hours_by_day(records)
    month_hours: dict[tuple[int, int], float] = defaultdict(float)
    for day, hours in day_hours.items():
        month_hours[(day.year, day.month)] += hours

    years = sorted({record.begin_time.year for record in records})
    if len(years) > 4:
        years = years[-4:]

    observations: list[dict] = []
    for year in years:
        yearly_records = [record for record in records if record.begin_time.year == year]
        if not yearly_records:
            continue

        month_sessions = Counter(record.begin_time.month for record in yearly_records)
        peak_month, peak_sessions = sorted(
            month_sessions.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        total_sessions = len(yearly_records)
        total_hours = round(
            sum(hours for (item_year, _), hours in month_hours.items() if item_year == year),
            2,
        )
        active_days = len({record.begin_time.date() for record in yearly_records})
        floor = _pick_top_value(
            record.floor_name or record.floor_id or "未知楼层"
            for record in yearly_records
        )
        seat = _pick_top_value(record.seat_info for record in yearly_records)
        peak_hours = round(month_hours.get((year, peak_month), 0.0), 2)
        peak_ratio = round(peak_sessions / total_sessions * 100, 1)

        observations.append(
            {
                "year": str(year),
                "sessions": total_sessions,
                "hours": total_hours,
                "active_days": active_days,
                "peak_month": f"{peak_month:02d}",
                "peak_month_label": f"{peak_month}月",
                "peak_month_sessions": peak_sessions,
                "peak_month_hours": peak_hours,
                "peak_month_ratio": peak_ratio,
                "favorite_floor": floor,
                "favorite_seat": seat,
                "roast": build_year_roast(
                    year=year,
                    peak_month=peak_month,
                    peak_sessions=peak_sessions,
                    total_sessions=total_sessions,
                ),
            }
        )

    return observations


def build_monthly_heatmap(records: list[MemoryRecord]) -> list[dict]:
    if not records:
        return []

    day_hours = compute_merged_hours_by_day(records)
    month_hours: dict[tuple[int, int], float] = defaultdict(float)
    for day, hours in day_hours.items():
        month_hours[(day.year, day.month)] += hours

    month_sessions = Counter(
        (record.begin_time.year, record.begin_time.month) for record in records
    )
    max_sessions = max(month_sessions.values(), default=1)
    years = sorted({record.begin_time.year for record in records})
    if len(years) > 4:
        years = years[-4:]

    rows: list[dict] = []
    for year in years:
        months = []
        for month in range(1, 13):
            sessions = month_sessions.get((year, month), 0)
            hours = round(month_hours.get((year, month), 0.0), 2)
            months.append(
                {
                    "month": f"{month:02d}",
                    "label": f"{month}月",
                    "sessions": sessions,
                    "hours": hours,
                    "level": round(sessions / max_sessions, 3)
                    if max_sessions
                    else 0,
                }
            )
        rows.append({"year": str(year), "months": months})

    return rows


def build_top_months(monthly_heatmap: list[dict]) -> list[dict]:
    months = [
        {"year": row["year"], **month}
        for row in monthly_heatmap
        for month in row["months"]
        if month["sessions"] > 0
    ]
    return sorted(
        months,
        key=lambda item: (-item["sessions"], -item["hours"], item["year"], item["month"]),
    )[:5]


SEMESTER_LABELS = [
    "大一上",
    "大一下",
    "大二上",
    "大二下",
    "大三上",
    "大三下",
    "大四上",
    "大四下",
]


FINAL_SEASON_MONTHS = {1, 6, 12}


def build_exam_pulse_stats(records: list[MemoryRecord]) -> dict:
    if not records:
        return {
            "alert_level": "样本不足",
            "copy": "期末季样本不足，暂时无法生成心电图。",
            "finals_sessions": 0,
            "finals_hours": 0,
            "finals_ratio": 0,
            "active_days": 0,
            "early_sessions": 0,
            "late_sessions": 0,
            "hourly_distribution": [],
            "checkout_hourly_distribution": [],
            "stress_months": [],
            "strongest_month": None,
            "strongest_day": None,
            "checkout_peak_hour": "--:--",
            "checkout_roast": "这段记录太轻，暂时看不出明显签退习惯。",
            "month_roast": "这段记录太轻，期末月份暂时没有明显波峰。",
            "roast": "这段记录太轻，心电图暂时没有明显波形。",
        }

    day_hours = compute_merged_hours_by_day(records)
    month_hours: dict[tuple[int, int], float] = defaultdict(float)
    for day, hours in day_hours.items():
        month_hours[(day.year, day.month)] += hours

    finals_records = [
        record for record in records if record.begin_time.month in FINAL_SEASON_MONTHS
    ]
    pulse_records = finals_records or records
    hourly_counter = Counter(record.begin_time.hour for record in pulse_records)
    checkout_hourly_counter = Counter(get_record_end(record).hour for record in pulse_records)
    max_hourly = max(hourly_counter.values(), default=1)
    max_checkout_hourly = max(checkout_hourly_counter.values(), default=1)
    hourly_distribution = [
        {
            "hour": hour,
            "label": f"{hour:02d}:00",
            "sessions": hourly_counter.get(hour, 0),
            "level": round(hourly_counter.get(hour, 0) / max_hourly, 3)
            if max_hourly
            else 0,
        }
        for hour in range(24)
    ]
    checkout_hourly_distribution = [
        {
            "hour": hour,
            "label": f"{hour:02d}:00",
            "sessions": checkout_hourly_counter.get(hour, 0),
            "level": round(checkout_hourly_counter.get(hour, 0) / max_checkout_hourly, 3)
            if max_checkout_hourly
            else 0,
        }
        for hour in range(24)
    ]

    final_month_records: dict[tuple[int, int], list[MemoryRecord]] = defaultdict(list)
    for record in finals_records:
        final_month_records[(record.begin_time.year, record.begin_time.month)].append(
            record
        )

    max_month_sessions = max(
        (len(items) for items in final_month_records.values()),
        default=1,
    )
    stress_months = []
    for (year, month), month_records in final_month_records.items():
        active_days = len({record.begin_time.date() for record in month_records})
        sessions = len(month_records)
        hours = round(month_hours.get((year, month), 0.0), 2)
        stress_months.append(
            {
                "key": f"{year}-{month:02d}",
                "label": f"{year}.{month:02d}",
                "sessions": sessions,
                "hours": hours,
                "active_days": active_days,
                "level": round(sessions / max_month_sessions, 3)
                if max_month_sessions
                else 0,
            }
        )
    stress_months = sorted(
        stress_months,
        key=lambda item: (-item["sessions"], -item["hours"], item["key"]),
    )[:5]

    finals_sessions = len(finals_records)
    finals_hours = round(
        sum(
            hours
            for day, hours in day_hours.items()
            if day.month in FINAL_SEASON_MONTHS
        ),
        2,
    )
    finals_ratio = round(finals_sessions / len(records) * 100, 1)
    active_days = len({record.begin_time.date() for record in finals_records})
    early_sessions = sum(1 for record in finals_records if record.begin_time.hour < 8)
    late_sessions = sum(1 for record in finals_records if get_record_end(record).hour >= 22)
    final_day_counts = Counter(record.begin_time.date() for record in finals_records)
    strongest_day_date, strongest_day_sessions = (
        max(final_day_counts.items(), key=lambda item: (item[1], item[0]))
        if final_day_counts
        else (None, 0)
    )
    strongest_day = (
        {
            "date": strongest_day_date.isoformat(),
            "sessions": strongest_day_sessions,
            "hours": round(day_hours.get(strongest_day_date, 0.0), 2),
        }
        if strongest_day_date
        else None
    )
    peak_hour = sorted(
        hourly_counter.items(),
        key=lambda item: (-item[1], item[0]),
    )[0][0]
    checkout_peak_hour = sorted(
        checkout_hourly_counter.items(),
        key=lambda item: (-item[1], item[0]),
    )[0][0]
    strongest_month = stress_months[0] if stress_months else None

    return {
        "alert_level": classify_exam_alert(finals_sessions, finals_ratio),
        "copy": build_exam_copy(
            finals_sessions=finals_sessions,
            finals_ratio=finals_ratio,
            strongest_month=strongest_month,
            peak_hour=peak_hour,
            checkout_peak_hour=checkout_peak_hour,
        ),
        "finals_sessions": finals_sessions,
        "finals_hours": finals_hours,
        "finals_ratio": finals_ratio,
        "active_days": active_days,
        "early_sessions": early_sessions,
        "late_sessions": late_sessions,
        "peak_hour": f"{peak_hour:02d}:00",
        "checkout_peak_hour": f"{checkout_peak_hour:02d}:00",
        "hourly_distribution": hourly_distribution,
        "checkout_hourly_distribution": checkout_hourly_distribution,
        "stress_months": stress_months,
        "strongest_month": strongest_month,
        "strongest_day": strongest_day,
        "checkout_roast": build_checkout_roast(
            finals_sessions=finals_sessions,
            finals_ratio=finals_ratio,
            late_sessions=late_sessions,
            early_sessions=early_sessions,
            checkout_peak_hour=checkout_peak_hour,
        ),
        "month_roast": build_exam_roast(
            finals_sessions=finals_sessions,
            finals_ratio=finals_ratio,
            late_sessions=late_sessions,
            early_sessions=early_sessions,
            strongest_month=strongest_month,
        ),
        "roast": build_exam_roast(
            finals_sessions=finals_sessions,
            finals_ratio=finals_ratio,
            late_sessions=late_sessions,
            early_sessions=early_sessions,
            strongest_month=strongest_month,
        ),
    }


def get_record_end(record: MemoryRecord) -> datetime:
    return record.end_time or record.begin_time + timedelta(hours=record.duration_hours)


def classify_exam_alert(finals_sessions: int, finals_ratio: float) -> str:
    if finals_sessions >= 180 or finals_ratio >= 35:
        return "红色预警"
    if finals_sessions >= 90 or finals_ratio >= 24:
        return "橙色预警"
    if finals_sessions >= 30:
        return "黄色预警"
    return "低压观测"


def build_exam_copy(
    finals_sessions: int,
    finals_ratio: float,
    strongest_month: dict | None,
    peak_hour: int,
    checkout_peak_hour: int,
) -> str:
    if not strongest_month:
        return "期末季样本不多，签退分布暂时没有明显波峰。"
    return (
        f"期末季共 {finals_sessions} 次预约，占全部记录 {finals_ratio:g}%；"
        f"最强波峰落在 {strongest_month['label']}，高频启动时间 {peak_hour:02d}:00，"
        f"高频签退时间 {checkout_peak_hour:02d}:00。"
    )


def build_checkout_roast(
    finals_sessions: int,
    finals_ratio: float,
    late_sessions: int,
    early_sessions: int,
    checkout_peak_hour: int,
) -> str:
    if finals_sessions < 12:
        return "期末季签退样本偏少，暂时只能算轻微波动。"
    if late_sessions >= max(12, finals_sessions * 0.25) or checkout_peak_hour >= 21:
        return "签退高峰压到闭馆线附近，保安看监控都能把你的撤退路线背下来。"
    if early_sessions >= max(8, finals_sessions * 0.18):
        return "这条线更像早起抢跑型：别人还在加载，你已经完成一轮进馆。"
    if finals_ratio >= 24:
        return "期末季签退曲线明显抬头：不是路过图书馆，是把一天的尾巴留在了馆里。"
    return "签退分布不算失控，但期末季的停留痕迹已经开始变重。"


def build_exam_roast(
    finals_sessions: int,
    finals_ratio: float,
    late_sessions: int,
    early_sessions: int,
    strongest_month: dict | None,
) -> str:
    if not strongest_month:
        return "期末季没有形成明显尖峰，可能你把焦虑分期付款了。"
    if finals_sessions >= 180 or finals_ratio >= 35:
        return (
            f"{strongest_month['label']} 这根尖峰很不客气："
            "别人复习是临时抱佛脚，你更像直接把佛脚借走开了自习室。"
        )
    if late_sessions >= max(12, finals_sessions * 0.25):
        return (
            f"{strongest_month['label']} 的压力峰值很明显："
            "不是每个月都这么疯，但这个月确实像把复习债集中还款。"
        )
    if early_sessions >= max(8, finals_sessions * 0.18):
        return "期末季还在早起抢跑，属于早八特种兵的高压进化形态。"
    return "波形不算失控，但期末季明显抬头：你说没焦虑，数据说先别急着狡辩。"


def build_semester_stats(records: list[MemoryRecord]) -> dict:
    if not records:
        return {
            "cohort_year": None,
            "semesters": [],
            "archive_copy": "有效记录不足，暂时无法生成学期编年史。",
            "strongest_semester": None,
            "crossroads": {},
        }

    sorted_records = sorted(records, key=lambda record: record.begin_time)
    cohort_year = infer_academic_start_year(sorted_records[0].begin_time.date())
    records_by_semester: dict[int, list[MemoryRecord]] = defaultdict(list)

    for record in sorted_records:
        index = get_academic_semester_index(record.begin_time.date(), cohort_year)
        if 0 <= index < len(SEMESTER_LABELS):
            records_by_semester[index].append(record)

    day_hours = compute_merged_hours_by_day(sorted_records)
    semester_day_hours: dict[int, float] = defaultdict(float)
    month_hours: dict[tuple[int, str], float] = defaultdict(float)

    for day, hours in day_hours.items():
        index = get_academic_semester_index(day, cohort_year)
        if 0 <= index < len(SEMESTER_LABELS):
            semester_day_hours[index] += hours
            month_hours[(index, day.strftime("%Y-%m"))] += hours

    semesters = [
        build_semester_entry(
            index=index,
            cohort_year=cohort_year,
            records=records_by_semester[index],
            total_hours=semester_day_hours.get(index, 0.0),
            month_hours=month_hours,
        )
        for index in range(len(SEMESTER_LABELS))
    ]
    non_empty = [item for item in semesters if item["sessions"] > 0]
    strongest = max(
        non_empty,
        key=lambda item: (item["sessions"], item["hours"], item["active_days"]),
        default=None,
    )

    return {
        "cohort_year": cohort_year,
        "semesters": semesters,
        "archive_copy": build_semester_archive_copy(strongest),
        "strongest_semester": strongest,
        "crossroads": build_crossroads_stats(semesters),
    }


def infer_academic_start_year(first_day: date) -> int:
    if first_day.month >= 8:
        return first_day.year
    return first_day.year - 1


def get_academic_semester_index(day: date, cohort_year: int) -> int:
    if day.month >= 8:
        return (day.year - cohort_year) * 2
    if day.month == 1:
        return (day.year - cohort_year - 1) * 2
    return (day.year - cohort_year - 1) * 2 + 1


def build_semester_entry(
    index: int,
    cohort_year: int,
    records: list[MemoryRecord],
    total_hours: float,
    month_hours: dict[tuple[int, str], float],
) -> dict:
    start_date, end_date = get_semester_bounds(cohort_year, index)
    sessions = len(records)
    hours = round(total_hours, 2)
    active_days = len({record.begin_time.date() for record in records})
    month_sessions = Counter(record.begin_time.strftime("%Y-%m") for record in records)
    period_counts = Counter(classify_period(record.begin_time.hour) for record in records)
    peak_month = ""
    peak_month_label = "暂无"
    peak_month_sessions = 0
    peak_month_hours = 0.0

    if month_sessions:
        peak_month, peak_month_sessions = sorted(
            month_sessions.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        peak_month_label = format_year_month_label(peak_month)
        peak_month_hours = round(month_hours.get((index, peak_month), 0.0), 2)

    favorite_floor = (
        _pick_top_value(record.floor_name or record.floor_id or "未知楼层" for record in records)
        if records
        else "暂无记录"
    )
    favorite_seat = (
        _pick_top_value(record.seat_info for record in records)
        if records
        else "暂无记录"
    )
    favorite_period = _pick_top_counter(period_counts) if period_counts else "暂无"

    return {
        "key": get_semester_key(index),
        "index": index,
        "label": SEMESTER_LABELS[index],
        "term": "上" if index % 2 == 0 else "下",
        "calendar_label": f"{start_date:%Y.%m}-{end_date:%Y.%m}",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "sessions": sessions,
        "hours": hours,
        "active_days": active_days,
        "avg_hours_per_active_day": round(hours / active_days, 1)
        if active_days
        else 0,
        "peak_month": peak_month,
        "peak_month_label": peak_month_label,
        "peak_month_sessions": peak_month_sessions,
        "peak_month_hours": peak_month_hours,
        "favorite_floor": favorite_floor,
        "favorite_seat": favorite_seat,
        "favorite_period": favorite_period,
        "phase_tag": classify_semester_phase(index, sessions, hours, active_days),
        "roast": build_semester_roast(
            index=index,
            sessions=sessions,
            hours=hours,
            active_days=active_days,
            peak_month_label=peak_month_label,
        ),
    }


def get_semester_bounds(cohort_year: int, index: int) -> tuple[date, date]:
    if index % 2 == 0:
        start_year = cohort_year + index // 2
        return date(start_year, 8, 1), date(start_year + 1, 1, 31)

    spring_year = cohort_year + index // 2 + 1
    return date(spring_year, 2, 1), date(spring_year, 7, 31)


def get_semester_key(index: int) -> str:
    grade = index // 2 + 1
    term = "autumn" if index % 2 == 0 else "spring"
    return f"grade{grade}_{term}"


def format_year_month_label(month_key: str) -> str:
    try:
        year_text, month_text = month_key.split("-", 1)
        return f"{int(year_text)}年{int(month_text)}月"
    except (TypeError, ValueError):
        return month_key or "暂无"


def classify_semester_phase(
    index: int,
    sessions: int,
    hours: float,
    active_days: int,
) -> str:
    if sessions == 0:
        return "空白观测期"
    if index == 5:
        return "考研/就业预热期"
    if index == 6:
        return "现实岔路口"
    if active_days >= 90 or hours >= 700:
        return "长期驻扎期"
    if sessions >= 80 or hours >= 450:
        return "高强度常驻期"
    if sessions >= 35:
        return "稳定刷题期"
    if index <= 1:
        return "大一试探期"
    return "低调在线期"


def build_semester_roast(
    index: int,
    sessions: int,
    hours: float,
    active_days: int,
    peak_month_label: str,
) -> str:
    if sessions == 0:
        return "这一学期记录很轻，可能是在别的战场开了分线。"
    if index == 5:
        return (
            "这段很像考研、就业或考公前夜的系统性加压：嘴上说顺其自然，"
            "预约记录已经开始自证清白。"
        )
    if index == 6:
        return (
            "大四上的选择题通常不止一个：秋招、实习、复习和毕设可能同时开线程，"
            "图书馆只负责记录你没有完全下线。"
        )
    if active_days >= 90:
        return "出勤天数已经接近日常化，不像冲刺，更像把图书馆写进生活作息。"
    if sessions >= 80 or hours >= 450:
        return f"峰值落在 {peak_month_label}，这一学期的强度已经不适合用偶尔来形容。"
    if sessions >= 35:
        return "节奏不算夸张，但胜在持续在线，属于图书馆看了会点头的稳定型。"
    return "存在感不算高，但也不是完全隐身；这是保留体力、择日再战。"


def build_semester_archive_copy(strongest: dict | None) -> str:
    if not strongest:
        return "把四年切成八个学期后，暂时没有足够样本生成最强阶段。"
    return (
        f"把四年切成八段，最重的一段是 {strongest['label']}："
        f"{strongest['sessions']} 次预约、{strongest['hours']:g} 小时，"
        f"峰值落在 {strongest['peak_month_label']}。"
    )


def build_crossroads_stats(semesters: list[dict]) -> dict:
    third_year_spring = semesters[5] if len(semesters) > 5 else None
    fourth_year_autumn = semesters[6] if len(semesters) > 6 else None
    focus_terms = [item for item in [third_year_spring, fourth_year_autumn] if item]
    non_empty = [item for item in focus_terms if item["sessions"] > 0]
    stronger = max(
        non_empty,
        key=lambda item: (item["sessions"], item["hours"], item["active_days"]),
        default=None,
    )

    return {
        "diagnosis": "考研/就业准备期" if non_empty else "关键阶段样本不足",
        "stage_copy": (
            "大三下和大四上常常是考研、就业、考公、实习和毕设一起挤压的阶段。"
            "数据不能替你确认动机，但能看出那段时间有没有真正加压。"
        ),
        "third_year_spring": third_year_spring,
        "fourth_year_autumn": fourth_year_autumn,
        "stronger_term": stronger,
        "signal_cards": build_crossroad_signal_cards(
            third_year_spring,
            fourth_year_autumn,
            stronger,
        ),
        "comparison_copy": build_crossroad_comparison_copy(
            third_year_spring,
            fourth_year_autumn,
        ),
    }


def build_crossroad_signal_cards(
    third_year_spring: dict | None,
    fourth_year_autumn: dict | None,
    stronger: dict | None,
) -> list[dict]:
    third_sessions = third_year_spring["sessions"] if third_year_spring else 0
    fourth_sessions = fourth_year_autumn["sessions"] if fourth_year_autumn else 0
    third_hours = third_year_spring["hours"] if third_year_spring else 0
    fourth_hours = fourth_year_autumn["hours"] if fourth_year_autumn else 0
    floor = stronger["favorite_floor"] if stronger else "暂无记录"

    return [
        {
            "label": "预约变化",
            "value": format_signed_delta(fourth_sessions - third_sessions, "次"),
            "detail": "大四上 - 大三下",
        },
        {
            "label": "时长变化",
            "value": format_signed_delta(round(fourth_hours - third_hours, 1), "h"),
            "detail": "同一天重叠预约已合并",
        },
        {
            "label": "更强阶段",
            "value": stronger["label"] if stronger else "暂无",
            "detail": f"主战场：{floor}",
        },
    ]


def format_signed_delta(value: int | float, unit: str) -> str:
    if value > 0:
        prefix = "+"
    elif value < 0:
        prefix = "-"
    else:
        prefix = "0"

    amount = abs(value)
    if isinstance(value, float) and not value.is_integer():
        amount_text = f"{amount:.1f}"
    else:
        amount_text = f"{int(amount)}"
    return f"{prefix}{amount_text}{unit}" if prefix != "0" else f"0{unit}"


def build_crossroad_comparison_copy(
    third_year_spring: dict | None,
    fourth_year_autumn: dict | None,
) -> str:
    if not third_year_spring or not fourth_year_autumn:
        return "关键阶段数据不足，暂时只能先把这一页留作空白观察。"

    third_sessions = third_year_spring["sessions"]
    fourth_sessions = fourth_year_autumn["sessions"]
    if third_sessions == 0 and fourth_sessions == 0:
        return "这两个关键学期都没有明显记录，可能主战场不在图书馆。"
    if fourth_sessions >= max(1, third_sessions) * 1.2:
        return (
            "从大三下到大四上，图书馆强度继续上扬。很像把考研、就业或考公选项"
            "同时打开，现实压力开始变得具体。"
        )
    if third_sessions >= max(1, fourth_sessions) * 1.2:
        return (
            "大三下已经把强度打满，大四上略有回落。它更像从备考蓄力切到投递、"
            "面试、实习或毕业事项的多线并行。"
        )
    return (
        "两个阶段强度接近，说明你没有在关键选择期突然消失。"
        "图书馆记录不到你的答案，但记录到了你持续在线。"
    )


def build_year_roast(
    year: int,
    peak_month: int,
    peak_sessions: int,
    total_sessions: int,
) -> str:
    ratio = peak_sessions / total_sessions if total_sessions else 0
    if peak_month in {6, 12, 1} and ratio >= 0.25:
        return "考试周把你从普通用户临时升级成馆内常驻进程。"
    if peak_month in {7, 8}:
        return "暑假还在馆里刷新存在感，这不是自律，这是把空调费赚回来了。"
    if peak_month in {3, 4, 9, 10}:
        return "开学没多久就进入状态，嘴上说随便学学，身体已经自动导航到图书馆。"
    if ratio >= 0.35:
        return "这一年高度集中爆发，像是把全年 KPI 都塞进了一个月。"
    return "这一年的节奏比较分散，不靠单月爆发，主打一个长期在线。"


def classify_trend(values: list[int]) -> str:
    if len(values) < 2:
        return "steady"

    peak = max(values)
    peak_index = values.index(peak)
    first = values[0]
    last = values[-1]

    if peak_index >= max(1, len(values) // 2) and peak >= max(first * 2, 20):
        return "nuwa"
    if first >= max(last * 2, 20):
        return "high_to_low"
    return "steady"


def estimate_coverage_percent(records: list[MemoryRecord]) -> int:
    unique_locations = {
        record.seat_info
        for record in records
        if record.seat_info and record.seat_info != "未知座位"
    }
    if not records:
        return 0
    return max(6, min(96, round(len(unique_locations) / 2)))


def classify_meme_profile(
    total_sessions: int,
    total_days: int,
    total_hours: float,
) -> tuple[str, str, str]:
    if total_sessions < 20 or total_days < 10 or total_hours < 60:
        return (
            "图书馆云股东",
            "赛博读书人",
            "四年了，图书馆大门朝哪你清楚吗？但也许你钟爱教室或宿舍，也许你度过了整个快乐的大学，作为真正的人生赢家，其中的酸甜苦辣咸麻辣鲜香只有你自己知道。",
        )
    if total_sessions < 60 or total_days < 30 or total_hours < 180:
        return (
            "偶尔闪现型选手",
            "低频但到场",
            "你不是图书馆常驻人口，但也不是完全隐身。每一次出现都像临时上线，主打一个需要时再启动。",
        )
    if total_sessions < 150 or total_days < 90 or total_hours < 700:
        return (
            "稳定在线型同学",
            "正常发挥",
            "你没有把图书馆当家，但也确实持续出现过。数据看起来不夸张，却很像一个普通大学生认真生活的样子。",
        )
    if total_sessions < 350 or total_days < 180 or total_hours < 1800:
        return (
            "深度常驻型选手",
            "馆内熟面孔",
            "你已经不是偶尔学习，而是把图书馆写进了大学生活的默认路线。工作人员可能不认识你，但数据认识。",
        )
    if total_sessions < 500 or total_days < 260 or total_hours < 3000:
        return (
            "高强度驻馆人",
            "长期在线",
            "你的图书馆记录已经有明显重量。它不是一阵热血，而是一段反复出现、反复坐下、反复坚持的长期轨迹。",
        )
    return (
        "人形自走卷王",
        "肝帝本帝",
        "你不是在图书馆，就是在去图书馆的路上。建议查查肝功能，或者直接把床搬来。",
    )


def build_extra_tags(
    favorite_period: str,
    favorite_seat_ratio: float,
    coverage_percent: int,
    trend_type: str,
    total_sessions: int,
    total_hours: float,
    early_start_sessions: int = 0,
    morning_sessions: int = 0,
    late_checkout_sessions: int = 0,
    evening_sessions: int = 0,
) -> list[dict]:
    tags: list[dict] = []
    early_ratio = early_start_sessions / total_sessions if total_sessions else 0
    morning_ratio = morning_sessions / total_sessions if total_sessions else 0
    late_ratio = late_checkout_sessions / total_sessions if total_sessions else 0
    evening_ratio = evening_sessions / total_sessions if total_sessions else 0

    if (
        favorite_period == "早晨"
        or early_start_sessions >= max(3, round(total_sessions * 0.04))
        or morning_sessions >= max(8, round(total_sessions * 0.14))
    ):
        tags.append(
            {
                "name": "早八特种兵",
                "tag": "晨型作战单位",
                "reason": (
                    f"早场启动 {morning_sessions} 次，其中 8:30 前 {early_start_sessions} 次；"
                    f"占比 {morning_ratio * 100:.1f}%，已经不是偶然早起。"
                ),
            }
        )
    if (
        favorite_period == "晚上"
        or late_checkout_sessions >= max(4, round(total_sessions * 0.05))
        or evening_sessions >= max(8, round(total_sessions * 0.14))
    ):
        tags.append(
            {
                "name": "关灯侠",
                "tag": "闭馆友好型",
                "reason": (
                    f"晚间启动 {evening_sessions} 次，21 点后签退 {late_checkout_sessions} 次；"
                    f"占比 {max(late_ratio, evening_ratio) * 100:.1f}%，闭馆线对你不陌生。"
                ),
            }
        )

    if favorite_seat_ratio >= 0.2:
        tags.append(
            {
                "name": "座位纯爱战士",
                "tag": "固定据点",
                "reason": "你对固定座位的偏好很明显，像是给自己找了一个馆内出生点。",
            }
        )
    elif coverage_percent >= 80:
        tags.append(
            {
                "name": "游牧学者",
                "tag": "空间管理大师",
                "reason": "行动范围很广，图书馆多个空间都留下过你的记录。",
            }
        )

    if trend_type == "nuwa":
        tags.append(
            {
                "name": "期末补天选手",
                "tag": "临时高压锅",
                "reason": "曲线在后段明显抬升，像是被关键节点按下加速键。",
            }
        )
    elif trend_type == "high_to_low":
        tags.append(
            {
                "name": "放弃挣扎曲线",
                "tag": "与自己和解",
                "reason": "前期火力更猛，后期明显回落，曲线非常诚实。",
            }
        )

    if total_sessions >= 500 or total_hours >= 3000:
        tags.append(
            {
                "name": "长期后台进程",
                "tag": "高频稳定",
                "reason": "总次数和总时长都很高，已经超出普通到馆频率。",
            }
        )

    return tags[:5]


def estimate_defeat_percentage(
    total_sessions: int,
    total_hours: float,
    low_frequency: bool,
) -> float:
    if low_frequency:
        return 12.0

    score = 55 + total_sessions * 0.055 + total_hours * 0.004
    return round(min(99.5, max(35.0, score)), 1)


def classify_seat_meme(favorite_seat_ratio: float, coverage_percent: int) -> str:
    if favorite_seat_ratio >= 0.7:
        return "座位纯爱战士"
    if coverage_percent >= 80:
        return "空间管理大师"
    if favorite_seat_ratio >= 0.3:
        return "固定据点守门员"
    return "游牧学者"


def find_peak_time(records: list[MemoryRecord]) -> str:
    if not records:
        return "--:--"
    buckets = Counter(
        f"{record.begin_time.hour:02d}:{0 if record.begin_time.minute < 30 else 30:02d}"
        for record in records
    )
    return _pick_top_counter(buckets)


def find_earliest_time(records: list[MemoryRecord]) -> str:
    if not records:
        return "--:--"
    value = min(records, key=lambda record: record.begin_time.time()).begin_time
    return value.strftime("%H:%M")


def find_latest_time(records: list[MemoryRecord]) -> str:
    if not records:
        return "--:--"
    latest = max(
        (
            record.end_time or record.begin_time + timedelta(hours=record.duration_hours)
        ).time()
        for record in records
    )
    return latest.strftime("%H:%M")


def build_low_frequency_copy(summary: dict) -> str:
    return (
        f"虽然你只留下了 {summary['total_sessions']} 次记录，但你的同届卷王们还在默默发电。"
        "感谢你为他们腾出了宝贵的座位。也许你钟爱教室或宿舍，也许你度过了整个快乐的大学，"
        "作为真正的人生赢家，其中的酸甜苦辣咸麻辣鲜香只有你自己知道。"
    )


def build_report_data(
    records: list[MemoryRecord], stats: dict, filters: MemoryFilters
) -> dict:
    summary = stats["summary"]
    habits = stats["habits"]
    timeline = stats["timeline"]
    source_label = describe_data_source(records)
    source_breakdown = describe_source_breakdown(records)

    scope_from = (
        filters.date_from.isoformat() if filters.date_from else summary["first_date"]
    )
    scope_to = filters.date_to.isoformat() if filters.date_to else summary["last_date"]
    title_suffix = f"{filters.year} 学习轨迹" if filters.year else "学习轨迹"

    metrics = [
        {
            "label": "累计预约",
            "value": f"{summary['total_sessions']} 次",
            "note": "统计的是纳入记忆库的成功预约记录。",
        },
        {
            "label": "累计时长",
            "value": f"{summary['total_hours']:g} 小时",
            "note": (
                f"折合约 {summary['days_equivalent']:g} 天；"
                "同一天重叠预约已合并计时。"
            ),
        },
        {
            "label": "最长连续打卡",
            "value": f"{summary['max_streak']} 天",
            "note": "按自然日连续出现计算，同一天多次预约只算一天。",
        },
        {
            "label": "最活跃月份",
            "value": summary["most_active_month"],
            "note": "按预约次数统计，这个月份是你最常出现的阶段。",
        },
    ]

    story = (
        f"你在这段时间里最常出现于 {summary['favorite_floor']}，"
        f"并且明显偏向 {summary['favorite_period']} 入馆。"
        "从数据上看，这不是偶尔发生的集中冲刺，而是一种已经形成惯性的学习节奏。"
    )

    favorites = [
        {
            "label": "最爱座位",
            "value": summary["favorite_seat"],
            "detail": f"共出现 {summary['favorite_seat_count']} 次，是你最常回到的位置。",
        },
        {
            "label": "最爱楼层",
            "value": summary["favorite_floor"],
            "detail": "在当前数据里，这里是你最常驻留的空间。",
        },
        {
            "label": "单日峰值",
            "value": summary["longest_day_date"],
            "detail": f"当天累计 {summary['longest_day_hours']:g} 小时，是目前记录中的最高强度日。",
        },
    ]

    quote = (
        f"{summary['total_hours']:g} 个小时并不只是数字，"
        "它更像是你在图书馆里反复回到同一节奏、同一位置、同一段时间的痕迹。"
    )

    footer_note = (
        f"当前报告基于 {source_label} 生成。"
        if source_breakdown
        else "当前报告基于预约历史生成。"
    )
    h5_contract = build_h5_contract(
        records=records,
        summary=summary,
        habits=habits,
        timeline=timeline,
        filters=filters,
    )

    return {
        "user": filters.user,
        "title": f"图书馆记忆 / {title_suffix}",
        "range": f"{scope_from} ~ {scope_to}",
        "source": source_label,
        "syncTime": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "quote": quote,
        "footerNote": footer_note,
        "summary": {
            "sessions": summary["total_sessions"],
            "hours": summary["total_hours"],
            "daysEquivalent": summary["days_equivalent"],
            "streak": summary["max_streak"],
            "favoriteFloor": summary["favorite_floor"],
            "favoriteSeat": summary["favorite_seat"],
            "favoritePeriod": summary["favorite_period"],
            "mostActiveMonth": summary["most_active_month"],
            "longestDayHours": summary["longest_day_hours"],
            "firstVisit": summary["first_date"],
            "latestVisit": summary["last_date"],
        },
        "story": story,
        "metrics": metrics,
        "scope": [
            {"label": "用户", "value": filters.user},
            {"label": "数据范围", "value": f"{scope_from} ~ {scope_to}"},
            {"label": "记录来源", "value": source_label},
            {"label": "记录数量", "value": str(len(records))},
        ],
        "months": timeline["months"],
        "periods": habits["periods"],
        "favorites": favorites,
        "floors": habits["floors"][:5],
        "milestones": timeline["milestones"],
        **h5_contract,
    }


def render_memory_report(records: list[MemoryRecord], stats: dict, filters: MemoryFilters) -> None:
    """打印命令行记忆报告。"""
    summary = stats["summary"]
    scope_from = (
        filters.date_from.isoformat() if filters.date_from else summary["first_date"]
    )
    scope_to = filters.date_to.isoformat() if filters.date_to else summary["last_date"]
    year_label = f"{filters.year} 年" if filters.year else "全部记录"

    console.header("你的图书馆记忆")
    print(f"用户             {filters.user}")
    print(f"统计范围         {year_label}")
    print(f"数据范围         {scope_from} ~ {scope_to}")
    print(f"记录来源         {describe_data_source(records)}")
    print()
    print(f"累计预约         {summary['total_sessions']} 次")
    print(f"累计时长         {summary['total_hours']:g} 小时")
    print(f"首次到访         {summary['first_date']}")
    print(f"最近到访         {summary['last_date']}")
    print()
    print(f"最爱楼层         {summary['favorite_floor']}")
    print(f"最爱座位         {summary['favorite_seat']}")
    print(f"最爱时段         {summary['favorite_period']}")
    print(f"最长连续打卡     {summary['max_streak']} 天")
    print(f"最活跃月份       {summary['most_active_month']}")
    print(f"单日最长时长     {summary['longest_day_hours']:g} 小时")


def export_html_report(report_data: dict, output_path: Path | None = None) -> Path:
    """导出 HTML 报告。"""
    output = output_path or DEFAULT_HTML_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)

    if HTML_TEMPLATE_FILE.exists():
        template = HTML_TEMPLATE_FILE.read_text(encoding="utf-8")
        marker = "const memory = {"
        start = template.find(marker)
        if start != -1:
            brace_start = template.find("{", start)
            brace_end = _find_matching_brace(template, brace_start)
            if brace_start != -1 and brace_end != -1:
                payload = json.dumps(report_data, ensure_ascii=False, indent=6)
                template = template[:brace_start] + payload + template[brace_end + 1 :]
                output.write_text(template, encoding="utf-8")
                return output

    output.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Library Memory</title></head>"
        f"<body><pre>{json.dumps(report_data, ensure_ascii=False, indent=2)}</pre></body></html>",
        encoding="utf-8",
    )
    return output


def describe_data_source(records: list[MemoryRecord]) -> str:
    sources = {record.source for record in records}
    if sources == {"remote"}:
        return "图书馆后台预约历史"
    if sources == {"local"}:
        return "本地预约历史"
    if "remote" in sources and "local" in sources:
        return "图书馆后台预约历史 + 本地补充记录"
    return "预约历史"


def describe_source_breakdown(records: list[MemoryRecord]) -> str:
    counter = Counter(record.source for record in records)
    parts: list[str] = []
    if counter.get("remote"):
        parts.append(f"后台 {counter['remote']} 条")
    if counter.get("local"):
        parts.append(f"本地 {counter['local']} 条")
    return "，".join(parts)


def _build_record_keys(records: list[MemoryRecord]) -> set[str]:
    keys: set[str] = set()
    for record in records:
        _register_record_keys(keys, record)
    return keys


def _register_record_keys(keys: set[str], record: MemoryRecord) -> None:
    booking_key = _build_booking_key(record)
    if booking_key:
        keys.add(booking_key)
    keys.add(_build_session_key(record))


def _record_exists(keys: set[str], record: MemoryRecord) -> bool:
    booking_key = _build_booking_key(record)
    if booking_key and booking_key in keys:
        return True
    return _build_session_key(record) in keys


def _build_booking_key(record: MemoryRecord) -> str | None:
    if not record.booking_id:
        return None
    return f"booking:{record.user}:{record.booking_id}"


def _build_session_key(record: MemoryRecord) -> str:
    begin_key = record.begin_time.replace(second=0, microsecond=0).isoformat()
    duration_key = f"{record.duration_hours:g}"
    floor_key = record.floor_id or record.floor_name or ""
    seat_key = record.seat_number or record.seat_info or ""
    return "|".join([record.user, begin_key, duration_key, floor_key, seat_key])


def _find_matching_brace(text: str, start_index: int) -> int:
    depth = 0
    in_string = False
    escape = False
    quote_char = ""

    for index in range(start_index, len(text)):
        char = text[index]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote_char:
                in_string = False
            continue

        if char in ('"', "'"):
            in_string = True
            quote_char = char
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index

    return -1


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_datetime_value(value) -> datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp /= 1000
        if timestamp > 1_000_000_000:
            return datetime.fromtimestamp(timestamp)
        return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        if text.isdigit():
            return parse_datetime_value(int(text))

        normalized = text.replace("T", " ").replace("Z", "")
        for fmt in [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d",
        ]:
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    return None


def parse_duration_value(value) -> float:
    if value is None or value == "":
        return 0.0

    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 100:
            return round(numeric / 3600, 2)
        return numeric

    text = str(value).strip().lower()
    if not text:
        return 0.0

    if text.endswith("h"):
        text = text[:-1]

    if text.isdigit():
        return parse_duration_value(int(text))

    try:
        numeric = float(text)
    except ValueError:
        return 0.0

    if numeric > 100:
        return round(numeric / 3600, 2)
    return numeric


def parse_remote_status(*payloads: dict) -> str:
    text = stringify(
        extract_first_value(
            *payloads,
            keys=[
                "process_name",
                "processName",
                "status_name",
                "statusName",
                "status",
                "result",
            ],
        )
    )
    if text:
        if "取消" in text:
            return "cancelled"
        if "失效" in text or "过期" in text:
            return "expired"
        if "失败" in text:
            return "failed"

    process = stringify(extract_first_value(*payloads, keys=["process"]))
    if process in {"4", "5", "6"}:
        return "cancelled"
    return "success"


def resolve_remote_user(
    item: dict,
    detail: dict,
    booking: dict,
    expected_user: str | None = None,
) -> str | None:
    """Resolve the report user; platform uid is internal, so prefer student id."""
    if expected_user:
        return expected_user

    nested_user = _pick_mapping(booking.get("user"))
    user = extract_first_value(
        nested_user,
        detail,
        booking,
        item,
        keys=[
            "student_number",
            "cardno",
            "rid",
            "name",
            "login_name",
            "user_name",
            "userName",
            "user_id",
            "userId",
            "uid",
        ],
    )
    return stringify(user)


def extract_first_value(*payloads, keys: list[str]):
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
    return None


def format_seat_info(
    room_name: str | None,
    floor_id: str | None,
    floor_name: str | None,
    seat_number: str | None,
) -> str:
    if floor_name and seat_number:
        return f"{floor_name} / Seat {seat_number}"
    if floor_id and seat_number:
        return f"Floor {floor_id}, Seat {seat_number}"
    if room_name and seat_number:
        return f"{room_name} / Seat {seat_number}"
    if seat_number:
        return f"Seat {seat_number}"
    if floor_name:
        return floor_name
    if room_name:
        return room_name
    if floor_id:
        return f"Floor {floor_id}"
    return "未知座位"


def stringify(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick_mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _choose_string(primary: str | None, fallback: str | None) -> str | None:
    return primary or fallback
