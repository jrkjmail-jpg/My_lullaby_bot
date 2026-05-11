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

## Оплата

Бот продаёт пакеты генераций:

- 1 генерация 🌰 — 350 ₽
- 2 генерации 🌰 — 500 ₽
- 3 генерации 🌰 — 600 ₽

Перед созданием платежа бот спрашивает email клиента и передаёт его в ЮKassa для чека.
В чеке ЮKassa слово `генерация` и значок орешка не указываются: там будет понятная услуга `персональная музыкальная колыбельная`.
1 генерация 🌰 списывается только после того, как готовая музыкальная колыбельная отправлена пользователю.
