# Liriel Privada — one command to chat via Telegram, with MindReader open.
#
# Starts the local llama-server (scripts/llamacpp/start_server.sh, via Git
# Bash) and MindReader (mindreader/app.py — it opens its own browser tab at
# http://localhost:5050 on its own, nothing extra needed here), waits for
# the llama-server to report healthy, then runs telegram_bot.py in the
# foreground. Stops both background processes automatically when the bot
# exits or you press Ctrl+C — no separate terminals, no manual waiting.
#
# Usage (from this folder, in PowerShell):
#   .\run_telegram.ps1
#
# Override the model size for this run only, same as start_server.sh itself:
#   $env:LLAMA_MODEL_SIZE = "26b"; .\run_telegram.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- Locate Git Bash (start_server.sh needs it; PowerShell can't run a
# .sh script on its own) ------------------------------------------------
$bash = "C:\Program Files\Git\bin\bash.exe"
if (-not (Test-Path $bash)) {
    $found = Get-Command bash.exe -ErrorAction SilentlyContinue
    if ($found) { $bash = $found.Source }
}
if (-not (Test-Path $bash)) {
    Write-Error "Git Bash not found (expected at 'C:\Program Files\Git\bin\bash.exe'). scripts\llamacpp\start_server.sh needs it to run."
    exit 1
}

# --- Read the port llamacpp_client.py itself will call, straight out of
# .env, so this never drifts from what's actually configured -----------
$port = "8080"
$envLine = Select-String -Path (Join-Path $root ".env") -Pattern "^LLAMACPP_BASE_URL=" -ErrorAction SilentlyContinue
if ($envLine) {
    if ($envLine.Line -match ":(\d+)\s*$") { $port = $Matches[1] }
}
$healthUrl = "http://localhost:$port/health"

# --- Start llama-server (backgrounded; start_server.sh itself owns model
# choice, context size, warm-up — nothing about that is duplicated here) -
$logOut = Join-Path $root "llama-server.out.log"
$logErr = Join-Path $root "llama-server.err.log"
Write-Host "Starting llama-server (logs: $logOut / $logErr)..."
$serverProc = Start-Process -FilePath $bash `
    -ArgumentList @("scripts/llamacpp/start_server.sh") `
    -WorkingDirectory $root `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError $logErr `
    -WindowStyle Hidden -PassThru

# --- Start MindReader (backgrounded; it opens its own browser tab once
# Flask is listening, via its own __main__ block -- nothing to wait on
# here, it can come up in parallel with everything else) ----------------
$mrLogOut = Join-Path $root "mindreader.out.log"
$mrLogErr = Join-Path $root "mindreader.err.log"
Write-Host "Starting MindReader (logs: $mrLogOut / $mrLogErr)..."
$mindreaderProc = Start-Process -FilePath "python" `
    -ArgumentList @("mindreader/app.py") `
    -WorkingDirectory $root `
    -RedirectStandardOutput $mrLogOut `
    -RedirectStandardError $mrLogErr `
    -WindowStyle Hidden -PassThru

function Stop-LlamaServer {
    if ($serverProc -and -not $serverProc.HasExited) {
        Write-Host "Stopping llama-server (pid $($serverProc.Id))..."
        # /T kills the whole process tree -- start_server.sh's own bash
        # process AND the llama-server.exe child it launched, not just
        # the bash wrapper (its EXIT trap can't be trusted to fire the
        # same way under a Windows process-tree kill).
        & taskkill /PID $serverProc.Id /T /F 2>$null | Out-Null
    }
}

function Stop-MindReader {
    if ($mindreaderProc -and -not $mindreaderProc.HasExited) {
        Write-Host "Stopping MindReader (pid $($mindreaderProc.Id))..."
        & taskkill /PID $mindreaderProc.Id /T /F 2>$null | Out-Null
    }
}

try {
    Write-Host "Waiting for llama-server to report healthy at $healthUrl (model load + prefix warm-up, usually well under a minute)..."
    # curl.exe (the real Windows-native binary at System32\curl.exe), not
    # Invoke-WebRequest: confirmed for real that Invoke-WebRequest can
    # fail silently on every single attempt in this exact script context
    # (empty catch swallowed whatever it was), leaving the loop spinning
    # for several minutes after the server had already reported healthy
    # in its own log -- the same curl.exe start_server.sh's own bash
    # health-check already relies on, so this matches proven behavior
    # instead of introducing a second, less reliable check.
    $healthy = $false
    for ($i = 0; $i -lt 150; $i++) {
        if ($serverProc.HasExited) {
            Write-Error "llama-server exited before becoming healthy -- check $logOut / $logErr"
            exit 1
        }
        & curl.exe -sf -o $null -w "%{http_code}" $healthUrl 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $healthy = $true; break }
        Write-Host -NoNewline "."
        Start-Sleep -Seconds 2
    }
    Write-Host ""
    if (-not $healthy) {
        Write-Error "llama-server never became healthy after 5 minutes -- check $logOut / $logErr"
        exit 1
    }
    Write-Host "llama-server is healthy. Starting telegram_bot.py -- Ctrl+C to stop everything."
    python telegram_bot.py
}
finally {
    Stop-LlamaServer
    Stop-MindReader
}
