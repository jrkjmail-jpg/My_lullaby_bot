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
YOOKASSA_WEBHOOK_PORT=8080
YOOKASSA_WEBHOOK_PATH=/yookassa-webhook
```

`YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` берутся в личном кабинете ЮKassa.
`YOOKASSA_RETURN_URL` можно указать ссылкой на Telegram-бота.
В личном кабинете ЮKassa нужно добавить webhook на событие `payment.succeeded`.
URL вебхука должен вести на публичный HTTPS-адрес сервера и путь из `YOOKASSA_WEBHOOK_PATH`, например `https://example.com/yookassa-webhook`.
На время модерации ЮKassa бот может запускаться без `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`; в этом случае покупка будет временно недоступна, но остальные сценарии бота работают.

## Оплата

Бот продаёт пакеты орешков:

- 1 орешек — 349 ₽
- 2 орешка — 499 ₽
- 3 орешка — 599 ₽

Перед созданием платежа бот спрашивает электронную почту для отправки чека.
После оплаты бот получает webhook от ЮKassa, перепроверяет платёж через API и автоматически начисляет орешки.
В чеке ЮKassa орешки не указываются: там будет понятная услуга `персональная музыкальная колыбельная`.
1 орешек списывается только после того, как готовая музыкальная колыбельная отправлена пользователю.

## Проверка

```bash
python3 -m unittest discover -s tests
```
