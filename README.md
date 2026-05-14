# My_lullaby_bot

## Переменные окружения

Для работы бота нужны:

```env
TELEGRAM_TOKEN=
OPENAI_API_KEY=
SUNO_API_KEY=
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
YOOKASSA_RETURN_URL=https://t.me/username_бота
YOOKASSA_VAT_CODE=1
# YOOKASSA_TAX_SYSTEM_CODE=
YOOKASSA_WEBHOOK_HOST=0.0.0.0
YOOKASSA_WEBHOOK_PORT=3000
YOOKASSA_WEBHOOK_PATH=/yookassa-webhook
YOOKASSA_TEST_MODE=1
ADMIN_IDS=
DB_PATH=/path/to/persistent/kolybelka.db
```

`YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` берутся в личном кабинете ЮKassa.
`YOOKASSA_RETURN_URL` можно указать ссылкой на Telegram-бота.
В личном кабинете ЮKassa нужно добавить webhook на событие `payment.succeeded`.
URL вебхука должен вести на публичный HTTPS-адрес сервера и путь из `YOOKASSA_WEBHOOK_PATH`, например `https://example.com/yookassa-webhook`.
На время модерации ЮKassa бот может запускаться без `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`; в этом случае покупка будет временно недоступна, но остальные сценарии бота работают.
Для пробной оплаты используйте тестовый магазин ЮKassa и его тестовые `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`. `YOOKASSA_TEST_MODE=1` только добавляет в сообщениях пометку, что оплата тестовая; API ЮKassa остаётся тем же.
`ADMIN_IDS` — список Telegram ID администраторов через запятую. Администраторы могут вручную начислять орешки командой `/addnuts`.
`DB_PATH` можно не указывать: по умолчанию база хранится рядом с `bot.py`. Если хостинг пересоздаёт папку проекта при перезапуске или деплое, укажите путь к постоянному хранилищу, чтобы орешки пользователей не терялись.

## Оплата

Бот продаёт орешки:

- 1 орешек — 349 ₽
- 2 орешка — 499 ₽
- 3 орешка — 599 ₽

Перед созданием платежа бот спрашивает электронную почту для отправки чека.
После оплаты бот получает webhook от ЮKassa, перепроверяет платёж через API и автоматически начисляет орешки.
1 орешек списывается только после того, как готовая музыкальная колыбельная отправлена пользователю.

## Служебные команды

- `/balance` — показать текущий баланс орешков.
- `/myid` — показать свой Telegram ID для настройки `ADMIN_IDS`.
- `/addnuts user_id количество` — вручную начислить орешки пользователю. Команда работает только для ID из `ADMIN_IDS`.

## Пробная оплата ЮKassa

1. В личном кабинете ЮKassa создайте или откройте тестовый магазин.
2. Скопируйте тестовые `shopId` и `secretKey` в переменные `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`.
3. Укажите `YOOKASSA_TEST_MODE=1`.
4. В настройках тестового магазина добавьте webhook на публичный адрес бота: `https://ваш-домен/yookassa-webhook`.
5. Включите событие `payment.succeeded`. При желании можно добавить `payment.canceled`.
6. Перезапустите бота на хостинге.
7. В боте нажмите `Купить орешки`, выберите количество, введите email и оплатите тестовой картой ЮKassa.
8. Для успешного теста можно использовать карту `5555 5555 5555 4477`, срок действия в будущем, CVC и код подтверждения — любые числа.

## Проверка

```bash
python3 -m unittest discover -s tests
```
