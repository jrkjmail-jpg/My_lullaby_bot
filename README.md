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
```

`YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` берутся в личном кабинете ЮKassa.
`YOOKASSA_RETURN_URL` можно указать ссылкой на Telegram-бота.
На время модерации ЮKassa бот может запускаться без `YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY`; в этом случае покупка будет временно недоступна, но остальные сценарии бота работают.

## Оплата

Бот продаёт пакеты орешков:

- 1 орешек — 350 ₽
- 2 орешка — 500 ₽
- 3 орешка — 600 ₽

Перед созданием платежа бот спрашивает электронную почту для отправки чека.
В чеке ЮKassa орешки не указываются: там будет понятная услуга `персональная музыкальная колыбельная`.
1 орешек списывается только после того, как готовая музыкальная колыбельная отправлена пользователю.
