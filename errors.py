# Базовое исключение доменного слоя
class DomainError(Exception):
    pass


# Ошибка создания объекта-значения
class InvalidValueObject(DomainError):
    pass


# Ошибка нарушения инварианта агрегата
class DomainInvariantViolation(DomainError):
    pass


# Ошибка поиска агрегата в репозитории
class EntityNotFound(DomainError):
    pass


# Событие не разрешено в текущем состоянии сущности
class TransitionNotAllowed(DomainError):
    def __init__(self, state: str, event: str) -> None:
        super().__init__(f"event '{event}' is not allowed in state '{state}'")
        self.state = state
        self.event = event
