from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from parking.errors import DomainInvariantViolation, TransitionNotAllowed

if TYPE_CHECKING:
    from parking.domain import Money, ParkingSession

# После оплаты у водителя есть 15 минут на выезд
EXIT_WINDOW = timedelta(minutes=15)


# Базовое состояние запрещает все события таблицы переходов
class SessionState:
    name = ""
    allowed: frozenset[str] = frozenset()
    is_final = False

    def enter_number(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "enter_number")

    def admit(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "admit")

    def reject_blocked(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "reject_blocked")

    def reject_no_space(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "reject_no_space")

    def pay(self, session: ParkingSession, amount: Money, due: Money) -> SessionState:
        raise TransitionNotAllowed(self.name, "pay")

    def expire_exit_window(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "expire_exit_window")

    def stop_at_exit(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "stop_at_exit")

    def leave(self, session: ParkingSession, now: datetime) -> SessionState:
        raise TransitionNotAllowed(self.name, "leave")

    def notify_long_stay(self, session: ParkingSession) -> SessionState:
        raise TransitionNotAllowed(self.name, "notify_long_stay")


def _check_payment(amount: Money, due: Money) -> None:
    # Оплата принимается только на всю сумму к оплате
    if due.cents == 0:
        raise DomainInvariantViolation("nothing to pay")
    if amount != due:
        raise DomainInvariantViolation(f"amount must be equal to {due.cents} kopecks")


# Автомобиль у въезда, решение о допуске ещё не принято
class Arrived(SessionState):
    name = "Прибыл"
    allowed = frozenset({"enter_number", "admit", "reject_blocked", "reject_no_space"})

    def enter_number(self, session):
        # Ручной ввод нужен только при нераспознанном номере
        if session.vehicle_number is not None:
            raise DomainInvariantViolation("vehicle number is already known")
        return self

    def _require_number(self, session) -> None:
        if session.vehicle_number is None:
            raise DomainInvariantViolation("vehicle number is not identified")

    def admit(self, session):
        self._require_number(session)
        return Parked()

    def reject_blocked(self, session):
        self._require_number(session)
        return RejectedBlocked()

    def reject_no_space(self, session):
        self._require_number(session)
        return RejectedNoSpace()


# Автомобиль стоит на месте, стоянка не оплачена
class Parked(SessionState):
    name = "Припаркован"
    allowed = frozenset({"pay", "stop_at_exit", "notify_long_stay"})

    def pay(self, session, amount, due):
        _check_payment(amount, due)
        return Paid()

    def stop_at_exit(self, session):
        return WaitingAtExit()

    def notify_long_stay(self, session):
        return self


# Стоянка оплачена, действует окно выезда 15 минут
class Paid(SessionState):
    name = "Оплачен"
    allowed = frozenset({"expire_exit_window", "leave", "notify_long_stay"})

    def expire_exit_window(self, session):
        return Parked()

    def leave(self, session, now):
        # Выезд разрешён только в оплаченное окно
        if now - session.paid_at > EXIT_WINDOW:
            raise DomainInvariantViolation("exit window expired, extra payment is required")
        return Finished()

    def notify_long_stay(self, session):
        return self


# Автомобиль у выезда без оплаты, ожидается оплата в терминале
class WaitingAtExit(SessionState):
    name = "Ожидает оплаты на выезде"
    allowed = frozenset({"pay", "notify_long_stay"})

    def pay(self, session, amount, due):
        _check_payment(amount, due)
        return Paid()

    def notify_long_stay(self, session):
        return self


# Конечные состояния не разрешают ни одного события
class RejectedBlocked(SessionState):
    name = "Отклонён: заблокирован"
    is_final = True


class RejectedNoSpace(SessionState):
    name = "Отклонён: нет мест"
    is_final = True


class Finished(SessionState):
    name = "Завершён"
    is_final = True
