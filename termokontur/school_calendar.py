"""Weekly measurement schedules and date overrides in local school time."""
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import db, settings

Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]
Building = Annotated[str, Field(min_length=1, max_length=100)]


class ExceptionDays(BaseModel):
    model_config = ConfigDict(extra="forbid")

    building: str = Field(default="", max_length=100)
    start: date
    end: date
    expected: bool

    @model_validator(mode="after")
    def check_dates(self):
        if self.end < self.start:
            raise ValueError("Конец периода раньше начала")
        return self


class Calendar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekly: dict[Building, list[Weekday]] = Field(default_factory=dict, max_length=250)
    exceptions: list[ExceptionDays] = Field(default_factory=list, max_length=250)

    @model_validator(mode="after")
    def check_schedule(self):
        if any(len(days) > 7 or len(set(days)) != len(days) for days in self.weekly.values()):
            raise ValueError("Некорректные дни недели")
        end_by_building = {}
        for rule in sorted(self.exceptions, key=lambda rule: rule.start):
            previous = end_by_building.get(rule.building)
            if previous is not None and rule.start <= previous:
                raise ValueError("Периоды одного здания не должны пересекаться")
            end_by_building[rule.building] = rule.end
        return self

    def for_building(self, building: str, default: frozenset[int]):
        global_rules = [rule for rule in self.exceptions if not rule.building]
        building_rules = [rule for rule in self.exceptions if rule.building == building] if building else []
        rules = building_rules + global_rules
        weekdays = self.weekly.get(building, default)

        def expected(day: date) -> bool:
            for rule in rules:
                if rule.start <= day <= rule.end:
                    return rule.expected
            return day.weekday() in weekdays

        return expected


def load() -> tuple[Calendar, frozenset[int]]:
    calendar = Calendar.model_validate_json(settings.get("school_calendar", "{}") or "{}")
    default = frozenset(map(int, settings.get("reminder_days", "0,1,2,3,4").split(",")))
    return calendar, default


def buildings() -> list[str]:
    with db.connect() as conn:
        return [row[0] for row in conn.execute("SELECT DISTINCT building FROM rooms ORDER BY building")]


def save(calendar: Calendar) -> None:
    known = set(buildings())
    referenced = set(calendar.weekly) | {rule.building for rule in calendar.exceptions if rule.building}
    if referenced - known:
        raise ValueError("Выберите существующий корпус")
    with db.connect() as conn:
        conn.execute("""
            INSERT INTO settings(key,value) VALUES('school_calendar',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (calendar.model_dump_json(),))
