param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8790,
    [string]$Config = "config.example.json",
    [string]$VllmBaseUrl = "http://127.0.0.1:18000/v1"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = "C:\Users\alexs\miniforge3\envs\ai_env\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "ai_env Python was not found: $python"
}
if ([string]::IsNullOrWhiteSpace($env:PIPELINE_VLLM_API_KEY)) {
    throw "PIPELINE_VLLM_API_KEY is not set"
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
$env:PIPELINE_VLLM_BASE_URL = $VllmBaseUrl

Set-Location -LiteralPath $projectRoot
& $python "run_server.py" `
    "--config" $Config `
    "--host" $HostAddress `
    "--port" $Port
