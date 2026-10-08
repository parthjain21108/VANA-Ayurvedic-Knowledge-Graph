$ErrorActionPreference = "Stop"

$envFile = Join-Path $PSScriptRoot ".env"

if (-not (Test-Path $envFile)) {
    Write-Host ".env not found: $envFile" -ForegroundColor Red
    exit 1
}

Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()

    if ($line -and -not $line.StartsWith("#")) {
        $parts = $line -split "=", 2

        if ($parts.Count -eq 2) {
            $name = $parts[0].Trim()
            $value = $parts[1]

            [Environment]::SetEnvironmentVariable(
                $name,
                $value,
                "Process"
            )
        }
    }
}

Write-Host "Environment loaded from .env" -ForegroundColor Green
Write-Host "Starting FastAPI..." -ForegroundColor Cyan

python -m uvicorn api:app --host 127.0.0.1 --port 8000
