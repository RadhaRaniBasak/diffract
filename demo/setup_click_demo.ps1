param([string]$Target = "click-demo")
$ErrorActionPreference = "Stop"
$BaseCommit = "25edc1e31360efba4f64c8c3c540bbbcfb7876da"
$HeadCommit = "0039359443e73ab1034c63a1f6d58aba22c0ebc8"

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

if (Test-Path $Target) { throw "$Target already exists" }
Write-Host "==> cloning pallets/click into $Target"
Invoke-Checked git @("clone", "--quiet", "https://github.com/pallets/click.git", $Target)
Set-Location $Target
$RepoDir = (Get-Location).Path
$VenvDir = "$RepoDir-venv"

Write-Host "==> creating branches base-monster and monster-pr (one squashed commit)"
Invoke-Checked git @("branch", "base-monster", $BaseCommit)
Invoke-Checked git @("checkout", "--quiet", "-b", "monster-pr", $HeadCommit)
Invoke-Checked git @("reset", "--quiet", "--soft", "base-monster")
Invoke-Checked git @("-c", "user.name=Diffract Demo", "-c", "user.email=demo@example.com", "commit", "--quiet", "-m",
    "Q2 CLI improvements: typing, i18n, command suggestions, pager API, prompt and completion fixes")
Invoke-Checked git @("diff", "--shortstat", "base-monster", "monster-pr")

Write-Host "==> creating test environment at $VenvDir (pytest 9.0.2, the version click pins here)"
$Python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
Invoke-Checked $Python @("-m", "venv", $VenvDir)
Invoke-Checked "$VenvDir\Scripts\python.exe" @("-m", "pip", "install", "--quiet", "pytest==9.0.2")

Write-Host "==> baseline check at the branch head"
$env:PYTHONPATH = "src"
Invoke-Checked "$VenvDir\Scripts\python.exe" @("-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", "not pager")
Write-Host ""
Write-Host "Ready. Open $RepoDir in Bob and use this verify command (Bob runs it through cmd):"
Write-Host "  set PYTHONPATH=src&& `"$VenvDir\Scripts\python.exe`" -m pytest -q -p no:cacheprovider -k `"not pager`""
