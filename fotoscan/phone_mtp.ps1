# FotoScan - accesso al telefono Android (MTP) tramite Shell di Windows.
# Sola lettura sul telefono: enumera cartelle e copia file in una cartella di staging.
# Input: variabile d'ambiente FOTOSCAN_PHONE_ARGS (JSON). Output: una riga JSON per messaggio su stdout.
# Script solo ASCII (PowerShell 5.1 legge i file senza BOM come ANSI).

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$cfg = $env:FOTOSCAN_PHONE_ARGS | ConvertFrom-Json
$action = $cfg.action
$MEDIA_EXT = @('.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif', '.dng', '.mp4', '.mov', '.3gp', '.m4v')
$SKIP_TOP = @('Android', '.trash-storage')

$shell = New-Object -ComObject Shell.Application

function Emit($obj) {
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $obj -Compress -Depth 12))
    [Console]::Out.Flush()
}

function Is-Media($item) {
    $ext = [System.IO.Path]::GetExtension($item.Name).ToLowerInvariant()
    return $MEDIA_EXT -contains $ext
}

function Get-PhoneItems {
    # Dispositivi portatili (WPD): il percorso Shell contiene il GUID della classe MTP.
    $shell.Namespace(17).Items() | Where-Object { $_.Path -match '6ac27878' }
}

function Get-ChildByName($folder, [string]$name) {
    foreach ($i in $folder.Items()) {
        if ($i.Name -eq $name) { return $i }
    }
    return $null
}

function Resolve-Node($device, [string]$storage, $path) {
    # Ritorna l'oggetto Shell della cartella indicata (path = array di nomi sotto lo storage).
    $st = Get-ChildByName $device.GetFolder $storage
    if ($null -eq $st) { throw "Storage non trovato: $storage" }
    $node = $st
    foreach ($part in $path) {
        $node = Get-ChildByName $node.GetFolder ([string]$part)
        if ($null -eq $node) { throw "Cartella non trovata: $part" }
    }
    return $node
}

# ---- list: navigazione a livelli (lenta su MTP, quindi una cartella alla volta)
# senza storage: dispositivi e storage. Con storage + path: figli diretti della cartella
# (solo file multimediali diretti come conteggio; "sub" indica se ci sono sottocartelle).
if ($action -eq 'list') {
    $devices = @()
    foreach ($dev in Get-PhoneItems) {
        $storages = @()
        foreach ($st in $dev.GetFolder.Items()) {
            if ($st.IsFolder) { $storages += $st.Name }
        }
        $devices += [pscustomobject]@{ name = $dev.Name; storages = @($storages) }
    }
    if (-not $cfg.storage) {
        Emit @{ devices = $devices }
        exit 0
    }
    $dev = Get-PhoneItems | Where-Object { $_.Name -eq $cfg.device } | Select-Object -First 1
    if ($null -eq $dev) { throw "Telefono non collegato o non sbloccato: $($cfg.device)" }
    $parts = @($cfg.path)
    $node = Resolve-Node $dev $cfg.storage $parts
    $folders = @()
    $media = 0
    foreach ($i in $node.GetFolder.Items()) {
        if ($i.IsFolder) {
            if ($parts.Count -eq 0 -and ($SKIP_TOP -contains $i.Name)) { continue }
            $sub = $false
            $direct = 0
            foreach ($j in $i.GetFolder.Items()) {
                if ($j.IsFolder) { $sub = $true } elseif (Is-Media $j) { $direct++ }
            }
            $folders += [pscustomobject]@{ name = $i.Name; media = $direct; sub = $sub }
        } elseif (Is-Media $i) {
            $media++
        }
    }
    Emit @{ devices = $devices; storage = $cfg.storage; path = $parts; media = $media; folders = @($folders) }
    exit 0
}

# ---- copy: copia dei file multimediali delle cartelle scelte in staging (mantiene la struttura)
function Collect-Media($item, [string[]]$relBase, $out) {
    foreach ($i in $item.GetFolder.Items()) {
        if ($i.IsFolder) {
            Collect-Media $i ($relBase + $i.Name) $out
        } elseif (Is-Media $i) {
            $size = -1
            try { $size = [long]$i.ExtendedProperty('System.Size') } catch { $size = -1 }
            $out.Add([pscustomobject]@{ item = $i; rel = @($relBase + $i.Name); size = $size }) | Out-Null
        }
    }
}

if ($action -eq 'scan') {
    # Solo elenco (nome, dimensione, percorso relativo) dei file multimediali: nessuna copia.
    $dev = Get-PhoneItems | Where-Object { $_.Name -eq $cfg.device } | Select-Object -First 1
    if ($null -eq $dev) { throw "Telefono non collegato o non sbloccato: $($cfg.device)" }
    $list = New-Object System.Collections.ArrayList
    foreach ($p in $cfg.folders) {
        $parts = @($p)
        $node = Resolve-Node $dev $cfg.storage $parts
        Collect-Media $node $parts $list
    }
    $items = @()
    foreach ($e in $list) {
        $items += [pscustomobject]@{ rel = ($e.rel -join '/'); name = $e.item.Name; size = $e.size }
    }
    Emit @{ type = 'scan'; items = @($items) }
    exit 0
}

if ($action -eq 'copy') {
    $dev = Get-PhoneItems | Where-Object { $_.Name -eq $cfg.device } | Select-Object -First 1
    if ($null -eq $dev) { throw "Telefono non collegato o non sbloccato: $($cfg.device)" }

    $list = New-Object System.Collections.ArrayList
    foreach ($p in $cfg.folders) {
        $parts = @($p)
        $node = Resolve-Node $dev $cfg.storage $parts
        Collect-Media $node $parts $list
    }
    # file da saltare (gia' presenti, decisi dall'app prima della copia): nessun trasferimento
    $skipSet = @{}
    foreach ($s in @($cfg.skip)) { if ($s) { $skipSet[[string]$s] = $true } }
    $kept = New-Object System.Collections.ArrayList
    $skipped = 0
    foreach ($e in $list) {
        if ($skipSet.ContainsKey(($e.rel -join '/'))) { $skipped++ } else { $kept.Add($e) | Out-Null }
    }
    $list = $kept
    $totalBytes = 0
    $unknown = 0
    foreach ($e in $list) { if ($e.size -ge 0) { $totalBytes += $e.size } else { $unknown++ } }
    if ($unknown -gt 0) { $totalBytes = -1 }   # dimensione non nota per alcuni file: la barra usera' il conteggio
    Emit @{ type = 'total'; n = $list.Count; bytes = $totalBytes; skipped = $skipped }

    $staging = $cfg.staging
    $idx = 0
    foreach ($entry in $list) {
        $idx++
        $rel = $entry.rel
        $item = $entry.item
        $relDir = if ($rel.Count -gt 1) { Join-Path $staging ([System.IO.Path]::Combine([string[]]$rel[0..($rel.Count - 2)])) } else { $staging }
        New-Item -ItemType Directory -Force -Path $relDir | Out-Null
        $relPath = ($rel -join '/')
        $expected = -1
        try { $expected = [long]$item.ExtendedProperty('System.Size') } catch { $expected = -1 }
        $target = Join-Path $relDir $item.Name
        $ok = $false
        $err = $null
        try {
            $shell.Namespace($relDir).CopyHere($item, 1028)   # 4 = niente progresso, 1024 = niente finestre
            $deadline = (Get-Date).AddSeconds(300)
            $last = -1
            while ((Get-Date) -lt $deadline) {
                if (Test-Path -LiteralPath $target) {
                    $len = (Get-Item -LiteralPath $target).Length
                    if (($expected -ge 0 -and $len -eq $expected) -or ($expected -lt 0 -and $len -gt 0 -and $len -eq $last)) {
                        $ok = $true; break
                    }
                    $last = $len
                }
                Start-Sleep -Milliseconds 400
            }
            if (-not $ok) { $err = 'timeout nella copia' }
        } catch {
            $err = $_.Exception.Message
        }
        $bytes = 0
        if ($ok) { $bytes = (Get-Item -LiteralPath $target).Length }
        Emit @{ type = 'file'; i = $idx; rel = $relPath; ok = $ok; bytes = $bytes; error = $err }
    }
    Emit @{ type = 'done' }
    exit 0
}

throw "azione sconosciuta: $action"
