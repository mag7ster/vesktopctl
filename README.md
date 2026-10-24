# vesktopctl

Глобальное переключение mute/deafen в Vesktop через niri или DMS. Это событийная версия идеи [vesktopCustomCommands (VCC)](https://github.com/NitramO-YT/vesktopCustomCommands): код в главном процессе Vesktop получает команду, выполняет JavaScript в окне Discord и нажимает соответствующую кнопку. Файлов-команд, опроса раз в 350 мс, таймеров и внешнего демона здесь нет.

## Как это работает

`vesktopctl mute|deafen` подключается к `$XDG_RUNTIME_DIR/vesktopctl/control.sock`. Сервер работает внутри Vesktop, последовательно выполняет команды и отвечает `OK` только после успешного нажатия кнопки. Команды **переключают** состояние, как в исходном VCC. Если кнопка недоступна, CLI возвращает ошибку и ненулевой код выхода.

Для Flatpak `$XDG_RUNTIME_DIR` внутри sandbox обычно отличается от каталога на хосте. Сервер использует общий путь `/run/user/<UID>/vesktopctl/control.sock`, а узкое разрешение Flatpak открывает только каталог `vesktopctl` из runtime хоста. Каталог принадлежит пользователю и имеет режим `0700`, сокет — `0600`.

## Установка для Vesktop Flatpak

Нужны Python 3 на хосте и установленный Vesktop `dev.vencord.Vesktop`. Один раз запустите Vesktop, чтобы он загрузил файлы Vencord. Затем из каталога проекта:

```sh
python3 install.py
```

Установщик сам найдёт используемый Vencord bundle, создаст каталог сокета, добавит точечное разрешение Flatpak и скопирует CLI в `~/.local/bin`. Если найдены Flatpak и обычный Vesktop одновременно, укажите `python3 install.py --target flatpak`. Если в настройках Vesktop выбран собственный Vencord build, установщик прочитает его путь из `state.json`; путь можно также задать вручную через `--bundle /путь/к/vencordDesktopMain.js` или `--bundle /путь/к/папке`. Для собственного пути вне каталога Flatpak при наличии нескольких установок добавьте `--target flatpak`.

Если Vesktop уже работает, установщик перезапустит его один раз после успешной установки. Если он закрыт, установщик его не запускает. Для ручного перезапуска можно добавить `--no-restart`. Проверьте команды после запуска Vesktop:

```sh
vesktopctl mute
vesktopctl deafen
```

Если `~/.local/bin` отсутствует в `PATH`, используйте абсолютный путь в конфигурации горячих клавиш. Для DMS назначьте команды `vesktopctl mute` и `vesktopctl deafen`.

Пример для `~/.config/niri/config.kdl`:

```kdl
binds {
    Mod+Shift+M repeat=false { spawn "vesktopctl" "mute"; }
    Mod+Shift+D repeat=false { spawn "vesktopctl" "deafen"; }
}
```

`repeat=false` исключает повторное переключение при удержании клавиши. Примените конфигурацию niri обычным способом. Если другие сочетания уже заняты, выберите свободные.

## Обновления Vesktop и Vencord

`install.py` читает путь активного Vencord из настройки Vesktop `vencordDir`. Для стандартной установки он проверяет текущий `sessionData/vencordFiles` и старый `Vencord/dist`. Исходные файлы, их права и сведения о созданных каталогах сохраняются в скрытом каталоге `~/.vesktopctl/`. Там же хранится исходный пользовательский Flatpak override. Повторная установка не дублирует вставку.

После обновления Vencord файл bundle может быть заменён, поэтому работающий Vesktop нужно закрыть и запустить через установленный `vesktopctl-vesktop`:

```sh
vesktopctl-vesktop
```

Этот запуск однократно проверяет и при необходимости восстанавливает патч **перед** стартом Vesktop; при автоматической установке он также повторно определяет активный путь, если вы переключили Vencord build в настройках. Если при установке был явно указан `--bundle`, launcher использует именно его. Запуск обычным значком Flatpak не вызывает установщик: в этом случае после обновления выполните `python3 ~/.local/share/vesktopctl/install.py --patch-only --target flatpak` и перезапустите Vesktop. Таймеров и фоновой проверки нет.

Если CLI сообщает `cannot contact Vesktop`, убедитесь, что Vesktop перезапущен после установки, Flatpak override применён и сокет существует. Если команда сообщает, что кнопка не найдена, откройте окно Discord; селекторы могут потребовать обновления после изменения интерфейса Discord.

## Удаление

Запустите:

```sh
python3 ~/.local/share/vesktopctl/uninstall.py
```

Можно также запустить `python3 uninstall.py` из каталога проекта. Скрипт восстановит прежние версии заменённых файлов и пользовательский Flatpak override, удалит созданные CLI, launcher, bridge и пустые каталоги. После успешного удаления каталог резервных копий `~/.vesktopctl/` тоже удаляется.

Если Vesktop был запущен, деинсталлятор остановит его перед откатом и запустит снова после него. Если Vesktop был закрыт, он останется закрытым. Для ручного управления перезапуском есть `--no-restart`: при удалении с этой опцией сначала закройте Vesktop самостоятельно.

Если Vencord обновился после установки, деинсталлятор сохраняет новый bundle и удаляет из него только код vesktopctl. Если какой-то другой управляемый файл был изменён после установки, скрипт оставит его и резервную копию нетронутыми, сообщит об этом и позволит повторить удаление после проверки. Для установок старых версий этого проекта без `~/.vesktopctl/manifest.json` точный автоматический откат недоступен: прежнее состояние файлов не было сохранено.

Источники: [исходный код VCC](https://github.com/NitramO-YT/vesktopCustomCommands/blob/main/dist/vencord/customCode.js), [документация Flatpak о `xdg-run/path`](https://docs.flatpak.org/en/latest/sandbox-permissions.html), [заметки Vesktop о Flatpak](https://vesktop.dev/wiki/linux/flatpak/), [документация niri о горячих клавишах](https://github.com/niri-wm/niri/wiki/Configuration%3A-Key-Bindings).
