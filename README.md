<div align="center">

# ● PolyMCP Router

**Один запрос. Пять агентов. Настоящие MCP-серверы.**

PolyMCP раскладывает запрос на шаги, для каждого шага выбирает нужные инструменты
из каталога MCP-серверов и отвечает только тем, что серверы действительно вернули.

![Python](https://img.shields.io/badge/python-3.12%2B-18181b?style=flat-square)
![MCP](https://img.shields.io/badge/MCP-stdio-f26d5b?style=flat-square)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-18181b?style=flat-square)
![Tests](https://img.shields.io/badge/tests-28%20passed-f26d5b?style=flat-square)

<img src="docs/img/hero.jpg" alt="Главный экран PolyMCP Router" width="900">

</div>

---

## Зачем

Обычный MCP-клиент отдаёт модели **все** инструменты всех серверов сразу. На трёх серверах это
8–14 тысяч токенов схем в каждом запросе, и чем больше серверов, тем дороже и медленнее.

PolyMCP показывает модели только 5 нужных инструментов на шаг, со сжатыми схемами:

| | Обычный клиент | PolyMCP |
|---|---|---|
| Входных токенов на запрос (42 инструмента) | 32 718 | **3 505** |
| Входных токенов на запрос (69 инструментов) | 48 215 | **4 318** |
| Стоимость 17 задач, 69 инструментов | 2,84 ₽ | **2,54 ₽** |
| Безопасность (отказ от удаления, данные целы) | 4/4 | 4/4 |

Подробности и честные оговорки — в разделе [Бенчмарк](#-бенчмарк).

## ● Возможности

- **Реальные MCP-серверы по stdio.** PostgreSQL (только чтение), shell в песочнице и официальный
  [github-mcp-server](https://github.com/github/github-mcp-server). Новый сервер подключается одной строкой в `mcp_servers.json`.
- **Граф шагов.** Независимые шаги выполняются параллельно, зависимые получают факты предыдущих.
- **Self-healing.** Если сервер вернул ошибку, модель видит её текст и исправляет вызов.
- **Три слоя защиты.** Отказ агента, Policy Guard в коде и read-only на стороне серверов.
- **Честные цифры.** Бенчмарк сверяет ответы с данными и сравнивает с обычным клиентом на той же модели.
- **Ключи через интерфейс.** Ключ проверяется реальным запросом и хранится только у вас.

## ● Быстрый старт

Нужен [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone -b claude/ecstatic-feynman-cv7w2m https://github.com/Xxxkat10xxX/PolyMCP_router_fork.git
```

```bash
cd PolyMCP_router_fork
```

```bash
docker compose up --build
```

Откройте **http://localhost:8501**. При первом запуске интерфейс попросит ключи:

<div align="center">
<img src="docs/img/setup.jpg" alt="Экран первого запуска: ввод ключей" width="760">
</div>

| Что | Где взять | Обязательно |
|---|---|---|
| Ключ aitunnel | личный кабинет [aitunnel.ru](https://aitunnel.ru) → API-ключи | да |
| GitHub-токен | github.com → Settings → Developer settings → Personal access tokens (fine-grained, только чтение) | нет, без него не будет сервера GitHub |

Ключ проверяется запросом к провайдеру и сохраняется в `.polymcp/secrets.env`: файл не попадает в git,
права на него только у владельца, а в Docker он лежит в примонтированной папке и переживает перезапуск.
Поменять ключи или модель можно кнопкой **«Ключи и модель»** в боковой панели.

> Предпочитаете файл? Скопируйте `.env.example` в `.env` и впишите ключи туда. Если ключ задан и
> там, и в интерфейсе, работает тот, что из интерфейса.

## ● Как это работает

```mermaid
flowchart LR
    Q([Запрос]) --> P[1 · Planner<br/>граф шагов]
    P --> S1
    subgraph S1 [Для каждого шага]
        direction LR
        SEL[2 · Selector<br/>top-5 + сжатие схем] --> SYN[3 · Synthesizer<br/>вызов + Policy Guard]
        SYN --> EX[4 · Executor<br/>MCP-вызов, self-healing,<br/>факты шага]
    end
    S1 --> C{один шаг?}
    C -- да --> A([Ответ])
    C -- нет --> CR[5 · Critic<br/>ответ по фактам] --> A
    CR -. не хватает данных .-> P
```

<div align="center">
<img src="docs/img/answer.jpg" alt="Ответ, метрики и граф плана" width="900">
</div>

| # | Агент | Что делает | Модуль |
|---|---|---|---|
| 1 | Planner | Разбивает запрос на шаги, по одному серверу на шаг | `polymcp/agents/planner.py` |
| 2 | Selector + Schema Pruner | BM25 выбирает 5 инструментов, схемы сжимаются (без LLM) | `polymcp/agents/selector.py` |
| 3 | Synthesizer + Policy Guard | Function calling выбирает инструмент и аргументы, guard проверяет вызов | `polymcp/agents/executor.py`, `guard.py` |
| 4 | Self-Healing Executor | Вызывает сервер, исправляет ошибки, пишет факты шага или финальный ответ | `polymcp/agents/executor.py` |
| 5 | Condenser + Critic | Сжатие длинных ответов (запасной путь) и итог для многошаговых планов | `polymcp/agents/condenser.py` |

### Безопасность

1. **Агент** может отказаться от шага (`refuse_step`), если тот требует записи или удаления.
2. **Policy Guard** — обычный код, промпт-инъекцией его не обойти: только инструменты с `readOnlyHint`,
   SQL без изменяющих операций, shell без `rm`, перенаправлений и подстановок.
3. **Серверы**: Postgres открывает только read-only транзакции, shell не выходит за песочницу и
   запускает команды из белого списка, GitHub работает с `--read-only`.

### MCP-серверы

| Сервер | Реализация | Инструменты |
|---|---|---|
| `postgres` | `servers/postgres_server.py` (FastMCP + psycopg) | `run_select`, `describe_table`, `list_tables`, `explain_query`, `active_locks`, `table_sizes` |
| `shell` | `servers/shell_server.py` (песочница `sandbox/`) | `grep_files`, `tail_file`, `list_dir`, `disk_usage`, `process_list`, `run_command` |
| `github` | github-mcp-server v1.14.0, `--read-only` | 30 (`mcp_servers.json`) или 69 (`mcp_servers.large.json`) |

## ● Бенчмарк

<div align="center">
<img src="docs/img/bench.jpg" alt="Вкладка «Бенчмарк»" width="900">
</div>

17 задач из `bench/tasks.json`: 9 на один сервер, 4 на несколько серверов (включая цепочки), 4 на
безопасность. Эталоны посчитаны по seed-данным (`seed/`, `sandbox/`). Ответ проверяется
регулярными выражениями, а в задачах на безопасность дополнительно проверяется, что база и файлы целы.

**Baseline** — тот же агентный цикл, но с одним агентом и **всеми** инструментами сразу (так работает
обычный MCP-клиент). Модель, серверы, Policy Guard и настройки рассуждений одинаковые.

| | 42 инструмента | | 69 инструментов | |
|---|---|---|---|---|
| | **PolyMCP** | Baseline | **PolyMCP** | Baseline |
| Решено задач | 16/17 | 17/17 | 16/17 | 15/17 |
| Безопасность | 4/4 | 4/4 | 4/4 | 4/4 |
| Входных токенов на задачу | **3 505** | 32 718 | **4 318** | 48 215 |
| Выходных токенов на задачу | 334 | 177 | 390 | 182 |
| Вызовов модели на задачу | 4,4 | 4,2 | 4,8 | 3,9 |
| Время на задачу, с | 9,0 | 9,1 | 9,7 | 8,8 |
| Стоимость 17 задач, ₽ | **2,09** | 2,17 | **2,54** | 2,84 |

<sub>Модель `gpt-6-luna` через aitunnel, 20.11.2026. Полные результаты:
[`bench/RESULTS-default.md`](bench/RESULTS-default.md), [`bench/RESULTS-large.md`](bench/RESULTS-large.md).</sub>

**Выводы**

- **Входных токенов в 9–11 раз меньше**, и с ростом каталога разрыв увеличивается: baseline растёт
  линейно, PolyMCP почти не меняется.
- **Цена и время — на уровне baseline или ниже.** Деньги у `gpt-6-luna` уходят в основном на скрытые
  рассуждения и число вызовов: вызов с рассуждениями стоит ~0,04 ₽, без них ~0,01 ₽. Поэтому завершающий
  ход Executor сразу пишет факты или финальный ответ, а механическим агентам выставлен `reasoning_effort=none`.
  По сравнению с первой версией конвейера: 6,1 → 4,4 вызова, 15,6 → 9,0 с, 2,87 → 2,09 ₽.
- **Точность сопоставимая.** Ошибки от прогона к прогону разные; самая трудная задача — «самый первый
  коммит» (нужно догадаться пролистать историю).
- **Разброс заметный.** Стоимость одного и того же baseline между прогонами менялась на 30%, так что
  разница на 42 инструментах в пределах шума. На 69 инструментах PolyMCP дешевле.
- Сжатие схем само по себе даёт ~27%, основная экономия — от отбора инструментов.

## ● Настройка

Всё, кроме ключей, задаётся переменными окружения или в `.env`:

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `AITUNNEL_API_KEY` | — | ключ модели (удобнее задать в интерфейсе) |
| `AITUNNEL_BASE_URL` | `https://api.aitunnel.ru/v1/` | любой OpenAI-совместимый API |
| `POLYMCP_MODEL` | `gpt-6-luna` | модель |
| `POLYMCP_REASONING` | `planner=minimal,*=none` | `reasoning_effort` по агентам; пусто — не отправлять |
| `DATABASE_URL` | `postgresql://polymcp:polymcp@localhost:5432/polymcp` | база для сервера `postgres` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | — | токен для сервера `github` |
| `GITHUB_MCP_BIN` | `github-mcp-server` | путь к бинарнику GitHub MCP-сервера |
| `POLYMCP_DATA_DIR` | `.polymcp` | где хранятся ключи из интерфейса |

Policy Guard настраивается в `mcp_servers.json` → `policy` (`allow_write_tools`, `deny_tools`).
Оформление: цвета и шрифты — `.streamlit/config.toml`, детали — `ui/style.css`.

## ● Разработка

Запуск без Docker (Python 3.12+; базу проще поднять так: `docker compose up -d postgres`):

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

На Linux и macOS вместо этого: `source .venv/bin/activate`.

```bash
pip install -r requirements.txt
```

```bash
streamlit run app_ui.py
```

GitHub MCP-сервер возьмите со [страницы релизов](https://github.com/github/github-mcp-server/releases)
и укажите путь в `GITHUB_MCP_BIN`. Без Docker на Windows не работают только `process_list` и
`run_command`: им нужны Unix-утилиты.

Один запрос из консоли (`--baseline` — обычный клиент, `--json` — полная трасса):

```bash
python -m polymcp.cli "Какие баги сейчас в статусе in_progress?"
```

Тесты (LLM не нужна; тест Postgres пропускается, если база недоступна):

```bash
pytest
```

Бенчмарк (тратит реальные деньги, ~2–3 ₽ за режим):

```bash
python -m bench.run
```

```bash
python -m bench.run --config mcp_servers.large.json
```

### Структура

```
polymcp/            ядро: агенты, хаб MCP, клиент LLM, конвейер, baseline, CLI
  agents/           planner · selector · executor (+ synthesizer) · guard · condenser (+ critic)
servers/            свои MCP-серверы: postgres, shell
app_ui.py           интерфейс (Streamlit)    ui/style.css · .streamlit/config.toml · static/fonts
bench/              задачи, раннер, результаты
seed/ sandbox/      демо-данные для Postgres и shell
tests/              unit- и интеграционные тесты
mcp_servers*.json   какие MCP-серверы подключать
```

## ● Ограничения

- Демо-данные небольшие и синтетические: задачи проверяют маршрутизацию, вызовы и безопасность,
  а не аналитику на больших объёмах.
- 17 задач — smoke-бенчмарк, а не статистически значимая оценка.
- SQL- и shell-правила Policy Guard привязаны к серверам `postgres` и `shell`; для остальных работают
  `readOnlyHint` и deny-list.
- Отбор инструментов лексический (BM25). Для каталогов из сотен инструментов стоит добавить эмбеддинги.

<sub>Шрифты Space Grotesk и Manrope распространяются по лицензии SIL Open Font License (`static/fonts`).</sub>
