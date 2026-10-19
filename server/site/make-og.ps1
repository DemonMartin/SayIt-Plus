# Render the share cards (server/web/img/og-en.jpg, og-zh.jpg) from server/site/og.html.
# Needs Microsoft Edge. Run: powershell -NoProfile -ExecutionPolicy Bypass -File server/site/make-og.ps1
# Each call uses a fresh --user-data-dir: reusing one makes the second screenshot silently not appear.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tpl = Join-Path $here 'og.html'
$out = Join-Path (Split-Path -Parent $here) 'web\img'
if (-not (Test-Path $tpl)) { Write-Output 'og.html not found'; exit 1 }
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path $edge)) { $edge = 'C:\Program Files\Microsoft\Edge\Application\msedge.exe' }
if (-not (Test-Path $edge)) { Write-Output 'Edge not found'; exit 1 }
$uri = ([System.Uri]$tpl).AbsoluteUri
foreach ($lang in @('en', 'zh')) {
  $png = Join-Path $out "og-$lang.png"
  if (Test-Path $png) { Remove-Item $png -Force }
  $prof = Join-Path $env:TEMP ('sayit-og-' + [guid]::NewGuid().ToString())
  $argList = @('--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1', "--user-data-dir=$prof",
    '--window-size=1200,630', '--virtual-time-budget=3000', '--allow-file-access-from-files', "--screenshot=$png", "$uri`?lang=$lang")
  $p = Start-Process -FilePath $edge -ArgumentList $argList -PassThru -WindowStyle Hidden
  $p.WaitForExit(45000) | Out-Null
  Remove-Item -Recurse -Force $prof -ErrorAction SilentlyContinue
  if (-not (Test-Path $png)) { Write-Output "FAILED $lang"; exit 1 }
  $size = (Get-Item $png).Length
  # A blank capture is only a few KB; a real card is far larger.
  if ($size -lt 20000) { Write-Output "SUSPICIOUSLY SMALL $lang $size"; exit 1 }
  # Gradients make the PNG ~550 KB; a JPEG of the same card is a fraction of that and
  # some share scrapers give up on large images.
  Add-Type -AssemblyName System.Drawing
  $jpg = Join-Path $out "og-$lang.jpg"
  $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
  $ep = New-Object System.Drawing.Imaging.EncoderParameters 1
  $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), ([long]88)
  $img = [System.Drawing.Image]::FromFile($png)
  $img.Save($jpg, $codec, $ep)
  $img.Dispose()
  Remove-Item $png -Force
  Write-Output "OK og-$lang.jpg $((Get-Item $jpg).Length)"
}
exit 0
