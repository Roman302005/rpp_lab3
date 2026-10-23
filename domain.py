from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
import re
from typing import Protocol

from parking.errors import DomainInvariantViolation, InvalidValueObject
from parking.states import EXIT_WINDOW, Arrived, SessionState


def _validate_positive_id(value: int, field_name: str) -> None:
    # Логические значения тоже не должны использоваться как идентификаторы
    if type(value) is not int or value <= 0:
        raise InvalidValueObject(f"{field_name} must be a positive integer")


# Идентификатор парковочного места
@dataclass(frozen=True)
class ParkingSpotId:
    value: int

    def __post_init__(self) -> None:
        _validate_positive_id(self.value, "parking_spot_id")


# Идентификатор заезда (парковочной сессии)
@dataclass(frozen=True)
class ParkingSessionId:
    value: int

    def __post_init__(self) -> None:
        _validate_positive_id(self.value, "parking_session_id")


# Номер автомобиля хранится как неизменяемое значение
@dataclass(frozen=True)
class VehicleNumber:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidValueObject("vehicle_number must be a string")
        # Номер сохраняется в едином формате для корректного сравнения
        normalized = self.value.strip().upper()
        pattern = r"^[A-ZАВЕКМНОРСТУХ]\d{3}[A-ZАВЕКМНОРСТУХ]{2}\d{2,3}$"
        if not re.fullmatch(pattern, normalized):
            raise InvalidValueObject("vehicle_number has an invalid format")
        object.__setattr__(self, "value", normalized)


# Денежная сумма хранится в копейках без ошибок округления
@dataclass(frozen=True)
class Money:
    cents: int

    def __post_init__(self) -> None:
        if type(self.cents) is not int or self.cents < 0:
            raise InvalidValueObject("money amount must be a non-negative integer")

    def multiply(self, factor: int) -> Money:
        # Умножение возвращает новый объект Money
        if type(factor) is not int or factor < 0:
            raise InvalidValueObject("money multiplier must be non-negative")
        return Money(self.cents * factor)

    def plus(self, other: Money) -> Money:
        return Money(self.cents + other.cents)



# Тариф задаёт положительную цену одного часа
@dataclass(frozen=True)
class HourlyTariff:
    price_per_hour: Money

    def __post_init__(self) -> None:
        if self.price_per_hour.cents == 0:
            raise InvalidValueObject("hourly tariff must be greater than zero")


# Период объединяет время въезда и выезда
@dataclass(frozen=True)
class ParkingPeriod:
    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        if self.ended_at <= self.started_at:
            raise InvalidValueObject("parking must end after it starts")

    @property
    def billable_hours(self) -> int:
        # Неполный час оплачивается как полный
        seconds = (self.ended_at - self.started_at).total_seconds()
        return ceil(seconds / 3600)


# Доменные события фиксируют сообщения процесса и не изменяются
@dataclass(frozen=True)
class VehicleArrived:
    session_id: ParkingSessionId
    occurred_at: datetime
    vehicle_number: VehicleNumber | None


@dataclass(frozen=True)
class NumberEnteredManually:
    session_id: ParkingSessionId
    occurred_at: datetime
    vehicle_number: VehicleNumber


@dataclass(frozen=True)
class EntryRejected:
    session_id: ParkingSessionId
    occurred_at: datetime
    reason: str


@dataclass(frozen=True)
class VehicleAdmitted:
    session_id: ParkingSessionId
    occurred_at: datetime
    spot_id: ParkingSpotId


@dataclass(frozen=True)
class PaymentAccepted:
    session_id: ParkingSessionId
    occurred_at: datetime
    amount: Money
    channel: str


@dataclass(frozen=True)
class ExitWindowExpired:
    session_id: ParkingSessionId
    occurred_at: datetime


@dataclass(frozen=True)
class StoppedAtExitUnpaid:
    session_id: ParkingSessionId
    occurred_at: datetime


@dataclass(frozen=True)
class VehicleLeft:
    session_id: ParkingSessionId
    occurred_at: datetime
    cost: Money


@dataclass(frozen=True)
class LongStayDetected:
    session_id: ParkingSessionId
    occurred_at: datetime


# Сроки процесса
LONG_STAY = timedelta(days=7)
PAYMENT_CHANNELS = frozenset({"app", "terminal"})


# Агрегат управляет текущей занятостью места
class ParkingSpot:
    def __init__(self, spot_id: ParkingSpotId) -> None:
        self._id = spot_id
        self._active_session_id: ParkingSessionId | None = None

    @property
    def id(self) -> ParkingSpotId:
        return self._id

    @property
    def is_occupied(self) -> bool:
        return self._active_session_id is not None

    @property
    def active_session_id(self) -> ParkingSessionId | None:
        return self._active_session_id

    def occupy(self, session_id: ParkingSessionId) -> None:
        # Занятое место нельзя выдать повторно
        if self.is_occupied:
            raise DomainInvariantViolation("parking spot is already occupied")
        self._active_session_id = session_id

    def release(self, session_id: ParkingSessionId) -> None:
        # Освободить место может только связанная с ним сессия
        if not self.is_occupied:
            raise DomainInvariantViolation("parking spot is already free")
        if self._active_session_id != session_id:
            raise DomainInvariantViolation("session does not own this parking spot")
        self._active_session_id = None


# Агрегат хранит жизненный цикл одного заезда: от въезда до выезда
class ParkingSession:
    def __init__(
        self,
        session_id: ParkingSessionId,
        arrived_at: datetime,
        vehicle_number: VehicleNumber | None,
    ) -> None:
        self._id = session_id
        self._arrived_at = arrived_at
        self._vehicle_number = vehicle_number
        self._spot_id: ParkingSpotId | None = None
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None
        self._cost: Money | None = None
        self._paid_total = Money(0)
        self._paid_at: datetime | None = None
        self._long_stay_notified = False
        self._state: SessionState = Arrived()
        self._events: list = [VehicleArrived(session_id, arrived_at, vehicle_number)]

    # --- данные только для чтения, публичных сеттеров нет ---
    @property
    def id(self) -> ParkingSessionId:
        return self._id

    @property
    def vehicle_number(self) -> VehicleNumber | None:
        return self._vehicle_number

    @property
    def spot_id(self) -> ParkingSpotId | None:
        return self._spot_id

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def ended_at(self) -> datetime | None:
        return self._ended_at

    @property
    def cost(self) -> Money | None:
        return self._cost

    @property
    def paid_total(self) -> Money:
        return self._paid_total

    @property
    def paid_at(self) -> datetime | None:
        return self._paid_at

    @property
    def status(self) -> str:
        return self._state.name

    @property
    def allowed_events(self) -> frozenset[str]:
        return self._state.allowed

    @property
    def is_closed(self) -> bool:
        return self._state.is_final

    @property
    def is_on_parking(self) -> bool:
        # Автомобиль занимает место от допуска до выезда
        return self._started_at is not None and not self._state.is_final

    @property
    def events(self) -> tuple:
        return tuple(self._events)

    def amount_due(self, now: datetime, tariff: HourlyTariff) -> Money:
        # В оплаченном окне выезда платить нечего
        if self._started_at is None or self._state.name == "Оплачен" or self._state.is_final:
            return Money(0)
        # Считается время с въезда или с последней оплаты, неполный час как полный
        since = self._paid_at or self._started_at
        hours = ParkingPeriod(since, now).billable_hours if now > since else 1
        return tariff.price_per_hour.multiply(hours)

    # --- события таблицы переходов ---
    def enter_number(self, number: VehicleNumber, now: datetime) -> None:
        self._state = self._state.enter_number(self)
        self._vehicle_number = number
        self._events.append(NumberEnteredManually(self._id, now, number))

    def admit(self, spot_id: ParkingSpotId, now: datetime) -> None:
        if now < self._arrived_at:
            raise DomainInvariantViolation("entry cannot happen before arrival")
        new_state = self._state.admit(self)
        self._spot_id = spot_id
        self._started_at = now
        self._state = new_state
        self._events.append(VehicleAdmitted(self._id, now, spot_id))

    def reject_blocked(self, now: datetime) -> None:
        self._state = self._state.reject_blocked(self)
        self._ended_at = now
        self._events.append(EntryRejected(self._id, now, "vehicle is blocked"))

    def reject_no_space(self, now: datetime) -> None:
        self._state = self._state.reject_no_space(self)
        self._ended_at = now
        self._events.append(EntryRejected(self._id, now, "no free parking spots"))

    def pay(self, amount: Money, channel: str, now: datetime, tariff: HourlyTariff) -> None:
        if channel not in PAYMENT_CHANNELS:
            raise InvalidValueObject("payment channel must be 'app' or 'terminal'")
        due = self.amount_due(now, tariff)
        self._state = self._state.pay(self, amount, due)
        self._paid_total = self._paid_total.plus(amount)
        self._paid_at = now
        self._events.append(PaymentAccepted(self._id, now, amount, channel))

    def stop_at_exit(self, now: datetime) -> None:
        self._state = self._state.stop_at_exit(self)
        self._events.append(StoppedAtExitUnpaid(self._id, now))

    def leave(self, now: datetime) -> Money:
        new_state = self._state.leave(self, now)
        # Период проверяет хронологию въезда и выезда, как в ЛР2
        ParkingPeriod(self._started_at, now)
        self._ended_at = now
        self._cost = self._paid_total
        self._state = new_state
        self._events.append(VehicleLeft(self._id, now, self._cost))
        return self._cost

    # --- события таймера ---
    def on_exit_window_expired(self, now: datetime) -> bool:
        if "expire_exit_window" not in self.allowed_events:
            return False
        if now - self._paid_at <= EXIT_WINDOW:
            return False
        self._state = self._state.expire_exit_window(self)
        self._events.append(ExitWindowExpired(self._id, now))
        return True

    def on_long_stay(self, now: datetime) -> bool:
        if "notify_long_stay" not in self.allowed_events or self._long_stay_notified:
            return False
        if now - self._started_at < LONG_STAY:
            return False
        self._state = self._state.notify_long_stay(self)
        self._long_stay_notified = True
        self._events.append(LongStayDetected(self._id, now))
        return True


# Порт репозитория парковочных мест
class ParkingSpotRepository(Protocol):
    def get(self, spot_id: ParkingSpotId) -> ParkingSpot: ...

    def find_free(self) -> ParkingSpot | None: ...

    def save(self, spot: ParkingSpot) -> None: ...


# Порт репозитория заездов
class ParkingSessionRepository(Protocol):
    def get(self, session_id: ParkingSessionId) -> ParkingSession: ...

    def next_id(self) -> ParkingSessionId: ...

    def all(self) -> list[ParkingSession]: ...

    def has_active_for_vehicle(self, vehicle_number: VehicleNumber) -> bool: ...

    def latest_ended_at_for_spot(self, spot_id: ParkingSpotId) -> datetime | None: ...

    def save(self, session: ParkingSession) -> None: ...
