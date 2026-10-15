<#
    kiln path -- put the kiln command on PATH, so it runs from any folder.

    Writes %USERPROFILE%\.local\bin\kiln.cmd, a shim that runs this
    checkout's kiln.bat, and adds that folder to the user PATH. It is
    the folder the macOS and Linux installers link kiln into, and the
    one uv and pipx use on Windows. Only the current user's PATH
    changes, so no admin rights are needed.

    Run from another checkout, the shim moves there. The last one wins,
    as with the symlink on macOS and Linux.

      kiln_path.ps1           add
      kiln_path.ps1 -Remove   undo
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
trap {
    Write-Host "[ERROR] kiln path: $_"
    exit 1
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).ProviderPath
$bin  = Join-Path $env:USERPROFILE ".local\bin"
$shim = Join-Path $bin "kiln.cmd"
# Marks a kiln.cmd as ours. Anything else by that name is left alone.
$tag  = "rem kiln path shim"
# cmd reads a batch file in the console's OEM code page, so that is
# what a non-ASCII path in the shim has to be written in.
$oem  = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)

function Test-SameDir([string]$a, [string]$b) {
    $x = [Environment]::ExpandEnvironmentVariables($a.Trim().Trim('"')).TrimEnd('\')
    $y = [Environment]::ExpandEnvironmentVariables($b.Trim().Trim('"')).TrimEnd('\')
    return ($x -ne "") -and ($x -ieq $y)
}

function Test-OnPath([string]$list) {
    foreach ($entry in ($list -split ";")) {
        if (Test-SameDir $entry $bin) { return $true }
    }
    return $false
}

# The user PATH is read and written raw. Entries such as
# %USERPROFILE%\AppData\Local\Microsoft\WindowsApps have to stay
# unexpanded, and the value has to stay REG_EXPAND_SZ.
# [Environment]::SetEnvironmentVariable would flatten both.
$envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
$raw = [string]$envKey.GetValue("Path", "", "DoNotExpandEnvironmentNames")
$kind = if ($envKey.GetValueNames() -contains "Path") {
    $envKey.GetValueKind("Path")
} else {
    [Microsoft.Win32.RegistryValueKind]::ExpandString
}

# Explorer, and every terminal opened from it afterwards, re-reads the
# user PATH only when told it changed. Setting any user variable through
# .NET sends that message. Clearing one that does not exist sends it
# and changes nothing.
function Send-EnvironmentChanged {
    [Environment]::SetEnvironmentVariable("KILN_PATH_REFRESH", $null, "User")
}

function Read-ShimTarget {
    $text = [IO.File]::ReadAllText($shim, $oem)
    if ($text -notmatch [regex]::Escape($tag)) { return $null }
    if ($text -match '(?m)^"(.+)\\kiln\.bat" %\*') { return $Matches[1].Replace("%%", "%") }
    return ""
}

if ($Remove) {
    if (Test-Path -LiteralPath $shim) {
        if ($null -eq (Read-ShimTarget)) {
            Write-Host "[WARN] $shim was not written by kiln. Leaving it."
            exit 1
        }
        Remove-Item -LiteralPath $shim -Force
        Write-Host "[OK] Removed $shim"
    } else {
        Write-Host "[OK] kiln was not on PATH. Nothing to remove."
    }
    # The folder leaves PATH only when nothing else is in it: uv, pipx
    # and others install into it too.
    $empty = -not (Test-Path -LiteralPath $bin) -or
             -not (Get-ChildItem -LiteralPath $bin -Force | Select-Object -First 1)
    if ($empty -and (Test-OnPath $raw)) {
        $kept = ($raw -split ";" | Where-Object { -not (Test-SameDir $_ $bin) }) -join ";"
        if ($kept.Trim(";")) {
            $envKey.SetValue("Path", $kept, $kind)
        } else {
            $envKey.DeleteValue("Path", $false)
        }
        Send-EnvironmentChanged
        Write-Host "[OK] Removed $bin from your PATH."
    } elseif (Test-OnPath $raw) {
        Write-Host "     $bin stays on your PATH: other programs are in it."
    }
    if ($empty -and (Test-Path -LiteralPath $bin)) {
        Remove-Item -LiteralPath $bin -Force
    }
    exit 0
}

# ---- the shim ----------------------------------------------------
if (Test-Path -LiteralPath $shim) {
    $old = Read-ShimTarget
    if ($null -eq $old) {
        Write-Host "[WARN] $shim exists and was not written by kiln. Leaving it,"
        Write-Host "       so kiln is not on PATH. Run kiln from $root instead."
        exit 1
    }
    if ($old -and -not (Test-SameDir $old $root)) {
        Write-Host "[OK] kiln command moved from $old"
    }
}

# Chained, not called: control passes to kiln.bat for good, so its exit
# code is the shim's. cmd expands %...% in a batch file even inside
# quotes, so a % in the path is doubled.
$target = (Join-Path $root "kiln.bat").Replace("%", "%%")
$text = "@echo off`r`n" +
        "$tag. kiln path wrote this; kiln path --remove deletes it.`r`n" +
        "`"$target`" %*`r`n"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
[IO.File]::WriteAllText($shim, $text, $oem)
Write-Host "[OK] kiln command: $shim"
Write-Host "     runs $root\kiln.bat"

# ---- the user PATH -----------------------------------------------
$machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
if ((Test-OnPath $raw) -or (Test-OnPath $machine)) {
    Write-Host "[OK] $bin is already on your PATH."
} else {
    $new = if ($raw.Trim(";")) { $raw.TrimEnd(";") + ";" + $bin } else { $bin }
    $envKey.SetValue("Path", $new, $kind)
    Send-EnvironmentChanged
    Write-Host "[OK] Added $bin to your PATH."
}

# This terminal keeps the PATH it started with.
if (Test-OnPath $env:Path) {
    Write-Host "     Type  kiln  in any folder."
} else {
    Write-Host "     Open a new terminal, then type  kiln  in any folder."
}
exit 0
