from __future__ import annotations

from datetime import datetime
from typing import Protocol

from parking.domain import (
    HourlyTariff,
    Money,
    ParkingSession,
    ParkingSessionId,
    ParkingSessionRepository,
    ParkingSpot,
    ParkingSpotRepository,
    VehicleNumber,
)
from parking.errors import DomainInvariantViolation


# Текущее время процесс получает только через часы
class Clock(Protocol):
    def now(self) -> datetime: ...


# Порт списка заблокированных автомобилей
class BlockedVehicles(Protocol):
    def is_blocked(self, vehicle_number: VehicleNumber) -> bool: ...


# Порт внешнего участника: оплата в приложении или терминале
class PaymentGateway(Protocol):
    def charge(self, session_id: ParkingSessionId, amount: Money, channel: str) -> None: ...


# Ни одно условие шлюза не выполнено
class GatewayUnresolved(Exception):
    pass


# --- предикаты исключающих шлюзов ---
def number_recognized(session: ParkingSession) -> bool:
    return session.vehicle_number is not None


def number_not_recognized(session: ParkingSession) -> bool:
    return session.vehicle_number is None


def vehicle_blocked(session: ParkingSession, blocked: BlockedVehicles) -> bool:
    return blocked.is_blocked(session.vehicle_number)


def vehicle_not_blocked(session: ParkingSession, blocked: BlockedVehicles) -> bool:
    return not blocked.is_blocked(session.vehicle_number)


def free_spot_exists(spot: ParkingSpot | None) -> bool:
    return spot is not None


def no_free_spot(spot: ParkingSpot | None) -> bool:
    return spot is None


def parking_paid(session: ParkingSession) -> bool:
    return session.status == "Оплачен"


def parking_not_paid(session: ParkingSession) -> bool:
    return session.status in ("Припаркован", "Ожидает оплаты на выезде")


# Прикладной сервис процесса «Платная парковка»
class ParkingProcess:
    def __init__(
        self,
        spots: ParkingSpotRepository,
        sessions: ParkingSessionRepository,
        blocked: BlockedVehicles,
        payments: PaymentGateway,
        clock: Clock,
        tariff: HourlyTariff,
    ) -> None:
        self._spots = spots
        self._sessions = sessions
        self._blocked = blocked
        self._payments = payments
        self._clock = clock
        self._tariff = tariff

    def get(self, session_id: ParkingSessionId) -> ParkingSession:
        return self._sessions.get(session_id)

    def amount_due(self, session: ParkingSession) -> Money:
        return session.amount_due(self._clock.now(), self._tariff)

    # Задача «Распознать номер на въезде» и шлюз «Номер распознан?»
    def recognize_number_at_entry(self, plate: str | None) -> ParkingSessionId:
        number = VehicleNumber(plate) if plate is not None else None
        session = ParkingSession(self._sessions.next_id(), self._clock.now(), number)
        if number_recognized(session):
            self._check_entry(session)
        elif number_not_recognized(session):
            # Номер не распознан: система ждёт ручного ввода оператора
            self._sessions.save(session)
        else:
            raise GatewayUnresolved("Номер распознан?")
        return session.id

    # Пользовательская задача оператора «Ввести номер вручную»
    def enter_number_manually(self, session_id: ParkingSessionId, plate: str) -> None:
        session = self._sessions.get(session_id)
        session.enter_number(VehicleNumber(plate), self._clock.now())
        self._check_entry(session)

    # Шлюзы «Автомобиль заблокирован?» и «Есть свободное место?»
    def _check_entry(self, session: ParkingSession) -> None:
        now = self._clock.now()
        if vehicle_blocked(session, self._blocked):
            session.reject_blocked(now)
            self._sessions.save(session)
        elif vehicle_not_blocked(session, self._blocked):
            spot = self._spots.find_free()
            if no_free_spot(spot):
                session.reject_no_space(now)
                self._sessions.save(session)
            elif free_spot_exists(spot):
                self._assign_spot_and_open_barrier(session, spot, now)
            else:
                raise GatewayUnresolved("Есть свободное место?")
        else:
            raise GatewayUnresolved("Автомобиль заблокирован?")

    # Задача «Назначить место, открыть шлагбаум»
    def _assign_spot_and_open_barrier(self, session: ParkingSession, spot: ParkingSpot, now: datetime) -> None:
        # Инварианты ЛР2: одна активная сессия на автомобиль и хронология места
        if self._sessions.has_active_for_vehicle(session.vehicle_number):
            raise DomainInvariantViolation("vehicle already has an active session")
        latest_ended_at = self._sessions.latest_ended_at_for_spot(spot.id)
        if latest_ended_at is not None and now < latest_ended_at:
            raise DomainInvariantViolation("session starts before the previous session ends")
        session.admit(spot.id, now)
        spot.occupy(session.id)
        self._spots.save(spot)
        self._sessions.save(session)

    # Событие «Получена оплата» и задача «Получить оплату в терминале»
    def accept_payment(self, session_id: ParkingSessionId, amount: Money, channel: str) -> None:
        session = self._sessions.get(session_id)
        now = self._clock.now()
        at_exit = session.status == "Ожидает оплаты на выезде"
        session.pay(amount, channel, now, self._tariff)
        self._payments.charge(session_id, amount, channel)
        if at_exit:
            # После оплаты у выезда процесс возвращается к проверке оплаты
            self._check_payment_and_exit(session, now)
        else:
            self._sessions.save(session)

    # Задача «Распознать номер и проверить оплату» на выезде
    def recognize_number_at_exit(self, session_id: ParkingSessionId, plate: str) -> None:
        session = self._sessions.get(session_id)
        if VehicleNumber(plate) != session.vehicle_number:
            raise DomainInvariantViolation("vehicle number at exit does not match the session")
        now = self._clock.now()
        # Таймер 15 минут мог истечь до подъезда к выезду
        session.on_exit_window_expired(now)
        self._check_payment_and_exit(session, now)

    # Шлюз «Стоянка оплачена?»
    def _check_payment_and_exit(self, session: ParkingSession, now: datetime) -> None:
        if parking_paid(session):
            self._open_barrier_and_close_session(session, now)
        elif parking_not_paid(session):
            session.stop_at_exit(now)
            self._sessions.save(session)
        else:
            raise GatewayUnresolved("Стоянка оплачена?")

    # Задача «Открыть шлагбаум, закрыть сессию»
    def _open_barrier_and_close_session(self, session: ParkingSession, now: datetime) -> None:
        spot = self._spots.get(session.spot_id)
        session.leave(now)
        spot.release(session.id)
        self._sessions.save(session)
        self._spots.save(spot)

    # Обработчик POST /internal/tick: события таймера 15 минут и 7 суток
    def on_tick(self) -> list[ParkingSessionId]:
        now = self._clock.now()
        changed = []
        for session in self._sessions.all():
            expired = session.on_exit_window_expired(now)
            notified = session.on_long_stay(now)
            if expired or notified:
                self._sessions.save(session)
                changed.append(session.id)
        return changed
