# Prova di fumo di PhotoScan.exe (usata da .github/workflows/release.yml, eseguibile anche in locale):
#   .\smoke.ps1 -Exe dist\PhotoScan\PhotoScan.exe -Mode runtime-check -ExpectExternal false
#   .\smoke.ps1 -Exe dist\PhotoScan\PhotoScan.exe -Mode runtime-check -RuntimeDir C:\tmp\rt -ExpectExternal true
#   .\smoke.ps1 -Exe dist\PhotoScan\PhotoScan.exe -Mode help
# runtime-check: PhotoScan.exe --runtime-check importa torch, torchvision, transformers... e scrive un JSON;
#   -RuntimeDir e' la cartella con <versione>-<cpu|gpu> (FOTOSCAN_RUNTIME_DIR), -ExpectExternal dice se il runtime
#   deve venire da li (app leggera) o da _internal\runtime (pacchetto completo).
# help: PhotoScan.exe -m fotoscan --help (l'app parte e il codice si importa).
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [ValidateSet('runtime-check', 'help')][string]$Mode = 'runtime-check',
    [string]$RuntimeDir = '',
    [ValidateSet('true', 'false', 'any')][string]$ExpectExternal = 'any'
)
$ErrorActionPreference = 'Stop'
$tmp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$stamp = [guid]::NewGuid().ToString('N')
$json = Join-Path $tmp "smoke-$stamp.json"
$stdout = Join-Path $tmp "smoke-$stamp.out"
$stderr = Join-Path $tmp "smoke-$stamp.err"
if ($RuntimeDir) { $env:FOTOSCAN_RUNTIME_DIR = $RuntimeDir } else { Remove-Item Env:FOTOSCAN_RUNTIME_DIR -ErrorAction SilentlyContinue }
if ($Mode -eq 'runtime-check') { $argList = @('--runtime-check', $json) } else { $argList = @('-m', 'fotoscan', '--help') }
$p = Start-Process -FilePath (Resolve-Path $Exe) -ArgumentList $argList -Wait -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
Write-Host "PhotoScan.exe $($argList -join ' ') -> codice $($p.ExitCode)"
if (Test-Path $stdout) { Get-Content $stdout -TotalCount 20 | Write-Host }
if ($p.ExitCode -ne 0) {
    if (Test-Path $json) { Get-Content $json | Write-Host }
    if (Test-Path $stderr) { Get-Content $stderr -Tail 40 | Write-Host }
    throw "Prova di fumo fallita ($Mode, codice $($p.ExitCode))"
}
if ($Mode -eq 'runtime-check') {
    $res = Get-Content $json -Raw | ConvertFrom-Json
    $res.modules.PSObject.Properties | ForEach-Object { Write-Host ("  {0}: {1}" -f $_.Name, $_.Value.version) }
    if (-not $res.ok) { throw 'runtime-check: ok = false' }
    if ($ExpectExternal -ne 'any' -and ([string]$res.runtime.external).ToLower() -ne $ExpectExternal) {
        throw "runtime-check: runtime esterno = $($res.runtime.external), atteso $ExpectExternal"
    }
}
