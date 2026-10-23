from datetime import datetime, timedelta


# Перевод часов приложения назад запрещён
class ClockMovedBack(Exception):
    pass


# Часы приложения: системное время со сдвигом
class ShiftedClock:
    def __init__(self) -> None:
        self._offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now() + self._offset

    def move_to(self, moment: datetime) -> None:
        if moment < self.now():
            raise ClockMovedBack("clock cannot be moved back")
        self._offset = moment - datetime.now()
