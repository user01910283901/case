# Case Stars

## Важно
GitHub Pages не запускает Python.
Поэтому схема только такая:
- Frontend (`index.html`, `Icons/`) -> GitHub Pages
- Backend (`secure_server.py`, `progress/progress.json`) -> отдельный хост (Render/Railway/VPS)

Без backend сайт теперь работает в `SERVER_ONLY` режиме и не уходит в локальный режим.

## Структура репозитория
- `index.html` - frontend
- `Icons/` - иконки
- `secure_server.py` - backend API
- `progress/progress.json` - серверная база прогресса
- `SECURITY_NOTES.md` - заметки по безопасности

## 1) Deploy Frontend (GitHub Pages, Deploy from a branch)
1. Push в ветку `main`.
2. GitHub -> `Settings` -> `Pages`.
3. `Build and deployment` -> `Source: Deploy from a branch`.
4. `Branch: main` и `/ (root)`.
5. Сохрани.

Сайт будет вида:
`https://<username>.github.io/<repo>/`

## 2) Deploy Backend (обязательно отдельно)
Пример локального запуска:
```powershell
python secure_server.py
```

Backend должен быть доступен по HTTPS URL, например:
`https://case-stars-api.onrender.com`

API фронт ожидает по пути `/api/*`.

## 3) Подключить frontend к backend
Открой сайт так:

`https://<username>.github.io/<repo>/?api=https://<your-backend-domain>/api`

После первого открытия адрес API сохранится в браузере.

## 4) Проверка что всё серверное
- В шапке должен быть бейдж `SERVER`.
- Если backend не найден -> экран `Требуется сервер`.
- Подарки по ID и прогресс между перезапусками работают только через backend.

## 5) Массовое изменение баланса (dev)
В профиле есть блок `Dev Control` (виден только в `SERVER`).

- `Добавить всем` -> POST `/api/admin/balance_all` mode=`add`
- `Установить всем` -> POST `/api/admin/balance_all` mode=`set`

Опциональная защита:
- На backend выстави env `CASE_DEV_TOKEN=<token>`
- На клиенте в консоли браузера:
```js
localStorage.setItem('case_dev_token','<token>')
```
