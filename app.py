import dataclasses
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging
import os

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from parking.domain import HourlyTariff, Money, ParkingSessionId, ParkingSpot, ParkingSpotId
from parking.errors import DomainInvariantViolation, EntityNotFound, InvalidValueObject, TransitionNotAllowed
from parking.memory import (
    InMemoryBlockedVehicles,
    InMemoryParkingSessionRepository,
    InMemoryParkingSpotRepository,
    InMemoryPaymentGateway,
)
from parking.process import GatewayUnresolved, ParkingProcess
from web.clock import ClockMovedBack, ShiftedClock

# Исключение -> (код ответа, код ошибки)
ERRORS = {
    TransitionNotAllowed: (409, "transition_not_allowed"),
    DomainInvariantViolation: (422, "invariant_violation"),
    InvalidValueObject: (422, "validation_error"),
    EntityNotFound: (404, "not_found"),
    ClockMovedBack: (422, "clock_moved_back"),
    GatewayUnresolved: (500, "gateway_unresolved"),
}


def _json_value(value):
    # Объекты-значения выводятся простыми значениями
    if isinstance(value, Money):
        return f"{value.cents / 100:.2f}"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if dataclasses.is_dataclass(value):
        return _json_value(getattr(value, "value", None))
    return value


def _event_view(event) -> dict:
    data = {"type": type(event).__name__}
    for field in dataclasses.fields(event):
        if field.name != "session_id":
            data[field.name] = _json_value(getattr(event, field.name))
    return data


def _pretty(data: dict) -> Response:
    # Каждое поле и каждое событие журнала выводятся отдельной строкой
    dump = lambda value: json.dumps(value, ensure_ascii=False)
    lines = []
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            items = ",\n".join(f"    {dump(item)}" for item in value)
            lines.append(f'  "{key}": [\n{items}\n  ]')
        else:
            lines.append(f'  "{key}": {dump(value)}')
    return Response("{\n" + ",\n".join(lines) + "\n}\n", mimetype="application/json")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InvalidValueObject("request body must be a JSON object")
    return data


def _money(value) -> Money:
    # Сумма передаётся в рублях, хранится в копейках
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidValueObject("amount must be a number")
    if amount != amount.quantize(Decimal("0.01")):
        raise InvalidValueObject("amount must have at most two decimal places")
    return Money(int(amount * 100))


def create_app() -> Flask:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    app = Flask(__name__)
    # Ответы в UTF-8 с отступами и в порядке полей
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    app.json.compact = False

    # Часы, репозитории и сервис создаются один раз при запуске
    clock = ShiftedClock()
    spots = InMemoryParkingSpotRepository()
    for number in range(1, int(os.environ.get("PARKING_SPOTS", "2")) + 1):
        spots.save(ParkingSpot(ParkingSpotId(number)))
    blocked = InMemoryBlockedVehicles(os.environ.get("BLOCKED_NUMBERS", "X999XX54").split(","))
    process = ParkingProcess(
        spots,
        InMemoryParkingSessionRepository(),
        blocked,
        InMemoryPaymentGateway(),
        clock,
        HourlyTariff(Money(int(os.environ.get("TARIFF_KOPECKS", "15000")))),
    )

    def view(session_id: int):
        session = process.get(ParkingSessionId(session_id))
        return {
            "id": session.id.value,
            "status": session.status,
            "allowed_events": sorted(session.allowed_events),
            "vehicle_number": _json_value(session.vehicle_number),
            "spot_id": _json_value(session.spot_id),
            "paid_total": _json_value(session.paid_total),
            "amount_due": _json_value(process.amount_due(session)),
            "events": [_event_view(event) for event in session.events],
        }

    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.name.lower().replace(" ", "_"), message=error.description), error.code
        for error_type, (code, name) in ERRORS.items():
            if isinstance(error, error_type):
                return jsonify(error=name, message=str(error)), code
        app.logger.exception("unexpected error")
        return jsonify(error="internal_error", message="unexpected error"), 500

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.post("/sessions")
    def arrive():
        # Камера на въезде передаёт номер или null, если номер не распознан
        session_id = process.recognize_number_at_entry(_body().get("plate"))
        return _pretty(view(session_id.value)), 201

    @app.post("/sessions/<int:session_id>/manual-number")
    def manual_number(session_id: int):
        process.enter_number_manually(ParkingSessionId(session_id), _body().get("plate"))
        return _pretty(view(session_id))

    @app.post("/sessions/<int:session_id>/pay")
    def pay(session_id: int):
        data = _body()
        process.accept_payment(ParkingSessionId(session_id), _money(data.get("amount")), data.get("channel"))
        return _pretty(view(session_id))

    @app.post("/sessions/<int:session_id>/exit")
    def exit_(session_id: int):
        process.recognize_number_at_exit(ParkingSessionId(session_id), _body().get("plate"))
        return _pretty(view(session_id))

    @app.get("/sessions/<int:session_id>")
    def get_session(session_id: int):
        return _pretty(view(session_id))

    @app.post("/internal/tick")
    def tick():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise InvalidValueObject("request body must be a JSON object")
        if data.get("now") is not None:
            try:
                moment = datetime.fromisoformat(data["now"])
            except (TypeError, ValueError):
                raise InvalidValueObject("now must be an ISO 8601 datetime")
            if moment.tzinfo is not None:
                # Часы приложения работают в локальном времени без пояса
                moment = moment.astimezone().replace(tzinfo=None)
            clock.move_to(moment)
        changed = process.on_tick()
        return jsonify(now=_json_value(clock.now()), changed=[item.value for item in changed])

    return app
