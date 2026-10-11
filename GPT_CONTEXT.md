# Argus Reviews Monitor — Контекст и инструкция для AI / GPT

## 1. О проекте
**Argus** — автоматизированная система мониторинга отзывов и Telegram-бот для 6 учебных отделений колледжа КАИТ-20:
- УО Датахаб
- УО Моссовет
- УО Диджитал
- УО АртТех
- УО Техно
- УО Кибер

Система каждые 15 минут опрашивает две платформы для каждого отделения (всего **12 источников**):
1. **Яндекс Карты** (Yandex Maps)
2. **2ГИС** (2GIS)

Новые отзывы сохраняются в SQLite, анализируются Gemini AI на тональность (GREEN, YELLOW, RED) и пересылаются в Telegram-канал/группу ответственным лицам.

---

## 2. Пути и окружение
- **Локальный репозиторий (Windows):**  
  `D:\IMPORTANT\AIProjects\AntigravitiProject\Feedback and notifications\Argus-Reviews`
- **Боевой сервер (Staging VPS Linux):**  
  `/opt/argus-staging`
- **SSH доступ к серверу:**  
  `ssh -p 8347 root@213.165.51.106`  
  *(КРИТИЧНО: при вызове команд из скриптов всегда передавать флаг `-n`, например `ssh -n -p 8347 ...`, иначе SSH ждет ввода из stdin и намертво вешает сессию)*.
- **База данных SQLite на сервере:**  
  `/opt/argus-staging/data/argus.sqlite3`
- **Виртуальное окружение Python на сервере:**  
  `/opt/argus-staging/.venv/bin/python`
- **Системный сервис:**  
  `argus.service` (`systemctl status argus`, `systemctl restart argus`)

---

## 3. СТРОГИЕ ПРАВИЛА И ОГРАНИЧЕНИЯ
> [!WARNING]
> **НЕ ДЕЛАТЬ `git commit` И `git push`!**  
> Пользователь явно запретил коммитить изменения в git. Все доработки ведутся локально и синхронизируются на сервер через `scp` напрямую в `/opt/argus-staging/`.

---

## 4. Архитектура и ключевые файлы
- `app/reviews/models.py` — структуры данных `ReviewItem`, `ReviewSource`, статусы `ReviewSyncStatus`, `AiReviewStatus`.
- `app/reviews/client_yandex.py` — клиент Яндекс Карт:
  - Выполняется через `urllib` в отдельном потоке (`asyncio.to_thread`), чтобы не ломать event loop и избегать TLS-фингерпринтинга.
  - Инициализирует веб-сессию (`csrfToken`, `sessionId`), парсит рейтинг и количество отзывов из HTML.
  - Опрашивает API `/maps/api/business/fetchReviews` с расчетом сигнатуры `s` (`djb2_hash`).
  - **Важная доработка от 11.10.2026:** встроен 4-кратный retry с экспоненциальной задержкой при 500-х ошибках бэкенда Яндекса (для европейского IP сервера) + fallback на отзывы, вшитые в исходный HTML (`reviewResults.reviews`). Временные ошибки классифицируются как `HTTP_ERROR`, а не `PARSER_FORMAT_CHANGED`.
- `app/reviews/client_2gis.py` — клиент 2ГИС:
  - Запросы к публичному API 2ГИС через `aiohttp`.
- `app/reviews/service.py` — ядро координации:
  - Проверка новых отзывов, дедупликация по `external_review_id`.
  - Алгоритм catchup-догрузки пагинации при обнаружении новых отзывов.
  - Health-мониторинг источников (если 3 ошибки подряд — отправка алерта и экспоненциальный backoff).
- `app/reviews/analyzer.py` — Gemini AI анализатор тональности.
- `app/reviews/scheduler.py` — периодический планировщик опроса (по умолчанию 900 сек / 15 минут).
- `app/bot/` — обработчики Telegram бота на `aiogram`.

---

## 5. Доставка отзывов и фильтрация уведомлений
- **Зеленые отзывы (5 звезд):** доставляются в Telegram без звука пуш-уведомления (`disable_notification=True`).
- **Отзывы с 4 звездами или желтые/красные (жалобы, проблемы):** доставляются со звуком (`disable_notification=False`).

---

## 6. Полезные команды для проверки и починки

### Запуск тестов на сервере:
```bash
ssh -n -p 8347 root@213.165.51.106 "cd /opt/argus-staging && .venv/bin/python -m pytest -q"
```

### Проверка состояния сервиса и логов:
```bash
ssh -n -p 8347 root@213.165.51.106 "systemctl status argus --no-pager"
ssh -n -p 8347 root@213.165.51.106 "journalctl -u argus -n 50 --no-pager"
```

### Проверка статусов источников в SQLite:
```bash
ssh -n -p 8347 root@213.165.51.106 "sqlite3 /opt/argus-staging/data/argus.sqlite3 'SELECT id, branch_name, platform, last_status, consecutive_errors, backoff_until, last_rating, total_reviews_count FROM review_sources;'"
```

### Сброс бэкоффа источников (если нужно немедленно опросить):
```bash
ssh -n -p 8347 root@213.165.51.106 "sqlite3 /opt/argus-staging/data/argus.sqlite3 'UPDATE review_sources SET backoff_until = NULL, consecutive_errors = 0, health_alert_active = 0, last_error = NULL;'"
```

### Перезапуск сервиса:
```bash
ssh -n -p 8347 root@213.165.51.106 "systemctl restart argus"
```

### Доставка файла с локалки на VPS:
```powershell
scp -P 8347 "D:\IMPORTANT\AIProjects\AntigravitiProject\Feedback and notifications\Argus-Reviews\app\reviews\client_yandex.py" root@213.165.51.106:/opt/argus-staging/app/reviews/client_yandex.py
```
