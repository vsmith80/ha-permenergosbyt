# Тестирование

```
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest tests/ -v
```

Покрыто модульными тестами (без реального Home Assistant — файлы
компонента загружаются напрямую через `importlib`, минуя
`custom_components/permenergosbyt/__init__.py`):

- `api.py` — парсинг HTML-формы сайта (на реальных, захваченных с
  lk.permenergosbyt.ru образцах в `tests/fixtures/`, для 1/2/3-тарифных
  вариантов) и HTTP-клиент (`fetch_measure_form`/`submit_measures` —
  через самодельные fake-объекты вместо `aioresponses`, которая на
  момент разработки была несовместима с актуальной версией `aiohttp`).
- `const.py` — чистые функции `resolved_schedule`/`resolved_tariff_entities`/
  `device_info` (нужен минимальный stub для `homeassistant.config_entries`/
  `homeassistant.helpers.device_registry`, если полный пакет `homeassistant`
  не установлен — см. `tests/conftest.py`).

Логика в `scheduler.py`, `config_flow.py`, `sensor.py`/`button.py`/`switch.py`
пока не покрыта — там нужен `pytest-homeassistant-custom-component` с
реальным тестовым `hass` (таймеры, `Store`, dispatcher, config flow). Как и
раньше: тесты проверяют, что наш код делает то, что задумано, при заданном
ответе сайта — не то, что сайт реально себя так ведёт (маркер успеха/неудачи
в ответе на `save_measure` по-прежнему не подтверждён вживую).
