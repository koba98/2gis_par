"""
Скрипт автоматической интеграции с GitHub:
- Инициализирует git-репозиторий
- Привязывает origin к https://github.com/koba98/2gis_par
- Создает ветку main
- Индексирует файлы (учитывая .gitignore)
- Делает коммит и выполняет git push
"""

import argparse
import os
import subprocess
import sys


DEFAULT_REMOTE_URL = "https://github.com/koba98/2gis_par"


def run_command(cmd, check=True):
    """Выполняет команду оболочки и возвращает результат."""
    print(f"-> Выполнение: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(f"Ошибка: {result.stderr.strip()}", file=sys.stderr)
        if check:
            sys.exit(result.returncode)
    return result


def main():
    parser = argparse.ArgumentParser(description="Скрипт инициализации и пуша в GitHub-репозиторий.")
    parser.add_argument("--remote", default=DEFAULT_REMOTE_URL, help=f"Remote URL (по умолчанию: {DEFAULT_REMOTE_URL})")
    parser.add_argument("--branch", default="main", help="Целевая ветка (по умолчанию: main)")
    parser.add_argument("--message", default="Initial commit: autonomous 2GIS catalog and reviews parser with anti-ban and SQLite storage", help="Сообщение коммита")
    parser.add_argument("--token", default=None, help="GitHub Personal Access Token (опционально, для авторизации)")
    args = parser.parse_args()

    # 1. Проверяем наличие .git или выполняем git init
    if not os.path.exists(".git"):
        print("[1/5] Инициализация локального git-репозитория...")
        run_command(["git", "init", "-b", args.branch])
    else:
        print("[1/5] Локальный git-репозиторий уже инициализирован.")
        run_command(["git", "branch", "-M", args.branch], check=False)

    # 2. Проверяем или настраиваем remote origin
    remote_url = args.remote
    if args.token:
        # Если передан токен, внедряем его в URL для пуша без пароля
        # https://<token>@github.com/koba98/2gis_par.git
        if remote_url.startswith("https://"):
            remote_url = remote_url.replace("https://", f"https://{args.token}@")

    print("[2/5] Настройка remote origin...")
    existing_remote = run_command(["git", "remote", "get-url", "origin"], check=False)
    if existing_remote.returncode == 0:
        run_command(["git", "remote", "set-url", "origin", remote_url])
    else:
        run_command(["git", "remote", "add", "origin", remote_url])

    # 3. Индексация файлов
    print("[3/5] Индексация файлов (git add .)...")
    run_command(["git", "add", "."])

    # 4. Создание коммита
    print("[4/5] Создание коммита...")
    status = run_command(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        print("Нет новых изменений для коммита.")
    else:
        run_command(["git", "commit", "-m", args.message])

    # 5. Выполнение push
    print(f"[5/5] Выполнение git push origin {args.branch}...")
    push_res = run_command(["git", "push", "-u", "origin", args.branch], check=False)
    if push_res.returncode == 0:
        print("\nУСПЕХ! Проект успешно запушен в GitHub-репозиторий:")
        print(args.remote)
    else:
        print("\nВнимание: git push завершился с ошибкой (вероятно, требуется авторизация GitHub).")
        print("Для отправки выполните:")
        print(f"  git push -u origin {args.branch}")
        print("Или запустите скрипт с токеном доступа:")
        print(f"  python push_to_github.py --token YOUR_GITHUB_TOKEN")


if __name__ == "__main__":
    main()
