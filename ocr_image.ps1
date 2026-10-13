# OCR one image with Windows.Media.Ocr and emit recognized text boxes as JSON.
# MUST run under Windows PowerShell 5.1 (PowerShell 7 has no WinRT projection).
# ASCII-only on purpose: PS 5.1 decodes BOM-less .ps1 as ANSI.
#
# The image is read with System.Drawing and handed to WinRT as an in-memory
# SoftwareBitmap. We deliberately avoid StorageFile.GetFileFromPathAsync: the
# WinRT broker is refused access to these paths (UnauthorizedAccessException).
#
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File ocr_image.ps1 -Path x.png
#
# Output (compressed JSON, one line):
#   { image, width, height, lang, count, lines: [ { text, x, y, w, h } ] }

param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Language = 'zh-Hans-CN',
    [double]$Scale = 2.0
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Test-Path -LiteralPath $Path)) { throw "image not found: $Path" }
$full = (Resolve-Path -LiteralPath $Path).Path

# --- WinRT async -> sync -------------------------------------------------
try { [void][System.Reflection.Assembly]::LoadWithPartialName('System.Runtime.WindowsRuntime') } catch { }
if (-not ('System.WindowsRuntimeSystemExtensions' -as [type])) {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction SilentlyContinue
}

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($op, [Type]$type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    try {
        $task.Wait(-1) | Out-Null
    } catch {
        $inner = $task.Exception
        while ($inner -is [System.AggregateException] -and $inner.InnerException) { $inner = $inner.InnerException }
        throw "Await($($type.Name)) failed: $($inner.GetType().FullName): $($inner.Message)"
    }
    $task.Result
}

# --- pixels --------------------------------------------------------------
# Read the file with plain .NET IO, re-encode to PNG in memory, then hand WinRT
# a projected IRandomAccessStream. This dodges two PS/WinRT binding traps:
#   * StorageFile.GetFileFromPathAsync  -> broker denies access to these paths
#   * SoftwareBitmap.CreateCopyFromBuffer(IBuffer,...) -> PS cannot bind the
#     __ComObject IBuffer to the WinRT interface parameter
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap($full)

# Upscale before OCR. Measured: at 1x Windows OCR mangles CJK ("联通测试" ->
# garbage); at 2x HighQualityBicubic it is character-perfect. Nearest-neighbour
# is NOT good enough either -- the smoothing matters.
if ($Scale -ne 1.0) {
    $nw = [int][Math]::Round($bmp.Width * $Scale)
    $nh = [int][Math]::Round($bmp.Height * $Scale)
    $dst = New-Object System.Drawing.Bitmap($nw, $nh)
    $g = [System.Drawing.Graphics]::FromImage($dst)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($bmp, 0, 0, $nw, $nh)
    $g.Dispose()
    $bmp.Dispose()
    $bmp = $dst
}

$width = $bmp.Width
$height = $bmp.Height

[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$mem = New-Object System.IO.MemoryStream
try {
    $bmp.Save($mem, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $bmp.Dispose()
}
$mem.Position = 0
$ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($mem)

$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

# --- OCR -----------------------------------------------------------------
$engine = $null
try {
    $lang = New-Object Windows.Globalization.Language($Language)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
} catch { $engine = $null }
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw "OCR engine unavailable (language pack for $Language missing?)" }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = New-Object System.Collections.ArrayList
foreach ($line in $result.Lines) {
    $minX = [double]::MaxValue; $minY = [double]::MaxValue
    $maxX = [double]::MinValue; $maxY = [double]::MinValue
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        if ($r.X -lt $minX) { $minX = $r.X }
        if ($r.Y -lt $minY) { $minY = $r.Y }
        if (($r.X + $r.Width) -gt $maxX) { $maxX = $r.X + $r.Width }
        if (($r.Y + $r.Height) -gt $maxY) { $maxY = $r.Y + $r.Height }
    }
    if ($minX -eq [double]::MaxValue) { continue }
    [void]$lines.Add([ordered]@{
            text = $line.Text
            x    = [int]$minX
            y    = [int]$minY
            w    = [int][Math]::Ceiling($maxX - $minX)
            h    = [int][Math]::Ceiling($maxY - $minY)
        })
}

$payload = [ordered]@{
    image  = $full
    width  = $width
    height = $height
    lang   = $engine.RecognizerLanguage.LanguageTag
    count  = $lines.Count
    lines  = $lines
}
$payload | ConvertTo-Json -Depth 5 -Compress
