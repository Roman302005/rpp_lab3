from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from parking.domain import (
    Money,
    ParkingSession,
    ParkingSessionId,
    ParkingSpot,
    ParkingSpotId,
    VehicleNumber,
)
from parking.errors import EntityNotFound


# Репозиторий мест хранит агрегаты в словаре
class InMemoryParkingSpotRepository:
    def __init__(self) -> None:
        self._items: dict[ParkingSpotId, ParkingSpot] = {}

    def get(self, spot_id: ParkingSpotId) -> ParkingSpot:
        try:
            # Копия защищает хранилище от изменения в обход агрегата
            return deepcopy(self._items[spot_id])
        except KeyError as error:
            raise EntityNotFound("parking spot was not found") from error

    def find_free(self) -> ParkingSpot | None:
        # Свободное место с наименьшим номером
        for spot_id in sorted(self._items, key=lambda item: item.value):
            if not self._items[spot_id].is_occupied:
                return deepcopy(self._items[spot_id])
        return None

    def save(self, spot: ParkingSpot) -> None:
        self._items[spot.id] = deepcopy(spot)


# Репозиторий заездов хранит агрегаты в словаре
class InMemoryParkingSessionRepository:
    def __init__(self) -> None:
        self._items: dict[ParkingSessionId, ParkingSession] = {}

    def get(self, session_id: ParkingSessionId) -> ParkingSession:
        try:
            return deepcopy(self._items[session_id])
        except KeyError as error:
            raise EntityNotFound("parking session was not found") from error

    def next_id(self) -> ParkingSessionId:
        return ParkingSessionId(len(self._items) + 1)

    def all(self) -> list[ParkingSession]:
        return [deepcopy(item) for item in self._items.values()]

    def has_active_for_vehicle(self, vehicle_number: VehicleNumber) -> bool:
        # Активным считается заезд, по которому автомобиль занимает место
        return any(
            session.vehicle_number == vehicle_number and session.is_on_parking
            for session in self._items.values()
        )

    def latest_ended_at_for_spot(self, spot_id: ParkingSpotId) -> datetime | None:
        ended_values = [
            session.ended_at
            for session in self._items.values()
            if session.spot_id == spot_id and session.ended_at is not None
        ]
        return max(ended_values, default=None)

    def save(self, session: ParkingSession) -> None:
        self._items[session.id] = deepcopy(session)


# Список заблокированных автомобилей
class InMemoryBlockedVehicles:
    def __init__(self, numbers: list[str] | None = None) -> None:
        self._numbers = {VehicleNumber(number) for number in numbers or []}

    def is_blocked(self, vehicle_number: VehicleNumber) -> bool:
        return vehicle_number in self._numbers


# Заглушка платёжного терминала и приложения хранит платежи в памяти
class InMemoryPaymentGateway:
    def __init__(self) -> None:
        self.payments: list[tuple[ParkingSessionId, Money, str]] = []

    def charge(self, session_id: ParkingSessionId, amount: Money, channel: str) -> None:
        self.payments.append((session_id, amount, channel))
