# PhotoScan - accesso al telefono via Shell di Windows (Android, MTP) e via WPD diretto (iPhone).
# Sola lettura sul telefono: enumera cartelle e copia file in una cartella di staging.
# Input: variabile d'ambiente FOTOSCAN_PHONE_ARGS (JSON). Output: una riga JSON per messaggio su stdout.
# iPhone: Shell non espone il contenuto della memoria, quindi si usa WPD (IPortableDevice) direttamente.
# Su iPhone si importa solo la cartella DCIM.
# Script solo ASCII (PowerShell 5.1 legge i file senza BOM come ANSI).

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$cfg = $env:FOTOSCAN_PHONE_ARGS | ConvertFrom-Json
$action = $cfg.action
$MEDIA_EXT = @('.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif', '.dng', '.mp4', '.mov', '.3gp', '.m4v')
$SKIP_TOP = @('Android', '.trash-storage')

# Interfacce COM WPD. Ordine delle vtable da PortableDeviceApi.h / PortableDeviceTypes.h (SDK 10.0.22621);
# gli slot non usati sono placeholder P*, presenti solo per mantenere l'ordine.
$WPD_CS = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public struct PhoneWpdKey
{
    public Guid fmtid;
    public uint pid;
    public PhoneWpdKey(string g, uint p) { fmtid = new Guid(g); pid = p; }
}

public static class WpdKeys
{
    public static PhoneWpdKey OrigName = new PhoneWpdKey("EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C", 12); // WPD_OBJECT_ORIGINAL_FILE_NAME
    public static PhoneWpdKey ContentType = new PhoneWpdKey("EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C", 7); // WPD_OBJECT_CONTENT_TYPE
    public static PhoneWpdKey Size = new PhoneWpdKey("EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C", 11);       // WPD_OBJECT_SIZE
    public static PhoneWpdKey ResourceDefault = new PhoneWpdKey("E81E79BE-34F0-41BF-B53F-F1A06AE87842", 0); // WPD_RESOURCE_DEFAULT
    public static PhoneWpdKey ClientName = new PhoneWpdKey("204D9F0C-2292-4080-9F42-40664E70F859", 2);  // WPD_CLIENT_NAME
    public static Guid FolderType = new Guid("27E2E392-A111-48E0-AB0C-E17705A05F85");                  // WPD_CONTENT_TYPE_FOLDER
    public static Guid FunctionalType = new Guid("99ED0160-17FF-4C44-9D98-1D7A6F941921");              // WPD_CONTENT_TYPE_FUNCTIONAL_OBJECT
}

[ComImport, Guid("A1567595-4C2F-4574-A6FA-ECEF917B9A40"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdManager
{
    void GetDevices([MarshalAs(UnmanagedType.LPArray)] IntPtr[] ids, ref uint count);
    void RefreshDeviceList();
    void GetDeviceFriendlyName([MarshalAs(UnmanagedType.LPWStr)] string id, StringBuilder name, ref uint count);
}

[ComImport, Guid("625E2DF8-6392-4CF0-9AD1-3CFA5F17775C"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdDevice
{
    void Open([MarshalAs(UnmanagedType.LPWStr)] string id, IWpdValues clientInfo);
    void P4();
    void Content(out IWpdContent content);
    void P6();
    void P7();
    void Close();
}

[ComImport, Guid("6A96ED84-7C73-4480-9938-BF5AF477D426"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdContent
{
    void EnumObjects(uint flags, [MarshalAs(UnmanagedType.LPWStr)] string parentId, IntPtr filter, out IWpdEnum en);
    void Properties(out IWpdProps props);
    void Transfer(out IWpdResources res);
}

[ComImport, Guid("10ECE955-CF41-4728-BFA0-41EEDF1BBF19"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdEnum
{
    void Next(uint count, [MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 0)] IntPtr[] ids, out uint fetched);
}

[ComImport, Guid("7F6D695C-03DF-4439-A809-59266BEEE3A6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdProps
{
    void P3();
    void P4();
    void GetValues([MarshalAs(UnmanagedType.LPWStr)] string objectId, IWpdKeys keys, out IWpdValues values);
}

[ComImport, Guid("DADA2357-E0AD-492E-98DB-DD61C53BA353"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdKeys
{
    void P3();
    void P4();
    void Add(ref PhoneWpdKey key);
}

[ComImport, Guid("6848F6F2-3155-4F86-B6F5-263EEEAB3143"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdValues
{
    void P3(); void P4(); void P5(); void P6();
    void SetStringValue(ref PhoneWpdKey key, [MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetStringValue(ref PhoneWpdKey key, [MarshalAs(UnmanagedType.LPWStr)] out string value);
    void P9(); void P10(); void P11(); void P12(); void P13();
    void GetUnsignedLargeIntegerValue(ref PhoneWpdKey key, out ulong value);
    void P15(); void P16(); void P17(); void P18(); void P19(); void P20(); void P21(); void P22();
    void P23(); void P24(); void P25(); void P26(); void P27();
    void GetGuidValue(ref PhoneWpdKey key, out Guid value);
}

[ComImport, Guid("FD8878AC-D841-4D17-891C-E6829CDB6934"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdResources
{
    void P3();
    void P4();
    void GetStream([MarshalAs(UnmanagedType.LPWStr)] string objectId, ref PhoneWpdKey key, uint mode, ref uint optimalBufferSize, out IWpdStream stream);
}

[ComImport, Guid("0C733A30-2A1C-11CE-ADE5-00AA0044773D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IWpdStream
{
    [PreserveSig]
    int Read([MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 1)] byte[] pv, uint cb, out uint read);
}

[ComImport, Guid("0AF10CEC-2ECD-4B92-9581-34F6AE0637F3")] public class WpdManagerClass { }
[ComImport, Guid("F7C0039A-4762-488A-B4B3-760EF9A1BA9B")] public class WpdDeviceClass { }
[ComImport, Guid("0C15D503-D017-47CE-9016-7B3F978721CC")] public class WpdValuesClass { }
[ComImport, Guid("DE2D022D-2480-43BE-97F0-D1FA2CF98F4F")] public class WpdKeysClass { }

public class WpdItem
{
    public string Id;
    public string Name;
    public bool Folder;
    public long Size;   // -1 se sconosciuta
}

public class WpdDevice
{
    public string Id;
    public string Name;
}

public class WpdSession : IDisposable
{
    private IWpdDevice dev;
    private IWpdContent content;
    private IWpdProps props;
    private IWpdResources res;

    public WpdSession(string id)
    {
        dev = (IWpdDevice)new WpdDeviceClass();
        IWpdValues info = (IWpdValues)new WpdValuesClass();
        info.SetStringValue(ref WpdKeys.ClientName, "PhotoScan");
        dev.Open(id, info);
        dev.Content(out content);
        content.Properties(out props);
        content.Transfer(out res);
    }

    // Figli diretti di un oggetto ("DEVICE" = radice, cioe' gli storage).
    public List<WpdItem> Children(string parentId)
    {
        var list = new List<WpdItem>();
        IWpdEnum en;
        content.EnumObjects(0, parentId, IntPtr.Zero, out en);
        while (true)
        {
            IntPtr[] buf = new IntPtr[32];
            uint got;
            en.Next(32, buf, out got);
            if (got == 0) break;
            for (int i = 0; i < got; i++)
            {
                string oid = Marshal.PtrToStringUni(buf[i]);
                Marshal.FreeCoTaskMem(buf[i]);
                list.Add(Describe(oid));
            }
        }
        Marshal.ReleaseComObject(en);
        return list;
    }

    public WpdItem Describe(string oid)
    {
        var keys = (IWpdKeys)new WpdKeysClass();
        keys.Add(ref WpdKeys.OrigName);
        keys.Add(ref WpdKeys.ContentType);
        keys.Add(ref WpdKeys.Size);
        IWpdValues v;
        props.GetValues(oid, keys, out v);
        var it = new WpdItem { Id = oid, Name = "", Folder = false, Size = -1 };
        try { string n; v.GetStringValue(ref WpdKeys.OrigName, out n); it.Name = n ?? ""; } catch (COMException) { }
        try
        {
            Guid ct;
            v.GetGuidValue(ref WpdKeys.ContentType, out ct);
            it.Folder = ct == WpdKeys.FolderType || ct == WpdKeys.FunctionalType;
        }
        catch (COMException) { }
        try { ulong sz; v.GetUnsignedLargeIntegerValue(ref WpdKeys.Size, out sz); it.Size = (long)sz; } catch (COMException) { }
        Marshal.ReleaseComObject(keys);
        Marshal.ReleaseComObject(v);
        return it;
    }

    // Copia il contenuto di un file del telefono in dest. Lancia eccezione se la lettura fallisce.
    public void Download(string objectId, string dest)
    {
        uint optimal = 0;
        IWpdStream st;
        res.GetStream(objectId, ref WpdKeys.ResourceDefault, 0, ref optimal, out st);
        int size = optimal > 0 ? (int)Math.Min(optimal, 4u * 1024 * 1024) : 1024 * 1024;
        byte[] buf = new byte[size];
        using (var fs = new FileStream(dest, FileMode.CreateNew, FileAccess.Write))
        {
            while (true)
            {
                uint read;
                int hr = st.Read(buf, (uint)buf.Length, out read);
                if (hr < 0) Marshal.ThrowExceptionForHR(hr);
                if (read == 0) break;
                fs.Write(buf, 0, (int)read);
            }
        }
        Marshal.ReleaseComObject(st);
    }

    public void Dispose()
    {
        try { dev.Close(); } catch (COMException) { }
        if (res != null) Marshal.ReleaseComObject(res);
        if (props != null) Marshal.ReleaseComObject(props);
        if (content != null) Marshal.ReleaseComObject(content);
        if (dev != null) Marshal.ReleaseComObject(dev);
    }
}

public static class PhoneWpd
{
    public static List<WpdDevice> Devices()
    {
        var mgr = (IWpdManager)new WpdManagerClass();
        uint n = 0;
        mgr.GetDevices(null, ref n);
        var list = new List<WpdDevice>();
        if (n == 0) return list;
        IntPtr[] ptrs = new IntPtr[n];
        mgr.GetDevices(ptrs, ref n);
        for (int i = 0; i < n; i++)
        {
            string id = Marshal.PtrToStringUni(ptrs[i]);
            Marshal.FreeCoTaskMem(ptrs[i]);
            uint cap = 512;
            var sb = new StringBuilder((int)cap);
            mgr.GetDeviceFriendlyName(id, sb, ref cap);
            list.Add(new WpdDevice { Id = id, Name = sb.ToString() });
        }
        Marshal.ReleaseComObject(mgr);
        return list;
    }

    public static List<WpdDevice> AppleDevices()
    {
        var list = new List<WpdDevice>();
        foreach (var d in Devices())
            if (d.Id.IndexOf("vid_05ac", StringComparison.OrdinalIgnoreCase) >= 0) list.Add(d);
        return list;
    }

    public static string AppleDeviceId(string name)
    {
        foreach (var d in AppleDevices())
            if (d.Name == name) return d.Id;
        return null;
    }
}
'@

$shell = New-Object -ComObject Shell.Application

function Emit($obj) {
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $obj -Compress -Depth 12))
    [Console]::Out.Flush()
}

function Is-Media($item) {
    $ext = [System.IO.Path]::GetExtension($item.Name).ToLowerInvariant()
    return $MEDIA_EXT -contains $ext
}

function Is-MediaName([string]$name) {
    $ext = [System.IO.Path]::GetExtension($name).ToLowerInvariant()
    return $MEDIA_EXT -contains $ext
}

function Get-PhoneItems {
    # Dispositivi portatili (WPD): il percorso Shell contiene il GUID della classe MTP.
    $shell.Namespace(17).Items() | Where-Object { $_.Path -match '6ac27878' }
}

function Test-AppleDevice($dev) {
    # iPhone/iPad: il percorso Shell contiene l'ID fornitore Apple (vid_05ac), oppure il nome lo indica.
    return ([string]$dev.Path -match 'vid_05ac') -or ([string]$dev.Name -match 'iPhone|iPad')
}

function Test-AppleShellDevice([string]$name) {
    foreach ($d in Get-PhoneItems) {
        if ($d.Name -eq $name -and (Test-AppleDevice $d)) { return $true }
    }
    return $false
}

function Use-Wpd {
    # Compila il codice WPD solo quando serve (iPhone): evita il costo di Add-Type per i telefoni Android.
    if (-not ('WpdSession' -as [type])) { Add-Type -TypeDefinition $WPD_CS -ErrorAction Stop }
}

function Assert-PhoneScope($folders) {
    # Su iPhone si importa solo DCIM (foto e video della fotocamera).
    foreach ($p in $folders) {
        if (@($p)[0] -ne 'DCIM') { throw 'Su iPhone si importa solo la cartella DCIM.' }
    }
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

function Get-WpdNode($sess, [string]$storage, $path) {
    # Ritorna l'oggetto WPD della cartella indicata (path = array di nomi sotto lo storage).
    $st = $sess.Children('DEVICE') | Where-Object { $_.Name -eq $storage -and $_.Folder } | Select-Object -First 1
    if ($null -eq $st) { throw "Storage non trovato: $storage" }
    $node = $st
    foreach ($part in $path) {
        $node = $sess.Children($node.Id) | Where-Object { $_.Name -eq [string]$part -and $_.Folder } | Select-Object -First 1
        if ($null -eq $node) { throw "Cartella non trovata: $part" }
    }
    return $node
}

function Get-AppleWpdDevices {
    # Elenco degli iPhone collegati (via WPD) con i rispettivi storage.
    $out = @()
    foreach ($d in [PhoneWpd]::AppleDevices()) {
        $sess = New-Object WpdSession -ArgumentList $d.Id
        try {
            $st = @($sess.Children('DEVICE') | Where-Object { $_.Folder } | ForEach-Object { $_.Name })
        } finally {
            $sess.Dispose()
        }
        $out += [pscustomobject]@{ name = $d.Name; storages = @($st); apple = $true }
    }
    return $out
}

function Collect-WpdMedia($sess, [string]$itemId, [string[]]$relBase, $out) {
    foreach ($i in $sess.Children($itemId)) {
        if ($i.Folder) {
            Collect-WpdMedia $sess $i.Id ($relBase + $i.Name) $out
        } elseif (Is-MediaName $i.Name) {
            $out.Add([pscustomobject]@{ id = $i.Id; name = $i.Name; rel = @($relBase + $i.Name); size = $i.Size }) | Out-Null
        }
    }
}

function Invoke-WpdAction([string]$wpdId) {
    Use-Wpd
    $sess = New-Object WpdSession -ArgumentList $wpdId
    try {
        if ($action -eq 'list') {
            $parts = @($cfg.path)
            $node = Get-WpdNode $sess $cfg.storage $parts
            $folders = @()
            $media = 0
            foreach ($i in $sess.Children($node.Id)) {
                if ($i.Folder) {
                    if ($parts.Count -eq 0 -and $i.Name -ne 'DCIM') { continue }
                    $sub = $false
                    $direct = 0
                    foreach ($j in $sess.Children($i.Id)) {
                        if ($j.Folder) { $sub = $true } elseif (Is-MediaName $j.Name) { $direct++ }
                    }
                    $folders += [pscustomobject]@{ name = $i.Name; media = $direct; sub = $sub }
                } elseif (Is-MediaName $i.Name) {
                    $media++
                }
            }
            Emit @{ storage = $cfg.storage; path = $parts; media = $media; folders = @($folders) }
            return
        }

        Assert-PhoneScope $cfg.folders
        $list = New-Object System.Collections.ArrayList
        foreach ($p in $cfg.folders) {
            $parts = @($p)
            $node = Get-WpdNode $sess $cfg.storage $parts
            Collect-WpdMedia $sess $node.Id $parts $list
        }

        if ($action -eq 'scan') {
            $items = @()
            foreach ($e in $list) {
                $items += [pscustomobject]@{ rel = ($e.rel -join '/'); name = $e.name; size = $e.size }
            }
            Emit @{ type = 'scan'; items = @($items) }
            return
        }

        # copy: come per Android, i file gia' presenti (decisi prima dall'app) non vengono trasferiti
        $skipSet = @{}
        foreach ($s in @($cfg.skip)) { if ($s) { $skipSet[[string]$s] = $true } }
        $kept = New-Object System.Collections.ArrayList
        $skipped = 0
        foreach ($e in $list) {
            if ($skipSet.ContainsKey(($e.rel -join '/'))) { $skipped++ } else { $kept.Add($e) | Out-Null }
        }
        $totalBytes = 0
        $unknown = 0
        foreach ($e in $kept) { if ($e.size -ge 0) { $totalBytes += $e.size } else { $unknown++ } }
        if ($unknown -gt 0) { $totalBytes = -1 }
        Emit @{ type = 'total'; n = $kept.Count; bytes = $totalBytes; skipped = $skipped }

        $staging = $cfg.staging
        $idx = 0
        foreach ($entry in $kept) {
            $idx++
            $rel = @($entry.rel)
            $relDir = if ($rel.Count -gt 1) { Join-Path $staging ([System.IO.Path]::Combine([string[]]$rel[0..($rel.Count - 2)])) } else { $staging }
            New-Item -ItemType Directory -Force -Path $relDir | Out-Null
            $target = Join-Path $relDir $entry.name
            $ok = $false
            $err = $null
            try {
                $sess.Download($entry.id, $target)
                $len = (Get-Item -LiteralPath $target).Length
                if ($entry.size -lt 0 -or $len -eq $entry.size) { $ok = $true } else { $err = "dimensione diversa ($len di $($entry.size) byte)" }
            } catch {
                $err = $_.Exception.Message
            }
            if (-not $ok) { Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue }
            $bytes = 0
            if ($ok) { $bytes = (Get-Item -LiteralPath $target).Length }
            Emit @{ type = 'file'; i = $idx; rel = ($rel -join '/'); ok = $ok; bytes = $bytes; error = $err }
        }
        Emit @{ type = 'done' }
    } finally {
        $sess.Dispose()
    }
}

# Telefono iPhone selezionato: si legge via WPD (Shell non espone la memoria).
$wpdId = $null
if ($cfg.device -and (Test-AppleShellDevice ([string]$cfg.device))) {
    Use-Wpd
    $wpdId = [PhoneWpd]::AppleDeviceId([string]$cfg.device)
    if (-not $wpdId) { throw 'iPhone non accessibile: sbloccalo, lascia lo schermo acceso e tocca Consenti sul telefono.' }
}

if ($null -ne $wpdId -and ($action -ne 'list' -or $cfg.storage)) {
    Invoke-WpdAction $wpdId
    exit 0
}

# ---- list: navigazione a livelli (lenta su MTP, quindi una cartella alla volta)
# senza storage: dispositivi e storage. Con storage + path: figli diretti della cartella
# (solo file multimediali diretti come conteggio; "sub" indica se ci sono sottocartelle).
if ($action -eq 'list') {
    $devices = @()
    foreach ($dev in Get-PhoneItems) {
        if (Test-AppleDevice $dev) { continue }   # gli iPhone sono elencati via WPD (sotto)
        $storages = @()
        foreach ($st in $dev.GetFolder.Items()) {
            if ($st.IsFolder) { $storages += $st.Name }
        }
        $devices += [pscustomobject]@{ name = $dev.Name; storages = @($storages); apple = $false }
    }
    if (Get-PhoneItems | Where-Object { Test-AppleDevice $_ }) {
        Use-Wpd
        $devices += @(Get-AppleWpdDevices)
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

# ---- copy (Android, Shell): copia dei file multimediali delle cartelle scelte in staging (mantiene la struttura)
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
