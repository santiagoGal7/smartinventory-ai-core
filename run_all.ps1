# SmartInventory AI - Levanta backend .NET y microservicio FastAPI en ventanas separadas.

$ErrorActionPreference = "Stop"

$dotnetProject = "C:\Users\santy\Documentos\Softwaree\dev1\ProjectNetIa"
$fastApiRoot = "C:\Users\santy\Documentos\Softwaree\smartinventory-ai-core"

Write-Host "Iniciando backend .NET (ProjectNetIa)..." -ForegroundColor Cyan
Start-Process -FilePath "dotnet" `
    -ArgumentList "run", "--project", $dotnetProject `
    -WorkingDirectory $dotnetProject

Write-Host "Iniciando FastAPI (uvicorn)..." -ForegroundColor Cyan
Start-Process -FilePath "uvicorn" `
    -ArgumentList "main:app", "--reload", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $fastApiRoot

Write-Host "Servicios iniciados en ventanas separadas." -ForegroundColor Green
Write-Host "  .NET API: http://localhost:5083" -ForegroundColor Yellow
Write-Host "  FastAPI:  http://127.0.0.1:8000" -ForegroundColor Yellow
