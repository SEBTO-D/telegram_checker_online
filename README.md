# Telegram Tracker (Flask + Telethon, веб-авторизация)

- Добавление ников и логирование статусов (online/offline/…)
- Настройки через веб (все ключи .env)
- **Авторизация Telegram через веб-интерфейс** (в Настройках): ввод кода и 2FA-пароля
- Экспорт логов в CSV

## Запуск
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # заполните значения
python app.py
```

Откройте http://localhost:5000 → /login → /settings → «Авторизация Telegram».
Если сессии нет, нажмите «Отправить код», затем введите код. При 2FA введите пароль.
Сессия сохраняется в `tracker_session.session`.
