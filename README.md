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

Бот продаёт пакеты оплаченных колыбельных:

- 1 персональная музыкальная колыбельная — 350 ₽
- 2 персональные музыкальные колыбельные — 500 ₽
- 3 персональные музыкальные колыбельные — 600 ₽

Перед созданием платежа бот спрашивает email клиента и передаёт его в ЮKassa для чека.
Одна оплаченная колыбельная списывается в момент создания текста, потому что текст и правки тоже используют AI.
