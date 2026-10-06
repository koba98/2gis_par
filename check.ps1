# Проверка перед запуском на Windows (Docker Desktop):  powershell -ExecutionPolicy Bypass -File .\check.ps1
# Смотрит Git, Docker, файлы проекта, .env, память и диск, затем запускает проверку внутри контейнера:
# БД, доступ к 2ГИС по каждому прокси, API отзывов, Chrome.
$ErrorActionPreference = "Continue"
$fail = 0
function Report($ok, $what, $hint) {
    if ($ok) { Write-Host "  OK   $what" -ForegroundColor Green }
    else { Write-Host "  FAIL $what  ->  $hint" -ForegroundColor Red; $script:fail++ }
}
Set-Location $PSScriptRoot
Write-Host "Проверка компьютера"

Report ([bool](Get-Command git -ErrorAction SilentlyContinue)) "git установлен" "поставьте Git for Windows"
$docker = [bool](Get-Command docker -ErrorAction SilentlyContinue)
Report $docker "docker установлен" "поставьте Docker Desktop"
if ($docker) {
    docker info *> $null
    Report ($LASTEXITCODE -eq 0) "Docker Desktop запущен" "запустите Docker Desktop и дождитесь Engine running"
    docker compose version *> $null
    Report ($LASTEXITCODE -eq 0) "docker compose доступен" "обновите Docker Desktop"
}

foreach ($f in "Dockerfile", "docker-compose.yml", "docker-compose.pc.yml", "docker-entrypoint.sh",
               "requirements.txt", "main.py", "parser.py", "fetcher.py", "storage.py", "sql\schema.sql", ".env") {
    Report (Test-Path $f) "файл $f" ($(if ($f -eq ".env") { "copy .env.example .env и заполните PG_DSN" } else { "git pull" }))
}

if (Test-Path .env) {
    $envText = Get-Content .env -Raw
    $dsn = [regex]::Match($envText, '(?m)^PG_DSN=(.*)$').Groups[1].Value.Trim()
    Report ($dsn -and $dsn -notmatch 'password@localhost') "PG_DSN заполнен" "впишите строку подключения к вашей БД"
    Report ($dsn -notmatch '@localhost[:/]') "PG_DSN не указывает на localhost" "из контейнера localhost — это сам контейнер: укажите IP сервера БД или host.docker.internal"
    $proxies = [regex]::Matches($envText, '(?m)^DGIS_PROXY(_\d)?=\S+').Count
    Write-Host "  ...  прокси в .env: $proxies (каждый прокси — ещё одна пара воркеров, профили proxy2..proxy4)"
}

$os = Get-CimInstance Win32_OperatingSystem
$freeGb = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
Report ($freeGb -ge 3) "свободная память: $freeGb ГБ" "нужно >= 3 ГБ на пару воркеров (обычно занимают ~1,2 ГБ)"
$disk = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':'))
$diskGb = [math]::Round($disk.Free / 1GB, 1)
Report ($diskGb -ge 5) "свободно на диске ${env:SystemDrive}: $diskGb ГБ" "под образ нужно ~2 ГБ"

$standby = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -match 'Current AC Power Setting Index: 0x00000000|Текущий индекс параметра питания от сети: 0x00000000'
Report ([bool]$standby) "сон при питании от сети отключён" "powercfg /change standby-timeout-ac 0 (иначе обход встанет, когда компьютер уснёт)"

if ($fail -eq 0 -and $docker) {
    Write-Host "`nПроверка внутри контейнера (собирает образ при первом запуске)"
    docker compose -f docker-compose.yml -f docker-compose.pc.yml run --rm parser check
    if ($LASTEXITCODE -ne 0) { $fail++ }
}
Write-Host ""
if ($fail -eq 0) { Write-Host "Всё в порядке. Запуск: docker compose -f docker-compose.yml -f docker-compose.pc.yml up -d kz kz-http" -ForegroundColor Green }
else { Write-Host "Проблем: $fail — исправьте пункты FAIL и запустите проверку ещё раз" -ForegroundColor Red }
exit $fail
