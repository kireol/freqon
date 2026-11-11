<#
.SYNOPSIS
  Install Freqon on Windows 10 or 11 (64-bit). *UNTESTED: not yet run on a real Windows PC.

.DESCRIPTION
  Double-click deploy\install-windows.cmd, or in an administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 [-NoService] [-Uninstall]

  Installs Python, ffmpeg and MSYS2 with winget, builds nrsc5 in MSYS2, installs Freqon into
  C:\ProgramData\Freqon, and starts it hidden at every sign-in of the user who ran this.
  Settings: %USERPROFILE%\.config\freqon\config.toml. Stations and logos:
  %USERPROFILE%\.local\share\freqon. Log: %LOCALAPPDATA%\Freqon\freqon.log

  -NoService   install only; don't start it at sign-in
  -Uninstall   stop it, remove the sign-in task and the firewall rule (keeps settings)
#>
param(
    [switch]$NoService,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Src = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Prefix = if ($env:FREQON_PREFIX) { $env:FREQON_PREFIX } else { Join-Path $env:ProgramData 'Freqon' }
$Msys = if ($env:FREQON_MSYS2) { $env:FREQON_MSYS2 } else { 'C:\msys64' }
$TaskName = 'Freqon'
$Port = 8080
$LogFile = Join-Path $env:LOCALAPPDATA 'Freqon\freqon.log'

function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

# Native programs don't throw on failure; check their exit code.
function Invoke-Checked([string]$What, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# Windows PowerShell 5.1 turns a native program's redirected stderr into a terminating error
# under ErrorActionPreference=Stop; run those with Continue and judge them by exit code.
function Invoke-Quietly([scriptblock]$Command) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $saved }
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an administrator PowerShell, or double-click deploy\install-windows.cmd.'
}

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName 'Freqon' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "Freqon stopped and removed from sign-in. Program files: $Prefix (delete by hand if you like)."
    Write-Host 'Settings and stations were kept.'
    return
}

if ($Prefix -match ' ') {
    throw "The install folder can't contain spaces (nrsc5's build breaks): $Prefix. Set FREQON_PREFIX."
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'winget is needed: install "App Installer" from the Microsoft Store, then run this again.'
}

Write-Host 'Note: this Windows installer is *untested (not yet run on a real Windows PC).' -ForegroundColor Yellow
Step 'Installing Python, ffmpeg and MSYS2'
foreach ($id in 'Python.Python.3.12', 'Gyan.FFmpeg', 'MSYS2.MSYS2') {
    Invoke-Quietly { winget list --id $id --exact --accept-source-agreements *> $null }
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ${id}: already installed"
        continue
    }
    Invoke-Checked "winget install $id" {
        winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
    }
}
Update-SessionPath

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) {
    $ffmpeg = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'), $env:ProgramFiles `
        -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $ffmpeg) { throw 'ffmpeg was installed but not found; open a new PowerShell and run this again.' }
$bash = Join-Path $Msys 'usr\bin\bash.exe'
if (-not (Test-Path $bash)) { throw "MSYS2 not found at $Msys (set FREQON_MSYS2 to its folder)." }

Step 'Building nrsc5 in MSYS2 (the first time takes a while)'
# Freqon later runs from here as a normal user, and this installer runs code from here as
# administrator, so only administrators may change it. Refuse a folder someone else made.
if (Test-Path $Prefix) {
    $owner = (Get-Acl $Prefix).GetOwner([Security.Principal.SecurityIdentifier]).Value
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($owner -notin 'S-1-5-32-544', 'S-1-5-18', $me) {
        throw "$Prefix already exists and belongs to someone else ($owner). Delete it, then run this again."
    }
}
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
# Administrators and SYSTEM: full control. Users: read and run. (SIDs, so any Windows language.)
Invoke-Checked 'securing the install folder' {
    icacls $Prefix /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q
}
if (Get-ChildItem $Prefix -Force) {
    Invoke-Checked 'securing the install folder' { icacls (Join-Path $Prefix '*') /reset /T /Q }
}
$env:MSYSTEM = 'MINGW64'
$env:CHERE_INVOKING = '1'
# The first update may replace MSYS2's own runtime and stop early; the second finishes it.
& $bash -lc 'pacman -Syuu --noconfirm' | Out-Host
Invoke-Checked 'MSYS2 update' { & $bash -lc 'pacman -Syuu --noconfirm' | Out-Host }
$buildScript = (& (Join-Path $Msys 'usr\bin\cygpath.exe') -u (Join-Path $Src 'deploy\windows\build-nrsc5.sh')).Trim()
$prefixUnix = (& (Join-Path $Msys 'usr\bin\cygpath.exe') -u $Prefix).Trim()
Invoke-Checked 'nrsc5 build' { & $bash -l $buildScript $prefixUnix | Out-Host }
$nrsc5Lib = Join-Path $Prefix 'bin\libnrsc5.dll'
$rtlsdrLib = Join-Path $Prefix 'bin\librtlsdr.dll'
if (-not (Test-Path $nrsc5Lib)) { throw "libnrsc5.dll was not built ($nrsc5Lib)" }

Step 'Installing Freqon'
$venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Invoke-Checked 'creating the Python environment' { py -3.12 -m venv (Join-Path $Prefix 'venv') }
}
Invoke-Checked 'pip upgrade' { & $venvPython -m pip install --quiet --upgrade pip }
Invoke-Checked 'installing Freqon' { & $venvPython -m pip install --quiet $Src }

# Settings: start from the example, minus the Linux paths (data folder and library come from
# the defaults and the command line). Written without a byte-order mark, which TOML rejects.
$configDir = Join-Path $HOME '.config\freqon'
$config = Join-Path $configDir 'config.toml'
if (-not (Test-Path $config)) {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    $lines = Get-Content (Join-Path $Src 'deploy\config.example.toml') |
        Where-Object { $_ -notmatch '^(data_dir|nrsc5_lib) = ' }
    [IO.File]::WriteAllLines($config, [string[]]$lines)
}

# The launcher: sets up the libraries and restarts Freqon after a stalled dongle or a crash.
$runner = Join-Path $Prefix 'run-freqon.ps1'
function Quote([string]$Text) { "'" + ($Text -replace "'", "''") + "'" }  # a PowerShell string literal
$runnerText = @"
`$env:RTLSDR_LIB = $(Quote $rtlsdrLib)
`$env:Path = $(Quote ((Join-Path $Prefix 'bin') + ';')) + `$env:Path
while (`$true) {
    & $(Quote (Join-Path $Prefix 'venv\Scripts\freqon.exe')) --nrsc5-lib $(Quote $nrsc5Lib) --ffmpeg $(Quote $ffmpeg) --log-file $(Quote $LogFile)
    if (`$LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
"@
[IO.File]::WriteAllText($runner, $runnerText)

Step 'Checking for RTL-SDR dongles'
$env:RTLSDR_LIB = $rtlsdrLib
$freqonExe = Join-Path $Prefix 'venv\Scripts\freqon.exe'
$dongles = Invoke-Quietly { & $freqonExe --nrsc5-lib $nrsc5Lib --list-dongles 2>&1 | Out-String }
Write-Host $dongles
$noDongle = ($LASTEXITCODE -ne 0) -or ($dongles -notmatch 'serial')

if (-not $NoService) {
    Step 'Starting Freqon now and at every sign-in'
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $taskUser = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $taskUser -Description 'Freqon network HD Radio / FM tuner' -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    # Let Plex, Jellyfin and other devices on the home network reach it (not public networks).
    if (-not (Get-NetFirewallRule -DisplayName 'Freqon' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'Freqon' -Direction Inbound -Protocol TCP -LocalPort $Port `
            -Action Allow -Profile Private, Domain | Out-Null
    }
    Write-Host ''
    Write-Host "Freqon is starting: http://$($env:COMPUTERNAME.ToLower()):$Port/"
    Write-Host "Log: $LogFile"
} else {
    Write-Host ''
    Write-Host "Freqon is installed. Run it with: powershell -ExecutionPolicy Bypass -File `"$runner`""
}

if ($noDongle) {
    Write-Host ''
    Write-Host 'No RTL-SDR dongle was found. Windows needs the WinUSB driver for it, once:' -ForegroundColor Yellow
    Write-Host '  1. Plug in the dongle and run Zadig (https://zadig.akeo.ie).'
    Write-Host '  2. Options > List All Devices, pick "Bulk-In, Interface (Interface 0)" (or "RTL2838UHIDIR").'
    Write-Host '  3. Choose WinUSB and click Replace Driver. Then restart Freqon (sign out and in).'
}
