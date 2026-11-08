# Контейнер и командная строка

Образ запускает бота «Школа действия» через исходящий Long Polling. Для работы
бота нужен доступ к API MAX. Входящий порт и webhook не нужны. Публичная
демо-школа развёрнута отдельно. [Архитектура](ARCHITECTURE.md).

## Готовый образ Taskflow

Скачайте архив образа и `SHA256SUMS.txt` из [релиза Taskflow](https://github.com/ozerinpro-arch/ideyafix-max/releases/tag/hackathon-taskflow-2026-11-08-fix).
В релизе один образ Linux AMD64. Для Linux ARM64 можно собрать образ из исходников.

```sh
shasum -a 256 -c SHA256SUMS.txt --ignore-missing
docker load -i ideyafix-max-taskflow-linux-amd64.tar.gz
export IDEYAFIX_IMAGE=ideyafix-max:taskflow-62a71d7
docker image inspect --format '{{.Id}} {{.Architecture}}' "$IDEYAFIX_IMAGE"
```

Ожидаемый ID опубликован в заметках релиза. Проверьте основные сценарии бота
в загруженном образе. Тестам не нужны токен, учётные записи и доступ к сети.

```sh
docker run --rm --platform linux/amd64 --network none \
  --entrypoint python "$IDEYAFIX_IMAGE" -B -m unittest \
  tests.test_platform_store tests.test_polling tests.test_school_notifications
```

## Сборка из исходников

Из корня проекта:

```sh
docker build -t ideyafix-max:local .
export IDEYAFIX_IMAGE=ideyafix-max:local
```

Python 3.12.16 взят из официального образа и закреплён digest в Dockerfile.
Все 78 Python-зависимостей имеют полные версии в requirements.txt и requirements.lock.
Сборка устанавливает их через pip; при запуске установка и загрузка моделей не нужны.
В образ входят код, вымышленный пакет планирования, шрифт Inter с лицензией OFL и тесты.
Токены, настройки подключения, `.env`, рабочие базы, `.venv`, `.git` и отчёты исключены.
По умолчанию процесс работает от пользователя 10001, без root.

## Приватные файлы на компьютере

Каталог настроек и каталог данных создаёт владелец файлов. Пример:

```sh
test "$(id -u)" -ne 0
mkdir -p runtime/container-private runtime/container-data
chmod 700 runtime/container-private runtime/container-data
cp config/max.example.json runtime/container-private/connection.json
cp app/assets/max_public_root.cert runtime/container-private/additional-ca.pem
chmod 600 runtime/container-private/connection.json runtime/container-private/additional-ca.pem
```

Поместите токен бота в `runtime/container-private/max-token.txt` без других полей.
Файл должен принадлежать тому же пользователю и иметь права 0600.
Секрет не вводится в Dockerfile, YAML, аргументы команды, переменные окружения или Git.
Пути внутри connection.json — **контейнерные**, как в примере.

Пример выключен (`enabled:false`), поэтому не выполняет запросы MAX.
Для настоящего запуска установите `enabled:true` и актуальное время активации
`activatedAtMs` в миллисекундах UTC. Старые события до этого времени пропускаются.
Пример явно подключает публичный корневой сертификат MAX через
`/run/private/additional-ca.pem`. Сертификат взят из официального источника;
его происхождение и SHA указаны рядом с `app/assets/max_public_root.cert`.
Он добавляется только в TLS-контекст клиента MAX. Системное хранилище доверия
не меняется, проверка TLS остаётся включённой. Файл не содержит приватного ключа.

Смена UID не отменяет проверки владельца и прав. На Linux и macOS передавайте
UID/GID владельца bind-mount файлов, чтобы 0600-файлы читались без изменения прав:

```sh
: "${IDEYAFIX_IMAGE:=ideyafix-max:local}"
export IDEYAFIX_IMAGE
export IDEYAFIX_UID="$(id -u)"
export IDEYAFIX_GID="$(id -g)"
export IDEYAFIX_PRIVATE_DIR="$PWD/runtime/container-private"
export IDEYAFIX_RUNTIME_DIR="$PWD/runtime/container-data"
docker compose up --no-build -d
docker compose ps
docker compose logs --tail=20 bot
docker compose down
```

Compose использует образ из `IDEYAFIX_IMAGE`. Если исходники уже на компьютере,
можно вместо отдельной команды сборки запустить `docker compose up --build -d`.

Compose подключает настройки только для чтения, данные — в `/app/runtime`,
а основной filesystem — только для чтения. `/tmp` и `/private/tmp` временные.
Порты не публикуются. Healthcheck проверяет локальный heartbeat, без токена и сети.
Одна база допускает одного polling consumer. Не запускайте CLI и контейнер
одновременно с одним и тем же каталогом данных.

## Первый вход в чистом контейнере

До запуска приёмника создайте ссылку тестового оператора на этом компьютере.
Команда работает без сети, читает только настройку путей и создаёт приглашение
в собственной базе; файл токена не открывает:

```sh
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/runtime/container-private,dst=/run/private,readonly" \
  --mount "type=bind,src=$PWD/runtime/container-data,dst=/app/runtime" \
  --entrypoint python "${IDEYAFIX_IMAGE:-ideyafix-max:local}" -B -c \
  'from app.max_settings import MaxSettings; from app.platform_store import PlatformStore; s=MaxSettings.load("/run/private/connection.json"); i=PlatformStore(s.db_path,bot_username=s.bot_username).issue_invite(710008,operator=True); print("https://max.ru/"+s.bot_username+"?start="+i["payload"])'
```

Ссылка действует 15 минут. Храните её приватно, не коммитьте и не прикладывайте
к отчётам. Запустите приёмник, откройте ссылку в MAX и нажмите «Начать».
Этот первоначальный оператор может переключать вымышленные профили через `/role`.
Обычные приглашённые пользователи получают отдельный профиль с закреплёнными правами.

На развёрнутом стенде хакатона `/start` показывает обложку и выбор роли для
отдельной веб-школы. Для такого режима добавьте в `connection.json` поле
`webDemoAccountsFile` с абсолютным путём к приватному JSON (в контейнере
например `/run/private/web-demo-accounts.json`). Файл принадлежит пользователю
процесса и имеет режим 0600. В нём четыре ключа: `student`, `teacher`, `planner`,
`director`. Значение каждого ключа — объект с полями `login` (`ifx.demo.<роль>`)
и `password` вымышленной учётной записи на демонстрационном стенде. Эти пары
показываются всем, кто открывает бота, поэтому подходят только для общих
учётных записей с тестовыми данными. Без файла используется личный вход по приглашению.

Для интеграции с очередью демо-школы на Taskflow задайте
`schoolNotificationsTokenFile` с путём к отдельному приватному ключу очереди.
Интеграция включается только на согласованном стенде. Самостоятельному запуску
бота для работы в MAX ключ очереди не нужен.

До запуска приёмника также можно подготовить необязательный вымышленный пример:

```sh
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/runtime/container-data,dst=/app/runtime" \
  --entrypoint python "${IDEYAFIX_IMAGE:-ideyafix-max:local}" -B -m app.demo_seed
```

Он создаёт один материал преподавателя и разные личные задания Славику и Саше
через собственные API, без вызова ИИ. Повтор не создаёт дубликаты.

Эквивалент без Compose:

```sh
docker run --rm --init --name ideyafix-max \
  --user "$(id -u):$(id -g)" --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --tmpfs /private/tmp:rw,nosuid,nodev,mode=1777 \
  --mount "type=bind,src=$PWD/runtime/container-private,dst=/run/private,readonly" \
  --mount "type=bind,src=$PWD/runtime/container-data,dst=/app/runtime" \
  "${IDEYAFIX_IMAGE:-ideyafix-max:local}" --config /run/private/connection.json
```

Для проверки конфигурации и бота используйте тот же запуск с `--check` после пути
настроек. При `enabled:true` эта команда обращается к MAX, проверяет имя бота и
отсутствие webhook. Для `--check` используйте отдельный каталог данных или
остановите второй процесс, если он работает с той же базой.

## Без Docker

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m app.polling --config /absolute/private/connection.json
```

В CLI задайте реальные абсолютные пути компьютера, а не `/run/private` и `/app`.
При необходимости локальная консоль запускается отдельно:

```sh
.venv/bin/python -B run.py --max-config /absolute/private/connection.json
```

Она слушает только `127.0.0.1:5211`. Пользователь бота работает непосредственно в MAX.
ИИ-генерация по умолчанию выключена. Для запуска достаточно конфигурации MAX.

Основание сборки: [официальный Python Docker image](https://hub.docker.com/_/python).
Настройки пользователя и контекста: [Dockerfile reference](https://docs.docker.com/reference/dockerfile/).
